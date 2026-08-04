# Known issues

Real, understood problems that were deliberately **not** fixed, with the
reasoning and what would change the decision. This is not a bug tracker for
new discoveries — it is a record of accepted trade-offs, so nobody re-derives
the same analysis or "fixes" something that was left alone on purpose.

Each entry: what you'd see, why it happens, why it was left, and the trigger
that should make someone revisit it.

---

## 1. Discord mentions can render as `@unknown-user`

**Symptom.** A signup/pairing/call-out post shows `Player Name (@unknown-user)`
instead of the player's Discord handle.

**Cause.** Mentions are built from `Player.user_id → User.discord_id` — the
account the player *logged into the app with*. Discord's client resolves
`<@id>` against the membership of the server the webhook posts into. If those
don't line up, it renders `@unknown-user`. Two ways that happens:

- the player isn't in the club's Discord server at all, or
- the player is in the server under a **different account** from the one they
  use to log in to the app.

The stored ID is not corrupt — in the observed case the snowflake was valid
and the account was live (confirmed via `GET /users/{id}` with the bot token).

**Why it's left.** The tempting fix — repointing `User.discord_id` at the
account that's in the server — **breaks that person's login.** `User.discord_id`
is the unique key `auth.discord_callback` matches on, so after such an edit
their next login finds no user, creates a fresh one, and prompts them to make
a new profile, orphaning their existing player row and history. That is a far
worse outcome than a cosmetic label.

The real resolution isn't ours to make: the player lines their two accounts up
(joins the server with the account they use for the app, or logs in with the
account already in the server). No data change required.

**Related consequence for the Discord membership gate.** The gate checks guild
membership *per account*, not per person. Someone in this situation is a
genuine false positive: they really are in the club's Discord, just not with
the account the app knows. The prompt still tells them something actionable
("join the Discord" → for them, "log in with the other account"), and the gate
fails open on any undetermined answer, but don't treat the check as a perfect
signal. An argument for staying on `monitor` a good while before `enforce`.

**Revisit when.** The bot is actually in a club's Discord server. At that point
the graceful fix becomes possible: tag only players confirmed to be guild
members and fall back to a plain bold name for everyone else, so
`@unknown-user` can never appear. That degrades display without touching
anyone's identity. Pair it with a one-off audit listing which existing linked
players aren't in the server — that list is the actionable output, and note
that **`monitor` mode will not produce it**, because `require_discord_member`
short-circuits on any player with `discord_verified_at` set and every existing
player was grandfathered.

**Code.** `database.discord_mentions_for_player_ids` / `database.name_with_mention`,
`signups.require_discord_member`, `discord_guild.is_guild_member`.

---

## 2. Admin page can 500 in a burst: `SSL connection has been closed unexpectedly`

**Symptom.** Loading the admin page occasionally throws a wave of
`psycopg2.OperationalError: SSL connection has been closed unexpectedly`,
always on `SELECT users WHERE id = ?` — the first query of every authenticated
request. It self-recovers within about a minute.

**Cause.** `database.py` builds the engine with `poolclass=NullPool` against
Supabase's **transaction pooler** (port 6543), so every request opens a brand
new connection. The admin page fires roughly **40 requests in parallel** on
load, which means ~40 simultaneous fresh pooler connections. The pooler runs
out and drops them.

**Why it's left.** It's admin-only (a player's signup flow makes a handful of
requests, not 40), it self-heals, and it loses no data. The fix — a bounded
client-side pool, e.g. `pool_size=5, max_overflow=10, pool_pre_ping=True,
pool_recycle=300` — touches every database call in the application, so it
deserves a deliberate session with staging verification rather than being
tacked onto a feature. It would also make that page noticeably faster by
reusing connections instead of paying a TLS handshake per request. Client-side
pooling is safe with the transaction pooler here: psycopg2 doesn't use
server-side prepared statements, which is the usual transaction-mode hazard.

**Revisit when.** It recurs, a *player* hits it rather than only an admin, or
anything new is added to the admin page's parallel load. Reducing that
~40-request fan-out is the deeper fix; the pool change makes it survivable
either way.

**Code.** `database.py` engine construction; the fan-out is in
`call-to-arms-web/src/routes/admin/+page.svelte`.

---

## 3. `POST /signups/prearranged` doesn't check the caller is one of the players

**Symptom.** None visible. Any logged-in member of a club can create a
pre-arranged game between two *arbitrary* players at that club.

**Cause.** The endpoint authenticates the caller (`require_user`) and validates
that both players belong to the caller's club, but never checks that the caller
is one of them — `player_a_id` and `player_b_id` come straight from the request
body.

**Why it's left.** Same class as the nine missing-ownership bugs found and
fixed during the Phase 1 club-scoping work, but with no cross-club exposure:
everything stays inside the caller's own club, and the blast radius is a
nuisance signup that any admin can delete. Found while adding the Discord gate
and deliberately kept out of that change's scope.

**Revisit when.** Anyone touches this endpoint, or a club reports unexpected
pre-arranged games. The fix is small — compare `active_player_id_for(db, user,
club_id)` against `player_a_id`/`player_b_id` and 403 otherwise — but it needs
a decision first on whether an admin should still be allowed to arrange games
on others' behalf.

**Code.** `signups.submit_prearranged`.
