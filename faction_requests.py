"""Faction requests: a system admin asks, the platform admin approves.

A system's faction list is shared by every club (it lives on SystemConfig), so
no club admin edits it directly. They ask for names here; each one is approved
(appended to the list, optionally with the spelling fixed) or declined, from
the queue on the platform admin page.

Additions only. Renaming or removing a faction orphans every signup that holds
the old name, so those stay a direct platform-admin edit of the system.

Who hears about it:
  - a new request posts to the platform alerts channel (ALERTS_WEBHOOK_URL),
    mentioning the platform admins, so it isn't missed among error alerts;
  - an approval posts the new names to the updates channel
    (DISCORD_UPDATES_WEBHOOK_URL), for everyone who plays the system;
  - the requester gets an in-app notice either way (notices.py).
Every post is fire-and-forget: a dead webhook must not fail a request or a
review, the same convention as every other webhook here.
"""
import os
import uuid
from datetime import datetime
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from admin import _require_system_scope
from admin_platform import _clean_ruleset, _faction_icon_slug
from auth import require_admin, require_platform_admin
from database import get_session, log_audit
from identity import discord_ids_for_users, display_name_for
from models import Club, FactionRequest, SystemConfig, User
from notices import FACTION_REQUEST, record as record_notice
from observability import capture

router = APIRouter(prefix="/admin", tags=["faction-requests"])

# Generous for a real faction ("Imperial Navy Breachers") and short enough that
# a pasted paragraph is obviously not one.
MAX_NAME_LEN = 60
MAX_NAMES_PER_REQUEST = 20
MAX_NOTE_LEN = 500


def _norm(name: str) -> str:
    """Case- and spacing-blind key for "is this the same faction"."""
    return " ".join((name or "").split()).lower()


def _tidy(name: str) -> str:
    return " ".join((name or "").split())


def _system_row(db: Session, system: str) -> SystemConfig:
    row = db.exec(select(SystemConfig).where(SystemConfig.legacy_system_name == system)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="System not found.")
    return row


def _group_labels(row: SystemConfig) -> list[str]:
    return [g.get("label") for g in (row.faction_groups or []) if isinstance(g, dict) and g.get("label")]


def _post(url: str, content: str, mention_ids: list[str] = ()) -> None:
    """Post to a platform webhook. `allowed_mentions` pins pings to exactly the
    ids passed: names in these messages are typed by people, and an
    "@everyone" in a faction name must stay text."""
    if not url:
        return
    try:
        httpx.post(
            url,
            json={
                "content": content[:1900],
                "allowed_mentions": {"parse": [], "users": list(mention_ids)},
            },
            timeout=httpx.Timeout(10.0, connect=5.0),
        )
    except Exception:
        pass


def _platform_admin_mentions(db: Session) -> list[str]:
    ids = [u.id for u in db.exec(select(User).where(User.is_platform_admin == True)).all()]  # noqa: E712
    return list(discord_ids_for_users(db, ids).values())


def _row_dict(r: FactionRequest, club_name: Optional[str] = None) -> dict:
    return {
        "id": r.id,
        "batch_id": r.batch_id,
        "created_at": r.created_at.isoformat(),
        "club_id": r.club_id,
        "club_name": club_name,
        "system": r.system,
        "name": r.name,
        "suggested_group": r.suggested_group,
        "note": r.note,
        "status": r.status,
        "requested_by_name": r.requested_by_name,
        "approved_name": r.approved_name,
        "decline_reason": r.decline_reason,
        "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
        "reviewed_by_name": r.reviewed_by_name,
        # The file this faction's icon must be committed as.
        "icon_file": _faction_icon_slug(r.approved_name or r.name) + ".png",
    }


# ---------------------------------------------------------------------------
# Asking (a system admin at their club)
# ---------------------------------------------------------------------------


class FactionRequestBody(BaseModel):
    system: str
    names: list[str]
    suggested_group: Optional[str] = None
    note: Optional[str] = None


@router.post("/faction-requests")
def create_faction_request(
    body: FactionRequestBody,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """Ask for one or more factions to be added to a system's list.

    Names already in the list, or already waiting for review, are skipped and
    reported back rather than refused outright: asking for six teams where one
    went in last week should still send the other five.
    """
    _require_system_scope(body.system, user, db)
    system = _system_row(db, body.system)

    in_list = {_norm(f) for f in (system.faction_list or [])}
    pending = {
        _norm(r.name)
        for r in db.exec(
            select(FactionRequest)
            .where(FactionRequest.system == body.system)
            .where(FactionRequest.status == "pending")
        ).all()
    }

    names: list[str] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for raw in body.names:
        name = _tidy(raw)
        key = _norm(name)
        if not name or key in seen:
            continue
        seen.add(key)
        if len(name) > MAX_NAME_LEN:
            skipped.append({"name": name[:MAX_NAME_LEN] + "…", "reason": "too long"})
        elif key in in_list:
            skipped.append({"name": name, "reason": "already in the list"})
        elif key in pending:
            skipped.append({"name": name, "reason": "already requested"})
        else:
            names.append(name)

    if len(names) > MAX_NAMES_PER_REQUEST:
        raise HTTPException(
            status_code=422, detail=f"At most {MAX_NAMES_PER_REQUEST} factions per request."
        )
    if not names:
        raise HTTPException(
            status_code=422,
            detail="Nothing to request: "
            + ("; ".join(f"{s['name']} ({s['reason']})" for s in skipped) or "no names given."),
        )

    group = (body.suggested_group or "").strip() or None
    if group and group not in _group_labels(system):
        group = None
    note = (body.note or "").strip()[:MAX_NOTE_LEN] or None

    batch = uuid.uuid4().hex
    requester = display_name_for(user)
    for name in names:
        db.add(FactionRequest(
            batch_id=batch, club_id=user.club_id, system=body.system, name=name,
            suggested_group=group, note=note,
            requested_by_user_id=user.id, requested_by_name=requester,
        ))
    log_audit(db, user, "faction_request.create", "system", system.id,
              f"{body.system}: {', '.join(names)}")
    db.commit()

    club = db.get(Club, user.club_id)
    mentions = _platform_admin_mentions(db)
    content = (
        ("".join(f"<@{m}> " for m in mentions))
        + f"📥 **Faction request: {body.system}**\n"
        + "\n".join(f"• {n}" for n in names)
        + (f"\n🗂️ Category: {group}" if group else "")
        + f"\n👤 {requester}" + (f" · {club.name}" if club else "")
        + (f"\n💬 {note}" if note else "")
        + "\nReview it in Platform Admin."
    )
    _post(os.environ.get("ALERTS_WEBHOOK_URL", ""), content, mentions)

    return {"ok": True, "requested": names, "skipped": skipped}


@router.get("/faction-requests")
def list_my_club_faction_requests(
    system: str,
    user: User = Depends(require_admin),
    db: Session = Depends(get_session),
):
    """This club's requests for one system, newest first, so the form can show
    what is waiting and what came of it."""
    _require_system_scope(system, user, db)
    rows = db.exec(
        select(FactionRequest)
        .where(FactionRequest.club_id == user.club_id)
        .where(FactionRequest.system == system)
        .order_by(FactionRequest.created_at.desc())
        .limit(50)
    ).all()
    return {"requests": [_row_dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# Reviewing (platform admin)
# ---------------------------------------------------------------------------


@router.get("/platform/faction-requests")
def list_faction_requests(
    status: str = "pending",
    _: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """The review queue. `status` is pending (default), approved, declined or
    all. Carries each system's category labels, because approving into a
    system with categories means choosing one."""
    query = select(FactionRequest)
    if status != "all":
        query = query.where(FactionRequest.status == status)
    rows = db.exec(query.order_by(FactionRequest.created_at.desc()).limit(200)).all()

    clubs = {
        c.id: c.name
        for c in db.exec(select(Club).where(Club.id.in_({r.club_id for r in rows} or {0}))).all()
    }
    systems = {}
    for name in {r.system for r in rows}:
        row = db.exec(select(SystemConfig).where(SystemConfig.legacy_system_name == name)).first()
        if row is not None:
            systems[name] = {"groups": _group_labels(row), "icon_folder": row.icon_folder}
    return {"requests": [_row_dict(r, clubs.get(r.club_id)) for r in rows], "systems": systems}


class Decision(BaseModel):
    id: int
    action: str  # "approve" | "decline"
    name: Optional[str] = None  # approve: the name to add, if the request's needs fixing
    group: Optional[str] = None  # approve: required when the system has categories
    reason: Optional[str] = None  # decline: shown to the requester


class ReviewBody(BaseModel):
    decisions: list[Decision]


@router.post("/platform/faction-requests/review")
def review_faction_requests(
    body: ReviewBody,
    user: User = Depends(require_platform_admin),
    db: Session = Depends(get_session),
):
    """Approve and decline any number of requests in one go.

    One call rather than one per row so a season's worth of teams lands as one
    edit to the list, one updates-channel post and one notice per requester.
    All or nothing: if any decision is invalid, none are applied.
    """
    if not body.decisions:
        raise HTTPException(status_code=422, detail="No decisions given.")
    by_id = {d.id: d for d in body.decisions}
    rows = db.exec(select(FactionRequest).where(FactionRequest.id.in_(list(by_id)))).all()
    if len(rows) != len(by_id):
        raise HTTPException(status_code=404, detail="Request not found.")
    for r in rows:
        if r.status != "pending":
            raise HTTPException(status_code=409, detail=f"'{r.name}' has already been reviewed.")
        if by_id[r.id].action not in ("approve", "decline"):
            raise HTTPException(status_code=422, detail="action must be approve or decline.")

    now = datetime.utcnow()
    reviewer = display_name_for(user)
    added: dict[str, list[str]] = {}

    for system_name in sorted({r.system for r in rows}):
        system = _system_row(db, system_name)
        factions = list(system.faction_list or [])
        groups = [dict(g, factions=list(g.get("factions") or [])) for g in (system.faction_groups or [])
                  if isinstance(g, dict)]
        labels = [g.get("label") for g in groups]
        existing = {_norm(f): f for f in factions}

        for r in [r for r in rows if r.system == system_name]:
            d = by_id[r.id]
            r.reviewed_at, r.reviewed_by_user_id, r.reviewed_by_name = now, user.id, reviewer
            if d.action == "decline":
                r.status = "declined"
                r.decline_reason = (d.reason or "").strip()[:MAX_NOTE_LEN] or None
                db.add(r)
                continue

            name = _tidy(d.name or r.name)
            if not name or len(name) > MAX_NAME_LEN:
                raise HTTPException(status_code=422, detail=f"'{name}' is not a usable faction name.")
            r.status, r.approved_name = "approved", name
            db.add(r)
            if _norm(name) in existing:
                # Added by hand meanwhile, or two requests for one faction:
                # approve it without listing it twice.
                r.approved_name = existing[_norm(name)]
                continue
            if groups:
                if d.group not in labels:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Choose a category for '{name}' ({', '.join(labels)}).",
                    )
                next(g for g in groups if g.get("label") == d.group)["factions"].append(name)
            factions.append(name)
            existing[_norm(name)] = name
            added.setdefault(system_name, []).append(name)

        if added.get(system_name):
            # The same validation as the system editor, so an approval can
            # never leave the list in a shape that editor would refuse.
            # Also puts the new names in the system's order: alphabetical
            # slots them in, custom leaves them at the end.
            system.faction_list, system.faction_groups, system.icon_folder = _clean_ruleset(
                factions, groups or None, system.icon_folder, system.faction_order or "custom"
            )
            db.add(system)
            log_audit(db, user, "faction_request.approve", "system", system.id,
                      f"{system_name}: {', '.join(added[system_name])}")

    db.commit()

    # Tell the requesters: one notice per (account, club, system).
    try:
        per_person: dict[tuple, list[FactionRequest]] = {}
        for r in rows:
            per_person.setdefault((r.requested_by_user_id, r.club_id, r.system), []).append(r)
        for (uid, club_id, system_name), mine in per_person.items():
            ok = [r.approved_name for r in mine if r.status == "approved"]
            no = [r for r in mine if r.status == "declined"]
            parts = []
            if ok:
                parts.append(f"Added to {system_name}: {', '.join(ok)}.")
            for r in no:
                parts.append(f"Not added: {r.name}" + (f" ({r.decline_reason})." if r.decline_reason else "."))
            record_notice(db, user_id=uid, club_id=club_id, system=system_name, week="",
                          kind=FACTION_REQUEST, body=" ".join(parts))
        db.commit()
    except Exception as e:
        db.rollback()
        capture(e, kind="notices_on_faction_review")

    updates_url = os.environ.get("DISCORD_UPDATES_WEBHOOK_URL", "")
    for system_name, names in added.items():
        who = sorted({r.requested_by_name for r in rows if r.system == system_name and r.status == "approved"})
        _post(
            updates_url,
            f"🆕 **New {system_name} factions:** {', '.join(names)}\n"
            f"Pick them at signup from now on." + (f" Requested by {', '.join(who)}." if who else ""),
        )

    return {
        "ok": True,
        "added": added,
        "reviewed": [_row_dict(r) for r in rows],
    }
