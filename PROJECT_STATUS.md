## Working process — read this first, every session

Joel's established workflow for this project. Any Claude session working on
this repo (Cowork or otherwise) should follow this without being re-asked:

1. **Cowork does research, decisions, and planning — not direct code edits
   to the working repos.** Read code, find mismatches between plans/docs
   and actual code, surface decisions that are genuinely Joel's to make
   (schema design, scope calls, behavior-preserving vs. behavior-changing
   tradeoffs), and keep `PROJECT_STATUS.md` current.
2. **Actual code changes go through a handoff file, written for Claude
   Code, not implemented directly in Cowork.** Each handoff is a single
   self-contained markdown file covering: background/decisions already
   made (so Claude Code doesn't re-derive or contradict them), the exact
   task, what's explicitly out of scope, how to verify the change, and an
   instruction to report back what was done and how it was verified.
3. **Every time a handoff file is ready, say explicitly and step-by-step:**
   - which file it is (exact filename)
   - which repo/directory it goes in (this repo, `call-to-arms-api`, vs.
     the separate frontend repo, `call-to-arms-web` — they're different
     Claude Code sessions)
   - that it must be pasted as literal message text into Claude Code, NOT
     dragged in as a file attachment (attaching it silently fails — Claude
     Code sees a file reference with no instruction to act on)
   - to open the file, select all, copy, and paste the text directly into
     the Claude Code chat box
4. **When Claude Code reports back**, fold the results into this file
   (`PROJECT_STATUS.md`) and flag anything it deviated on or couldn't
   verify — don't just take "done" at face value, check what it says it
   verified and how.
5. Large or ambiguous tasks (e.g. "consolidate these three scripts") get
   scoped/confirmed with Joel in Cowork *before* a handoff is written, not
   left for Claude Code to improvise scope on a live user-facing app.
6. **Live debugging via relayed terminal commands has a two-strikes limit.**
   If a diagnostic thread hits a second dead end without a clear answer —
   especially anything in a repo Cowork doesn't have direct access to
   (e.g. `call-to-arms-web`, never uploaded here), or that needs searching
   across many files at once — stop proposing one-off commands through
   Joel and write a handoff for Claude Code instead.
7. **Production safety is sacrosanct: the live app must never break.**
   `git commit`/`git push` only touch GitHub and are safe anytime. `fly
   deploy` pushes to production and must NEVER run until production's
   Supabase schema has been migrated to match the deployed code — see
   "Next up" below for exactly where this stands right now. Commits only
   happen when Joel explicitly asks for them.

_Last updated: 2026-07-15 (Phase 2 kickoff)_

---

## Incident: GitHub Actions broke against production after Phase 1 merged to `main` (2026-07-14, resolved)

**Symptom:** admin "post to Discord" button reported success but nothing
posted. Real cause surfaced via a GitHub Actions traceback Joel pasted:
`psycopg2.errors.UndefinedColumn: column pairings.club_id does not
exist`, from `post_pairings_image.py`.

**Cause:** `git push`/`git commit` were treated as always-safe for this
repo (only `fly deploy` touches production — the standing rule). That
was wrong. Several GitHub Actions workflows (`call-to-arms.yml`,
`hh-call-to-arms.yml`, `kt-call-to-arms.yml`, `auto-pairings-check.yml`,
`post-pairings-image.yml`, `post_league_rankings_image.yml`) check out
`main` and run scripts directly against **production's** `DATABASE_URL`,
independent of `fly deploy`. When Phase 1's `club_id` fields landed on
`models.py` via commits `cb80378`/`82d700d`/`d8f0ae2`, every one of those
scripts started throwing `UndefinedColumn` against production, since
production's schema didn't have the column yet. The live FastAPI app
itself (still on pre-Phase-1 release `v74`, no `fly deploy` since Phase
0) was never affected — confirmed throughout via `fly logs` and direct
endpoint checks. Full blast radius (found by reproducing the failing
query shapes read-only, since `gh` auth wasn't available to pull Actions
history directly): `post_pairings_image.py`, `run_auto_pairings_check.py`
(all its `scoped()` reads), `post_league_rankings_image.py` (via
`LeagueRating`) — i.e. every model Phase 1 touched, not just `Pairing`.

**Fix (2026-07-14, via Claude Code handoff, run directly against
production, staging-safe):** `seed_clubs.py` run against production
(Manchester's real production `id` = `1`, `club_systems` verified against
live scheduling). `club_id` added as **nullable** to all 9 tables +
`club_settings` created, then backfilled. Real production row counts
(first real-volume proof this backfill pattern has had — staging never
exceeded a few rows per table):

| table | rows | backfilled | NULLs left |
|---|---|---|---|
| pairing_blocks | 0 | 0 | 0 |
| players | 100 | 100 | 0 |
| users | 52 | 52 | 0 |
| admin_roles | 7 | 7 | 0 |
| publish_state | 47 | 47 | 0 |
| signups | 520 | 520 | 0 |
| pairings | 245 | 245 | 0 |
| league_results | 72 | 72 | 0 |
| league_ratings | 31 | 31 | 0 |

Spot-checked full rows on the high-traffic tables — no other column
touched. Verified clean: all 9 columns still nullable (no NOT NULL run),
`fly releases` still shows `v74` (no deploy), live app hit before/after
with real concurrent user traffic in `fly logs` throughout, completely
unaffected. Re-ran the exact failing query shapes read-only post-fix —
all succeed now.

**Confirmed resolved, 2026-07-14:** Joel clicked "post to Discord" for a
real pairing set after the fix — it posted successfully. Incident fully
closed.

**Guardrail added:** `CLAUDE.md` now has a "GitHub Actions run against
production directly from `main`" section — any future schema/model
change must land the production-side column addition (or equivalent)
before or alongside the `main` push if any GitHub Actions workflow reads
that model against production, not deferred to "whenever `fly deploy`
happens."

**Side effect — production migration is now partially done.** Steps 1-3
of `HANDOFF_production_migration.md` (seed clubs, add nullable columns,
backfill) are complete in production as of this incident fix, with real
production volumes now proven clean. Steps 4-5 finished the same night —
see below.

## Production migration — COMPLETE (2026-07-14)

Joel decided not to wait further once the incident above was already
resolved and steps 1-3 proven clean — "may as well be now." Steps 4-5 ran
the same night, via Claude Code handoff:

- **Deployed:** `main` → release `v75`. Clean startup, no tracebacks. One
  transient "not listening" warning during rollout was a false-positive
  readiness-probe race (no HTTP health check configured) — confirmed
  harmless via logs and repeated endpoint checks (`/systems`, `/auth/me`,
  `/pairings`, `/league/factions` → 200; `/players`, `/league/rankings` →
  401 as expected, no session). No organic write traffic in the
  monitoring window (late evening) — verified via read-only checks
  instead of waiting for one.
- **NOT NULL contract:** ran on all 10 tables. One housekeeping gap
  found: `add_club_id_to_pairing_blocks.py` (the first script written,
  before the later scripts standardized a `--contract` flag) never got
  one — its `ALTER TABLE ... SET NOT NULL` was run by hand instead (0
  rows, trivial, no risk). Verified via `information_schema`/`pg_constraint`:
  all 10 tables NOT NULL, FKs to `clubs` intact, zero NULLs, row counts
  unchanged from the backfill (100 players, 52 users, 7 admin_roles, 47
  publish_state, 520 signups, 245 pairings, 72 league_results, 31
  league_ratings, 0 pairing_blocks, 0 club_settings).
- Post-contract health check clean: `fly status` healthy on `v75`, all
  endpoints still correct, no errors in logs.

**Phase 1's production migration is now fully done — schema, deploy, and
contract all complete and verified.** Two items remain, both small and
non-blocking:
1. Commit the `_recalculate_ratings()` fix (still staging-only,
   low-risk, no schema dependency on anything above).
2. Make `run_auto_pairings_check.py` genuinely club-aware (real per-club
   iteration via `club_systems`) — deferred, becomes relevant once a real
   second club exists (Phase 4).

---

## Phase 2 — admin hierarchy (IN PROGRESS, kickoff slice done 2026-07-15)

Scoped/confirmed in Cowork first, per house process — full plan is
`multitenancy-plan-v2.md`'s Phase 2 section. Key finding from that scoping
pass: the doc frames making `admin_scopes()`/`require_scope()`/
`require_super_admin()` "gain a club_id dimension" as core Phase 2 work —
but that already happened as a side effect of Phase 1's `scoped()` rollout
(every admin query already filters by `user.club_id`, `AdminRole` already
carries `club_id`). The real gap was one tier up: nothing could act
*across* clubs at all. Kickoff slice (via Claude Code handoff,
`HANDOFF_phase2_platform_admin_slice.md`) closes the smallest safe piece of
that gap only:

**Added (staging only, not committed):**
- `User.is_platform_admin: bool = Field(default=False)` (`models.py`) —
  single-step migration, no expand/backfill/contract needed (unlike every
  `club_id` column) since no existing user is ambiguously a platform admin.
- `add_is_platform_admin_to_users.py` — one-off migration script
  (`--add-column` / `--verify`).
- `require_platform_admin` dependency in `auth.py`, same shape as
  `require_super_admin`.
- `POST /admin/platform/clubs` in `admin.py`, gated on
  `require_platform_admin`. Body: `name`, `slug`, `timezone` (default
  `Europe/London`), `contact_email` (optional), `leagues_enabled` (default
  `true`). 409 on duplicate slug. Deliberately does not seed `club_systems`
  for the new club — that's a separate future step.

**Verified on staging (`jxayumjjhgedbyrrcazq`):** migration confirmed
(column added, both existing users `false`, row count unchanged at 2);
`is_platform_admin=true` flipped on Kirkboi's real row (id=1) via direct
SQL, same "by SQL only" mechanism as `is_super_admin`; throwaway
`TestClient` script (deleted after use) confirmed normal user → 403,
real platform-admin → 200 with correct `Club` rows created (default and
non-default field sets both checked), duplicate slug → 409; spot-checked
`GET /admin/roles`, `GET /admin/blocks`, `GET /admin/league/results`,
`GET /admin/me` all unaffected; test clubs cleaned up, staging back to
exactly 1 real club (Manchester).

**Deviation found:** the handoff assumed a real staging user already had
`is_super_admin=True` to test against. Both real staging users currently
have it `False` — status doc was stale on this. Worked around with an
in-memory-only fake super-admin for that one 403 check (no real row
mutated), consistent with this repo's existing test conventions.

**Not done, explicitly out of scope for this slice:** no commit, no push,
no `fly deploy`, no production changes; no appointment endpoint/UI for
`is_platform_admin`/`is_super_admin`/`AdminRole` (all three still "by SQL
only"); no `club_systems` seeding for new clubs; `is_super_admin` /
`require_scope` / `admin_scopes` / existing `admin.py` endpoints
untouched. See "Next up" below for what's still open in Phase 2.

**Housekeeping note:** the working tree also has pre-existing uncommitted
changes from before this slice (deleted `verify_*.py` scripts, a modified
`PROJECT_STATUS.md`) — left untouched, not this slice's to resolve.

**Committed and pushed, 2026-07-15:** `5f9450d` on `main`
(`de93dd2..5f9450d`), scoped to exactly `models.py`, `auth.py`, `admin.py`,
`add_is_platform_admin_to_users.py` — the pre-existing unrelated
uncommitted changes (deleted `verify_*.py`, modified `PROJECT_STATUS.md`)
were deliberately left out of this commit.

**Production migration + deploy — COMPLETE, 2026-07-15 (via Claude Code
handoff, `HANDOFF_phase2_production_migration_and_deploy.md`):**

- `add_is_platform_admin_to_users.py` run against production first (per
  the guardrail below). `users` row count unchanged (52 → 52); column
  confirmed via `information_schema` as `boolean NOT NULL DEFAULT false`,
  all 52 rows `false`.
- `fly deploy` → release **`v76`**, clean build/rollout.
- Post-deploy health checks all matched expectations: `/systems`,
  `/auth/me`, `/pairings` (with `system`/`week` params), `/league/factions`
  → 200; `/players`, `/league/rankings` with no session → 401.
  (`/pairings` with no query params correctly 422s — normal FastAPI
  validation, not a bug — retested with params, got 200.) `fly logs
  --no-tail` clean startup at 23:38:33, zero tracebacks in the buffer.
- **Deviation:** `fly ssh console`'s WireGuard tunnel timed out from the
  Claude Code environment (unlike the 2026-07-14 incident recovery, which
  apparently could reach it). Worked around by Joel pasting the production
  `DATABASE_URL` directly, used inline as an env var for the two migration
  commands only — never written to a file or echoed back.
- `is_platform_admin` remains `false` for everyone in production — flipping
  it for Joel's real account is a deliberate separate step, not done here
  (same "by SQL only" pattern as `is_super_admin`).

Sequencing rationale (checked before any of the above ran): the
GitHub-Actions-off-`main` guardrail from the 2026-07-14 incident was
re-checked first — none of the six production-run scripts
(`run_auto_pairings_check.py`, `run_call_to_arms.py`,
`run_hh_call_to_arms.py`, `run_kt_call_to_arms.py`,
`post_pairings_image.py`, `post_league_rankings_image.py`, or anything
they transitively import) query the `users` table, so `5f9450d` landing
on `main` was already safe for scheduled Actions runs on its own. The real
risk was `fly deploy`: `current_user()` (`auth.py`) runs
`db.get(User, user_id)` on every authenticated request, and would have
broken authentication for the entire live app if deployed before
production's `users` table had the `is_platform_admin` column — hence
migration-then-deploy, in that order, both now done and verified.

---

## Phase 2 — club_systems endpoint (SHIPPED, production `v77`, 2026-07-15)

Continuation of Phase 2 after the platform-admin kickoff slice
(`5f9450d`, production `v76`). Closes the gap that slice deliberately
left open: a club created via `POST /admin/platform/clubs` had zero
enabled systems and couldn't do anything.

**Added:** `POST /admin/platform/clubs/{club_id}/systems` in `admin.py`,
gated on `require_platform_admin`. Upserts a `ClubSystem` row keyed on
`(club_id, system_id)` — the real, repeatable version of what
`seed_clubs.py` did by hand for Manchester. Validates `club_id` (404),
`system_id` against a real `SystemConfig` row (404), `session_day`
against `week_logic._DAY_NAME_TO_INT`'s 7 canonical names (422, reused
not redefined), `session_cadence` ∈ `{weekly, fortnightly}` (422).
Weekly + `cadence_anchor`-provided is a **422 reject** (not silent
nulling) — matches this codebase's existing fail-loud validation style
(e.g. duplicate-slug 409s rather than silent overwrites). Docstring notes
`session_day`/`session_cadence`/`cadence_anchor` are stored but not yet
read by `week_logic.week_id_for_system()` — same informational-only
status as `icon_folder` post-Phase-0.

**Verified on staging** (throwaway `TestClient` script, deleted after
use): create test club → enable weekly system → 200 correct body; same
`(club_id, system_id)` upsert with different values → 200, same row id,
count stayed at 1 (no duplicate); enable fortnightly with anchor → 200,
anchor stored; unknown `system_id`/`club_id` → 404; bad `session_day` /
`session_cadence` → 422; fortnightly missing anchor → 422; weekly with
anchor provided → 422; normal user → 403; super-admin-only (not
platform-admin) → 403; Manchester's 3 real `ClubSystem` rows
byte-identical before/after. Test club and rows cleaned up afterward, back
to exactly 1 real club. No deviations, nothing left unverified.

**Committed, pushed, deployed:** `15996ba` — "Phase 2: add
`POST /admin/platform/clubs/{club_id}/systems`" — pushed to `main`. Diff
scoped to `admin.py` only (new endpoint + 3 import lines); confirmed
`_collect_signups_for_rows`/`_pairing_rows_to_display` (what
`post_pairings_image.py`'s GitHub Actions script depends on) were
untouched. `PROJECT_STATUS.md` and the `verify_*.py` deletions left
uncommitted, same exclusion pattern as `5f9450d`. Deployed as Fly release
**`v77`**, clean rollout. Post-deploy health check clean: `fly logs
--no-tail` zero tracebacks; `/systems`, `/auth/me`, `/pairings` (with
params), `/league/factions` → 200; `/players`, `/league/rankings` (no
session) → 401.

**Real end-to-end platform-admin smoke test in production: not done yet,
needs Joel.** Two blockers: production's `is_platform_admin` is still
`false` for all 52 users (only ever flipped on staging), and Claude Code's
environment has no way to hold a real production session cookie regardless.
**To smoke-test for real:** flip `is_platform_admin=true` on your own
production user row via direct SQL (same "by SQL only" pattern as
`is_super_admin`), then hit the endpoints yourself while logged in
normally.

---

## Phase 2 — delegate appointment endpoints (staging only, 2026-07-15, not committed)

Closes the last gap in the create-club chain: `is_super_admin` was "set by
SQL, never via this API" (per `admin.py`'s own docstring) — there was a
full scope-admin grant/revoke API already, but nothing could bootstrap a
brand-new club's *first* super-admin without raw SQL.

**Added (`admin.py`):**
- `POST /admin/platform/clubs/{club_id}/super-admins` — body
  `{"user_id": int}`, sets `is_super_admin=True`, idempotent, returns
  `{id, discord_name, club_id, is_super_admin}`.
- `DELETE /admin/platform/clubs/{club_id}/super-admins/{user_id}` — sets
  `False`, idempotent, `{"ok": True, "removed": bool}` (matches the
  existing `DELETE /admin/roles` shape).
- Both gated on `require_platform_admin`, both use this file's existing
  cross-club 404 convention (never distinguishes "doesn't exist" from
  "exists in a different club").

**Verified on staging** (throwaway `TestClient` script against the real
staging DB, deleted after use): the full chain end-to-end — platform admin
creates a club → enables a system on it → appoints its first super-admin
→ that super-admin, with zero extra setup, correctly used the *existing*
`GET /admin/roles`, `GET /admin/grantable-users`, and `POST /admin/roles`
scoped to their own new club, never touching Manchester's data. Confirms
Phase 1's `scoped()` rollout genuinely holds for a brand-new club, not
just Manchester. Also verified: idempotent appoint/revoke; all 404 cases
(nonexistent club_id, nonexistent user_id, cross-club mismatch both
directions); 403 for a normal user and separately for an existing
club-super-admin who isn't a platform admin; revoke immediately 403s the
revoked user on `GET /admin/roles` (live check, not cached); Manchester's
real super-admin set (currently empty on staging) unchanged throughout.
All test rows cleaned up afterward. No deviations, nothing left
unverified.

**Not committed, not pushed, not deployed** — staging only, awaiting
go-ahead.

---

## Current state (read this, then skip to "Next up" unless you need history)

- **Phase -1** (subdomain + auth prototype), **Phase 0** (systems-as-data
  refactor), and **Phase 1** (introduce clubs, club_id scoping) —
  **all complete, deployed to production (`v75`), verified.** Detail in
  the "Completed phases" section and the incident/migration writeups
  above.
  - All 10 club-owned tables have `club_id`, NOT NULL, FKs intact, in
    both staging and production.
  - Every query call site in the codebase (~88 sites, 8 files) is scoped
    via `scoped(model, club_id)` / `user.club_id` — 11 chunks, each
    proven cross-club. Found and fixed 9 missing-ownership-check bugs and
    2 destructive cross-club-delete bugs along the way.
  - Live app is running the Phase 1 code for real (`fly deploy`'d,
    release `v75`).
  - **Correction (found 2026-07-15, doc was stale):** `run_auto_pairings_check.py`
    already does real per-club iteration via `club_systems` — it was NOT
    still on the `_default_club_id()` placeholder as previously stated
    here. `_default_club_id()` is now only used in one place:
    `discord_callback`'s new-user branch (`auth.py`), for the
    club-at-signup placeholder (still open, see "Next up").
    `list_factions()`/`get_pairings()` are unscoped public pages, by
    design deferred to Phase 3 (subdomain-based club resolution).
  - `_recalculate_ratings()`'s cross-club isolation fix is committed
    (`d8f0ae2`, one of the three commits that triggered the GitHub
    Actions incident above — already resolved).
- **Phase 2** (admin hierarchy) — **kickoff slice complete, committed
  (`5f9450d`), migrated + deployed to production (`v76`), verified,
  2026-07-15.** See "Phase 2 — admin hierarchy (in progress)" section
  below for what this slice covered and what's still open in the phase.
  **Phase 3** (per-club Discord + public page scoping), **Phase 4**
  (second club onboarding) — not started.

## Next up

Phase 2 (admin hierarchy) is well underway. Shipped to production so far:
platform-admin kickoff (`5f9450d`, `v76`) and the `club_systems` endpoint
(`15996ba`, `v77`). Staging-only, awaiting go-ahead: the delegate
appointment endpoints (see section above) — verified end-to-end, just
needs commit/push/deploy whenever you're ready.

Remaining Phase 2 work, in no particular order, none scoped/confirmed yet:

- Club-at-signup UX: replace the `_default_club_id(db)` placeholder in
  `discord_callback`'s new-user branch (`auth.py`) with a real club-picker.
  No frontend club-selection UI exists at all yet (`claim` page has none).
- Decide whether `VALID_SCOPES` (currently a hardcoded 4-item list,
  duplicated in `auth.py` and the frontend's `admin/+page.svelte`) should
  become per-club (derived from that club's `club_systems`) before a real
  second club with a different system mix exists.
- The `scoped()` "act as club X" override for platform-admin support access
  — not built yet, no platform-admin UI depends on it yet either.

**Correction, 2026-07-15:** `run_auto_pairings_check.py` is already
genuinely club-aware (confirmed by reading the actual code, not prior
status text) — it iterates `club_systems` per system and generates/
publishes per club_id already. Nothing outstanding here. What IS still a
real, documented gap in the same area: `post_pairings_image.py`'s
`WEBHOOK_MAP` is keyed only by system name, not by club — a second club
sharing a system would have its pairings posted to Manchester's webhook.
This is the per-club Discord webhook routing item already flagged as
must-decide-before-Phase-4 (Phase 3 territory), not a Phase 2 blocker.

Also found while scoping the next Phase 2 slice: `week_logic.py`'s
`week_id_for_system()` — which computes which date a system's next
session falls on — is fully hardcoded by system name (TOW=Wed, KT=Fri,
HH=fortnightly-Friday) and does **not** read `ClubSystem.session_day` /
`session_cadence` / `cadence_anchor` at all, despite those columns
existing and being seeded. A club_systems row's schedule fields are
currently stored but not yet load-bearing — same status as `icon_folder`
was in Phase 0 (informational only until something reads it). Relevant
to any future club_systems-writing endpoint: it can safely accept/store
these fields, but must not imply they control real scheduling yet.

## Housekeeping

- **Any `verify_*.py` script is disposable.** This includes the 12
  `verify_scoped_helper_chunk*.py` files, the per-table proofs
  (`verify_club_settings.py`, `verify_league_ratings_club_id.py`,
  `verify_league_results_club_id.py`, `verify_pairings_club_id.py`,
  `verify_signups_club_id.py`, and others of the same shape for the
  other tables), `verify_pairings_dual_run.py` (Phase 0), and
  `verify_recalculate_ratings_scoping.py`. All are one-off proof scripts
  whose results are already folded into this file and already committed
  to git history — safe to delete from the working tree, nothing depends
  on them going forward.
- The `add_club_id_to_*.py` scripts, `create_club_settings_table.py`, and
  `seed_clubs.py` must be **kept** — they're the actual migration scripts
  the production migration (above) will run.

---

## Completed phases (condensed — full blow-by-blow is in git commit
history on `main`, this is just enough to orient a fresh session)

**Phase -1 — subdomain + auth prototype.** Wildcard `*.calltoarms.app`
domain, nameserver migration, Discord login + cookie sharing across
subdomains all verified end-to-end. Two bugs found/fixed (CORS regex,
post-login redirect). Deployed and live.

**Phase 0 — systems-as-data refactor.** Replaced hardcoded per-system
(TOW/HH/KT) branches across `signups.py`, `pairings_engine.py`, and the
frontend with a `SystemConfig` catalogue table, behind a
`systems_from_catalogue` flag for dual-run safety. Verified via synthetic
dual-run testing, then real end-to-end signup submission on staging.
Deployed to production 2026-07-13, flag flipped on, no issues. New public
`GET /systems` endpoint added for the frontend. `run_*_call_to_arms.py`
scripts deliberately left hardcoded (structurally different, not worth the
risk to consolidate). Along the way, caught and fixed a real incident: the
local frontend `.env` was pointed at production, so an early "local" test
signup briefly landed in production (confirmed cleaned up, no lasting
effect).

**Incident, 2026-07-13 (resolved): Discord OAuth broke in production.**
Local/prod share one Discord OAuth app; resetting the client secret for
local testing invalidated it in production too, and `fly secrets set` was
never run to match. Fixed by resetting again and running `fly secrets set
DISCORD_CLIENT_SECRET=<new> -a call-to-arms-api`. Guardrail added to
`CLAUDE.md`: any future secret rotation must update local `.env` and `fly
secrets set` together, same sitting.

**Phase 1 — introduce clubs, club_id scoping.** `Club`/`ClubSystem` models
seeded on staging (Manchester + its 3 system schedules). Decided (with
Joel): split `app_settings` into a global-only table plus a new
`club_settings` table with composite `(club_id, key)` PK, rather than
force a nullable `club_id` onto the existing key-only-PK table (Postgres
treats every NULL as distinct, so that wouldn't have enforced uniqueness
correctly). Added `club_id` to all 10 club-owned tables one at a time
(`pairing_blocks`, `players`, `users`, `admin_roles`, `publish_state`,
`signups`, `pairings`, `league_results`, `league_ratings`, plus the
`app_settings`/`club_settings` split), each via expand → backfill →
dual-run write → contract, each verified with real row counts and
spot-checks, not just "0 nulls." Committed and pushed.

Then converted every query call site in the codebase to use a new
`scoped(model, club_id)` helper (or a manual `.where(club_id == ...)` for
joins/column-tuple selects `scoped()` doesn't cover), in 11 chunks —
decided with Joel to skip a feature flag for this phase (with one club,
filtered/unfiltered queries are behaviorally identical, so a flag can't
catch anything a flag would normally catch; incremental chunked rollout +
git revert was the actual safety net). Found and fixed along the way:

- **9 missing-ownership-check bugs** — an endpoint doing an unchecked
  fetch-by-id with no verification the row belongs to the caller's club:
  `claim_player`, `submit_result`, admin league-result patch/delete,
  `submit_prearranged`, `pairings_save`/`pairings_delete`,
  `admin_signup_patch`/`create`/`delete`, `patch_player`, `get_player`.
- **2 destructive cross-club-data bugs** — an unscoped delete that would
  silently destroy another club's data: `admin.py::pairings_generate`'s
  and `run_auto_pairings_check.py`'s independent copies of "delete
  existing pending pairings before regenerating."

Every fix proven with a genuine second temporary club, not single-club
sanity checks. Committed and pushed.

Finally, fixed `_recalculate_ratings()` — the one deliberately-deferred
correctness gap flagged throughout the table-by-table work: it replayed
*all* `LeagueResult` rows and deleted *all* `LeagueRating` rows with no
club filter, which would have silently blended/wiped ELO across clubs.
Fixed, proven both directions (isolation) and checked against an
independent from-scratch computation (math unchanged). Staging only, not
yet committed.

**Everything above is staging-only for anything not explicitly marked
committed. Production's Supabase schema now has all 10 tables' `club_id`
columns too (nullable, backfilled — added during the 2026-07-14 GitHub
Actions incident fix above), but the live app is still running
pre-Phase-1 code and the columns aren't NOT NULL yet — see "Next up" for
what's left.**