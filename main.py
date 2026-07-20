from datetime import date, datetime, timedelta
from typing import Optional
from fastapi import FastAPI, Depends, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlmodel import Session, select, or_

from database import get_session, resolve_request_club_id, scoped
from models import Club, ClubEvent, ClubSystem, PlatformBanner, Player, LeagueResult, LeagueRating, Signup, Pairing, PublishState, User, SystemConfig
from week_logic import next_session_date, sessions_in_range
from systems import factions_for, icon_folder_for
from services import (
    compute_league_record,
    fetch_player_results,
    fetch_player_signups,
    signup_counts_per_system,
    faction_usage_per_system,
    build_elo_history,
    first_league_winner_id,
    compute_achievements,
    player_titles,
    ACHIEVEMENT_DESCRIPTIONS,
)
from auth import router as auth_router, require_user, current_user
from signups import router as signups_router, CANONICAL_VIBES, _get_system_config
from league import router as league_router, _resolve_system_id, _current_season_id
from admin import router as admin_router

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
    ],
    # Matches:
    #   - Vercel preview URLs for the frontend (call-to-arms-web*.vercel.app)
    #   - calltoarms.app, www.calltoarms.app, and any club subdomain
    #     (e.g. manchester.calltoarms.app, test1.calltoarms.app) now that
    #     the wildcard domain is live in Vercel. Multi-club rollout needs
    #     every future subdomain to work here without another deploy.
    allow_origin_regex=r"^https://(call-to-arms-web.*\.vercel\.app|([a-zA-Z0-9-]+\.)?calltoarms\.app)$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(signups_router)
app.include_router(league_router)
app.include_router(admin_router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/platform-banner")
def get_platform_banner(session: Session = Depends(get_session)):
    """Public read of the site-wide announcement banner, for the frontend
    to show at the top of every page (any club, logged in or not). Only
    ever returns the banner when active — an inactive/never-set banner
    looks identical to the frontend (active: False), so there's no need
    for a separate "does a banner exist" signal."""
    banner = session.get(PlatformBanner, 1)
    if banner is None or not banner.active:
        return {"active": False, "message": "", "severity": "info"}
    return {"active": True, "message": banner.message, "severity": banner.severity}


@app.get("/systems")
def list_systems(club: Optional[str] = None, session: Session = Depends(get_session)):
    """Public read of the systems-as-data catalogue (Phase 0), for the
    frontend to fetch signup-form config (vibes/scenarios/points) instead of
    keeping its own hardcoded copies. No auth required — this is the same
    information that's currently only available as hardcoded frontend
    constants, not new access.

    Optional ?club=<slug>: when given, each system's vibe options/default are
    overridden by that club's ClubSystem config where it has set one, so the
    signup form shows the club's own vibes. No club (or no override) → the
    platform catalogue defaults, unchanged.

    Not gated by the systems_from_catalogue flag: that flag controls whether
    the backend's own signup/pairing computation uses the catalogue
    internally. This endpoint is a brand-new read path with no prior
    behavior to preserve, so it's always on.
    """
    rows = session.exec(
        select(SystemConfig).where(SystemConfig.active == True)
    ).all()
    overrides: dict = {}
    if club:
        club_row = session.exec(select(Club).where(Club.slug == club)).first()
        if club_row is not None:
            for cs in session.exec(
                select(ClubSystem).where(ClubSystem.club_id == club_row.id)
            ).all():
                overrides[cs.system_id] = cs
    return [_system_dict(r, overrides.get(r.id)) for r in rows]


def _system_dict(r: SystemConfig, club_system=None) -> dict:
    vibe_options = r.vibe_options
    default_vibe = r.default_vibe
    if club_system is not None and club_system.vibe_options:
        vibe_options = club_system.vibe_options
        default_vibe = club_system.default_vibe or (vibe_options[0] if vibe_options else None)
    # Only surface canonical vibes — drops any stale/removed value (e.g. the
    # retired "Escalation") still lingering in catalogue data.
    vibe_options = [v for v in (vibe_options or []) if v in CANONICAL_VIBES]
    if default_vibe not in vibe_options:
        default_vibe = vibe_options[0] if vibe_options else None
    return {
        "id": r.id,
        "slug": r.slug,
        "name": r.name,
        "legacy_system_name": r.legacy_system_name,
        "uses_points": r.uses_points,
        "default_points": r.default_points,
        "max_points": r.max_points,
        "vibe_options": vibe_options,
        "default_vibe": default_vibe,
        "uses_scenarios": r.uses_scenarios,
        "scenario_options": r.scenario_options,
        "default_scenario": r.default_scenario,
        "allows_demo": r.allows_demo,
        # Per-club reality when club context is available (does THIS club run
        # a league for this system — ClubSystem.league_enabled, the source of
        # truth since the modular-leagues work); falls back to the platform
        # catalogue flag only for the fully-unscoped call (no club to ask).
        "has_league": bool(club_system.league_enabled) if club_system is not None else r.has_league,
        # System *rules* — sourced from the hardcoded per-system modules in
        # systems/, keyed by legacy_system_name, NOT from the (dead)
        # SystemConfig.faction_list / icon_folder DB columns. None for any
        # catalogue system without a hardcoded ruleset yet.
        "faction_list": factions_for(r.legacy_system_name),
        "icon_folder": icon_folder_for(r.legacy_system_name),
    }


@app.get("/systems/mine")
def list_my_systems(user: User = Depends(require_user), session: Session = Depends(get_session)):
    """Authenticated, club-scoped: the caller's own club's currently-enabled
    systems, in the same shape as GET /systems so the frontend can swap
    between the two feeds without special-casing. Unlike GET /systems
    (the full global catalogue, always public and unfiltered — used to
    populate "which system to enable" pickers, and must stay that way),
    this reflects each club's own self-service enable/disable choices."""
    pairs = session.exec(
        select(SystemConfig, ClubSystem)
        .join(ClubSystem, ClubSystem.system_id == SystemConfig.id)
        .where(ClubSystem.club_id == user.club_id)
        .where(ClubSystem.enabled == True)
        .where(SystemConfig.active == True)
    ).all()
    return [_system_dict(r, cs) for r, cs in pairs]


@app.get("/clubs")
def list_clubs(session: Session = Depends(get_session)):
    """Public read of active clubs, for the frontend's club-picker at
    signup, and the multi-club map on the logged-out hero (which uses
    latitude/longitude — clubs without coordinates just don't get a pin).
    No auth required — same tone/structure as GET /systems."""
    rows = session.exec(
        select(Club).where(Club.active == True)
    ).all()
    return [
        {
            "id": c.id,
            "name": c.name,
            "slug": c.slug,
            "address": c.address,
            "latitude": c.latitude,
            "longitude": c.longitude,
        }
        for c in rows
    ]


DEFAULT_ACCENT_COLOR = "#c9a14a"  # platform gold, used when a system admin hasn't set accent_color


@app.get("/club")
def get_club(
    month: Optional[str] = None,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """Club landing page: profile, the systems carousel (this club's enabled
    systems, ordered for display), and one calendar month's worth of
    entries — auto-derived recurring sessions (from each ClubSystem's
    schedule) merged with one-off ClubEvent rows, both colour-coded by
    system via accent_color.

    ?month=YYYY-MM selects the calendar month (default: current month)."""
    club = session.get(Club, user.club_id)
    if club is None:
        raise HTTPException(status_code=404, detail="Club not found")

    # Deliberately no manual ordering here (ClubSystem.carousel_order is
    # unused — kept as a harmless orphan column, same convention as
    # Club.leagues_enabled). A fixed admin-chosen order would silently
    # favour whichever system got put first; the frontend shuffles this
    # list itself on every page load instead, so no system is permanently
    # advantaged. Ordering by name here just keeps the API response stable
    # for anything that doesn't shuffle (e.g. tests).
    club_systems = session.exec(
        select(ClubSystem, SystemConfig)
        .join(SystemConfig, SystemConfig.id == ClubSystem.system_id)
        .where(ClubSystem.club_id == club.id)
        .where(ClubSystem.enabled == True)
        .where(SystemConfig.active == True)
        .order_by(SystemConfig.name)
    ).all()

    systems_out = [
        {
            "system_id": sc.id,
            "slug": sc.slug,
            "name": sc.name,
            "legacy_system_name": sc.legacy_system_name,
            "session_day": cs.session_day,
            "session_cadence": cs.session_cadence,
            "blurb": cs.carousel_blurb,
            "photo_url": cs.carousel_photo_url,
            "accent_color": cs.accent_color or DEFAULT_ACCENT_COLOR,
        }
        for cs, sc in club_systems
    ]

    if month:
        try:
            month_start = datetime.strptime(month, "%Y-%m").date()
        except ValueError:
            raise HTTPException(status_code=422, detail="month must be formatted YYYY-MM")
    else:
        today = date.today()
        month_start = date(today.year, today.month, 1)
    next_month = date(month_start.year + (month_start.month == 12), (month_start.month % 12) + 1, 1)
    month_end = next_month - timedelta(days=1)

    accent_by_system_id = {cs.system_id: (cs.accent_color or DEFAULT_ACCENT_COLOR) for cs, _ in club_systems}
    name_by_system_id = {sc.id: sc.name for _, sc in club_systems}
    slug_by_system_id = {sc.id: sc.slug for _, sc in club_systems}

    calendar_out: list[dict] = []
    for cs, sc in club_systems:
        for d in sessions_in_range(cs.session_day, cs.session_cadence, cs.cadence_anchor, month_start, month_end):
            title = f"{sc.name} session"
            if cs.session_start_time:
                title = f"{title} {cs.session_start_time}"
            calendar_out.append({
                "type": "session",
                "date": d.isoformat(),
                "title": title,
                "system_id": sc.id,
                "system_name": sc.name,
                "system_slug": sc.slug,
                "accent_color": cs.accent_color or DEFAULT_ACCENT_COLOR,
                # An all-day entry when no start time is set (unchanged
                # default); otherwise a timed entry, same shape as ClubEvent.
                "all_day": cs.session_start_time is None,
                "start_time": cs.session_start_time,
                "end_time": None,
            })

    events = session.exec(
        scoped(ClubEvent, club.id)
        .where(ClubEvent.event_date >= month_start)
        .where(ClubEvent.event_date <= month_end)
        .order_by(ClubEvent.event_date)
    ).all()
    for ev in events:
        calendar_out.append({
            "type": "event",
            "date": ev.event_date.isoformat(),
            "title": ev.title,
            "description": ev.description,
            "system_id": ev.system_id,
            "system_name": name_by_system_id.get(ev.system_id) if ev.system_id else None,
            "system_slug": slug_by_system_id.get(ev.system_id) if ev.system_id else None,
            "accent_color": accent_by_system_id.get(ev.system_id, DEFAULT_ACCENT_COLOR) if ev.system_id else DEFAULT_ACCENT_COLOR,
            "all_day": ev.all_day,
            "start_time": ev.start_time,
            "end_time": ev.end_time,
        })

    calendar_out.sort(key=lambda e: (e["date"], e["start_time"] or ""))

    return {
        "club": {
            "id": club.id,
            "name": club.name,
            "slug": club.slug,
            "blurb": club.blurb,
            "logo_url": club.logo_url,
            "website_url": club.website_url,
            "discord_url": club.discord_url,
            "opening_hours": club.opening_hours or [],
            "address": club.address,
            "latitude": club.latitude,
            "longitude": club.longitude,
        },
        "systems": systems_out,
        "calendar": {
            "month": month_start.strftime("%Y-%m"),
            "entries": calendar_out,
        },
    }


@app.get("/players")
def list_players(user: User = Depends(require_user), session: Session = Depends(get_session)):
    players = session.exec(
        scoped(Player, user.club_id).where(Player.active == True).order_by(Player.name)
    ).all()

    signup_rows = session.exec(
        select(Signup.player_id, Signup.system)
        .where(Signup.player_id.isnot(None))
        .where(Signup.club_id == user.club_id)
        .distinct()
    ).all()

    systems_by_player: dict[int, set] = {}
    for player_id, system in signup_rows:
        systems_by_player.setdefault(player_id, set()).add(system)

    result = []
    for player in players:
        player_dict = player.model_dump()
        player_dict["systems_played"] = sorted(systems_by_player.get(player.id, set()))
        result.append(player_dict)
    return result


def _parse_week(week: str) -> datetime:
    try:
        return datetime.strptime(week, "%d/%m/%Y")
    except ValueError:
        return datetime.min


@app.get("/players/{player_id}")
def get_player(player_id: int, user: User = Depends(require_user), session: Session = Depends(get_session)):
    player = session.get(Player, player_id)
    if player is None or player.club_id != user.club_id:
        raise HTTPException(status_code=404, detail="Player not found")

    club = session.get(Club, player.club_id)

    signups = fetch_player_signups(session, player_id, user.club_id)
    sign_counts = signup_counts_per_system(signups)
    fac_usage = faction_usage_per_system(signups)

    # A club can run more than one system's league now — build one section
    # per league-enabled system the player has actually played in, not just
    # the first one. Achievements are computed per system too (so "5+ games"
    # etc. mean 5+ games IN THAT league, not summed across leagues) and then
    # merged with the cross-system achievements (Diversifier/Veteran/etc,
    # which use signups across all systems and are identical every call) —
    # merge dedupes by name since compute_achievements() returns the same
    # cross-system entries on every iteration.
    league_rows = session.exec(
        select(ClubSystem, SystemConfig)
        .join(SystemConfig, SystemConfig.id == ClubSystem.system_id)
        .where(ClubSystem.club_id == user.club_id, ClubSystem.league_enabled == True)
        .order_by(SystemConfig.id)
    ).all()

    achievement_names: list[str] = []
    seen_achievements: set[str] = set()
    leagues_out: list[dict] = []
    # (frozenset{p1,p2}, date) -> LeagueResult, across every league system —
    # feeds the per-system "recent games" Win/Loss/Draw badge below. Used to
    # be built from only the single resolved league's results, so a second
    # league's recent games silently got no result badge; now covers all of
    # them.
    tow_lookup: dict[tuple, LeagueResult] = {}

    def _record_lookup(results: list[LeagueResult]) -> None:
        for r in results:
            if r.result_date and r.player_1_id and r.player_2_id:
                parsed_date = None
                for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
                    try:
                        parsed_date = datetime.strptime(r.result_date, fmt).date()
                        break
                    except ValueError:
                        continue
                if parsed_date:
                    tow_lookup[(frozenset({r.player_1_id, r.player_2_id}), parsed_date)] = r

    if not league_rows:
        record0 = compute_league_record(player_id, [])
        for a in compute_achievements(player_id, record0, [], fac_usage, [], None):
            if a not in seen_achievements:
                seen_achievements.add(a)
                achievement_names.append(a)
    else:
        for _cs, sys_config in league_rows:
            sid = sys_config.id
            rating_row = session.exec(
                scoped(LeagueRating, user.club_id)
                .where(LeagueRating.player_id == player_id)
                .where(LeagueRating.system_id == sid)
            ).first()
            results = fetch_player_results(session, player_id, user.club_id, sid)
            record = compute_league_record(player_id, results)
            elo_history = build_elo_history(player_id, results)
            first_winner = first_league_winner_id(session, user.club_id, sid)

            for a in compute_achievements(player_id, record, results, fac_usage, elo_history, first_winner):
                if a not in seen_achievements:
                    seen_achievements.add(a)
                    achievement_names.append(a)

            _record_lookup(results)

            if record["total_games"] == 0:
                continue

            rank = None
            if rating_row is not None and player.active:
                higher = session.exec(
                    select(LeagueRating)
                    .join(Player, Player.id == LeagueRating.player_id)
                    .where(LeagueRating.club_id == user.club_id)
                    .where(LeagueRating.system_id == sid)
                    .where(Player.active == True)
                    .where(LeagueRating.rating > rating_row.rating)
                ).all()
                rank = len(higher) + 1

            leagues_out.append({
                "system": sys_config.legacy_system_name,
                "system_name": sys_config.name,
                "rating": rating_row.rating if rating_row else None,
                "rank": rank,
                **record,
                "recent_results": results[:5],
                "elo_history": elo_history,
            })

    achievements_detailed = [
        {"name": a, "description": ACHIEVEMENT_DESCRIPTIONS.get(a, "")}
        for a in achievement_names
    ]

    # Discord identity
    discord_user = session.exec(
        scoped(User, user.club_id).where(User.player_id == player_id)
    ).first()
    discord_info = (
        {"discord_name": discord_user.discord_name, "avatar_url": discord_user.avatar_url}
        if discord_user else None
    )

    # Index this player's signups by system and by id for quick lookup
    signup_ids_by_system: dict[str, set[int]] = {}
    signup_by_id: dict[int, Signup] = {}
    for s in signups:
        if s.id is not None:
            signup_ids_by_system.setdefault(s.system, set()).add(s.id)
            signup_by_id[s.id] = s

    # Systems come from the active catalogue rather than a hardcoded list, so
    # a newly-added system appears here automatically. Ordered by id for a
    # stable display order; systems the player has no signups in fall through
    # the empty-set guard below just as before.
    active_systems = session.exec(
        select(SystemConfig)
        .where(SystemConfig.active == True)
        .order_by(SystemConfig.id)
    ).all()
    systems = [s.legacy_system_name for s in active_systems]
    # League-eligible systems come from the has_league capability, not a
    # hardcoded name check — a future league system needs no code change here.
    league_systems = {s.legacy_system_name for s in active_systems if s.has_league}
    recent_games_by_system: dict[str, list] = {}

    for system in systems:
        sys_signup_ids = signup_ids_by_system.get(system, set())
        if not sys_signup_ids:
            recent_games_by_system[system] = []
            continue

        pairings = session.exec(
            scoped(Pairing, user.club_id)
            .where(Pairing.system == system)
            .where(
                or_(
                    Pairing.a_signup_id.in_(sys_signup_ids),
                    Pairing.b_signup_id.in_(sys_signup_ids),
                )
            )
        ).all()

        if not pairings:
            recent_games_by_system[system] = []
            continue

        sorted_pairings = sorted(pairings, key=lambda p: _parse_week(p.week), reverse=True)[:5]

        # Fetch opponent signup rows not already cached
        needed = (
            {p.a_signup_id for p in sorted_pairings}
            | {p.b_signup_id for p in sorted_pairings if p.b_signup_id}
        )
        missing = needed - set(signup_by_id.keys())
        if missing:
            for s in session.exec(scoped(Signup, user.club_id).where(Signup.id.in_(missing))).all():
                signup_by_id[s.id] = s

        games = []
        for p in sorted_pairings:
            a_signup = signup_by_id.get(p.a_signup_id)
            b_signup = signup_by_id.get(p.b_signup_id) if p.b_signup_id else None
            is_bye = p.b_signup_id is None
            a_is_me = p.a_signup_id in sys_signup_ids

            if a_is_me:
                my_faction = p.a_faction or (a_signup.faction if a_signup else None)
                opp_signup = b_signup
                opp_faction = None if is_bye else (p.b_faction or (b_signup.faction if b_signup else None))
            else:
                my_faction = p.b_faction or (b_signup.faction if b_signup else None)
                opp_signup = a_signup
                opp_faction = p.a_faction or (a_signup.faction if a_signup else None)

            opp_name = None if is_bye else (opp_signup.player_name if opp_signup else None)
            opp_player_id = opp_signup.player_id if opp_signup else None

            result_str = None
            if system in league_systems and opp_player_id is not None:
                week_date = _parse_week(p.week)
                if week_date is not datetime.min:
                    lr = tow_lookup.get((frozenset({player_id, opp_player_id}), week_date.date()))
                    if lr:
                        is_p1 = lr.player_1_id == player_id
                        if lr.result == "Draw":
                            result_str = "Draw"
                        elif lr.result == "Player 1 Victory":
                            result_str = "Win" if is_p1 else "Loss"
                        elif lr.result == "Player 2 Victory":
                            result_str = "Win" if not is_p1 else "Loss"

            games.append({
                "week": p.week,
                "your_faction": my_faction,
                "opponent_name": opp_name,
                "opponent_faction": opp_faction,
                "result": result_str,
            })

        recent_games_by_system[system] = games

    return {
        "player": player,
        "club": {"name": club.name, "slug": club.slug} if club else None,
        "discord": discord_info,
        "titles": player_titles(player),
        "achievements": achievements_detailed,
        "signup_counts": sign_counts,
        "faction_usage": fac_usage,
        "recent_games_by_system": recent_games_by_system,
        # One entry per league-enabled system the player has actually played
        # in (empty list if none) — replaces the old single `league` object,
        # which only ever showed whichever league-enabled system happened to
        # come back first from the DB.
        "leagues": leagues_out,
    }


def _compute_league_rankings(session: Session, club_id: int, system_id: int, season_id: int) -> list[dict]:
    """Standings for one (club, system, season). season_id is required —
    LeagueRating rows from a closed season are never deleted when a new one
    starts (see league._recalculate_ratings), so without this filter a
    club's second season would silently blend both seasons' ratings into one
    table. Pass an archived season_id to get that season's frozen final
    standings (no new results can land there once a newer season exists)."""
    statement = (
        select(LeagueRating, Player)
        .join(Player, Player.id == LeagueRating.player_id)
        .where(Player.active == True)
        .where(LeagueRating.club_id == club_id)
        .where(LeagueRating.system_id == system_id)
        .where(LeagueRating.season_id == season_id)
        .order_by(LeagueRating.rating.desc())
    )
    rows = session.exec(statement).all()

    # This season's results only — record/most-played-faction below are
    # season stats, distinct from fetch_player_results' all-time career view
    # (used by achievements, which are deliberately not season-scoped).
    season_results = session.exec(
        scoped(LeagueResult, club_id)
        .where(LeagueResult.system_id == system_id)
        .where(LeagueResult.season_id == season_id)
    ).all()
    results_by_player: dict[int, list[LeagueResult]] = {}
    for r in season_results:
        for pid in (r.player_1_id, r.player_2_id):
            if pid is not None:
                results_by_player.setdefault(pid, []).append(r)

    # Build previous_rank using a 7-day rolling comparison.
    # For each player, find their earliest-processed result in the last 7 days
    # and use their rating_before from that row as the comparison point.
    cutoff = datetime.utcnow().date() - timedelta(days=7)
    earliest_recent: dict[int, LeagueResult] = {}
    for r in season_results:
        if not r.result_date:
            continue
        parsed = None
        for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
            try:
                parsed = datetime.strptime(r.result_date, fmt).date()
                break
            except ValueError:
                continue
        if parsed is None or parsed < cutoff:
            continue
        for pid in (r.player_1_id, r.player_2_id):
            if pid is None:
                continue
            if pid not in earliest_recent or r.id < earliest_recent[pid].id:
                earliest_recent[pid] = r

    def _comparison_rating(rating: LeagueRating, player: Player) -> float:
        r = earliest_recent.get(player.id)
        if r is None:
            return rating.rating
        before = r.player_1_rating_before if r.player_1_id == player.id else r.player_2_rating_before
        return before if before is not None else rating.rating

    sorted_by_prev = sorted(rows, key=lambda x: _comparison_rating(x[0], x[1]), reverse=True)
    previous_rank_map = {player.id: i for i, (_, player) in enumerate(sorted_by_prev, start=1)}

    rankings = []
    for rank, (rating, player) in enumerate(rows, start=1):
        results = results_by_player.get(player.id, [])
        record = compute_league_record(player.id, results)

        faction_counts: dict[str, int] = {}
        for r in results:
            f = r.player_1_faction if r.player_1_id == player.id else r.player_2_faction
            if f:
                faction_counts[f] = faction_counts.get(f, 0) + 1
        most_played = max(faction_counts, key=faction_counts.get) if faction_counts else None

        rankings.append({
            "rank": rank,
            "previous_rank": previous_rank_map[player.id],
            "player_id": player.id,
            "name": player.name,
            "default_faction": player.default_faction,
            "most_played_faction": most_played,
            "rating": rating.rating,
            **record,
        })

    return rankings


@app.get("/league/rankings")
def league_rankings(
    system: Optional[str] = None,
    season_id: Optional[int] = None,
    user: User = Depends(require_user),
    session: Session = Depends(get_session),
):
    """`system` selects which system's league (omit for the club's one
    league-enabled system, today's behaviour — see league._resolve_system_id).
    `season_id` selects which season's standings (omit for the current one);
    pass an archived season's id to view its frozen final standings."""
    system_id = _resolve_system_id(session, user.club_id, system)
    if system_id is None:
        return []
    resolved_season_id = season_id or _current_season_id(session, user.club_id, system_id)
    if resolved_season_id is None:
        return []
    return _compute_league_rankings(session, user.club_id, system_id, resolved_season_id)


@app.get("/signups/stats")
def signups_stats(system: str, week: str, user: User = Depends(require_user), session: Session = Depends(get_session)):
    rows = session.exec(
        scoped(Signup, user.club_id)
        .where(Signup.system == system)
        .where(Signup.week == week)
        .order_by(Signup.created_at.desc())
    ).all()

    latest_by_player: dict = {}
    for s in rows:
        key = s.player_id if s.player_id is not None else id(s)
        if key not in latest_by_player:
            latest_by_player[key] = s

    signups = list(latest_by_player.values())
    total = len(signups)
    newcomers = sum(1 for s in signups if (s.experience or "").lower().startswith("new"))
    veterans = sum(1 for s in signups if (s.experience or "").lower().startswith("vet"))

    return {
        "system": system,
        "week": week,
        "signed_up": total,
        "newcomers": newcomers,
        "veterans": veterans,
    }


def _public_vibe_display(a_vibe, b_vibe):
    av = (a_vibe or "").strip()
    bv = (b_vibe or "").strip()
    av_l = av.lower()
    bv_l = bv.lower()
    if av_l == "intro" or bv_l == "intro":
        return "Intro"
    if av_l == "either" and bv_l == "either":
        return "Either"
    if av_l == "either" and bv:
        return bv
    if bv_l == "either" and av:
        return av
    return av or bv or None


@app.get("/week-id")
def get_week_id(
    system: str,
    club: str | None = None,
    origin: str | None = Header(default=None),
    user: Optional[User] = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Optional-auth — the backend-authoritative target session date for a
    system, replacing the frontend's independent duplicate of this same date
    logic (weekIdForSystem() in +page.server.ts). A logged-in caller is
    always scoped to their own club (user.club_id); for anonymous requests,
    the `club` param (SvelteKit's SSR loader calls this way — no browser
    Origin exists server-to-server) takes precedence, then the request's
    Origin header (real browser calls) — same pattern as GET /pairings and
    GET /league/factions. See resolve_request_club_id."""
    try:
        club_id = resolve_request_club_id(session, user, club, origin)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    system_config = session.exec(
        select(SystemConfig).where(SystemConfig.legacy_system_name == system)
    ).first()
    if system_config is None:
        raise HTTPException(status_code=404, detail="Unknown system.")

    club_system = session.exec(
        select(ClubSystem).where(
            ClubSystem.club_id == club_id,
            ClubSystem.system_id == system_config.id,
        )
    ).first()
    if club_system is None:
        raise HTTPException(status_code=404, detail="This club does not run that system.")

    target = next_session_date(
        club_system.session_day, club_system.session_cadence,
        club_system.cadence_anchor, date.today(),
    )
    return {"week_id": target.strftime("%d/%m/%Y")}


@app.get("/pairings")
def get_pairings(
    system: str,
    week: str,
    club: str | None = None,
    origin: str | None = Header(default=None),
    user: Optional[User] = Depends(current_user),
    session: Session = Depends(get_session),
):
    """Optional-auth. A logged-in caller is always scoped to their own club
    (user.club_id) and the `club`/Origin are ignored — this closes the leak
    where a Yorkshire user browsing the bare/default hostname (which resolves
    to "manchester") was served Manchester's pairings. Genuinely anonymous
    requests resolve via an explicit `club` param first (SSR loaders), then
    the request's Origin header (real browser calls — subdomain-based
    resolution, no param needed); with neither, an anonymous request falls
    back to the fail-loud single-active-club stopgap. See
    resolve_request_club_id."""
    try:
        club_id = resolve_request_club_id(session, user, club, origin)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    gate = session.exec(
        scoped(PublishState, club_id).where(
            (PublishState.week == week) & (PublishState.system == system)
        )
    ).first()

    if not gate or not gate.published:
        return {"published": False, "system": system, "week": week, "matchups": []}

    prs = session.exec(
        scoped(Pairing, club_id)
        .where((Pairing.week == week) & (Pairing.system == system))
        .order_by(Pairing.id)
    ).all()

    signup_ids = {p.a_signup_id for p in prs} | {p.b_signup_id for p in prs if p.b_signup_id}
    signups_by_id: dict[int, Signup] = {}
    if signup_ids:
        rows = session.exec(scoped(Signup, club_id).where(Signup.id.in_(signup_ids))).all()
        signups_by_id = {s.id: s for s in rows}

    system_config = _get_system_config(session, system)

    matchups = []
    for p in prs:
        a = signups_by_id.get(p.a_signup_id)
        b = signups_by_id.get(p.b_signup_id) if p.b_signup_id else None

        game_type = _public_vibe_display(
            a.vibe if a else None,
            b.vibe if b else None,
        )

        if system_config is not None and not system_config.uses_points:
            points = None
        else:
            pts_vals = [v for v in (a.points if a else None, b.points if b else None) if isinstance(v, int)]
            points = str(min(pts_vals)) if pts_vals else None

        a_eta = a.eta if a else None
        b_eta = b.eta if b else None
        if a_eta and b_eta:
            eta = max(a_eta, b_eta)
        else:
            eta = a_eta or b_eta

        is_bye = b is None

        matchups.append({
            "id": p.id,
            "player_a_name": a.player_name if a else f"A#{p.a_signup_id}",
            "player_a_id": a.player_id if a else None,
            "player_a_faction": p.a_faction or (a.faction if a else None),
            "player_b_name": b.player_name if b else None,
            "player_b_id": b.player_id if b else None,
            "player_b_faction": p.b_faction or (b.faction if b else None) if b else None,
            "is_bye": is_bye,
            "game_type": game_type,
            "eta": eta,
            "points": points,
            "prearranged": p.prearranged,
        })

    return {
        "published": True,
        "system": system,
        "week": week,
        "matchups": matchups,
        "total_players": len({p.a_signup_id for p in prs} | {p.b_signup_id for p in prs if p.b_signup_id}),
        "total_matchups": len([m for m in matchups if not m["is_bye"]]),
        "byes": len([m for m in matchups if m["is_bye"]]),
    }
