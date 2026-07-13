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

**Confirmed behavior change (Part 3, in progress):** `preShowPoints` on
the pre-arranged form was hardcoded to TOW-only, while HH's `uses_points`
is also `true` everywhere else in the app. User's call: this was an
oversight — HH pre-arranged games should show points too. Handoff written
(`HANDOFF_FRONTEND_PART3.md`) to make this change and confirm the
submit handler actually sends the value for HH, not just the input's
visibility.

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
- [ ] Phase 0 — systems-as-data refactor (in progress: table seeded and verified on staging; flag + signups.py + pairings_engine.py done, dual-run verified synthetically not against real data; render_*_image.py needs no change; run_*_call_to_arms.py scripts + frontend not started; nothing committed yet)
- [ ] Phase 1 — introduce clubs, club_id scoping
- [ ] Phase 2 — admin hierarchy
- [ ] Phase 3 — per-club Discord + public page scoping
- [ ] Phase 4 — second club onboarding
