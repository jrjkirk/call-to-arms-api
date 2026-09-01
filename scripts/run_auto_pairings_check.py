"""Auto-pairings check — runs both in-process (scheduler.py, every 5 minutes)
and on the auto-pairings-check GitHub Actions workflow.

For each club running each system (per club_systems) this does two independent
things, and the split is the whole point of the design:

1. **Pair.** Read the club's auto-pairings settings, decide whether pairings
   are due, and if so generate + publish them. Pure SQLAlchemy — no rendering,
   no matplotlib — so it runs perfectly well inside the 256 MB API container
   and therefore gets the in-process scheduler's reliable five-minute tick.

2. **Post the image.** Render this week's pairings and put them in Discord.
   That needs matplotlib, which the API image deliberately lacks, so wherever
   the renderer is unavailable this instead asks a GitHub runner to do it via
   workflow_dispatch — which queues immediately, unlike a `cron:` schedule.

Why they are separate
---------------------
They used to be one step behind a single due-check, which held the timely half
hostage to the heavyweight half's dependencies: the whole job had to live on
GitHub Actions because of matplotlib, and GitHub's "hourly" cron actually lands
about five times a day at unpredictable times. On 01/09/2026 The Old World at
EGNWGC was set to pair at 21:00; the day's last run arrived at 20:57 and
nothing came back before midnight, so a correctly-configured club silently lost
its week. Widening the fire window (week_logic.py) was not enough on its own —
five random samples a day against a weekday-equality gate is still close to no
chances.

Now the part that must be punctual is punctual, and the part that can wait a
few minutes is the only part that needs a runner.

One club/system failing does not stop the others.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlmodel import Session, select

from database import engine, posting_enabled, record_job_run, scoped, system_setting_slug as _slug, get_setting as _get_setting, upsert_setting as _upsert_setting
from github_dispatch import AUTO_PAIRINGS_WORKFLOW, dispatch_enabled, dispatch_workflow
from models import ClubSystem, Pairing, PublishState, Signup, SystemConfig
from pairings_engine import generate
from table_booking import maybe_send_table_booking
from week_logic import _is_auto_pairings_due, is_session_week, next_session_date

JOB_NAME = "auto_pairings_check"


def _load_image_poster():
    """Return post_pairings_image_for, or None where it cannot be imported.

    It pulls in matplotlib (via scripts.render_pairings_image), which the API
    container does not install — so this doubles as the test for "can this
    process render an image itself, or must it ask a runner?". Imported lazily
    for exactly that reason: at module scope it makes this whole file
    unimportable inside the API, which is what kept the job stranded on GitHub
    Actions in the first place.

    Qualified with the package name, not a bare sibling import: scripts/ is
    imported BOTH as scripts (where scripts/ lands on sys.path) and as a
    package (from scheduler.py, where it does not). Only the qualified form
    works in both, and PYTHONPATH=. — which CLAUDE.md requires and every
    workflow sets — is what makes it resolve for the script case.
    """
    try:
        from scripts.post_pairings_image import post_pairings_image_for

        return post_pairings_image_for
    except ImportError as exc:
        print(f"[auto-pairings] no local renderer ({exc}) — will ask a GitHub runner")
        return None


def _ensure_image_posted(
    db: Session, club_id: int, system: str, slug: str, target_week: str
) -> None:
    """Post this week's pairings image, or arrange for someone who can.

    Tracked by its own `posted_week` marker rather than piggy-backing on
    `last_week`: `last_week` means "pairings exist for this week" and is set
    the moment they are generated, so reusing it would dedup the Discord post
    away before it ever happened.
    """
    posted_key = f"auto_pairings_{slug}_posted_week"
    if _get_setting(db, club_id, posted_key) == target_week:
        return

    # Same switch the manual button obeys, so turning pairings posts off
    # silences the automation too rather than only the button. Checked before
    # the dispatch, so a club with posts switched off never queues a runner.
    if not posting_enabled(db, club_id, system, "pairings"):
        print(f"[{system} club={club_id}] pairings post switched off, not posting")
        return

    poster = _load_image_poster()
    if poster is None:
        requested_key = f"auto_pairings_{slug}_post_requested_week"
        if _get_setting(db, club_id, requested_key) == target_week:
            # A runner has already been asked for this week. Do not ask again
            # every five minutes — if that run failed to post, the workflow's
            # own cron sweeps Step 2 again on its next visit.
            return
        ok, reason = dispatch_workflow(AUTO_PAIRINGS_WORKFLOW)
        if ok:
            _upsert_setting(db, club_id, requested_key, target_week)
            db.commit()
            print(f"[{system} club={club_id}] asked a GitHub runner to post the {target_week} image")
        elif dispatch_enabled():
            # Configured but failing is worth saying out loud; not configured
            # at all is a deployment choice, and the cron backstop still works.
            print(f"[{system} club={club_id}] could not queue the image post — {reason}")
        return

    if poster(db, system, target_week, club_id=club_id):
        _upsert_setting(db, club_id, posted_key, target_week)
        db.commit()
        print(f"[{system} club={club_id}] posted the {target_week} pairings image")


def main() -> list[str]:
    now_uk = datetime.now(ZoneInfo("Europe/London"))
    print(f"Auto-pairings check — {now_uk.strftime('%Y-%m-%d %H:%M %Z')}")

    today = now_uk.date()
    errors: list[str] = []

    with Session(engine) as db:
        # Iterate the active catalogue directly rather than a hardcoded list,
        # so a newly-added system is picked up automatically. Ordered by id
        # for a stable, deterministic run order.
        system_configs = db.exec(
            select(SystemConfig)
            .where(SystemConfig.active == True)
            .order_by(SystemConfig.id)
        ).all()
        for system_config in system_configs:
            system = system_config.legacy_system_name
            try:
                slug = _slug(system)

                club_systems = db.exec(
                    select(ClubSystem)
                    .where(ClubSystem.system_id == system_config.id)
                    .where(ClubSystem.enabled == True)
                ).all()
                if not club_systems:
                    print(f"[{system}] SKIP — no club_systems rows for this system")
                    continue

                for club_system in club_systems:
                    club_id = club_system.club_id
                    try:
                        target_week_date = next_session_date(
                            club_system.session_day, club_system.session_cadence,
                            club_system.cadence_anchor, today,
                        )
                        target_week = target_week_date.strftime("%d/%m/%Y")

                        if not is_session_week(
                            club_system.session_cadence, club_system.cadence_anchor,
                            target_week_date, today,
                        ):
                            print(
                                f"[{system} club={club_id}] SKIP — not a session week "
                                f"(cadence={club_system.session_cadence})"
                            )
                            continue

                        settings = {
                            "enabled": (
                                _get_setting(db, club_id, f"auto_pairings_{slug}_enabled", "false") or "false"
                            ).lower() == "true",
                            "day": _get_setting(db, club_id, f"auto_pairings_{slug}_day", "Tuesday") or "Tuesday",
                            "time": _get_setting(db, club_id, f"auto_pairings_{slug}_time", "20:00") or "20:00",
                            "last_week": _get_setting(db, club_id, f"auto_pairings_{slug}_last_week", None),
                        }

                        # ---- Step 1: pair -----------------------------------
                        paired = settings["last_week"] == target_week

                        if paired:
                            pass                     # already paired this week
                        elif not _is_auto_pairings_due(settings, now_uk, target_week):
                            print(
                                f"[{system} club={club_id}] SKIP — not due "
                                f"(enabled={settings['enabled']}, day={settings['day']}, "
                                f"time={settings['time']}, last_week={settings['last_week']!r}, "
                                f"target={target_week})"
                            )
                            continue
                        else:
                            signups = db.exec(
                                scoped(Signup, club_id)
                                .where(Signup.system == system)
                                .where(Signup.week == target_week)
                            ).all()

                            if not signups:
                                # Deliberately does NOT record last_week. "Nobody
                                # has signed up yet" is not "nobody will sign up" —
                                # the first due tick can land days before the
                                # session (Tuesday's tick pairs Thursday's Age of
                                # Sigmar), and latching the dedup there silently
                                # burned the week with a green tick and no
                                # pairings: anyone signing up afterwards could
                                # never be paired. Re-checking costs one query per
                                # tick; the dedup is set only once pairings
                                # genuinely exist, below.
                                print(f"[{system} club={club_id}] SKIP — no signups yet for {target_week}; will re-check on the next tick")
                                continue

                            # Delete existing pending non-prearranged pairings before regenerating
                            old = db.exec(
                                scoped(Pairing, club_id)
                                .where(Pairing.system == system)
                                .where(Pairing.week == target_week)
                                .where(Pairing.status == "pending")
                                .where(Pairing.prearranged != True)
                            ).all()
                            for p in old:
                                db.delete(p)

                            generate(
                                db, target_week, system, allow_repeats_when_needed=True, persist=True,
                                club_id=club_id,
                            )

                            gate = db.exec(
                                scoped(PublishState, club_id)
                                .where(PublishState.system == system)
                                .where(PublishState.week == target_week)
                            ).first()
                            if gate is None:
                                gate = PublishState(
                                    system=system,
                                    week=target_week,
                                    published=True,
                                    club_id=club_id,
                                )
                            else:
                                gate.published = True
                            db.add(gate)

                            _upsert_setting(db, club_id, f"auto_pairings_{slug}_last_week", target_week)
                            db.commit()
                            paired = True
                            print(f"[{system} club={club_id}] paired + published {target_week}")

                            maybe_send_table_booking(db, club_id, system, target_week)

                            # Same as the manual publish in admin.py: the pairings
                            # are what tell the venue how many tables tonight needs,
                            # so lay the floor out now. Most clubs never publish by
                            # hand, so without this the automation path would be the
                            # one that leaves the diary stale.
                            try:
                                from venue_seating import lay_out_on_publish

                                lay_out_on_publish(db, club_id, system, target_week)
                            except Exception as exc:
                                print(f"[{system} club={club_id}] seating skipped — {exc}")

                        # ---- Step 2: post the image -------------------------
                        # Runs whether or not this tick did the pairing, so a run
                        # arriving after the pairing already happened still
                        # finishes the job. That is what makes the GitHub cron a
                        # backstop rather than a single point of failure.
                        if paired:
                            _ensure_image_posted(db, club_id, system, slug, target_week)

                    except Exception as exc:
                        import traceback
                        print(f"[{system} club={club_id}] ERROR — {exc}")
                        traceback.print_exc()
                        errors.append(f"{system} club={club_id}: {exc}")

            except Exception as exc:
                import traceback
                print(f"[{system}] ERROR — {exc}")
                traceback.print_exc()
                errors.append(f"{system}: {exc}")

        record_job_run(
            db, JOB_NAME,
            status="error" if errors else "ok",
            detail="; ".join(errors[:5]) + (f" (+{len(errors) - 5} more)" if len(errors) > 5 else "") if errors else None,
        )
        db.commit()

    return errors


if __name__ == "__main__":
    # Exit non-zero if any club/system errored. Every exception above is caught
    # per club so one bad club cannot silence the others, but the run then
    # returned normally and the workflow went green whatever happened — a
    # broken pairings webhook looked exactly like a quiet night, and the only
    # trace was a scheduled_job_runs row nobody reads until a club complains.
    # This is what makes the Actions tab (and its failure email) honest.
    #
    # It lives in the __main__ guard, not in main(), on purpose: main() is also
    # an importable entry point (scheduler.py calls the sibling run_*_check
    # main()s directly), and raising SystemExit from there would sail straight
    # through the scheduler's `except Exception` and kill its tick loop.
    _errors = main()
    if _errors:
        print(f"{JOB_NAME}: {len(_errors)} club/system failure(s)")
        raise SystemExit(1)
