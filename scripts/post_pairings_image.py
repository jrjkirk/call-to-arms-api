"""Render the weekly pairings as a PNG and post it to the relevant Discord webhook.

post_pairings_image_for(db, system, week, club_id) — importable helper used
by the auto-pairings scheduler.  Returns True if the image was posted,
False if there were no pairings rows to render.

The __main__ entry point (invoked by the post-pairings-image GitHub Actions
workflow) delegates to this function using SYSTEM/WEEK/CLUB env vars. That
workflow is a manual workflow_dispatch (a human picks system+week, and
optionally a club slug) — see _resolve_single_club_id below for how club_id
is resolved for that entry point specifically.
"""
import json
import os

import httpx
from sqlmodel import Session, select

from database import discord_mentions_for_player_ids, engine, resolve_webhook_url, scoped
from models import Club, ClubSystem, Pairing, SystemConfig
from admin import _collect_signups_for_rows, _pairing_rows_to_display
from render_pairings_image import render_pairings_image


# Discord hard-limits a message's content to 2000 characters. The pairings
# image carries the actual information, so if a club ever fields enough
# players to blow the budget we drop the overflow mentions rather than have
# Discord reject the whole post (image included).
_CONTENT_LIMIT = 1900


def _mention_line(db: Session, rows: list, signups_by_id: dict) -> str:
    """A space-separated `<@id>` list for everyone in this week's pairings,
    in pairing order, so being tagged tells you that you're playing.

    The pairings themselves are rendered into the PNG — a mention inside an
    embed or an image can't notify anyone, so the tags have to ride in the
    message content to actually ping. Players with no linked Discord account
    (unclaimed roster entries, guests) are silently skipped; they still
    appear by name in the image.
    """
    player_ids: list[int] = []
    for r in rows:
        for signup_id in (r.a_signup_id, r.b_signup_id):
            if not signup_id:
                continue
            su = signups_by_id.get(signup_id)
            if su is not None and su.player_id and su.player_id not in player_ids:
                player_ids.append(su.player_id)

    mentions_by_player = discord_mentions_for_player_ids(db, player_ids)
    mentions = [mentions_by_player[pid] for pid in player_ids if pid in mentions_by_player]
    if not mentions:
        return ""

    line = " ".join(mentions)
    if len(line) > _CONTENT_LIMIT:
        kept: list[str] = []
        used = 0
        for m in mentions:
            if used + len(m) + 1 > _CONTENT_LIMIT:
                break
            kept.append(m)
            used += len(m) + 1
        line = " ".join(kept)
    return line


def post_pairings_image_for(db: Session, system: str, week: str, club_id: int) -> bool:
    """Render pairings for (system, week, club_id) and post to Discord.

    Returns True if the image was posted, False if no pairing rows were found.
    The db session is used read-only here; no commits are made.
    """
    system_config = db.exec(
        select(SystemConfig).where(SystemConfig.legacy_system_name == system)
    ).first()
    system_id = system_config.id if system_config else None
    webhook_url = resolve_webhook_url(db, club_id, "pairings", system_id)
    if not webhook_url:
        print(f"No pairings webhook configured for {system!r}, skipping.")
        return False

    rows = db.exec(
        scoped(Pairing, club_id)
        .where(Pairing.week == week)
        .where(Pairing.system == system)
        .order_by(Pairing.id)
    ).all()

    signups_by_id = _collect_signups_for_rows(rows, db, club_id)
    display_rows = _pairing_rows_to_display(rows, signups_by_id, system)

    buf = render_pairings_image(display_rows, week, system)
    if buf is None:
        print(f"No pairings found for {system!r} week {week!r}, skipping.")
        return False

    content = f"📋 **{system} — Pairings for {week}**"
    mention_line = _mention_line(db, rows, signups_by_id)
    if mention_line:
        content = f"{content}\n{mention_line}"

    resp = httpx.post(
        webhook_url,
        data={
            "payload_json": json.dumps({
                "content": content,
                # Ping only the users we explicitly mention — never @everyone
                # /@here/roles, whatever a player has called themselves.
                "allowed_mentions": {"parse": ["users"]},
            })
        },
        files={"file": ("pairings.png", buf, "image/png")},
        timeout=30,
    )
    # Surface a broken/misconfigured webhook (404/401/413…) instead of claiming
    # success — the caller records it as a job error rather than silently
    # "posting" nothing.
    resp.raise_for_status()
    print(f"Posted pairings image for {system!r} {week!r} ({resp.status_code}).")
    return True


def _resolve_single_club_id(db: Session, system: str) -> int:
    """Resolve the one club running `system`, for the manual workflow_dispatch
    entry point below when no club slug is given. Only works while exactly
    one club_systems row exists for that system — raises rather than
    guessing if that's ever not true, so a second club sharing a system
    fails loudly here instead of silently posting the wrong club's
    pairings."""
    system_config = db.exec(
        select(SystemConfig).where(SystemConfig.legacy_system_name == system)
    ).first()
    if system_config is None:
        raise RuntimeError(f"No SystemConfig row for legacy_system_name={system!r}")

    club_systems = db.exec(
        select(ClubSystem).where(ClubSystem.system_id == system_config.id)
    ).all()
    if len(club_systems) != 1:
        raise RuntimeError(
            f"Cannot resolve a single club for {system!r} — found "
            f"{len(club_systems)} club_systems row(s), expected exactly 1. "
            f"Pass a club slug (the workflow's optional 'club' input) to "
            f"disambiguate."
        )
    return club_systems[0].club_id


def _resolve_manual_club_id(db: Session, system: str, club_slug: str | None) -> int:
    """Resolve club_id for the manual workflow_dispatch entry point: an
    explicit club slug (the workflow's optional 'club' input) takes
    precedence and is validated against that club actually running the
    system; blank/omitted falls back to _resolve_single_club_id unchanged,
    preserving today's behavior for any system only one club runs."""
    if not club_slug:
        return _resolve_single_club_id(db, system)

    club = db.exec(select(Club).where(Club.slug == club_slug)).first()
    if club is None or not club.active:
        raise RuntimeError(f"No active club found for slug {club_slug!r}.")

    system_config = db.exec(
        select(SystemConfig).where(SystemConfig.legacy_system_name == system)
    ).first()
    if system_config is None:
        raise RuntimeError(f"No SystemConfig row for legacy_system_name={system!r}")

    club_system = db.exec(
        select(ClubSystem)
        .where(ClubSystem.club_id == club.id)
        .where(ClubSystem.system_id == system_config.id)
        .where(ClubSystem.enabled == True)
    ).first()
    if club_system is None:
        raise RuntimeError(f"Club {club_slug!r} does not run {system!r}.")
    return club.id


def main() -> None:
    system = os.environ["SYSTEM"]
    week = os.environ["WEEK"]
    club_slug = os.environ.get("CLUB", "").strip() or None
    with Session(engine) as db:
        club_id = _resolve_manual_club_id(db, system, club_slug)
        post_pairings_image_for(db, system, week, club_id=club_id)


if __name__ == "__main__":
    main()
