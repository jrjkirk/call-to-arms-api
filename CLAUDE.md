# Call to Arms — API Backend

## Stack & architecture

```
Browser → Vercel (SvelteKit, ~/projects/call-to-arms-web) → Fly.io (FastAPI, this repo) → Supabase Postgres
```

- **Backend:** FastAPI + SQLModel, deployed to Fly.io
  - Live: https://call-to-arms-api.fly.dev
  - Interactive docs: https://call-to-arms-api.fly.dev/docs
  - GitHub: github.com/jrjkirk/call-to-arms-api
- **Database:** Supabase Postgres, connected via transaction pooler (`DATABASE_URL`)
  - We don't manage migrations here — schema source of truth is the Streamlit app (for now)
  - SQLModel models in `models.py` mirror the Supabase schema exactly
- **Auth:** Discord OAuth2, stateless HMAC session cookie (`cta_session`)
  - `SameSite=None; Secure` — cross-site from vercel.app → fly.dev

## File map

| File | Purpose |
|---|---|
| `main.py` | App entrypoint, mounts routers, CORS |
| `database.py` | Engine, `get_session` dependency, `WRITE_ALLOWED_TABLES` guard |
| `models.py` | SQLModel table definitions |
| `auth.py` | Discord OAuth, session cookie helpers, `require_user` / `current_user` deps, auth endpoints |
| `signups.py` | Signup CRUD endpoints |
| `players.py` | Player read endpoints |
| `league.py` | Rankings, results endpoints |
| `admin.py` | Admin role management, blocks, history, and pairings generation endpoints |
| `pairings_engine.py` | Pairing generation engine — faithful port of the original Streamlit matcher |
| `scheduler.py` | In-process tick loop for the five periodic jobs. **Off unless `SCHEDULER_ENABLED` is set** — local `.env` points at a real DB with real webhooks |

## Directory layout

The live app's own code (everything FastAPI imports at runtime) stays flat
at repo root: `main.py`, `admin.py`, `auth.py`, `league.py`, `signups.py`,
`services.py`, `database.py`, `models.py`, `week_logic.py`,
`call_to_arms_content.py`, `pairings_engine.py`, `scheduler.py`, plus the
`systems/`, `icons/`, `missions/` asset/rule directories.

**`scripts/` is no longer inert.** It used to be true that the live app
imported nothing from the subdirectories below; since the scheduler moved
in-process (2026-08-31), `scheduler.py` imports the five `run_*_check`
entry points. Two consequences worth knowing:

- `scripts/` is a real package (`__init__.py`), not an implicit namespace
  one, so the import doesn't depend on the working directory.
- Modules in `scripts/` must import each other **qualified**
  (`from scripts.render_pairings_image import ...`), never as bare
  siblings. A bare sibling import works when the file is run as a script
  and fails when it's imported as a package — which is a failure that only
  shows up with the scheduler switched on.

Standalone scripts, grouped by purpose (`migrations/` and `seed/` are never
imported by the live app; `scripts/` now is — see above):
- `migrations/` — one-off, already-run schema migration scripts
  (`add_club_id_to_*.py`, `create_club_settings_table.py`,
  `add_is_platform_admin_to_users.py`). Kept as historical record, not a
  live migration tool — see `models.py`'s docstring.
- `seed/` — one-off/idempotent data-seeding scripts (`seed_clubs.py`,
  `seed_club_webhooks.py`, `seed_systems_config.py`).
- `scripts/` — the five periodic job entry points and the render helpers
  they use (`run_auto_pairings_check.py`, `run_call_to_arms_check.py`,
  `run_call_outs_check.py`, `run_table_booking_cutoff_check.py`,
  `post_pairings_image.py`, `post_league_rankings_image.py`,
  `render_pairings_image.py`, `render_league_rankings_image.py`). Each
  `main()` runs three ways and must keep working in all of them: by hand,
  from a GitHub workflow, and from `scheduler.py`. The workflows set
  `PYTHONPATH: ${{ github.workspace }}` so the scripts' `from database
  import ...`-style repo-root imports resolve despite living one directory
  down; by hand you must set it yourself.

**Running any of these by hand:** since they import repo-root modules
(`database`, `models`, etc.), always set `PYTHONPATH` to the repo root
first, e.g. from `~/projects/call-to-arms-api`:
```bash
PYTHONPATH=. python migrations/add_club_id_to_users.py
PYTHONPATH=. python seed/seed_clubs.py --verify-only
PYTHONPATH=. python scripts/post_pairings_image.py
```
Running with a bare `python migrations/foo.py` (no `PYTHONPATH`) will fail
with `ModuleNotFoundError: No module named 'database'` — Python only adds
the *script's own* directory to `sys.path`, not the repo root.

## Auth patterns

```python
# Optional auth (returns None if not logged in)
user: Optional[User] = Depends(current_user)

# Required auth (raises 401 if not logged in)
user: User = Depends(require_user)
```

Both are defined in `auth.py` and resolve from the `cta_session` HMAC cookie.

## ⚠️ Shared Discord secret (local + production)

`DISCORD_CLIENT_ID` / `DISCORD_CLIENT_SECRET` belong to a single Discord
application whose Redirects list covers both `localhost` and
`calltoarms.app`/`fly.dev` — **local dev and production share the same
client secret.** There is no separate "local" secret.

**Incident, 2026-07-13:** the secret was reset in the Discord Developer
Portal to get local login working, without also updating the Fly secret.
Discord invalidates the old secret the instant you reset it, so production
silently broke (`invalid_client` on every `/auth/discord/callback`) until a
player reported it.

**Rule: any time `DISCORD_CLIENT_SECRET` is reset or rotated, update BOTH in
the same sitting:**
```bash
# local — edit .env
DISCORD_CLIENT_SECRET=<new value>

# production
fly secrets set DISCORD_CLIENT_SECRET=<new value> -a call-to-arms-api
```
Worth considering a second, separate Discord application for local dev to
remove this failure mode entirely (not done yet).

## ⚠️ WRITE_ALLOWED_TABLES guard

`database.py` registers a SQLAlchemy `before_flush` listener that raises `RuntimeError` for any write to a table not in `WRITE_ALLOWED_TABLES`. This is a safety net while the migration from Streamlit is in progress.

**Before writing to any table, add it to `WRITE_ALLOWED_TABLES` with a comment explaining the use case.**

The allow-list lives in `database.py` (each entry carries an inline comment on
its use case) — read it there rather than duplicating it here, so it can't
drift out of date.

## Dev loop

```bash
cd ~/projects/call-to-arms-api
source .venv/bin/activate
uvicorn main:app --reload   # http://localhost:8000
```

Frontend `.env` must point at local API:
```
PUBLIC_API_URL=http://localhost:8000
```

## Deploy

```bash
git push                    # push to GitHub
fly deploy                  # deploy to Fly.io (takes ~1 min)
fly logs                    # stream live logs / tracebacks
fly status                  # machine health
```

**When a change spans both repos, deploy backend first, then frontend.**

## Conventions

- Endpoint handlers follow the pattern in `auth.py::claim_player` — check preconditions, raise `HTTPException`, mutate, `db.commit()`, `db.refresh()`, return dict.
- Use `db.flush()` before linking foreign keys in the same transaction (to populate auto-generated IDs).
- All new write endpoints need the table in `WRITE_ALLOWED_TABLES` first.
- Request bodies use Pydantic `BaseModel` (not SQLModel) — keep input schemas separate from table models.
- **Any value that came from a person must go through `emailer.esc()` before it
  is interpolated into HTML.** Every HTML body here is built with f-strings, and
  none of it was escaped: a booker's name, phone and notes arrive from a public
  form with no account, and a player's name is whatever they typed at signup.
  `admin/table-booking/preview` also returns that HTML to the admin page, which
  renders it with `{@html}`, so an unescaped name ran in a club admin's browser.
  Do NOT escape email *subjects* — they are plain text, and `&amp;` showing up in
  a subject line is its own bug. Covered by `tests/test_email_escaping.py`.

## Pairing engine (pairings_engine.py)

`pairings_engine.py` is a **faithful port** of the original Streamlit matcher. Do not reorder,
"optimise", or change the algorithm logic without explicit instruction.

Key invariants:
- **`_pair_dist` returns `(last_opp_pen, block_pen, weighted_score)`.** `last_opp_pen`/`block_pen` stay hard, unconfigurable top-priority filters (admin blocks / "don't repeat last week's opponent"). `weighted_score` linearly combines the soft factors (mirror, rematch, vibe, experience, eta, scenario, points) using per-(club,system) weights from `PairingConfig` (`models.py`), editable via admin UI sliders — `GET/POST /admin/pairing-config`. Do not change how `last_opp_pen`/`block_pen` dominate without explicit instruction.
- Intro pre-pass applies to The Old World and The Horus Heresy only (never Kill Team)
- T&T / 3-way grouping intentionally removed (club never uses it)
- Odd numbers produce a single BYE via the greedy fallback — this is correct behaviour
- Cron/scheduling is out of scope; the engine is invoked only by admin HTTP endpoints

Admin pairings endpoints (all in `admin.py`, all require caller to hold the system scope):
- `POST /admin/pairings/preview` — dry run, no DB writes
- `POST /admin/pairings/generate` — delete pending non-prearranged, generate + persist
- `GET /admin/pairings?system=&week=` — fetch saved rows + publish state
- `POST /admin/pairings/publish` — upsert PublishState
- `POST /admin/pairings/save` — grid save-back (writes faction/vibe/eta/pts to Signup rows too)
- `DELETE /admin/pairings` — delete specific pairing IDs
- `POST /admin/pairings/post-discord` — plain-text post to system Discord webhook
- `GET /admin/pairings/signup-list?system=&week=` — de-duped signup list for grid dropdowns

## Known issues

`KNOWN_ISSUES.md` records real, understood problems that were **deliberately
left unfixed**, with the reasoning and the trigger that should make someone
revisit each one. Read it before "fixing" something that looks broken — and
before re-analysing a symptom listed there. Add to it when a problem is
consciously accepted rather than solved.

## When things break

**Crash loop on Fly.io:**
```bash
fly logs            # find the traceback
fly scale count 0   # kill the machine
# fix the bug, commit, push
fly scale count 1
fly deploy
```

**`RuntimeError: Write to 'X' is not currently permitted`:**
Add `"X"` to `WRITE_ALLOWED_TABLES` in `database.py` with a comment.
