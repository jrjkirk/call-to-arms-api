"""Admin endpoints: role management and scoped access, plus the helpers the
other admin modules share.

The rest of /admin is split by topic: admin_pairings, admin_signups,
admin_league, admin_club and admin_platform. Each imports what it shares from
here and nothing from the others.

Permission model:
- Super-admin: user.is_super_admin == True. Set by SQL, never via this API.
  Can do everything and is the only role that can appoint/remove scope admins.
- Scope admin: a row in admin_roles (user_id, scope). One row per scope held.
"""
import os
import re
from datetime import date, datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import text
from sqlmodel import Session, select

from auth import (
    admin_scopes,
    club_runnable_scopes,
    current_user,
    get_session,
    require_admin,
    require_super_admin,
    valid_scopes,
)
from database import scoped, log_audit, resolve_active_club_id
from league import (
    _current_season_id,
)
from identity import display_name_for
from models import AdminRole, AppSetting, Club, ClubSystem, ClubWebhook, LeagueRating, LeagueResult, Player, PlayerDiscordVerification, PlayerExperienceAdjustment, PlayerLevelAnnouncement, PublishState, Signup, SystemConfig, UK_REGIONS, User
import storage
from observability import capture
from experience import counts_for_players, games_played, tier_for
from levels import level_for
from services import player_titles, set_player_titles
from week_logic import _DAY_NAME_TO_INT

# Workflow dispatch lives in github_dispatch.py — shared with the auto-pairings
# job, which needs the same call when it has pairings to render and no
# matplotlib to render them with.


# Defaults for the league standings post, matching what the retired
# `0 19 * * 4` cron did in British Summer Time — a club that never opens the
# panel keeps the schedule it already had.
LR_DEFAULT_DAY = "Thursday"
LR_DEFAULT_TIME = "20:00"

# Where the sign-up link in a call-to-arms post points. Same env var the
# scheduled job reads, so a manual post carries the same URL.
APP_PUBLIC_URL = os.environ.get("APP_PUBLIC_URL", "")

router = APIRouter(prefix="/admin", tags=["admin"])


# Base gate for every club-admin endpoint here: any admin scope at the ACTIVE
# club, then re-base the caller onto it (see auth.require_admin). Endpoints that
# need a finer check still call _require_system_scope(system, user, db) on top —
# it sees the re-based user, so it authorizes against the active club too.
_require_any_admin = require_admin


@router.get("/me")
def admin_me(
    request: Request,
    user: Optional[User] = Depends(current_user),
    db: Session = Depends(get_session),
):
    """Return the caller's admin status for the club they're currently in.
    Always 200; unauthenticated = no access. Evaluated at the active club (from
    the subdomain): a platform admin is a super-admin of every club they visit;
    a club's own super-admin only in their own club (on a foreign subdomain they
    report as a plain player). is_platform_admin is cross-club and always
    reflects the flag."""
    if user is None:
        return {"is_super_admin": False, "is_platform_admin": False, "scopes": []}
    active = resolve_active_club_id(db, user, request.headers.get("origin"))
    # Scopes are evaluated AT the active club: a platform admin gets full scopes
    # at whatever club they're on; a club's own super-admin only in their club.
    scopes = admin_scopes(user, db, active)
    is_super = user.is_platform_admin or (user.is_super_admin and active == user.club_id)
    return {
        "is_super_admin": is_super,
        "is_platform_admin": user.is_platform_admin,
        "scopes": sorted(scopes),
        # Global support/community Discord link (platform-admin set), shown on
        # the admin page so any admin can reach the support server.
        "community_discord_url": _community_discord_url(db),
    }


@router.get("/onboarding")
def onboarding_status(
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """First-run checklist state for the club the caller is currently
    administering (the active club — the caller is re-based onto it by the gate).
    Each item is a boolean derived from real data, so it ticks itself off as the
    admin does the setup."""
    club_id = user.club_id
    club = db.get(Club, club_id)
    if club is None:
        raise HTTPException(status_code=404, detail="Club not found.")

    systems_enabled = db.exec(
        scoped(ClubSystem, club_id).where(ClubSystem.enabled == True)
    ).first() is not None
    webhook_set = db.exec(scoped(ClubWebhook, club_id)).first() is not None
    pairings_published = db.exec(
        scoped(PublishState, club_id).where(PublishState.published == True)
    ).first() is not None

    # "co_admin" = has this club delegated any per-game-system admin (an
    # AdminRole scope grant). Deliberately does NOT count super-admins — the
    # step is about handing a specific system to a lieutenant, so a club with
    # only its founding super-admin(s) and no scope grants reads as not-yet-done.
    any_scope_admin = db.exec(scoped(AdminRole, club_id)).first() is not None

    items = {
        "profile_blurb": bool((club.blurb or "").strip()),
        "logo": bool(club.logo_url),
        # Coordinates, not the address text. Without them a club is a list entry
        # on the finder with no marker on the map, and nothing anywhere told
        # them so: provisioning cannot set coordinates from a town name, and the
        # club-page editor has the fields but nobody was pointed at them.
        "map_pin": club.latitude is not None and club.longitude is not None,
        # The public Discord invite shown front-and-centre on the club page —
        # distinct from discord_webhook (the app's posting webhook) below.
        "discord_link": bool(club.discord_url),
        "systems_enabled": systems_enabled,
        "discord_webhook": webhook_set,
        "co_admin": any_scope_admin,
        "first_pairings_published": pairings_published,
    }
    return {
        "club": {"id": club.id, "name": club.name, "slug": club.slug, "active": club.active},
        "items": items,
        "complete": all(items.values()),
    }


@router.get("/roles")
def list_roles(
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """All current admin_roles rows joined to user info, plus super-admin list."""
    role_rows = db.exec(
        select(AdminRole, User)
        .join(User, User.id == AdminRole.user_id)
        .where(AdminRole.club_id == user.club_id)
        .order_by(User.discord_name)
    ).all()

    roles = []
    for role, role_user in role_rows:
        player_name = None
        if role_user.player_id:
            p = db.get(Player, role_user.player_id)
            player_name = p.name if p else None
        roles.append({
            "user_id": role.user_id,
            "discord_name": role_user.discord_name,
            "name": display_name_for(role_user),
            "player_name": player_name,
            "scope": role.scope,
        })

    super_admins_rows = db.exec(
        scoped(User, user.club_id).where(User.is_super_admin == True).order_by(User.discord_name)
    ).all()
    super_admins = []
    for sa in super_admins_rows:
        player_name = None
        if sa.player_id:
            p = db.get(Player, sa.player_id)
            player_name = p.name if p else None
        super_admins.append({
            "user_id": sa.id,
            "discord_name": sa.discord_name,
            "name": display_name_for(sa),
            "player_name": player_name,
        })

    return {"roles": roles, "super_admins": super_admins}


@router.get("/grantable-users")
def grantable_users(
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """Active users with a linked player — the candidate list for the appoint UI."""
    users = db.exec(
        scoped(User, user.club_id).where(User.player_id.isnot(None))
    ).all()
    # One query for every user's player, not one each: at ~130 users the
    # per-user lookup took seconds, and the Admins tab now waits on this.
    players = {
        p.id: p
        for p in db.exec(select(Player).where(Player.id.in_([u.player_id for u in users]))).all()
    } if users else {}

    result = []
    for u in users:
        p = players.get(u.player_id)
        if p and p.active:
            result.append({
                "id": u.id,
                "discord_name": u.discord_name,
                "name": display_name_for(u),
                "player_name": p.name,
            })

    result.sort(key=lambda x: x["player_name"].lower())
    return result


class RoleBody(BaseModel):
    user_id: int
    scope: str


@router.post("/roles")
def grant_role(
    body: RoleBody,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """Idempotent: insert (user_id, scope) if not already present."""
    scopes = valid_scopes(db)
    if body.scope not in scopes:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid scope. Must be one of: {sorted(scopes)}",
        )

    runnable = club_runnable_scopes(user.club_id, db)
    if body.scope not in runnable:
        raise HTTPException(
            status_code=422,
            detail=f"Your club does not run {body.scope!r}. Must be one of: {sorted(runnable)}",
        )

    target = db.get(User, body.user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found.")

    existing = db.exec(
        scoped(AdminRole, user.club_id)
        .where(AdminRole.user_id == body.user_id)
        .where(AdminRole.scope == body.scope)
    ).first()

    if existing is None:
        db.add(AdminRole(user_id=body.user_id, scope=body.scope, club_id=user.club_id))
        log_audit(db, user, "role.grant", "user", target.id, f"{display_name_for(target)!r} -> {body.scope!r}")
        db.commit()

    return {"ok": True}


@router.delete("/roles")
def remove_role(
    user_id: int,
    scope: str,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """Delete the (user_id, scope) row if present."""
    row = db.exec(
        scoped(AdminRole, user.club_id)
        .where(AdminRole.user_id == user_id)
        .where(AdminRole.scope == scope)
    ).first()

    if row is not None:
        target = db.get(User, user_id)
        target_name = display_name_for(target) if target else str(user_id)
        db.delete(row)
        log_audit(db, user, "role.revoke", "user", user_id, f"{target_name!r} -> {scope!r}")
        db.commit()
        return {"ok": True, "removed": True}

    return {"ok": True, "removed": False}


@router.get("/players")
def admin_players(
    scope: Optional[str] = None,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """Scoped read (scope provided) or global super-admin read (scope omitted).

    scope provided: 403 unless the caller holds that scope; returns active players.
    scope omitted:  403 unless super-admin; returns all players with full profile
                    fields so the edit-player panel can pre-fill forms.
    """
    if scope is not None:
        if scope not in valid_scopes(db):
            raise HTTPException(status_code=422, detail="Invalid scope.")
        if scope not in admin_scopes(user, db):
            raise HTTPException(status_code=403, detail=f"Admin access for '{scope}' required.")
        players = db.exec(
            scoped(Player, user.club_id).where(Player.active == True).order_by(Player.name)
        ).all()
        return [{"id": p.id, "name": p.name, "active": p.active} for p in players]

    # No scope — super-admin only, full player list for the edit-player panel.
    if not user.is_super_admin:
        raise HTTPException(status_code=403, detail="Super-admin access required.")
    players = db.exec(scoped(Player, user.club_id).order_by(Player.name)).all()
    owner_ids = {p.user_id for p in players if p.user_id is not None}
    owners = {
        u.id: u for u in db.exec(select(User).where(User.id.in_(owner_ids))).all()
    } if owner_ids else {}
    return [
        {
            "id": p.id,
            "name": p.name,
            "titles": player_titles(p),
            "active": p.active,
            "league_visible": p.league_visible,
            # Whether a Discord account owns this row. The admin needs to see
            # it before archiving or deleting: doing either to a claimed row
            # affects a real person who is logged in right now, while an
            # unclaimed row is just a roster entry nobody has taken.
            "claimed": p.user_id is not None,
            # The name the owning account chose for itself, if any, so an admin
            # can see it beside the roster name and reset it if it's abusive.
            "account_name": owners[p.user_id].display_name if p.user_id in owners else None,
            "admin_notes": p.admin_notes,
        }
        for p in players
    ]


@router.post("/players/{player_id}/reset-account-name")
def reset_player_account_name(
    player_id: int,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """Clear the name the account owning this player chose, so it goes by its
    Discord handle again. For a name that shouldn't be there; the player's
    roster name is edited through PATCH /admin/players/{id} as always.
    """
    player = db.get(Player, player_id)
    if player is None or player.club_id != user.club_id:
        raise HTTPException(status_code=404, detail="Player not found.")
    owner = db.get(User, player.user_id) if player.user_id else None
    if owner is None:
        raise HTTPException(status_code=404, detail="No account is linked to this player.")
    old = owner.display_name
    owner.display_name = None
    db.add(owner)
    log_audit(db, user, "account.name.reset", "user", owner.id,
              f"{old!r} on player {player.name!r} ({player.id})")
    db.commit()
    return {"ok": True, "name": display_name_for(owner)}



@router.delete("/players/{player_id}")
def delete_player(
    player_id: int,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    """Permanently remove a player row — but ONLY one with no playing history.

    Deleting a row that has history is not a tidy-up, it's data loss with a
    delay on it. Signup rows carry `player_id` as a plain int with no foreign
    key, so removing the player leaves signups pointing at an id that no longer
    resolves: they still appear in old pairings and league tables under their
    stored `player_name`, but nothing can attribute them to anybody, and the
    games stop counting toward experience and level. That is not hypothetical —
    player 113 was deleted straight from the database and orphaned two of Shaun
    Warne's signups (747, 775), which is part of why his record ended up split
    across three rows.

    So there are two different operations and this is the narrow one:

      Archive (PATCH active=False) — for a real player who has left. Keeps
        every game they played, keeps their account linked so they get their
        own profile back if they return.
      Delete (here) — for a row that should never have existed: a duplicate, a
        typo, a test entry, someone who signed up and never played.

    History-bearing rows are refused with a 409 pointing at archive.
    """
    player = db.get(Player, player_id)
    if not player or player.club_id != user.club_id:
        raise HTTPException(status_code=404, detail="Player not found.")

    signups = db.exec(
        scoped(Signup, user.club_id).where(Signup.player_id == player_id)
    ).all()
    results = db.exec(
        scoped(LeagueResult, user.club_id).where(
            (LeagueResult.player_1_id == player_id) | (LeagueResult.player_2_id == player_id)
        )
    ).all()
    if signups or results:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{player.name} has {len(signups)} signup(s) and {len(results)} league "
                f"result(s). Deleting would orphan them. Archive the player instead. "
                f"That hides them everywhere but keeps their history intact."
            ),
        )

    # Nothing depends on the row's play history, but these side tables can still
    # hold entries (a player can set "games played elsewhere" or pass the
    # Discord gate before ever signing up), and they'd outlive the row.
    for model, col in (
        (PlayerExperienceAdjustment, PlayerExperienceAdjustment.player_id),
        (PlayerLevelAnnouncement, PlayerLevelAnnouncement.player_id),
        (PlayerDiscordVerification, PlayerDiscordVerification.player_id),
        (LeagueRating, LeagueRating.player_id),
    ):
        for row in db.exec(select(model).where(col == player_id)).all():
            db.delete(row)

    # Clear the legacy User.player_id back-link, or the owner is left pointing
    # at a dead row and /auth/me resolves nothing.
    for u in db.exec(select(User).where(User.player_id == player_id)).all():
        u.player_id = None
        db.add(u)

    name = player.name
    db.delete(player)
    log_audit(db, user, "player.delete", "player", player_id, f"{name!r} (no history)")
    db.commit()
    return {"ok": True, "deleted": True}


class PatchPlayerBody(BaseModel):
    name: Optional[str] = None
    titles: Optional[list[str]] = None
    active: Optional[bool] = None
    league_visible: Optional[bool] = None
    admin_notes: Optional[str] = None


@router.patch("/players/{player_id}")
def patch_player(
    player_id: int,
    body: PatchPlayerBody,
    user: User = Depends(require_super_admin),
    db: Session = Depends(get_session),
):
    player = db.get(Player, player_id)
    if not player or player.club_id != user.club_id:
        raise HTTPException(status_code=404, detail="Player not found.")

    if body.name is not None:
        stripped = body.name.strip()
        if stripped:
            player.name = stripped

    if body.titles is not None:
        set_player_titles(player, body.titles)

    if body.active is not None:
        player.active = body.active

    if body.league_visible is not None:
        player.league_visible = body.league_visible

    if body.admin_notes is not None:
        player.admin_notes = body.admin_notes.strip() or None

    db.add(player)
    db.commit()
    db.refresh(player)

    return {
        "id": player.id,
        "name": player.name,
        "titles": player_titles(player),
        "active": player.active,
        "league_visible": player.league_visible,
        "claimed": player.user_id is not None,
        "admin_notes": player.admin_notes,
    }


# ---------------------------------------------------------------------------
# Per-system player roster
# ---------------------------------------------------------------------------
# The club roster answers "who is a member". This answers "who plays MY game
# night", which is a different and much shorter list, and it is the one a
# system admin actually works from. There is no membership table behind it and
# deliberately so: turning up is what makes you a player of a system, so the
# roster is derived from signups and needs nothing kept in step.


@router.get("/system-players")
def system_players(
    system: str,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """Everyone who has ever signed up for this system at this club.

    Signups rather than pairings, so someone who signed up and was byed still
    counts as one of yours. Archived players are included and flagged rather
    than hidden — a system admin looking at a name on last month's pairings
    needs to find them, and hiding the row makes it look like a data error.
    """
    _require_system_scope(system, user, db)

    # One aggregate for the whole roster: player_id, how many weeks they have
    # signed up for, and the most recent one. Doing this per player is the N+1
    # that made the pairings page expensive (see experience.counts_for_players).
    rows = db.exec(
        text(
            """
            SELECT player_id, count(DISTINCT week) AS weeks, max(created_at) AS last_at
            FROM signups
            WHERE club_id = :club_id AND system = :system AND player_id IS NOT NULL
            GROUP BY player_id
            """
        ),
        params={"club_id": user.club_id, "system": system},
    ).all()
    if not rows:
        return []

    weeks_by_id = {pid: weeks for pid, weeks, _ in rows}
    last_by_id = {pid: last for pid, _, last in rows}
    ids = list(weeks_by_id)

    players = {
        p.id: p
        for p in db.exec(scoped(Player, user.club_id).where(Player.id.in_(ids))).all()
    }
    games_by_id = counts_for_players(db, user.club_id, system, ids)

    # The Discord handle behind each player, so an admin reading the roster can
    # match a name here to a name in their server without opening two tabs.
    #
    # Both links are read because both exist: Player.user_id is the multi-club
    # ownership link and the one to trust, User.player_id is the pre-2026-07-25
    # home-club link that is still populated. Taking only one leaves a column
    # of blanks for whichever half of the roster was claimed under the other.
    discord_by_player: dict[int, Optional[str]] = {}
    account_by_player: dict[int, str] = {}
    owner_ids = {p.user_id for p in players.values() if p.user_id is not None}
    if owner_ids:
        for u in db.exec(select(User).where(User.id.in_(owner_ids))).all():
            for pid, p in players.items():
                if p.user_id == u.id:
                    discord_by_player[pid] = u.discord_name
                    account_by_player[pid] = display_name_for(u)
    for u in db.exec(select(User).where(User.player_id.in_(ids))).all():
        discord_by_player.setdefault(u.player_id, u.discord_name)
        account_by_player.setdefault(u.player_id, display_name_for(u))

    # Self-declared games played elsewhere. Already per (club, system), and
    # until now only the player could set it.
    adj_by_id = {
        a.player_id: a.extra_games
        for a in db.exec(
            select(PlayerExperienceAdjustment)
            .where(PlayerExperienceAdjustment.club_id == user.club_id)
            .where(PlayerExperienceAdjustment.system == system)
            .where(PlayerExperienceAdjustment.player_id.in_(ids))
        ).all()
    }

    # Rating comes from the CURRENT season only. An all-time number would mix
    # seasons that each reset to 1000 and mean nothing.
    system_id = _system_id_for(db, system)
    season_id = _current_season_id(db, user.club_id, system_id)
    ratings_by_id: dict[int, float] = {}
    if season_id is not None:
        ratings_by_id = {
            r.player_id: r.rating
            for r in db.exec(
                scoped(LeagueRating, user.club_id)
                .where(LeagueRating.system_id == system_id)
                .where(LeagueRating.season_id == season_id)
                .where(LeagueRating.player_id.in_(ids))
            ).all()
        }

    out = []
    for pid in ids:
        p = players.get(pid)
        if p is None:
            # A signup whose player row belongs to another club, or was
            # deleted. Not this roster's problem, and not worth a blank row.
            continue
        games = games_by_id.get(pid, 0)
        extra = adj_by_id.get(pid, 0)
        out.append({
            "player_id": pid,
            "name": p.name,
            "discord_name": discord_by_player.get(pid),
            # Linked to an account at all, which is not the same as having a
            # Discord handle once other sign-in methods exist.
            "linked": pid in account_by_player,
            "account_name": account_by_player.get(pid),
            "titles": player_titles(p),
            "active": p.active,
            "league_visible": p.league_visible,
            "weeks_signed_up": weeks_by_id.get(pid, 0),
            "last_signup_at": last_by_id.get(pid),
            "games": games,
            # The tier the matcher sees, which counts declared games too.
            "experience": tier_for(games + extra),
            "extra_games": extra,
            "level": level_for(games),
            "rating": ratings_by_id.get(pid),
        })
    out.sort(key=lambda r: (-r["games"], r["name"].lower()))
    return out


class SystemTitlesBody(BaseModel):
    system: str
    player_id: int
    titles: list[str]


@router.post("/system-players/titles")
def set_system_player_titles(
    body: SystemTitlesBody,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """Set a player's titles from their system's roster.

    Separate from PATCH /players, which is super-admin only and also carries
    name, roster status and league visibility. Those are club-level decisions
    and stay where they are; awarding "Old World Champion 2026" is the system
    admin's, so it gets an endpoint that can do nothing else.

    NOTE titles are stored on the Player and are therefore CLUB-WIDE: they show
    on that player's profile whatever system you are looking at. Two systems
    editing the same list will overwrite each other. That is a real limitation
    of putting them here, and the alternative was a per-system titles table
    nobody asked for.
    """
    _require_system_scope(body.system, user, db)

    player = db.exec(
        scoped(Player, user.club_id).where(Player.id == body.player_id)
    ).first()
    if player is None:
        raise HTTPException(status_code=404, detail="No such player at this club.")

    cleaned = [t.strip() for t in body.titles if t and t.strip()]
    if len(cleaned) > 20:
        raise HTTPException(status_code=422, detail="Twenty titles is the most one player can hold.")
    if any(len(t) > 80 for t in cleaned):
        raise HTTPException(status_code=422, detail="A title can be at most 80 characters.")

    set_player_titles(player, cleaned)
    db.add(player)
    log_audit(db, user, "player.titles", "player", player.id,
              f"{player.name!r} in {body.system!r}: {cleaned}")
    db.commit()
    db.refresh(player)
    return {"ok": True, "player_id": player.id, "titles": player_titles(player)}


class SystemExperienceBody(BaseModel):
    system: str
    player_id: int
    extra_games: int


@router.post("/system-players/experience")
def set_system_player_experience(
    body: SystemExperienceBody,
    user: User = Depends(_require_any_admin),
    db: Session = Depends(get_session),
):
    """Set a player's declared games-played-elsewhere for this system.

    The player can already set this themselves; an admin needs to as well,
    because the common case is someone who never will. It only moves the
    experience tier the matcher reads, never the level: levels come from
    pairings alone, on purpose, so that a self-declared number can never buy
    progression (see levels.py).
    """
    _require_system_scope(body.system, user, db)
    if body.extra_games < 0:
        raise HTTPException(status_code=422, detail="Games played elsewhere cannot be negative.")
    if body.extra_games > 1000:
        raise HTTPException(status_code=422, detail="That's more games than we can credit. 1000 is the maximum.")

    player = db.exec(
        scoped(Player, user.club_id).where(Player.id == body.player_id)
    ).first()
    if player is None:
        raise HTTPException(status_code=404, detail="No such player at this club.")

    row = db.exec(
        select(PlayerExperienceAdjustment)
        .where(PlayerExperienceAdjustment.club_id == user.club_id)
        .where(PlayerExperienceAdjustment.player_id == body.player_id)
        .where(PlayerExperienceAdjustment.system == body.system)
    ).first()
    if row is None:
        row = PlayerExperienceAdjustment(
            club_id=user.club_id, player_id=body.player_id,
            system=body.system, extra_games=body.extra_games,
        )
    else:
        row.extra_games = body.extra_games
        row.updated_at = datetime.utcnow()
    db.add(row)
    db.commit()

    games = games_played(db, user.club_id, body.player_id, body.system)
    return {
        "ok": True,
        "player_id": body.player_id,
        "extra_games": body.extra_games,
        "experience": tier_for(games + body.extra_games),
    }


# ---------------------------------------------------------------------------
# Shared by more than one admin module
# ---------------------------------------------------------------------------
# The topic modules (admin_pairings, admin_signups, admin_league, admin_club,
# admin_platform) import from here and never from each other, so a helper two
# of them need lives in this file.

MAX_CLUB_IMAGE_BYTES = 5 * 1024 * 1024

VALID_CADENCES: frozenset[str] = frozenset({"weekly", "fortnightly"})

# Community/support Discord link — a single global URL (platform-admin set),
# shown on every club's admin page so admins can reach the support server.
_COMMUNITY_DISCORD_KEY = "community_discord_url"


def _system_id_for(db: Session, system: str) -> int:
    """Catalogue id for a legacy system name.

    Deliberately NOT league._resolve_system_id, which additionally insists the
    club has a league enabled for that system. A pairing block has nothing to
    do with leagues, and routing it through that helper would have made
    blocking a pair impossible in any system whose league is switched off.
    """
    sc = db.exec(
        select(SystemConfig).where(SystemConfig.legacy_system_name == system)
    ).first()
    if sc is None:
        raise HTTPException(status_code=422, detail=f"Unknown system {system!r}.")
    return sc.id


_TIME_RE = re.compile(r"^(2[0-3]|[01]\d):[0-5]\d$")


def _require_system_scope(system: str, user: User, db: Session) -> None:
    if system not in valid_scopes(db):
        raise HTTPException(status_code=422, detail="Invalid scope.")
    if system not in admin_scopes(user, db):
        raise HTTPException(status_code=403, detail=f"Admin access for '{system}' required.")


def _resync_venue_floor(db: Session, club_id: int, system: str, week: str) -> None:
    """Keep the venue's table plan in step after this week's pairings change.

    Regenerating or deleting pairings gives every game a new id, which orphans
    every seat pointing at the old ones — the plan then shows an empty room
    while demanding a table for each game. Only ever touches a plan that
    already exists, so this is inert before the week is published.
    """
    try:
        from venue_seating import resync

        resync(db, club_id, system, week)
    except Exception as e:
        capture(e, kind="venue_seating_resync", system=system)


def _validate_schedule_fields(
    session_day: str, session_cadence: str, cadence_anchor: Optional[date],
    session_start_time: Optional[str] = None,
) -> None:
    """Shared by upsert_club_system (platform-admin, enabling a new
    system for a club) and update_club_system_schedule (club
    self-service, editing an already-enabled system's schedule) — same
    fields, same rules, extracted once rather than duplicated."""
    if session_start_time is not None and not _TIME_RE.match(session_start_time):
        raise HTTPException(status_code=422, detail="session_start_time must match HH:MM (00-23 / 00-59)")

    if session_day not in _DAY_NAME_TO_INT:
        raise HTTPException(
            status_code=422,
            detail=f"session_day must be one of: {sorted(_DAY_NAME_TO_INT)}",
        )

    if session_cadence not in VALID_CADENCES:
        raise HTTPException(
            status_code=422,
            detail=f"session_cadence must be one of: {sorted(VALID_CADENCES)}",
        )

    if session_cadence == "fortnightly" and cadence_anchor is None:
        raise HTTPException(
            status_code=422,
            detail="cadence_anchor is required when session_cadence is 'fortnightly'.",
        )
    if session_cadence == "weekly" and cadence_anchor is not None:
        raise HTTPException(
            status_code=422,
            detail="cadence_anchor must not be set when session_cadence is 'weekly'.",
        )

    if cadence_anchor is not None and _DAY_NAME_TO_INT[session_day] != cadence_anchor.weekday():
        raise HTTPException(
            status_code=422,
            detail=(
                f"cadence_anchor ({cadence_anchor.strftime('%A')}) must fall on "
                f"session_day ({session_day})."
            ),
        )


def _community_discord_url(db: Session) -> Optional[str]:
    row = db.get(AppSetting, _COMMUNITY_DISCORD_KEY)
    return row.value if row else None


class ClubProfileBody(BaseModel):
    blurb: Optional[str] = None
    website_url: Optional[str] = None
    discord_url: Optional[str] = None
    opening_hours: Optional[list[dict]] = None
    address: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    region: Optional[str] = None


def _club_profile_dict(club: Club) -> dict:
    return {
        "blurb": club.blurb,
        "logo_url": club.logo_url,
        "website_url": club.website_url,
        "discord_url": club.discord_url,
        "opening_hours": club.opening_hours or [],
        "address": club.address,
        "latitude": club.latitude,
        "longitude": club.longitude,
        "region": club.region,
    }


def _apply_club_profile_update(club: Club, body: ClubProfileBody, db: Session) -> Club:
    """Shared by the club's own super-admin (POST /admin/club, always their
    own club) and a platform admin editing any club
    (POST /admin/platform/clubs/{club_id}/profile) — same fields, same
    validation, extracted once rather than duplicated."""
    if body.opening_hours is not None:
        for row in body.opening_hours:
            if row.get("day") not in _DAY_NAME_TO_INT:
                raise HTTPException(status_code=422, detail=f"Invalid day: {row.get('day')!r}")

    if body.latitude is not None and not (-90 <= body.latitude <= 90):
        raise HTTPException(status_code=422, detail="latitude must be between -90 and 90.")
    if body.longitude is not None and not (-180 <= body.longitude <= 180):
        raise HTTPException(status_code=422, detail="longitude must be between -180 and 180.")

    provided = body.model_fields_set
    if "blurb" in provided:
        club.blurb = (body.blurb or "").strip() or None
    if "website_url" in provided:
        club.website_url = (body.website_url or "").strip() or None
    if "discord_url" in provided:
        club.discord_url = (body.discord_url or "").strip() or None
    if "opening_hours" in provided:
        club.opening_hours = body.opening_hours
    if "address" in provided:
        club.address = (body.address or "").strip() or None
    if "latitude" in provided:
        club.latitude = body.latitude
    if "longitude" in provided:
        club.longitude = body.longitude
    if "region" in provided:
        region = (body.region or "").strip() or None
        if region is not None and region not in UK_REGIONS:
            raise HTTPException(status_code=422, detail=f"Invalid region: {region!r}")
        club.region = region
    db.add(club)
    db.commit()
    db.refresh(club)
    return club


def _upload_club_logo_for(club: Club, image: UploadFile, db: Session) -> str:
    """Shared by the club's own super-admin and a platform admin uploading
    on any club's behalf."""
    data = image.file.read()
    if not data:
        raise HTTPException(status_code=422, detail="Empty image upload.")
    if len(data) > MAX_CLUB_IMAGE_BYTES:
        raise HTTPException(
            status_code=422,
            detail=f"Image too large (max {MAX_CLUB_IMAGE_BYTES // (1024 * 1024)} MB).",
        )
    try:
        storage.extension_for(image.content_type)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    try:
        object_path, public_url = storage.upload_club_logo(data, image.content_type, club.id)
    except RuntimeError as e:
        raise HTTPException(status_code=502, detail=f"Image upload failed: {e}")

    old_path = club.logo_path
    club.logo_path = object_path
    club.logo_url = public_url
    db.add(club)
    db.commit()
    if old_path:
        storage.delete_club_image(old_path)
    return public_url


def _delete_club_logo_for(club: Club, db: Session) -> None:
    old_path = club.logo_path
    club.logo_path = None
    club.logo_url = None
    db.add(club)
    db.commit()
    if old_path:
        storage.delete_club_image(old_path)
