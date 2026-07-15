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

_Last updated: 2026-07-16 (club-at-signup shipped, `v78`)_

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

**Committed, pushed, deployed:** part of the `v78` batch below
(commit `be14840`).

---

## Phase 2 — club-at-signup, backend half (SHIPPED, part of `v78`, 2026-07-16)

Companion frontend slice below. **Design decision, confirmed with Joel in
Cowork beforehand:** deferred user creation, not a nullable `club_id`
window — `users.club_id` stays NOT NULL throughout, never reopened.

**Added:**
- `GET /clubs` (`main.py`) — public, mirrors `GET /systems` exactly,
  active clubs only, `{id, name, slug}`.
- `_make_pending_signup_cookie()` / `_verify_pending_signup_cookie()`
  (`auth.py`) — reuse `_sign()`, base64-JSON body + signature, same shape
  as the session cookie's signing.
- `discord_callback`'s new-user branch now defers `User` creation: sets
  `cta_pending_signup` (`max_age=600`, `httponly=True`, `samesite="lax"`,
  `secure=True`) and redirects to `{return_to}/join` instead of creating
  the row inline. Existing-user branch unchanged.
- `POST /auth/complete-signup` — verifies the pending cookie, 404s on
  missing/inactive `club_id`, race-safely re-checks for an existing
  `User` row before creating (double-submit safe), sets the real session
  cookie, clears the pending one, returns `GET /auth/me`'s shape.
- `_default_club_id()` and its now-unused `Club` import removed from
  `database.py` (zero remaining callers, confirmed via grep before
  deleting).

**Docstring vs. code call:** the module docstring claims `cta_session` is
`SameSite=None`; the actual code (and both existing OAuth-transient
cookies) use `samesite="lax"`. Followed the code, matched it exactly for
the new cookie. **The docstring itself is stale and still uncorrected**
— a real but harmless drift, worth a cleanup pass sometime.

**Flagged as a conscious call, not an oversight:** the pending-signup
cookie has no expiry embedded in its signed payload — relies solely on
the browser-enforced 10-minute `max_age`, same as `cta_session` relies
solely on its 30-day `max_age`. A captured cookie value replayed via curl
(not a browser) past 10 minutes would still verify. Low severity (worst
case: an account for that Discord identity gets created later than
intended, no cross-account leak) — matches existing convention rather
than a one-off fix. Joel accepted this as-is.

**Verified:** everything in the handoff's list, plus real
`discord_callback` new/existing-user branches exercised end-to-end via a
mocked Discord API (not just a manually-built cookie): missing/tampered
pending cookie → 400; inactive/nonexistent `club_id` → 404; double-submit
race → second call logs into the same row, no duplicate; existing users'
login path completely unaffected; `claim`/`create-profile` work correctly
for the newly-created test user afterward. All test rows cleaned up. No
deviations, nothing left unverified.

**Committed, pushed, deployed:** part of the `v78` batch below (commit
`fd8017f`).

---

## Phase 2 — club-at-signup, frontend half (SHIPPED, 2026-07-16)

Repo: `call-to-arms-web`. Companion to the backend slice above.

**Added:**
- `src/routes/join/+page.svelte` (new) — fetches `GET /clubs` on mount,
  renders active clubs reusing `/claim`'s existing list styling (no new
  style invented). Submits `POST /auth/complete-signup` with
  `credentials: 'include'`; success calls `window.__refreshAuth` then
  `goto('/claim')`; 400 shows a distinct "signup session expired"
  message + link back to Discord login (no auto-retry); other errors
  show a generic retry-able message.
- `src/routes/+layout.svelte` — auth-gate now also excludes `/join`
  (confirmed via a repo-wide grep for `isAuthed`/`authenticated` that
  this was the only place blocking a page from rendering entirely).

**Verification — partial, deliberately, not a gap that was missed:**
contract-level verification only (minted valid `cta_pending_signup`
cookies against the real local backend, confirmed exact request/response
shapes match what `/join`'s code sends/expects, including error cases).
**Could not get a rendered screenshot** — Playwright's cached Chromium is
missing system shared libs in the Claude Code environment, no
passwordless `sudo` available to fix it (noted for later — worth a
one-time system-level fix). **Full real end-to-end with a live Discord
login was not attempted** — no way to complete that round trip from
either session.

**Shipped:** commit `5c16a52` on `main`, pushed. Deploys via Vercel's
GitHub integration (no `vercel.json` in-repo — dashboard-managed,
confirmed push-to-main auto-deploys). Live within ~20s of push. Confirmed
`https://www.calltoarms.app/join` returns real SvelteKit HTML, not a 404.
Frontend deployed *before* the backend batch below, deliberately — see
that section for why the order matters.

---

## Phase 2 — `v78` ship (backend batch, 2026-07-16)

Both remaining Phase 2 backend slices (delegate appointment,
club-at-signup) shipped together, **after** the frontend above was
confirmed live — required ordering: once the backend redirects new
signups to `{frontend}/join`, that route has to already exist in
production or a real new signup would 404.

**Three commits, in order, on `main`:**
- `be14840` — super-admin appointment endpoints (`admin.py`).
- `fd8017f` — club-at-signup backend changes (`auth.py`, `main.py`,
  `database.py`).
- `8b6159b` — **unplanned, flagged to Joel before committing, approved
  live in that session as a 3rd commit:** removed the 19 disposable
  `verify_*.py` one-off proof scripts (per this file's own Housekeeping
  section, already dead weight) and rewrote `PROJECT_STATUS.md`. **Real
  consequence of this one:** that rewrite was based on Claude Code's own
  local copy of this file, which had drifted behind Cowork's — it
  predated the club-at-signup and delegate-appointment sections above.
  Reconciled by hand in Cowork afterward (this edit). **Process
  guardrail worth keeping in mind going forward:** paste the
  latest Cowork-updated `PROJECT_STATUS.md` into the repo before a new
  Claude Code session starts, not only after — otherwise a session's own
  incidental edits to this file can silently regress it.

**Deploy:** `fly deploy` → release **`v78`**, clean rolling deploy, no
schema migration needed for either slice (`is_super_admin`/`clubs`
already existed with their current shape; club-at-signup only touches
application code). Post-deploy health check clean: `/systems`, `/clubs`,
`/auth/me`, `/pairings` (with params), `/league/factions` → 200;
`/players`, `/league/rankings` (no session) → 401. `fly logs` clean, no
tracebacks.

**Closed out, 2026-07-16:** Joel flipped `is_platform_admin=true` on his
own production user row via direct SQL (Supabase SQL editor), and did a
real end-to-end click-through in production with a throwaway Discord
account — signed in fresh, landed on `/join`, saw Manchester in the club
picker, selected it, landed on `/claim`, completed a profile, landed on
`/` correctly logged in. **Confirmed working, live, for real.** Phase 2's
shipped work (platform-admin kickoff, `club_systems` endpoint, delegate
appointment, club-at-signup) is now fully proven in production, not just
staging-verified.

---

## Phase 3 — club_webhooks table (staging only, 2026-07-16, not committed)

First slice of Phase 3 (per-club Discord + public page scoping). Six real
Discord webhook call sites confirmed in the actual code (not guessed),
all currently keyed by system name or global, none by club:
`signups.py` (per-system signup notifications), `post_pairings_image.py`
(per-system pairings image — already had a comment flagging this exact
gap), `run_call_to_arms.py`/`run_hh_call_to_arms.py`/`run_kt_call_to_arms.py`
(per-system weekly reminder), `league.py` (league result, global),
`post_league_rankings_image.py` (league rankings image, global),
`services.py` (achievement announcements, global). Matches
`multitenancy-plan-v2.md`'s Phase 3 `club_webhooks` design exactly.

**Deliberately expand-only, same pattern as every phase's first step:**
table created and seedable, but none of the six call sites read from it
yet — they all still read their env vars exactly as before. Encryption-
at-rest for the `url` column explicitly deferred to whenever a real
write-endpoint exists (not yet, not this slice). Uniqueness gotcha
avoided deliberately: no DB-level `UNIQUE(club_id, webhook_type,
system_id)` constraint (Postgres treats `NULL` `system_id` as always
distinct, which would silently fail to enforce "one row" for the three
club-level types — the exact trap `app_settings` had before Phase 1's
`club_settings` split) — follows `ClubSystem`'s existing precedent of no
DB constraint, uniqueness enforced by the seed script's own
check-then-upsert logic instead.

**Added:**
- `ClubWebhook` model (`models.py`) — `club_id`, `webhook_type`,
  `system_id` (nullable), `url`, timestamps.
- `"club_webhooks"` added to `WRITE_ALLOWED_TABLES` (`database.py`).
- `seed_club_webhooks.py` — table creation + idempotent upsert-by-select
  seed, `--verify-only` flag, `verify()` diffs against live env vars.

**Verified on staging:** table created; all 12 of 12 expected webhook env
vars were empty/unset on staging (expected — staging's `.env` doesn't
carry real Discord secrets) — skipped by name only:
`DISCORD_SIGNUP_WEBHOOK_URL`, `DISCORD_HH_SIGNUP_WEBHOOK_URL`,
`DISCORD_KT_SIGNUP_WEBHOOK_URL`, `DISCORD_TOW_PAIRINGS_WEBHOOK_URL`,
`DISCORD_HH_PAIRINGS_WEBHOOK_URL`, `DISCORD_KT_PAIRINGS_WEBHOOK_URL`,
`DISCORD_CALL_TO_ARMS_WEBHOOK_URL`, `DISCORD_HH_CALL_TO_ARMS_WEBHOOK_URL`,
`DISCORD_KT_CALL_TO_ARMS_WEBHOOK_URL`, `DISCORD_LEAGUE_RESULT_WEBHOOK_URL`,
`DISCORD_LEAGUE_RANKINGS_WEBHOOK_URL`, `DISCORD_ACHIEVEMENT_WEBHOOK_URL`.
`--verify-only` round-tripped cleanly; re-running the seed was idempotent
(0 seeded/12 skipped both times). To actually exercise the insert→update
path (no real env var available to trigger it naturally), temporarily set
one throwaway fake value in-process
(`https://discord.com/api/webhooks/test/throwaway-1`, never a real
secret), seeded, changed it, re-seeded, confirmed row count stayed at 1
(update, not duplicate), then deleted the row — staging back to 0 real
rows, no real webhook data ever created, printed, or persisted. Confirmed
all six existing call sites untouched (`git diff --stat` empty on all of
them). Security discipline held: no real webhook URL value printed or
logged anywhere.

**Committed and pushed:** `47c88ee` on `main` (three files:
`models.py`, `database.py`, `seed_club_webhooks.py`). **No `fly deploy`
performed** — not needed, since nothing reads from this table yet.

**Seeded against production, 2026-07-16:** the production machine was
found scale-to-zero stopped (clean, `exit_code=0`, Fly's normal
low-traffic behavior, not a crash) — started via `fly machine start`,
then `fly ssh console` worked on the first attempt (no fallback to asking
Joel for secrets needed this time). The running container was still on
the pre-commit image, so the three changed files were uploaded directly
onto the live machine's `/app` via `fly ssh sftp put` and the script run
there against production's real, already-present env vars — a one-off
workaround, not a deploy. Since the app runs plain `uvicorn main:app`
(no `--reload`), overwriting the files on disk didn't affect the
already-running server process — confirmed via `GET /docs` → 200
afterward. **These on-disk changes are superseded cleanly by the next
real `fly deploy`**, which rebuilds fresh from the Docker image off
`47c88ee` — no drift risk.

**Real production results — 5 of 12 env vars were actually set:**
seeded: signup webhooks for all 3 systems, plus club-level `achievement`
and `league_result`. Skipped (empty/unset, names only):
`DISCORD_TOW_PAIRINGS_WEBHOOK_URL`, `DISCORD_HH_PAIRINGS_WEBHOOK_URL`,
`DISCORD_KT_PAIRINGS_WEBHOOK_URL`, `DISCORD_CALL_TO_ARMS_WEBHOOK_URL`,
`DISCORD_HH_CALL_TO_ARMS_WEBHOOK_URL`, `DISCORD_KT_CALL_TO_ARMS_WEBHOOK_URL`,
`DISCORD_LEAGUE_RANKINGS_WEBHOOK_URL` — i.e. Manchester currently has no
pairings-image, call-to-arms-reminder, or league-rankings-image webhooks
configured at all in production, only signup notifications + achievement
+ league result. Worth knowing regardless of Phase 3's progress. Verified
idempotent (re-ran seed, still 5/12, confirmed via direct DB query — 5
rows, no duplicates); `--verify-only` clean both times. Six existing call
sites confirmed untouched. No real webhook URL value ever printed,
logged, or echoed anywhere — security discipline held throughout, and
the SSH-direct path meant Joel was never even asked for a secret.

---

## Phase 3 — league-result webhook read path (SHIPPED, production `v79`, 2026-07-16)

First "contract" step for Phase 3's `club_webhooks` table — proves the
DB-first, env-var-fallback mechanism on the simplest of the six call
sites before touching the other five (each of which has its own wrinkle:
`post_discord_achievement()` has no `club_id` parameter yet,
`post_league_rankings_image.py` isn't itself properly club-scoped, the
three per-system types need a `SystemConfig` lookup on top).

**Added:**
- `resolve_webhook_url(db, club_id, webhook_type, system_id=None)`
  (`database.py`, next to `scoped()`) — pure DB lookup, no env-var
  knowledge, returns the matching `ClubWebhook.url` or `None`.
- `league.py`'s `_post_league_webhook` now takes `db: Session`, resolves
  via `resolve_webhook_url(db, row.club_id, "league_result") or
  DISCORD_LEAGUE_RESULT_WEBHOOK_URL` (the `or` correctly falls through on
  both `None` and empty string). Its one call site (`submit_result`) now
  passes `db` through.

**Verified on staging:** DB-sourced path (seeded a throwaway
`ClubWebhook` row for Manchester, confirmed a constructed `LeagueResult`
+ monkeypatched `httpx.post` used the DB URL, not the env var);
env-var-fallback path (deleted the row, confirmed correct fallback —
staging's env var is empty, so this correctly no-ops, matching today's
unmodified behavior); scoping check (a second throwaway club's
`ClubWebhook` row was correctly ignored when resolving for Manchester's
`club_id` — confirms the lookup is genuinely scoped by `row.club_id`,
not "any row of this type"). Used a direct unit-level test of the two
functions rather than the full `/league/results` endpoint, to avoid
dragging in unrelated side effects (ratings recalc, achievement
announcements) — exercises the same three logic paths. All test data
cleaned up, staging back to exactly 1 club, 0 `club_webhooks` rows.

**One deviation, a stronger check than asked for:** the handoff's
scoping-check step suggested a "nonexistent `club_id`," but
`club_webhooks.club_id` has a real FK constraint to `clubs` — a bogus id
would error at the DB level rather than silently mismatch. Used a real
second throwaway club instead, which is the more meaningful proof
(genuine cross-club isolation, not just an FK-constraint side effect).

**Committed, pushed, deployed:** `536a4f7` on `main`, Fly release
**`v79`**. Health check clean: `fly logs` clean rolling deploy, no
tracebacks; `/systems`, `/clubs`, `/auth/me`, `/pairings` (with params),
`/league/factions` → 200; `/players`, `/league/rankings` (no session) →
401. Change-specific check: read-only `resolve_webhook_url()` call
directly against production via `fly ssh console` for Manchester's real
`club_id` confirmed a non-empty URL resolves correctly post-deploy —
presence only, value never printed. Other five webhook call sites
untouched.

---

## Phase 3 — achievement webhook read path (SHIPPED, production `v80`, 2026-07-16)

Second "contract" step, same pattern as the shipped league-result webhook
(`v79`). This one needed real new plumbing, not just a lookup swap —
`post_discord_achievement()` had no `club_id` parameter at all.

**Added:**
- `services.py`: `post_discord_achievement` now takes `club_id: int, db:
  Session`, resolves `resolve_webhook_url(db, club_id, "achievement") or
  DISCORD_ACHIEVEMENT_WEBHOOK_URL`. `announce_new_achievements`
  restructured — the old bare `if not DISCORD_ACHIEVEMENT_WEBHOOK_URL:
  return` guard at the top is gone; the check now happens after `player =
  db.get(Player, player_id)`, using the resolved-or-fallback URL instead.
  The internal call now passes `player.club_id, db`.
- `admin.py`: `achievement_post_discord` gained a `db` dependency, its
  discarded `_` param renamed to `user`, passes `user.club_id, db`
  through (this endpoint takes a free-text `player_name`, not a
  `player_id`, so the admin's own club is the correct source of
  `club_id`, consistent with every other club-scoped admin endpoint).

**Verified on staging:** DB-sourced path confirmed directly; **the
early-exit restructure specifically verified** — with a `ClubWebhook` row
present but the env var empty (the scenario a future env-var-less club
would be in), confirmed the full achievement computation now runs and
posts via the DB URL, proving the old bare-env-var guard is genuinely
gone, not just moved. Also verified the reverse: with nothing configured
anywhere, the new guard still short-circuits before even calling
`compute_achievements` (spied, zero invocations) — preserving the
original perf-guard for the common case while fixing the gap for the
DB-only case. Cross-club scoping confirmed (second club's row ignored).
`POST /admin/achievements/post-discord`'s existing behavior (401 with no
auth) confirmed unchanged. All test data cleaned up.

**Committed, pushed, deployed:** `0fa13d9` on `main`, Fly release
**`v80`**. Health check clean (the only error-level log lines found were
pre-existing SSH-session EOFs from earlier in the session, predating this
deploy, unrelated). `/systems`, `/clubs`, `/auth/me`, `/pairings` (with
params), `/league/factions` → 200; `/players`, `/league/rankings` (no
session) → 401. Change-specific check: read-only production lookup for
Manchester's `achievement` webhook confirmed non-empty, presence only.

**Two of six webhook call sites now DB-first with env-var fallback:
`league_result`, `achievement`. Four remain, untouched: `signup`,
`pairings`, `call_to_arms`, `league_rankings`.**

---

## Phase 3 — signup webhook read path (SHIPPED, production `v81`, 2026-07-16)

Third "contract" step, same pattern as the shipped league-result (`v79`)
and achievement (`v80`) webhooks — first of the three per-system types,
introducing a `system_id` dimension on top of the club lookup.

**Changed (`signups.py`):** `_post_webhook` signature is now
`_post_webhook(db, club_id, system, content)`. Resolves
`system_config = _get_system_config(db, system)` (existing Phase 0
helper, reused not reinvented) → `system_id`, then
`resolve_webhook_url(db, club_id, "signup", system_id) or
_signup_webhook_for_system(system)`. All 5 call sites updated: the two
wrappers (`_post_discord_signup`, `_post_discord_drop`) forward
`db, club_id`; the three direct calls (post-publish drop in
`drop_signup`, `submit_prearranged`, `swap_signups`) pass
`db, user.club_id` explicitly.

**Verified on staging:** both wrapper functions confirmed using the
DB-sourced URL directly; **system discrimination confirmed** — a
`ClubWebhook` row seeded for one system (TOW) was correctly NOT used when
resolving for a different system (KT), proving `system_id` genuinely
discriminates rather than being ignored; cross-club scoping confirmed
(second club's row ignored); fallback confirmed (row deleted → correct
no-op, matching today). **Went beyond a unit-level check for one
call site:** forged a valid session cookie and hit the real
`POST /signups/prearranged` endpoint through `TestClient` end-to-end with
a seeded DB webhook row — got 200, confirmed the actual Discord post used
the DB-sourced URL through the genuine route handler, not just a direct
function call. `drop_signup`'s post-publish path and `swap_signups`
weren't exercised via their live routes (more setup/cleanup risk, need
pre-existing published-pairings state) but their `_post_webhook(...)`
call is identical in shape to what was proven elsewhere. All test data
cleaned up — staging back to exactly 1 club, 0 `club_webhooks` rows, only
the 2 pre-existing real players remaining.

**Committed, pushed, deployed:** `85f9c69` on `main`, Fly release
**`v81`**. Health check clean. Change-specific check: read-only
production lookup for all 3 systems' `signup` webhooks (Manchester)
confirmed non-empty — TOW, HH, KT all `True`, presence only.

**Three of six webhook call sites now DB-first with env-var fallback:
`league_result`, `achievement`, `signup`. Three remain: `pairings`,
`call_to_arms`, `league_rankings`.**

---

## Phase 3 — pairings webhook read path (SHIPPED, production `v82`, 2026-07-16)

Fourth "contract" step. **This one closes a real, previously-flagged bug**
(not just another mechanical repeat): `post_pairings_image.py`'s
`WEBHOOK_MAP` mixing pairings across clubs sharing a system — flagged in
this project's own code comments since before this session started as a
must-fix-before-Phase-4 gap.

**Changed (`post_pairings_image.py`):** `post_pairings_image_for` now
resolves `system_id` (same `SystemConfig.legacy_system_name` lookup shape
`_resolve_single_club_id` already used) and checks
`resolve_webhook_url(db, club_id, "pairings", system_id) or
WEBHOOK_MAP.get(system, "")`. `WEBHOOK_MAP`'s comment rewritten — no
longer describes an unsolved gap, now correctly documents it as the
fallback for a club with no `club_webhooks` row. `_resolve_single_club_id`
and the manual `workflow_dispatch` entry point (`main()`) untouched — a
separate known gap (no club selector for that manual workflow), not this
handoff's problem.

**Verified on staging, including the scenario that matters most:**
created a second throwaway club with its own real `ClubSystem` row for
"The Old World" — genuinely sharing the system with Manchester — gave it
its own distinct `pairings` webhook, called `post_pairings_image_for` for
both clubs, and confirmed each posted to its own URL with zero crossover
in either direction. **This is the exact bug fixed, confirmed directly,
not just inferred.** Also verified: system discrimination (different
system, no row, correctly falls through without leaking the other
system's URL); fallback (row deleted → correct no-op matching today).
`run_auto_pairings_check.py` confirmed unaffected by inspection — its
call site already passes real per-club `club_id`, function signature
unchanged. All test data cleaned up.

**Worth knowing regardless of this ship:** production currently has zero
`club_webhooks` rows for `pairings` — the earlier production seed found
all three `DISCORD_*_PAIRINGS_WEBHOOK_URL` env vars empty (Manchester has
never had pairings-image webhooks configured at all, consistent with what
the seed step already surfaced). So this fix will be 100% fallback for
Manchester once shipped — nothing to regress, but also not load-bearing
yet. It only takes effect once a real `club_webhooks` row exists for
`pairings` (Manchester's own, or a future second club's).

**Committed, pushed, deployed:** `3018b3f` on `main`, Fly release
**`v82`**. Health check clean. Change-specific check: production
`resolve_webhook_url` for all 3 systems' `pairings` type correctly
returned `None` (no row exists yet), no exceptions — fallback path
exercised cleanly, not just theoretically safe. Manchester's actual
pairings-webhook behavior unchanged (still 100% `WEBHOOK_MAP`/env-var
sourced) until a real `club_webhooks` row exists for `pairings`.

**Four of six webhook call sites now DB-first with env-var fallback:
`league_result`, `achievement`, `signup`, `pairings`. Two remain:
`call_to_arms`, `league_rankings`.**

---

## Phase 3 — league-rankings webhook read path + latent bug fix (SHIPPED, production `v83`, 2026-07-16)

Last of the six webhook call sites for this round (the three
`call_to_arms` scripts remain explicitly parked — reopening them was
flagged as revisiting an earlier "leave as-is" decision, and Joel chose
to skip them). This one required a real fix, not just a lookup swap:
`post_league_rankings_image.py`'s `main()` called
`league_rankings(_=None, session=db)`, but the actual function takes
`user`, not `_` — a `TypeError` waiting to happen the moment
`DISCORD_LEAGUE_RANKINGS_WEBHOOK_URL` (or now, a `club_webhooks` row)
was ever non-empty. It never had been in production, which is the only
reason this was latent rather than already broken.

**Changed:**
- `main.py`: extracted `league_rankings`'s body into
  `_compute_league_rankings(session, club_id) -> list[dict]`. The
  `@app.get("/league/rankings")` endpoint is now a two-line wrapper
  calling it with `user.club_id` — signature, decorator, and auth
  completely untouched.
- `post_league_rankings_image.py`: added
  `_resolve_single_active_club_id(db)` (mirrors `post_pairings_image.py`'s
  `_resolve_single_club_id` — raises loudly on anything but exactly 1
  active club, rather than guessing). Replaced the broken call with
  `_compute_league_rankings(db, club_id)`. Webhook lookup now
  `resolve_webhook_url(db, club_id, "league_rankings") or
  DISCORD_LEAGUE_RANKINGS_WEBHOOK_URL`, still checked before rendering so
  it short-circuits cheaply when nothing's configured.

**Verified on staging:** live-endpoint equivalence proven directly —
hit the real `GET /league/rankings` via `TestClient` with a forged
session cookie, separately called `_compute_league_rankings` directly,
results were exactly equal with real seeded throwaway league data —
confirms the extraction is a pure refactor, zero behavior change. **The
latent bug fix specifically verified**: seeded a `ClubWebhook` row so the
previously-guaranteed-to-crash path actually executes — ran the real
`main()` (only `httpx.post` mocked), no exception, correct DB-sourced
post. Fallback confirmed (no exception, correct no-op with row removed).
Club-resolution safety confirmed (raises on a second active club,
resolves correctly again once removed). All test data cleaned up.

**This closes out all six webhook call sites except the three
`call_to_arms` scripts, which stay parked.**

**Committed, pushed, deployed:** `df2dbeb` on `main`, Fly release
**`v83`**. Health check clean. **Real authenticated request against
production**, not just a read-only lookup: forged a session cookie for
the `Testy Mctestface` test account (id 53, deliberately not a real
player) and hit the actual live app + real DB via `fly ssh console` —
`200`, 31 real ranking rows in the expected shape, confirming the
extraction produces correct output against production's real league
data. Webhook lookup for `league_rankings` confirmed `None` cleanly (no
row yet, same as `pairings`), no exceptions.

**Five of six planned webhook call sites now DB-first with env-var
fallback: `league_result`, `achievement`, `signup`, `pairings`,
`league_rankings`. The three `call_to_arms` scripts remain untouched per
the standing decision — not part of this round.**

---

## Phase 3 — club_webhooks save/list/delete endpoints (SHIPPED, production `v84`, 2026-07-16)

First self-service piece of Phase 3 — turns `club_webhooks` from
"seeded once by a script" into something a club's own super-admin can
manage directly. (One interrupted attempt at this handoff got as far as
adding `ClubWebhook` to `admin.py`'s import line before a server error cut
the session off mid-task — recovered cleanly in the next message, no lost
work, no leftover half-state.)

**Added (`admin.py`):**
- `WEBHOOK_TYPES_PER_SYSTEM`, `WEBHOOK_TYPES_CLUB_LEVEL`,
  `ALL_WEBHOOK_TYPES` constants; `_mask_webhook_row(row)` masking
  primitive.
- `GET /admin/webhooks` (`require_super_admin`) — full 12-row grid (3
  per-system types × 3 systems + 3 club-level types) for `user.club_id`.
  Configured rows: `{webhook_type, system_id, system_name, configured:
  true, last_four}`. Unconfigured: `configured: false`, no `last_four`.
  **The raw URL never appears anywhere in this response** — confirmed by
  grepping the raw response text for the full test URLs, never present.
- `POST /admin/webhooks` (`require_super_admin`) — validates
  `webhook_type`/`system_id` combination (422 for a `system_id` on a
  club-level type or a missing one on a per-system type, 404 for an
  invalid `system_id`, matching the earlier `club_systems` endpoint's
  precedent), upserts keyed on `(user.club_id, webhook_type, system_id)`,
  returns the masked shape only.
- `DELETE /admin/webhooks` (`require_super_admin`, query params) —
  idempotent removal.

**Verified on staging:** auth gating (403 for non-super-admin); empty
state returns exactly 12 rows, all unconfigured, no `url` key anywhere;
masked POST response confirmed for both a per-system and a club-level
type; upsert-not-duplicate confirmed (row count stays 1 on re-POST);
full validation matrix (422/404 cases); idempotent DELETE; **cross-club
isolation confirmed** with two real distinct clubs each configuring their
own `league_result` webhook — each club's `GET` showed only its own row,
neither club's `DELETE` touched the other's. (One test-data mistake
self-corrected along the way: the first isolation pass used URLs sharing
a suffix, making their `last_four` values collide by coincidence — not a
real leak, since the rows were already independently confirmed distinct
by `club_id` and delete behavior; re-ran with distinct suffixes for an
unambiguous result.) Confirmed no regression: all five already-shipped
webhook read paths still resolve correctly during and after this
endpoint's test data existed and was cleaned up. All test data removed
afterward — staging back to exactly 1 club, 0 `club_webhooks` rows, 2
pre-existing real users.

**Committed, pushed, deployed:** `0540aab` on `main`, Fly release
**`v84`**. Health check clean. Read-only production sanity check:
`GET /admin/webhooks` as production's real super-admin (`Kirkboi`, user
id 1) returned 200, the correct 12-row grid, no `url` key anywhere, 5 of
12 configured matching exactly what was seeded earlier (signup ×3,
`achievement`, `league_result`), the other 7 correctly unconfigured. No
POST/DELETE performed against production — nothing written or removed.

**This completes `club_webhooks` through to a working self-service API.**
No frontend yet (next up), no encryption-at-rest (still deferred), the
three `call_to_arms` scripts remain untouched.

---

## Phase 3 — Discord Webhooks admin panel, frontend (SHIPPED, 2026-07-16)

Repo: `call-to-arms-web`. Frontend for the `club_webhooks` self-service
API (`v84`).

**Added:** a "Discord Webhooks" panel in `admin/+page.svelte`, top-level
and super-admin-gated (same pattern as "Post Achievement to
Discord"/"Manage Admins"), between "Edit Player Profile" and "Pairing
Blocks". Three sub-sections for the per-system types (each listing 3
systems) and three for the club-level types (single row each). Each row:
status ("Configured (…1234)" / "Not configured"), URL input, Save
(disabled empty), Remove (shown only when configured). The
`call_to_arms` sub-heading carries an explicit note that saving there
doesn't yet change real behavior (the three `run_*_call_to_arms.py`
scripts are still deliberately unconverted). All existing CSS classes
reused, nothing new added.

**Verified at the contract level, not visually — same limitation as the
club-at-signup frontend handoff, now hit twice:** Playwright/Chromium
still won't launch in this environment (missing shared libs, no `sudo`).
Confirmed the request/response contract directly instead: minted a real
session for a throwaway staging super-admin, hit `GET`/`POST`/`DELETE
/admin/webhooks` via `curl` using the exact shapes the Svelte code
constructs — all matched (12-row `GET`, masked `POST` responses, correct
`DELETE` behavior including omitting `system_id` for club-level types,
422 validation errors with `detail`, 401 with no cookie). `npm run build`
clean. All test data cleaned up.

**Not verified: actual rendering in a real browser** (button states,
layout, the caveat note's visual placement) — recommend a quick manual
look once this ships, same spirit as the `/join` click-through
recommendation earlier. This is the second time the Playwright/shared-libs
gap has blocked visual verification in `call-to-arms-web` — worth
actually fixing once (real `sudo`, install the missing libs) rather than
hitting this a third time.

**Committed, pushed, deployed:** `792094d` on `main`. No Vercel/GitHub
CLI available to query deploy status directly, so verified indirectly
but conclusively: `/admin` returns 200 with a fresh (`x-vercel-cache:
MISS`, `age: 0`) response, and its content-hashed asset filenames match
byte-for-byte a local `npm run build` of this exact commit — strong proof
Vercel deployed this specific commit, not a stale cached one. Route
healthy, correct title, no error boundary. **The super-admin-gated panel
itself still hasn't been seen rendering by a human** — recommend Joel log
in and take a look for real.

---

## Housekeeping — PROJECT_STATUS.md sync mystery solved + auth.py docstring fixed (staged, not committed, 2026-07-16)

**The recurring "PROJECT_STATUS.md shows modified" mystery is resolved.**
Root cause confirmed by diffing the working tree against `HEAD`: commit
`8b6159b` (the unplanned cleanup commit from the `v78` ship) committed
Claude Code's own stale local copy of this file, which predated the
club-at-signup and delegate-appointment sections. Cowork manually
reconciled the working-tree copy back to accurate immediately afterward
— but that reconciliation itself was never committed, so every session
since correctly left the file alone (per the ownership rule) and the gap
just sat there, re-flagged repeatedly but never traced to root cause
until now. **The working-tree copy is confirmed authoritative** — every
commit hash, Fly release, and verification detail in it checks out
against what actually happened through the Discord Webhooks admin panel
ship (`792094d`). Resolution: commit it as-is, no further reconciliation
needed.

**`auth.py` docstring fixed**: removed the false "SameSite=None + Secure"
claim (module docstring never matched the actual code, which has always
used `samesite="lax"` for all four cookies in this file). Also found and
removed an identical false claim as an inline comment directly above the
`cta_session` `set_cookie` call in `discord_callback`'s existing-user
branch — leaving that would have kept the same misinformation right next
to the code. Confirmed via grep: no remaining `SameSite=None` claims
anywhere in the file.

**Staged, not committed** — awaiting go-ahead.

---

## Phase 3 — Discord Webhooks panel: real visual bug found and fixed (staged, not committed, 2026-07-16)

Repo: `call-to-arms-web`. Follow-up to the panel shipped in `792094d`,
which had only been contract-verified (Playwright didn't work in this
environment yet). Now that it does (Joel's shared-libs fix confirmed
working), this handoff got real screenshots for the first time — **and
found a real bug that's been live in production since the panel
shipped.**

**Bug:** each webhook row rendered broken — three stacked lines instead
of one (system name/status, then the URL input stretched to nearly full
section width on its own line, then the Save/Remove buttons orphaned on
a third line, floated to the far right). Still technically clickable,
but did not "render sensibly" by any reasonable definition — this is
exactly the class of thing contract-level verification structurally
cannot catch.

**Root cause:** a global rule in `app.css`
(`.field-input, .field-select { width: 100%; ... }`) assumes
`.field-input` is always wrapped in a `.field` div, which bounds its
width via `min-width: 160px` in a flex layout. The new panel used
`.field-input` as a bare flex child of `.block-row` instead, so it
inherited `width: 100%` against the whole row.

**Fix:** wrapped the input + Save + Remove buttons in a new
`<span class="webhook-actions">` per row, added ~15 lines of CSS scoped
to this component only (`.webhook-actions` flex container,
`.webhook-actions .field-input` override with `flex: 1 1 240px;
min-width: 180px`, a `.webhook-message` wrap tweak) — no changes to any
shared/global class definitions.

**Verified with real Playwright screenshots** (three states: normal,
typed-input, just-saved) — confirmed every row is now a single line with
correct button contrast (enabled Save turns gold, others stay dimmed),
the `call_to_arms` caveat badge renders legibly inline, focus states
work, and all six sub-sections appear in the correct order with correct
labels. Driven via real clicks against a real (throwaway, staging)
backend, not static rendering. All test data cleaned up.

**Staged, not committed** — awaiting go-ahead. This is a real bug fix for
something already live in production (`792094d`) — worth shipping
promptly, not letting it sit.

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
- **Phase 2** (admin hierarchy) — **platform-admin kickoff, `club_systems`
  endpoint, delegate appointment, and club-at-signup (backend + frontend)
  all shipped to production as of `v78`, 2026-07-16.** See the "Phase 2 —
  ..." sections above for what each slice covered. **Confirmed working
  live in production, 2026-07-16:** Joel flipped `is_platform_admin=true`
  on his own account and did a real end-to-end click-through of the new
  signup flow — both clean. Remaining, unscoped Phase 2 work: the
  `VALID_SCOPES`-per-club decision,
  and the `scoped()` "act as club X" platform-admin override. **Phase 3**
  (per-club Discord + public page scoping), **Phase 4** (second club
  onboarding) — not started.

## Next up

Everything shipped in `v78` is now confirmed working live in production
(Joel did the SQL flip and the real click-through, both clean — see the
`v78` ship section above). Phase 2's remaining open items (none
scoped/confirmed yet):

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
- **Playwright now works in `call-to-arms-web`'s environment, 2026-07-16.**
  The missing-shared-libs blocker that forced contract-level-only
  verification on the last two frontend handoffs (club-at-signup,
  Discord Webhooks admin panel) is fixed — Joel ran
  `sudo env "PATH=$PATH" npx --yes playwright@latest install-deps
  chromium` once, confirmed with a real screenshot capture
  (`npx --yes playwright@latest screenshot ...`). Chromium's binary was
  already cached from an earlier attempt; only the system libraries were
  ever actually missing. **Future frontend handoffs in this repo should
  use real Playwright screenshots for visual verification, not fall back
  to contract-level-only checks** — the excuse no longer applies.

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