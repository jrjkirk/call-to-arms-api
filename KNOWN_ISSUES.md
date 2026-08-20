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

## 2. Admin page fires ~40 requests in parallel on load

**Symptom.** The admin page is slower to load than the rest of the app, and
anything added to its per-system fan-out multiplies by the number of systems a
club runs.

**Cause.** `initSystemScope(scope)` runs once per game night and `Promise.all`s
about ten fetches inside it. A club running six systems therefore issues ~60
requests where the code reads as ten. This has bitten twice: the per-system
Discord gate panel turned one Discord API call into six (rate-limited, fixed by
caching in `discord_guild.py`), and `loadCarousel` refetched the entire `/club`
payload once per system (fixed by sharing one in-flight promise).

**No longer a 500.** This entry used to describe a wave of
`psycopg2.OperationalError: SSL connection has been closed unexpectedly`,
caused by `NullPool` opening ~40 brand new pooler connections at once. The
engine now uses a bounded pool (`pool_size=5, max_overflow=10, pool_pre_ping`),
which absorbs the burst — measured against prod, 40 parallel queries complete
in 0.59s using 5 connections, and per-request latency halved (0.77s → 0.38s)
now that each one no longer pays a TLS handshake.

**Why it's left.** What remains is a design smell, not a fault: the page works
and is reasonably quick. The deeper fix is to stop prefetching every system on
load — either lazy per-tab loading, or one batched admin-bootstrap endpoint.

**Revisit when.** Anything new is added to `initSystemScope` (check what it
costs times the number of systems first), or a club grows enough systems that
load time becomes noticeable again.

**Code.** The fan-out is in `call-to-arms-web/src/routes/admin/+page.svelte`;
the pool is in `database.py`.
