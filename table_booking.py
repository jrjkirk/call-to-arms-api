"""Table-booking core logic: compute how many tables/players a venue should
expect for a (club, system, week), render the notification email, and send
it via emailer.py. Shared by admin.py's manual preview/send endpoints and
the automatic triggers (pairings_publish, run_auto_pairings_check.py, and
later the cutoff scheduler script).

tables/headcount math:
  - If pairings already exist for the week, tables = the count of non-BYE
    pairings (a BYE has no opponent, so needs no table) and headcount comes
    from the distinct-signups count (same "latest row per player wins" rule
    as signups.py::_signup_count).
  - If no pairings exist yet (e.g. a cutoff-mode send firing before pairing
    generation), tables falls back to ceil(headcount / players_per_table).
"""
import math

from sqlmodel import Session, select

import emailer
from database import scoped
from models import ClubSystem, Pairing, Signup, TableBookingConfig, TableBookingNotification
from signups import _get_system_config


def _effective_signups(db: Session, club_id: int, system: str, week: str) -> list[Signup]:
    """Distinct players signed up for this week/system (latest row per
    player wins) — mirrors signups.py::_signup_count's dedup rule, but
    returns the rows themselves so player names are available."""
    rows = db.exec(
        scoped(Signup, club_id)
        .where(Signup.system == system)
        .where(Signup.week == week)
        .order_by(Signup.created_at.desc())
    ).all()
    seen: set = set()
    ordered: list[Signup] = []
    for s in rows:
        key = s.player_id if s.player_id is not None else id(s)
        if key not in seen:
            seen.add(key)
            ordered.append(s)
    return ordered


def compute_table_booking(db: Session, club_id: int, system: str, week: str, players_per_table: int) -> dict:
    signups = _effective_signups(db, club_id, system, week)
    headcount = len(signups)
    player_names = sorted(s.player_name for s in signups)

    pairings = db.exec(
        scoped(Pairing, club_id)
        .where(Pairing.system == system)
        .where(Pairing.week == week)
    ).all()
    if pairings:
        tables = sum(1 for p in pairings if p.b_signup_id is not None)
    else:
        tables = math.ceil(headcount / players_per_table) if headcount else 0

    return {"tables": tables, "headcount": headcount, "player_names": player_names}


def render_table_booking_email(
    cfg: TableBookingConfig, system: str, week: str, tables: int, headcount: int, player_names: list[str],
) -> tuple[str, str]:
    venue_label = cfg.venue_name or "there"
    subject = cfg.subject_template or f"{system} — {week}: {tables} table{'s' if tables != 1 else ''} needed"

    parts = [
        f"<p>Hi {venue_label},</p>",
        f"<p>For {system} on {week}, we're expecting <strong>{headcount} player"
        f"{'s' if headcount != 1 else ''}</strong>, needing approximately "
        f"<strong>{tables} table{'s' if tables != 1 else ''}</strong> "
        f"(based on {cfg.players_per_table} players per table).</p>",
    ]
    if cfg.include_player_names and player_names:
        items = "".join(f"<li>{n}</li>" for n in player_names)
        parts.append(f"<p>Players:</p><ul>{items}</ul>")
    if cfg.notes:
        parts.append(f"<p>{cfg.notes}</p>")
    parts.append("<p>Thanks!<br>Call to Arms</p>")
    return subject, "".join(parts)


def send_table_booking_notification(
    db: Session, club_id: int, system_id: int, system: str, week: str,
    cfg: TableBookingConfig, *, allow_duplicate: bool,
) -> TableBookingNotification | None:
    """Compute + send the venue email now, recording a TableBookingNotification
    row either way. allow_duplicate=False (used by automatic triggers) skips
    sending and returns None if a 'sent' row already exists for this
    (club, system, week) — the idempotency guard so on_publish can't
    double-send the same week regardless of which trigger fires it.
    allow_duplicate=True (manual admin "Send now") always executes."""
    if not allow_duplicate:
        existing = db.exec(
            select(TableBookingNotification)
            .where(TableBookingNotification.club_id == club_id)
            .where(TableBookingNotification.system_id == system_id)
            .where(TableBookingNotification.week == week)
            .where(TableBookingNotification.status == "sent")
        ).first()
        if existing:
            return None

    data = compute_table_booking(db, club_id, system, week, cfg.players_per_table)
    subject, html = render_table_booking_email(
        cfg, system, week, data["tables"], data["headcount"], data["player_names"]
    )

    notif = TableBookingNotification(
        club_id=club_id, system_id=system_id, week=week,
        tables=data["tables"], headcount=data["headcount"],
    )
    try:
        emailer.send_email(to=cfg.venue_email, subject=subject, html=html, cc=cfg.cc_emails or None)
        notif.status = "sent"
    except Exception as e:
        notif.status = "failed"
        notif.error = str(e)[:500]
    db.add(notif)
    db.commit()
    db.refresh(notif)
    return notif


def maybe_send_table_booking(db: Session, club_id: int, system: str, week: str) -> None:
    """Called from publish sites (admin.py::pairings_publish and
    run_auto_pairings_check.py) after a publish succeeds. Sends only if this
    club/system has table_booking_enabled and a config with send_mode
    'on_publish'. Idempotent per (club, system, week) via
    send_table_booking_notification's allow_duplicate=False guard, so it's
    safe to call from both trigger sites without risk of double-sending.
    Never raises — a publish must succeed even if table-booking fails."""
    try:
        config = _get_system_config(db, system)
        if config is None:
            return
        cs = db.exec(
            scoped(ClubSystem, club_id).where(ClubSystem.system_id == config.id)
        ).first()
        if cs is None or not cs.table_booking_enabled:
            return
        cfg = db.exec(
            select(TableBookingConfig)
            .where(TableBookingConfig.club_id == club_id)
            .where(TableBookingConfig.system_id == config.id)
        ).first()
        if cfg is None or cfg.send_mode != "on_publish":
            return
        send_table_booking_notification(db, club_id, config.id, system, week, cfg, allow_duplicate=False)
    except Exception as e:
        print(f"Warning: maybe_send_table_booking failed for club={club_id} system={system!r} week={week!r}: {e}")
