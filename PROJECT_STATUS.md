# Call to Arms — Multi-Club Rollout: Status

Auto-maintained by Claude in Cowork sessions on this repo. Updated whenever
a session's task list wraps up. The full decision log and phased plan live
in the Claude.ai project ("Call to Arms — Multi-Club Rollout") — this file
is a lightweight mirror so status survives even if that project context
isn't loaded.

_Last updated: 2026-07-13_

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
   Joel and write a handoff for Claude Code instead. It can search a whole
   repo in one pass; Cowork relaying single commands one at a time and
   waiting on pasted output each round is much slower and easy to get
   stuck in a loop with.

## Incident: Discord auth broken in production (2026-07-13, resolved)

**Symptom:** player report ("not working") →
`{"detail":"Discord token exchange failed: {\"error\": \"invalid_client\"}"}`
on every `/auth/discord/callback`. Signup/pairings endpoints unaffected —
only login.

**Cause:** earlier the same day, the Discord OAuth client secret was reset
in the Discord Developer Portal to unblock local login testing (see Phase 0
section below — "a genuinely local Discord client secret was needed").
Local dev and production share one Discord application (single
client_id/secret across all registered redirect URIs, confirmed by checking
the Redirects list — `localhost`, `fly.dev`, and `calltoarms.app` are all on
the same app). Resetting the secret instantly invalidates the old one, but
`fly secrets set` was never run afterward, so production kept running on
the now-dead secret until the player report came in.

**Fix:** `fly secrets set DISCORD_CLIENT_SECRET=<new value> -a
call-to-arms-api`. Confirmed via `fly logs`: machine redeployed, a real
login completed end-to-end afterward.

**Guardrail added:** `CLAUDE.md` now has a "Shared Discord secret" section
— any future reset/rotation must update local `.env` AND `fly secrets set`
together, in the same sitting. Worth considering a second, separate Discord
application for local dev later to remove the shared-secret footgun
entirely (not done).

## Current phase: Phase 0 (systems-as-data refactor) — IN PROGRESS

Step 0 (fresh repo pull + confirm before coding) done. Found two mismatches
against the `PHASE0` doc's assumptions, resolved with the user:

1. **`slug` ≠ existing `system` values.** The doc assumed a short slug
   (tow/hh/kt) matches what's already stored in `Signup.system` /
   `Pairing.system` / `PublishState.system`. It doesn't — those columns hold
   the full display string ("The Old World", "The Horus Heresy",
   "Kill Team"). Resolved: `SystemConfig` now has both `slug` (short,
   new-code-facing) and `legacy_system_name` (the exact existing string, for
   joining against current columns without a data migration).
2. **Icon lookup isn't per-system today.** `render_pairings_image.py`
   searches all of `icons/TOW`, `icons/HH`, `icons/KT` for every faction
   regardless of system. Resolved: `icon_folder` on `SystemConfig` is
   informational only for now — restricting the lookup to one folder per
   system would be a behavior change, not a pure refactor, so it's
   out of scope for Phase 0.

Also expanded `SystemConfig` beyond the doc's three flags
(`uses_points`/`has_intro_prepass`/`uses_scenarios`) to cover two more
system-specific behaviors found in `pairings_engine.py` that would otherwise
stay hardcoded on system-name string checks: `recent_weeks`/`extended_weeks`
(HH's pairing-history lookback is 6/12 weeks vs. 3/6 for TOW/KT) and
`escalation_priority` (TOW-only sort/match priority for "Escalation"-vibe
signups).

Step 1-2 (table + seed) drafted: `SystemConfig` model added to `models.py`,
`systems` added to `database.py`'s `WRITE_ALLOWED_TABLES`, and
`seed_systems_config.py` written (creates the table, seeds TOW/HH/KT,
verifies seeded values against the live hardcoded constants).

**Resolved:** `seed_systems_config.py` was run by the user against staging
(Cowork's sandbox can't reach Supabase — network egress allowlist doesn't
cover it). Verification passed: TOW/HH/KT rows confirmed in staging
`systems` table, matching the hardcoded constants byte-for-byte.

Step 3 + step 4.1 (partial) done:

- Added `systems_from_catalogue` flag — a single global `app_settings` row
  (per the doc's recommendation), read via
  `_systems_from_catalogue_enabled()` in `signups.py`. Off by default
  (missing row => `False`), so no behavior change until explicitly flipped
  on staging.
- Refactored `POST /signups` (`submit_signup`, the highest-traffic
  endpoint) to branch on the flag: catalogue-driven path when on, original
  hardcoded `is_hh`/`is_kt` branch left completely untouched as the `else`
  when off or if no matching `SystemConfig` row is found. Verified by hand
  that the catalogue formula reproduces the old branch exactly for all
  three systems (vibe/points/scenario/can_demo defaults and clamps).

`POST /signups/prearranged` (`submit_prearranged`) also refactored. It has
the same `is_hh`/`is_kt` shape as `submit_signup` but a genuine
inconsistency between the two endpoints — for Kill Team, `submit_signup`
sets `points = 0` while `submit_prearranged` sets `points = None`. User's
call: preserve `None` exactly (Kill Team isn't points-based regardless, so
`None` is the more correct sentinel for this endpoint) rather than
normalizing the two endpoints to match each other. `uses_points=False` in
the catalogue path now maps to this endpoint's own existing sentinel
(`None`), not a shared global rule — kept as a per-endpoint decision in the
code with a comment, not a new `SystemConfig` field, since it's about how
each endpoint represents "no points," not a property of the system itself.

Both `signups.py` endpoints with per-system defaults are now dual-run
complete.

Not yet done: `pairings_engine.py` refactor (highest-risk file —
per-system `if system == "..."` branches for points/intro-prepass/
scenarios/escalation-priority/recency-windows), `render_*_image.py`
(low-risk, icon_folder is informational only per the decision above), the
three `run_*_call_to_arms.py` scripts, and the frontend (separate repo, not
included in this pull, not yet reviewed).

Staging verification of the flag itself (flip it on, submit real signups
for all three systems, confirm identical resulting rows) has not been done
yet — needs to happen before moving further down the backend refactor
order.

### `pairings_engine.py` — done (via Claude Code, from the HANDOFF.md prompt)

All six per-system hardcoded branches now read from `SystemConfig` when
`systems_from_catalogue` is on and a matching row exists, original
hardcoded branch preserved byte-for-byte as fallback: `_scenario_diff_tow`
→ `uses_scenarios`, `_escalation_priority_penalty` → `escalation_priority`,
`_pair_dist`'s points-distance term → `uses_points`, intro pre-pass gate →
`has_intro_prepass`, candidate sort tie-break → `escalation_priority`,
recent/extended history window → `recent_weeks`/`extended_weeks`. `config`
looked up once at the top of `generate()`, threaded through as a parameter.

**Verification:** staging has zero real Signup/Pairing/Player rows for any
system yet, so a real flag-off/flag-on comparison against live data wasn't
possible. Instead, a new one-off script (`verify_pairings_dual_run.py`,
same pattern as `seed_systems_config.py`) builds synthetic signups
exercising all six branches per system plus a 4-week-back historical
pairing (to exercise the recency window), runs `generate(persist=False)`
against real staging with the flag off and on, and diffs the output — all
inside one transaction, rolled back at the end (confirmed staging left at
0 signups/0 pairings/no flag row afterward). All three systems came back
byte-identical. To sanity-check the test itself wasn't a rubber stamp, the
`escalation_priority` read was temporarily inverted, rerun confirmed a
caught mismatch for TOW only (HH/KT unaffected as expected), then reverted
and rerun clean.

**Still outstanding:** verification against *real* signup data (flip the
flag, submit real signups, compare) hasn't happened — there isn't any real
data in staging yet for this system. The synthetic dual-run is a strong
wiring/logic check but isn't a substitute — flag this again before
flipping `systems_from_catalogue` in production.

**Not committed yet** — `database.py`/`models.py`/`signups.py` modified,
`PROJECT_STATUS.md`/`seed_systems_config.py`/`verify_pairings_dual_run.py`
untracked, nothing staged across any of this Phase 0 work.

### `run_*_call_to_arms.py` scripts — decided: skip for now

Investigated; these three scripts are structurally different, not just
constant-swaps of the same shape (TOW has scenario/terrain-image logic and
its own date calc duplicated from `week_logic.py`; HH has fortnightly
anchor math + a skip-check; KT is fully static, no date logic at all). A
real consolidation needs new `SystemConfig` fields (message templates,
asset flags, cadence-check logic) — bigger and riskier than the doc's
"just parameterize it" framing implied. User's call: leave all three
exactly as-is for now, low-risk/low-traffic, revisit later if at all.

### Frontend (`call-to-arms-web`) — Phase 0 signup-form config migration done

Audit (Part 1) found `signupOptions.ts`'s `formConfig()`, `+page.svelte`
(main form + pre-arranged sub-form), and `admin/+page.svelte` as the real
scope — confirmed faction lists, scheduling math, and admin scope arrays
all correctly out of scope. Part 2 implemented a shared
`src/lib/systemsConfig.ts` fetch/cache utility feeding all three
surfaces, with the three-way TOW vibe-options split preserved on purpose
(main form + admin form exclude "Escalation", pre-arranged form keeps it —
confirmed deliberate via an existing code comment). Fallback to hardcoded
values on fetch failure, verified by mocking a fetch rejection.

Two things surfaced during testing, both resolved:

- **Vibe-option ordering bug, caught before shipping:** `SystemConfig.vibe_options`
  isn't stored in a meaningful order — `seed_systems_config.py` seeded it
  via Python's `sorted()` (alphabetical), so HH's list comes back
  `["Intro", "Standard"]` instead of `["Standard", "Intro"]`. This would
  have silently defaulted pre-arranged HH games to "Intro". Fixed on the
  frontend with a `sortVibeOptions()` helper and by reading `default_vibe`
  directly instead of indexing into the array. **Follow-up worth doing
  later:** fix `seed_systems_config.py` to seed `vibe_options` in actual
  intended display order instead of `sorted()`, so the frontend doesn't
  need its own canonical-order knowledge at all — not urgent, current fix
  works, but it's exactly the kind of duplication this migration is
  supposed to remove.
- **KT "Intro" pre-arranged option removed — confirmed NOT a regression.**
  The old frontend offered "Intro" as a pre-arranged KT vibe, but
  `submit_prearranged`'s hardcoded KT branch (`if is_kt: vibe = "Standard"`)
  never read `body.vibe` at all — that option was already dead before
  this migration; removing it changes nothing real.

**Part 3 done:** `preShowPoints` now reads `uses_points` from the fetched
config (TOW/HH `true`, KT `false`) instead of the hardcoded TOW-only
literal — HH pre-arranged games now show and submit a points value,
confirmed via a direct call to `submit_prearranged` (`points=4500`
persisted correctly, KT still forces `None`). During that test, a mistake
happened and was caught: `submit_prearranged` commits internally, so an
outer transaction rollback intended to make the test zero-residue was a
no-op, and 4 test rows briefly landed in staging. Caught immediately,
deleted by exact ID match. Independently verified clean afterward
(`select count(*) from players/signups/pairings` on staging → `0, 0, 0`).

**Real browser click-through done.** Joel fixed the local Discord secret,
logged in locally, and manually verified: main signup form, admin
add-signup form, admin pairings-grid inline dropdowns, and the
pre-arranged sub-form, across all three systems — matches the live app in
every respect (including the deliberate TOW-only-Escalation split, the
Part 3 HH-pre-arranged-points fix, and the confirmed-dead KT-Intro
removal). Frontend Phase 0 migration is now fully verified, both at the
logic layer and visually.

Verification note: no browser click-through was possible in the Claude
Code sandbox (Playwright/Chromium failed to launch — missing system libs,
no `sudo`). Verified instead via `npm run check`/`build` (clean) and
running the actual production logic (`formConfig`, `configFor`,
`sortVibeOptions`) against the real local backend for all three systems,
byte-for-byte matching today's values except the confirmed-safe KT gap
above. A real visual click-through still hasn't happened — worth doing
once before calling this fully shipped.

### New `GET /systems` endpoint added to `main.py`

Added ahead of the frontend refactor — a public (no-auth), read-only
endpoint returning active `SystemConfig` rows, for the frontend to fetch
signup-form config instead of keeping its own hardcoded copies. Not gated
by `systems_from_catalogue` (that flag is about backend signup/pairing
computation; this is a brand-new read path with no prior behavior to
preserve). Handoff for the frontend side of this work is
`HANDOFF_FRONTEND.md` — goes in the separate `call-to-arms-web` repo, not
this one. Also noted along the way: `main.py` has its own hardcoded
`"The Old World"`/`"Kill Team"` checks (`get_player`, `get_pairings`) not
covered by the original Phase 0 doc's scope — flagged for later, not
touched yet.

## Phase -1 (subdomain + auth prototype) — COMPLETE

- Wildcard domain `*.calltoarms.app` added and valid in Vercel.
- Nameserver migration (GoDaddy → Vercel) done and verified.
- test1/test2.calltoarms.app tested end-to-end: Discord login, cookie
  sharing across subdomains, and all API calls confirmed working.
- Two bugs found and fixed during testing (deployed, commit `d832a5c`):
  1. CORS allow-list didn't cover `*.calltoarms.app` subdomains — fixed
     with a combined `allow_origin_regex` in `main.py`.
  2. Post-login redirect always went to root `FRONTEND_URL` instead of the
     subdomain that started the login — fixed via `_safe_return_to()` in
     `auth.py`, using the `Referer` header + a short-lived cookie.
- **Deferred, not blocking:** Safari/iPhone login testing (needs a real
  device). The "wrong club subdomain" redirect behaviour can't really be
  decided until Phase 1 introduces multiple clubs — not a bug right now,
  just not yet meaningful with a single club.

## Phase list

- [x] Phase -1 — subdomain + auth prototype
### Correction: frontend `.env` was pointed at production the whole time

Found 2026-07-13, after a real Discord post appeared during what was
believed to be local testing: `call-to-arms-web/.env`'s `PUBLIC_API_URL`
was set to `https://call-to-arms-api.fly.dev` (production), not
`http://localhost:8000`. This means:

- The earlier full click-through (main form, admin form, pre-arranged
  form, all three systems) that confirmed the frontend migration was
  actually hitting **production**, not staging — a stronger integration
  test than believed at the time, since it exercised the real deployed
  `/systems` endpoint against real production data.
- A one-off test signup+drop (Kill Team, real Discord post visible in the
  live server) landed in **production**, not staging. Confirmed clean
  afterward — `drop_signup`'s pre-publish path deletes the underlying row,
  and a direct query against production confirmed no leftover row.
- Fixed: `PUBLIC_API_URL` changed to `http://localhost:8000` in the local
  frontend `.env`, dev server restarted. Local testing from this point
  forward is genuinely local (staging DB via the backend's `.env`, no
  Discord webhooks configured locally).

**Done, 2026-07-13.** Real end-to-end staging test completed, after fixing
two more environment issues along the way: local frontend `.env` had been
reverted to production by Claude Code's own test cleanup (clobbering
Joel's manual edit), and two stale processes (an old `vite` dev server and
an old `uvicorn` from earlier in the day) were still holding ports
5173/8000 — killed both, plus a genuinely local Discord client secret was
needed (the earlier "working" login had actually been against production,
not local, which is what masked this until now). Once properly local:

- Baseline (flag off): submitted real signups for TOW/HH/KT (week
  `20/07/2026`) plus a Kill Team pre-arranged game — all matched expected
  hardcoded behavior, confirmed via direct staging DB checks.
- Flag on: repeated the identical three signups + pre-arranged game (week
  `21/07/2026`) — every result matched the baseline exactly, including
  `points=None` for the KT pre-arranged game.

This closes the last verification gap for Phase 0's dual-run
implementation. Staging is now fully confirmed at every layer: synthetic
(pairings_engine.py), logic-reasoned (signups.py), and now real end-to-end
signup submission through the actual local app.

### `systems_from_catalogue` flipped ON in production — 2026-07-13

Original doc plan called for waiting a full weekly/fortnightly cycle per
system, watching closely, before flipping in production. Revisited that
with Joel and deliberately shortened it: today's `signups.py` verification
was real end-to-end testing (not synthetic), so that side earned immediate
confidence. `pairings_engine.py`'s dual-run was synthetic (staging has no
real historical pairing data to test against), so its residual risk is
real but bounded — and since flipping the flag back off is instant and
free (old hardcoded path is untouched, still there), this isn't a
one-way-door decision.

**Revised plan, in place of the original "full cycle" wait:**
- Flag is live in production now.
- Watch the *next* real pairing generation for each system specifically
  (not a vague multi-week window): next TOW Wednesday, next KT Friday,
  next HH session. Sanity-check output against expectations.
- No urgency on removing the old hardcoded constants + the flag itself
  afterward — that's tidiness, not a deadline. Fine to leave the dual-run
  code in place indefinitely if there's ever doubt.

**Update, same day:** Joel decided to skip the "watch the next real pairing
generation" check entirely rather than wait on it — comfortable with the
verification already done (real end-to-end signup testing + synthetic
pairings dual-run + instant rollback safety net). Flag stays on in
production, no further scheduled check planned. If anything ever looks off
with pairings for any system, flip `systems_from_catalogue` back to
`false` in production — instant, no code change needed — and report back
what looked wrong.

- [ ] Phase 0 — systems-as-data refactor. **Deployed to production** (2026-07-13):
  backend committed + `fly deploy`'d, `systems` table created/seeded on
  production Supabase via `fly ssh console` (verification passed, same
  clean result as staging), frontend committed + pushed (Vercel
  auto-deployed). `systems_from_catalogue` flag is off in production by
  default (no row in production's `app_settings` yet) — **zero live
  behavior change so far**, old hardcoded paths are still what's actually
  running for real signups. Not yet done: flip the flag in production,
  watch a full cycle per system (HH's fortnightly cadence is the long
  pole — 2 weeks minimum), then remove the old hardcoded constants + the
  flag itself. `run_*_call_to_arms.py` scripts skipped per decision,
  `render_*_image.py` needs no change, `project-brief_1.md` not updated.
- [ ] Phase 1 — introduce clubs, club_id scoping. **Step 1 done** (2026-07-13,
  via Claude Code handoff): `Club`/`ClubSystem` models added, `clubs`/
  `club_systems` tables created + seeded on staging (Manchester + its 3
  system schedules, HH anchor pulled live from `run_hh_call_to_arms.py`
  not retyped), verified against `week_logic.py`'s actual scheduling
  logic. Confirmed inert — nothing else reads these tables yet, Phase 0
  untouched. Not committed/pushed yet. Not run against production yet.
  Next: add `club_id` to the 10 club-owned tables, table-by-table
  (expand/contract each), then the scoped-query helper. 88 query call
  sites across 10 files eventually need conversion — biggest phase in the
  plan, do NOT attempt in one handoff.

  **`app_settings` PK decision (2026-07-13):** its `key`-only PK doesn't
  cleanly extend to club scoping — some rows are genuinely global
  (`systems_from_catalogue`), others are really per-club
  (`auto_pairings_{slug}_enabled/day/time/last_week`), and a nullable
  `club_id` on one table wouldn't actually enforce uniqueness on the global
  rows (Postgres treats every NULL as distinct). **Decided: split into two
  tables** — `app_settings` stays global-only (key PK, just
  `systems_from_catalogue` for now), new `club_settings`
  (`(club_id, key)` composite PK) takes the per-club keys, migrating
  `auto_pairings_*` there. Matches the platform-vs-club split already
  planned for Phase 2's admin hierarchy. Not yet handed off — its own
  table-sized piece of work, not bundled with the tables below.

  **First table done: `pairing_blocks`** (2026-07-13, via Claude Code
  handoff, staging only, **not committed/pushed**). Chosen to go first as
  the smallest-surface table (`admin.py` + `pairings_engine.py` only,
  low-traffic admin feature) — proves the expand/contract mechanics before
  the bigger tables. `club_id` added to `PairingBlock` (nullable →
  backfilled → written on new rows via a `_default_club_id()` placeholder
  in `admin.py`'s `POST /admin/blocks` → made `NOT NULL`), via a new
  one-off script `add_club_id_to_pairing_blocks.py` (same pattern as
  `seed_clubs.py`). Verified: schema (column/FK/NOT NULL via
  `information_schema`/`pg_constraint`), a real new row created through the
  actual route (FastAPI `TestClient`, real staging DB) confirmed
  `club_id = 1`, and `/admin/blocks` GET/POST/DELETE plus
  `pairings_engine.py`'s block-exclusion query all exercised before/after
  with no behavior change. Read-path filtering deliberately untouched
  (deferred to the scoped-query-helper step).
  **Caveat, worth remembering for the next tables:** staging's
  `pairing_blocks` had zero existing rows, so the backfill step was a
  no-op — the `NOT NULL` contract succeeded by default, not because
  backfill-of-real-rows was actually proven correct. `signups`/`pairings`/
  `players` etc. almost certainly have real staging data, so their
  backfill step needs to be verified against actual rows, not just an
  empty table. Also confirmed for reuse: Manchester's `id` on staging is
  `1` (looked up by slug, not assumed — matches how the handoff asked for
  it).

  **Second table done: `players`** (2026-07-13, via Claude Code handoff,
  staging only, **not committed/pushed** — sits in the working tree
  alongside the other uncommitted Phase 0/1 work; standing instruction is
  only commit when explicitly asked). Same expand/contract shape as
  `pairing_blocks`: nullable `club_id` → backfill → write on the one
  insert site (`auth.py::create_profile`) → `NOT NULL`, via new script
  `add_club_id_to_players.py`.
  **Deviation:** staging's `players` table had only 2 rows (`Joel Kirk`,
  `Testy McTestface`), not the ~70 that describes *production* — same
  "staging data doesn't mirror production volume" gap as `pairing_blocks`,
  now confirmed as a pattern rather than a one-off. Both rows backfilled
  correctly (2/2, spot-checked by hand, no other fields touched). This
  means **no table's backfill step has yet been proven against
  realistic data volume** — worth being extra careful on production
  eventually (a much later step, not blocking now).
  **Included cleanup:** `_default_club_id()` moved out of `admin.py` into
  `database.py` as a shared helper (no circular import — `models.py`
  doesn't import `database.py`). Both `admin.py`'s `POST /admin/blocks`
  and `auth.py`'s `create_profile` now use the shared version;
  `pairing_blocks` creation regression-checked post-refactor, unaffected.
  **Verified:** schema (NOT NULL + FK via `information_schema`), a new
  profile created via the real `/auth/create-profile` route got
  `club_id=1`, and `GET /players`, `GET /players/{id}`,
  `POST /auth/create-profile`, `POST /auth/claim/{player_id}`,
  `PATCH /admin/players/{id}` (confirmed genuine partial-update — doesn't
  touch `club_id`), and `POST /admin/blocks` all exercised post-contract,
  all unaffected. Test rows cleaned up — staging back to exactly 2 real
  players / 2 real users / 0 pairing_blocks.
  **Noted in passing, not acted on:** `GET /players`'s response now
  includes `club_id` in the payload (comes along for free via
  `model_dump()` dumping all fields) — harmless with one club and no
  filtering added yet, but worth remembering this will keep happening by
  default on every table as `club_id` gets added, unless response models
  are made to explicitly exclude it later.

  **Third table done: `users`** (2026-07-14, via Claude Code handoff,
  staging only, **not committed/pushed**). Picked because the plan's
  locked decisions tie club membership directly to `users.club_id`.
  Same shape: nullable → backfill → write on the one upsert site
  (`auth.py::discord_callback`, `else`/new-user branch only) → `NOT NULL`,
  via new script `add_club_id_to_users.py`.
  **Verified:** schema (NOT NULL + FK), staging had exactly 2 users
  (Kirkboi, Testy Mctestface), both backfilled correctly, 0 NULLs after,
  count matches before/after, other fields spot-checked unchanged. New-user
  upsert branch gets `club_id=1`; existing-user branch leaves `club_id`
  untouched (confirmed on Kirkboi's real row — did bump `last_login_at` as
  an expected harmless side effect of exercising that path, same as a real
  login). `GET /auth/me`, `POST /auth/claim/{player_id}`,
  `POST /auth/create-profile`, `GET /admin/players` (both as non-admin →
  403 and as super-admin via an in-memory-only override, no DB mutation),
  and `GET /admin/grantable-users` all exercised post-contract, unaffected.
  Reconfirmed only `auth.py` writes `User` rows. No deviations beyond the
  now-expected small staging row count.

  **Fourth table done: `admin_roles`** (2026-07-14, via Claude Code
  handoff, staging only, **not committed/pushed**). Same shape as the
  prior three: nullable → backfill → write on the one grant site
  (`admin.py::grant_role`, `POST /admin/roles`) → `NOT NULL`, via new
  script `add_club_id_to_admin_roles.py`.
  **Deviation:** staging had 0 `admin_roles` rows (not 1-2 as guessed) —
  backfill was a no-op, same situation `pairing_blocks` was in. Route
  paths otherwise matched the handoff's guesses exactly
  (`POST`/`DELETE`/`GET /admin/roles`).
  **Verified:** schema (NOT NULL + FK), a role granted via the real
  endpoint got `club_id=1`, revoke needed no `club_id` handling, and a
  full round trip on a `require_scope`-gated endpoint
  (`GET /admin/pairings` → 403 with no role → grant → 200 → revoke → 403
  again) worked correctly post-contract. Reconfirmed only `admin.py`
  writes `AdminRole` rows.
  **Remaining 6:** `signups`, `pairings`, `publish_state`,
  `league_results`, `league_ratings`, plus the separate
  `app_settings`/`club_settings` split (its own handoff, not yet written).
  Flagged in passing: `league_results`/`league_ratings` write via a "full
  ratings recalc" rather than simple CRUD — per the plan, ELO must stay
  per-club and fully isolated, so those two may need more than the
  mechanical four-step pattern (the recalc logic itself likely needs
  club-scoping). Worth extra thought when we get there, not blocking now.

  **Fifth table done: `publish_state`** (2026-07-14, via Claude Code
  handoff, staging only, **not committed/pushed**). Same shape: nullable →
  backfill → write on both create sites (`admin.py::pairings_publish` and
  `run_auto_pairings_check.py`'s inline publish step) → `NOT NULL`, via new
  script `add_club_id_to_publish_state.py`.
  Staging had 0 rows — backfill was a no-op again. Both write sites
  produce `club_id=1`, confirmed the update-in-place branch (flip
  `.published`) never touches `club_id`. `run_auto_pairings_check.py` now
  has an explicit note that it has no per-club concept yet and will need
  real `club_systems`-based iteration once a second club exists — not done,
  correctly left out of scope.

  **Sixth table done: `signups`** (2026-07-14, via Claude Code handoff,
  staging only, **not committed/pushed**). First table with genuinely
  real staging data (4 rows, not 0) — all backfilled and spot-checked
  byte-for-byte (only `club_id` changed). Four write sites found: 3 create
  sites got `club_id` (`submit_signup`'s create branch, `submit_prearranged`'s
  `su_a`/`su_b`, `admin_signup_create`); `submit_prearranged`'s `Pairing(...)`
  write correctly left untouched (out of scope, `pairings` is next).
  Confirmed non-creating and needing no change: `pairings_save`
  (update-only), `swap_signups` (only touches `Pairing`, not `Signup`),
  `drop_signup` (deletes `Signup`, but — important for the next
  handoff — creates a `Pairing` row in one branch).
  **Pairing-creation sites now known ahead of the `pairings` handoff**
  (compiled by grepping `Pairing(` across the whole codebase, not just
  from what Claude Code reported): `signups.py` has 5 —
  `drop_signup` (1, a BYE for the displaced opponent),
  `submit_prearranged` (1), `swap_signups` (3 — the new prearranged
  pairing plus up to two BYE pairings for displaced players). Separately,
  `pairings_engine.py::generate()` has 3 (intro pre-pass match, main
  match, BYE fallback) — all three already guarded by `if persist:` and
  structurally identical, reachable via a single club_id resolved once at
  the top of `generate()` (same pattern already used there for `config`).
  `generate(persist=True)` has exactly two callers:
  `admin.py::pairings_generate` and `run_auto_pairings_check.py`'s
  scheduler — resolving `club_id` inside `generate()` itself covers both
  automatically. **`admin.py` never constructs `Pairing` directly** — all
  its ~30 references are reads/updates/deletes.

  **Seventh table done: `pairings`** (2026-07-14, via Claude Code handoff,
  staging only, **not committed/pushed**). Biggest table yet by write-site
  count: all 8 sites got `club_id` — 5 in `signups.py`
  (`drop_signup`, `submit_prearranged` — the one deliberately left off in
  the `signups` handoff, and `swap_signups` ×3), and 3 in
  `pairings_engine.py::generate()` (intro pre-pass, main match, BYE),
  resolved once and reused, not resolved three times. Staging had 1 real
  row (a prearranged Kill Team pairing), backfilled and byte-for-byte
  verified. **Algorithm safety confirmed**: `git diff pairings_engine.py`
  showed exactly 5 additions (1 import, 1 resolution line, 3 field
  additions) — zero changes to matching logic, `_pair_dist` order, or BYE
  handling. `generate()`'s actual output re-verified algorithmically
  correct on a contrived 5-signup scenario (intro match + main match +
  BYE, exactly as expected). Full endpoint sweep pre/post-contract, all
  pass. Temporary `Player` rows (prefixed `ZZTest`) used for multi-player
  scenarios since staging's real player pool is only 2 — cleaned up after.

  **Eighth table done: `league_results`** (2026-07-14, via Claude Code
  handoff, staging only, **not committed/pushed**). Mechanically simple —
  one creation site (`league.py::submit_result`) — but flagged with a real
  correctness note, not just a deferred read-scoping gap like other
  tables: `_recalculate_ratings()` replays **every** `LeagueResult` row
  with no club filter. Harmless with one club; once a second club exists
  it will silently blend both clubs' games into one shared ELO pool,
  violating the locked "ELO stays per-club, fully isolated" decision.
  **Tracked explicitly as a must-fix-before-Phase-4 item** (not folded
  into the general scoped-query-helper backlog) — deliberately not fixed
  yet, same deferral reasoning as every other table's read-path scoping.
  Staging had 0 rows (no-op backfill). `LeagueRating`/`_recalculate_ratings`'s
  `LeagueRating` construction confirmed untouched (`git diff` shows only
  the import + one field addition). Full endpoint sweep incl. the
  duplicate-guard path, pre/post-contract, all pass.
  **Remaining 2:** `league_ratings`, and the `app_settings`/
  `club_settings` split.

  **Ninth table done: `league_ratings`** (2026-07-14, via Claude Code
  handoff, staging only, **not committed/pushed**). `club_id` written
  inside the single shared `_recalculate_ratings()` (resolved once, right
  before the rebuild loop, covering all 3 callers). Staging had 0 rows
  (derived from `league_results`, also 0) — no-op backfill.
  **Correctness flag consolidated with `league_results`'s:**
  `_recalculate_ratings()` has two unfiltered global queries — the
  `LeagueResult` replay (flagged last table) and, confirmed this table,
  an unfiltered `select(LeagueRating)` **delete-all** before every
  rebuild. The second one is actively destructive, not just under-scoped:
  once a second club exists, that club submitting any result would wipe
  the first club's entire ratings table before rebuilding. Both must be
  fixed together, before Phase 4, as part of the scoped-query helper —
  not generic backlog.
  **All 10 "regular" club-owned tables now have `club_id` except the
  `app_settings`/`club_settings` split**, which is its own separate,
  final handoff (not yet written) — the last piece of "add club_id to
  every table" before the scoped-query helper phase begins.

  **`app_settings`/`club_settings` split done** (2026-07-14, via Claude
  Code handoff, staging only, **not committed/pushed**) — closes out all
  10 tables. New `club_settings` table (composite `(club_id, key)` PK,
  added to `WRITE_ALLOWED_TABLES`) created; both `admin.py`'s and
  `run_auto_pairings_check.py`'s duplicate `_get_setting`/`_upsert_setting`
  helper pairs repointed at it via `_default_club_id(db)`.
  **Deviation:** staging's `app_settings` had exactly 1 row
  (`systems_from_catalogue`) — zero `auto_pairings_*` rows existed to
  migrate, so that part of the migration was a no-op (not a failure, just
  nothing there yet on staging). `systems_from_catalogue` confirmed
  byte-identical throughout, still reads from `app_settings` directly.
  Cleanup (`DELETE FROM app_settings WHERE key LIKE 'auto_pairings_%'`)
  affected 0 rows as expected; final `app_settings` state is exactly the
  1 `systems_from_catalogue` row.

  ---

  **MILESTONE: all 10 club-owned tables now have `club_id`, expand/backfill/
  dual-run/contract complete on staging, for all of it.** Summary of the
  9 "regular" tables + the split, in order done: `pairing_blocks`,
  `players`, `users`, `admin_roles`, `publish_state`, `signups`,
  `pairings`, `league_results`, `league_ratings`,
  `app_settings`/`club_settings` split. **Everything is still sitting
  uncommitted in the working tree, staging-only, per standing
  instruction** — worth deciding whether to commit now that this chunk of
  work is complete, rather than let 10 tables' worth of changes keep
  piling up uncommitted.

  **Two things carried forward, not yet done:**
  1. **Must-fix-before-Phase-4:** `_recalculate_ratings()`'s two
     unfiltered global queries (`LeagueResult` replay, `LeagueRating`
     delete-all) — flagged in the `league_results`/`league_ratings`
     handoffs. Real correctness bug once a second club exists (blends/wipes
     ELO across clubs), not just a read-visibility gap.
  2. **The scoped-query helper itself** — resolving the caller's club_id
     in the auth layer, and converting the ~88 query call sites across
     ~10 files to use it. This is the next major piece of Phase 1 work,
     separate from and larger than everything done so far. Not started,
     not yet planned in detail.
- [ ] Phase 2 — admin hierarchy
- [ ] Phase 3 — per-club Discord + public page scoping
- [ ] Phase 4 — second club onboarding
