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

## Auth patterns

```python
# Optional auth (returns None if not logged in)
user: Optional[User] = Depends(current_user)

# Required auth (raises 401 if not logged in)
user: User = Depends(require_user)
```

Both are defined in `auth.py` and resolve from the `cta_session` HMAC cookie.

## ⚠️ WRITE_ALLOWED_TABLES guard

`database.py` registers a SQLAlchemy `before_flush` listener that raises `RuntimeError` for any write to a table not in `WRITE_ALLOWED_TABLES`. This is a safety net while the migration from Streamlit is in progress.

**Before writing to any table, add it to `WRITE_ALLOWED_TABLES` with a comment explaining the use case.**

Current allow-list (in `database.py`):
- `"users"` — created on Discord login, updated on claim/create-profile
- `"signups"` — signup CRUD
- `"pairings"` — drop-out flow deletes prearranged pairings
- `"players"` — new player creation via `POST /auth/create-profile`

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
