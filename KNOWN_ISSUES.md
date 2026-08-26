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

## 2. Supabase's pooler caps us at ~16 concurrent connections

**Symptom.** Under a large enough burst of simultaneous requests, some fail —
historically `psycopg2.OperationalError: SSL connection has been closed
unexpectedly`, later `TimeoutError: QueuePool limit ... reached`. Both are the
same underlying wall seen from different sides.

**Cause.** Measured from the Fly machine against the prod pooler, holding N
connections simultaneously for 2s: 12 → 24/24 clean, 16 → 32/32 clean, 20 →
39/40, 30 → 56/60, 100 → 89/100. **Past roughly 16 the pooler starts dropping
connections**, and what it returns is the SSL error. So the ceiling is
Supabase's, not ours, and no pool size can exceed it.

Compounding it: a connection is acquired during FastAPI **dependency
resolution** (`require_user` does `db.get(User, ...)`) and held until
`get_session`'s generator tears down after the response. A request therefore
holds its connection while merely *queued* for a thread, so concurrent
checkouts track **in-flight requests**, not executing handlers. Sizing a pool
against the AnyIO threadpool limit is wrong for this reason — it was tried, and
it 500'd.

**Current state — not currently firing.** `database.py` uses
`pool_size=10, max_overflow=4` (=14, under the ceiling, leaving headroom for
cron processes), `pool_pre_ping`, `pool_recycle=300`, `pool_timeout=30`. Warm
is deliberately most of the capacity: a reused connection can't be refused.
`--limit-concurrency=64` in the Dockerfile is a safety valve for genuine
overload — **not** a throttle; at 32 it rejected 87 of 120 requests with hard
503s, which is worse than what it prevents. And the admin page no longer
prefetches every system (`ensureSystemScope`), which removed the burst that
caused all of this.

**Why it's left.** Nothing to fix while we stay under the ceiling. It's
recorded because the ceiling is invisible, low, and the error it produces looks
like a network fault rather than a limit.

**Revisit when.** The SSL or QueuePool error reappears; anything is added to
`initSystemScope` (its cost multiplies by the number of systems a club runs);
uvicorn gains `--workers` (the pool is per-process, so capacity must be divided
between them); or the Supabase plan changes, which would move the ceiling —
re-measure rather than assume.

**Code.** `database.py` (pool), `Dockerfile` (limit-concurrency),
`call-to-arms-web/src/routes/admin/+page.svelte` (`ensureSystemScope`).

---

## 3. A taken call-out books no table

**Symptom.** Two players agree an ad-hoc game through a call-out — one system,
one date, one time, both named — and the venue side knows nothing about it. No
booking, no table, nothing on the diary. They turn up to a room that isn't
expecting them.

**Cause.** `CallOut` has no link to a table or a booking, and no place field at
all: the model carries `game_at` and nothing about where. Nothing joins the two
halves of the product here.

**Why it's left.** Deliberate, decided 2026-08-26. Booking a table
automatically when a call-out is *posted* would hold a table for a game that may
never be taken up, which costs the venue exactly the thing the booking feature
exists to sell. Booking on *take-up* is more defensible but still guesses at a
duration, a table size and a venue the call-out never named — and a booking made
on someone's behalf that they then can't find is worse than no booking.

Instead the call-out form says plainly that a call-out reserves nothing, and
links to the booking page. The prompt is the honest version of the connection.

**Revisit when.** Call-outs get a place/venue field, or venues start reporting
turn-ups they weren't expecting. If it's built, the natural shape is a prompt on
*take-up* that pre-fills the booking form rather than anything automatic.

**Code.** `models.py::CallOut`, `call_outs.py`,
`call-to-arms-web/src/routes/signup/+page.svelte` (the `.callout-book` prompt).
