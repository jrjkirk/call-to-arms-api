# Account overhaul: unpicking Discord from identity

**Status:** Slabs 0, 1 and 2 LIVE (2026-09-15). Slab 3 BUILT, not deployed. Audit taken 2026-09-14, re-checked
against code and the prod schema 2026-09-15 (see §2b for what changed and what the
first pass missed). The four decisions in §8 are **settled**, plus four follow-ups.

**Goal:** let people have an account without Discord (Google, email magic link), plus
real account management: a display name you own, linked emails, linking/unlinking Discord,
and account recovery.

Line numbers drift. Re-run the commands in "Reproducing the sweep" before trusting any
`file:line` below.

---

## 1. "Discord" is three unrelated systems

672 `discord` references in the API's Python (2026-09-15; 667 on 09-14). They split like this:

| System | Rough size | What it is | Affected? |
|---|---|---|---|
| **Club output channel** | ~327 refs (`webhook`) | Per-club, per-system webhook URLs that pairings, signups, call-to-arms, league posts go to | **No.** A club posts to Discord whether or not any player signs in with it. Leave it alone. |
| **User identity** | ~110 refs (excl. tests/migrations) | `users.discord_id`, `users.discord_name`, the OAuth flow in `auth.py` | **Yes.** This is the whole real surface. Most of it is `discord_name` used as a display name. |
| **Verification source** | small | The guild gate: "is this player in our server?" (`discord_guild.py`, `signups.py`). Benched behind `DISCORD_GATE_ENABLED`. | Settled by Decision A: players with no Discord pass. |

---

## 2. Blockers: must change before a non-Discord account can exist

- **`models.py:330-331` — schema.** `discord_id: str = Field(unique=True, index=True)` is
  NOT NULL; `discord_name: str` is NOT NULL. Confirmed in prod 2026-09-15
  (`users_discord_id_key` UNIQUE; `club_id` is NOT NULL too). A non-Discord user can't be a
  row. Postgres allows multiple NULLs under a unique constraint, so dropping NOT NULL on
  `discord_id` is a clean ALTER that keeps the index. Per repo convention the ALTER is a
  hand-run script in `migrations/`, and prod is the first place it runs (staging DB is gone).

- **`auth.py:419-420` — THE TRAP.** Every returning login runs
  `existing.discord_name = discord_name` and `existing.avatar_url = avatar_url`. Correct
  today (the column mirrors Discord). The day a "change your display name" or custom avatar
  feature ships, this silently reverts the user's choice on their next sign-in. Any name or
  avatar the user owns must live somewhere this sync doesn't write. **Resolution (Decision
  B):** `discord_name` keeps meaning "Discord handle" and keeps syncing; the user-owned name
  is a new `users.display_name` that no provider writes.

- **`auth.py:115`, `auth.py:130`, `auth.py:166` — Discord-shaped half-signed-in identity.**
  `_make_pending_signup_cookie(discord_id, discord_name, avatar_url)` and
  `requester_identity` (returns `{discord_id, discord_name, user_id}`). Both need a
  provider-neutral payload: `{provider, provider_user_id, name, avatar_url, email}`.
  Callers: `auth.py:195`, `auth.py:408`, `auth.py:520`, `main.py:641-642`.

- **`models.py:1225` · `main.py:697,712-713` · `admin.py:5495-5500` — club requests.**
  `ClubRequest.discord_id` is the requester's identity (the organiser has no User row yet,
  see the users.club_id bootstrap problem in `requester_identity`'s docstring). Duplicate
  detection queries it; provisioning builds `User(discord_id=req.discord_id, ...)` when
  appointing the super-admin. All three must key on an identity instead.

- **`auth.py:75` — no email held for anyone.** `SCOPES = "identify"`. There is no email
  address for any existing user. **Decision D: stays that way.** Discord accounts get an
  email only when their owner adds Google or an email address from `/account`.

---

## 2b. Re-check, 2026-09-15

### Changed since 09-14
- `requester_identity` moved `auth.py:163` → `:166`; the `complete_signup` caller `:517` →
  `:520` (the archived-claim change added three lines). Everything else still points at the
  right code.
- Stale counts: ~~102 players~~ → 152 players, 105 linked and active, 111 users (106 signed
  in within 90 days), 0 users owning more than one player. ~~16 web routes~~ → 18.
- `auth.py`'s COOKIE NOTE says every auth cookie is `secure=True`. The three
  `cta_oauth_*` cookies (`auth.py:318-320`) are not. A new provider module would copy it.

### Missed by the first pass
1. **Sessions cannot be revoked.** `cta_session` is `user_id.hmac` with no server state;
   logout deletes the browser's copy only. Someone who hijacked a Discord account keeps a
   working session for up to 30 days and nothing can end it. Recovery and unlink both need
   this. → Slab 0 (`users.session_version`).
2. **No user merge exists.** Slab 5 assumed one. Only player merges exist
   (`migrations/merge_duplicate_players.py`, `merge_ian_players.py`). A user merge must move
   every user-id column: `players.user_id`, `users.player_id` back-links, `admin_roles.user_id`,
   `venue_staff.user_id`, `venue_bookings.user_id` + `cancelled_by_user_id`,
   `tournament_entries.user_id`, `tournaments.created_by_user_id`,
   `tournament_games.reported_by_user_id`, `venue_events.approved_by_user_id` +
   `created_by_user_id`, `audit_log_entries.actor_user_id`, `club_requests.requester_user_id`
   + `reviewed_by_user_id`, plus reconcile `is_super_admin`, `is_platform_admin`, `club_id`,
   `home_club_id` and one-player-per-club collisions. Already needed: 6 accounts own no
   player, Shaun has two or three Discord accounts. → its own slab, before link/unlink.
3. **`KNOWN_ISSUES.md` #1 (`@unknown-user`) is the same problem as linking Discord.** Once
   identities are separate, the Discord ID used for mentions and the guild gate must have one
   authoritative source, or unlinking leaves `users.discord_id` still pinging and still passing
   the gate as the removed account. → Slab 0: `user_identities` is authoritative; see §7.
4. **`/auth/me` serialises the raw `User` row** (`auth.py:487`, `:568`). Any column added to
   `users` reaches the browser automatically. → Slab 0: explicit response shape.
5. **Nothing is rate-limited anywhere in the API.** Slab 4 (magic link) needs it built.
6. **`user_identities` as proposed had no `email_verified`.** Linking and recovery must only
   trust verified addresses. → added.
7. **More `discord_name`-as-handle sites.** The web UI uses it to mean "their Discord
   handle", which is why it must not become the display name (Decision B):
   `admin/+page.svelte` roles and super-admin lists, Players tab `@name` / "not linked"
   (`:5456`), `displayName()` (`:3489`). Copy: `signup/+page.svelte:825`,
   `claim/+page.svelte:98,123`, `request-club/+page.svelte:87-88,225`, privacy page
   third-parties paragraph (`:70-72`, says Discord is the only external service).
   API: club-request notification email (`main.py:541`), `reviewed_by_name`
   (`admin.py:5363,5619`), audit-log detail strings (`admin.py:292,314,4258,4347,5552`),
   grantable-user lists (`admin.py:246,4319`).
8. **Supabase Auth was never considered.** The project has an unused `auth.users`. Rejected:
   it would replace our sessions, the subdomain return and the pending-signup bootstrap, and
   put users in a second table keyed on UUIDs. Recorded so it's a decision, not a gap.
   (Side effect: `information_schema` queries on `users` must filter
   `table_schema = 'public'`, or they pick up `auth.users` columns too.)

---

## 3. The name problem: renames reach into the matcher

There are already several names for one person, and they're allowed to disagree:

| Field | Scope | Refreshed | Who changes it |
|---|---|---|---|
| `User.display_name` (Slab 0) | per account | never by a provider | the user (Slab 2) |
| `User.discord_name` | per account | every Discord sign-in (`auth.py:419`) | Discord. Means "Discord handle". |
| `Player.name` | **per club** (multi-club network model) | never automatically | club super-admin only, `PATCH /admin/players/{id}` (`admin.py:451-465`) |
| `Signup.player_name` (`models.py:87`) | per signup row | on re-signup only (`signups.py:887`) | follows Player.name, eventually |
| `LeagueRating.player_name` (`models.py:219`) | per player/club/system/season | never | goes stale on rename |

**Matcher hazard.** `pairings_engine.py` keys identity on the normalised player *name*, not
`player_id`:

- `pairings_engine.py:514-515` — candidates collapse into `seen_names[_normalize_name(su.player_name).lower()] = su`,
  later entries overwriting earlier ones. Two players whose names normalise alike: one
  silently disappears from that week's pairings.
- `pairings_engine.py:122-123` and `752-753` — recent-opponent history is a set of sorted
  **name pairs**. Rename a player and their history evaporates, so the "don't repeat last
  week's opponent" hard filter in `_pair_dist` stops applying to them.

`pairings_engine.py` is under a documented do-not-change invariant (see CLAUDE.md).
**Decision B keeps it untouched:** the account name is separate, `Player.name` stays
admin-mediated.

---

## 4. Already safe: looks like a blocker on a grep, isn't

- **`database.py:271`** — mention map: `{u.id: u.discord_id for u in users if u.discord_id}`.
  No mention → plain bold name, same path as unclaimed roster entries and guests. A
  non-Discord player loses the @-ping and nothing else. (Slab 0 re-points it at
  `user_identities`, per §2b.3.)
- **`signups.py:379`** — guild gate: `if account is None or not account.discord_id: return`.
  Fails open. **Decision A: that is the intended behaviour.**
- **`venue_api.py:456`, `venue_api.py:1196-1199`, `tournaments.py:537`** — already chain
  `player_name or discord_name or "Guest"`-style fallbacks.

---

## 5. Copy, docs and admin debt: not blocking, wrong the day a second provider ships

Web repo (`~/projects/call-to-arms-web`):

- **"Sign in with Discord" hardcoded, 9 strings in 7 files:** `src/lib/LandingHero.svelte:58`,
  `src/lib/SignInPrompt.svelte:33`, `src/routes/leagues/+page.svelte:501`,
  `src/routes/request-club/+page.svelte:218,221`, `src/routes/claim/+page.svelte:94`,
  `src/routes/join/+page.svelte:94`, `src/routes/signup/+page.svelte:819,820`.
  The *links* already all go through `src/lib/loginUrl.ts` (`loginHref` / `loginHrefTo`), so
  the plumbing is one function. `loginUrl.ts:2` also mentions it in a comment.
- **`src/lib/SignInPrompt.svelte`** — body copy "Club nights run on Discord, so that's what
  you sign in with." Needs rewriting, not find-and-replace.
- **`src/routes/privacy/+page.svelte:17-18, 70-72`** — lists exactly what's stored (Discord ID,
  name, avatar) and names Discord as the only third party. A provider that hands over email
  changes what we hold: update in the **same release**.
- **`src/lib/handbookContent.ts:88`** — tells organisers "Accounts come from Discord, so there
  is nobody to grant it to until they have been through the door."
- **`src/routes/players/[id]/+page.svelte:138`** — profile card shows the Discord handle +
  avatar as the identity chip (fed by `main.py:880-883`).
- **`src/routes/+layout.svelte:342,352,396`**, **`src/routes/claim/+page.svelte:103`**,
  **`src/routes/signup/+page.svelte:825`** — greet the user by `auth.user.discord_name`.
- **`src/lib/VenueStaff.svelte:68,80`**, **`src/routes/platform-admin/+page.svelte`** (many),
  **`src/routes/admin/+page.svelte`** (roles, super-admins, Players tab) — display
  `discord_name` in staff/admin pickers.
- Plus the sites in §2b.7.

API repo:

- **`admin.py:5226`** — cross-club support search ("my account's broken") matches
  `User.discord_name.ilike(...)` and linked player names only. A non-Discord user with no
  claimed player is invisible to support. (It also joins players through the legacy
  `User.player_id`, so it misses anyone whose only player is at a non-home club.)
- **`admin.py:196, 213, 4279`** — admin lists `ORDER BY discord_name`.
- **`database.py:710`** — audit log snapshots `actor_name=actor.discord_name`. Fine as a
  snapshot; just becomes "whatever the display name is".
- Tests coupled to Discord identity: `tests/test_club_onboarding.py` (56 refs, heaviest),
  `test_per_system_blocks.py`, `test_tournament_entries.py`, `test_system_authoring.py`,
  `test_login_return_path.py`, `test_club_system_overrides.py`.

---

## 6. What doesn't exist at all

- **No account page.** 18 routes in the web app, none is settings/account management.
- **Account menu** (`+layout.svelte`, around line 332-375) has three things: profile link,
  "Change club", "Sign out".
- **No email column on `users`.** Only emails in the system: `ClubRequest.requester_email`
  (NOT NULL, unverified), guest table-booking `contact_email`, club/venue contact emails.
- **No self-serve rename.** Super-admin only.
- **No account recovery of any kind.** Lose the Discord account → lose profile, levels,
  league history. This is the strongest reason to do the work, more than reaching
  non-Discord players.
- **No session revocation, no user merge, no rate limiting** (§2b).

Existing infra worth reusing: Resend email (`emailer.py`, `email_layout.py`, verified
`calltoarms.app` sender), Supabase Storage (`storage.py`) for an uploaded avatar,
`migrations/merge_duplicate_players.py` as the player-merge precedent.

---

## 7. Slabs (a dependency chain)

Each slab lists what it **needs** from earlier slabs. Nothing ships out of order.

0. **Identity decoupling** (backend only, nothing user-visible). Needs: nothing.
   - `user_identities (id, user_id, provider, provider_user_id, is_primary, email,
     email_verified, name, avatar_url, created_at, last_used_at)`,
     `UNIQUE(provider, provider_user_id)`, partial `UNIQUE(user_id, provider) WHERE is_primary`.
     Backfilled from `users.discord_id` as primary. In `WRITE_ALLOWED_TABLES`.
   - **Several identities per provider, one primary.** First built as one Discord per account;
     changed while writing Slab 1, before anything was migrated. With one-per-provider, merging
     Shaun's two Discord accounts would have had to throw one away, and signing in with it again
     would recreate the duplicate. Now both sign in; the primary is THE Discord account.
   - **Authority:** the primary `user_identities` row is the source of truth for "which Discord
     account is this user". `users.discord_id` mirrors it for the expand phase and is cleared on
     unlink. Mentions (`database.py`) and the guild gate (`signups.py`) read through
     `identity.discord_ids_for_users`, so they can never disagree with sign-in. This is also the
     eventual fix for `KNOWN_ISSUES.md` #1: link the Discord account that's in the server and
     make it primary.
   - `users`: drop NOT NULL on `discord_id` and `discord_name`; add `display_name` (nullable,
     user-owned, no provider writes it); add `session_version` (int, default 0).
   - **Revocable sessions:** the cookie carries the version (`user_id:version.sig`; version 0
     keeps today's `user_id.sig` exactly, so nobody is logged out by the deploy). Bumping
     `session_version` kills every session for that user. `POST /auth/logout-everywhere`.
   - Split `/discord/callback` into "exchange code → provider profile" and the shared
     "find or defer user, set cookie, route new vs returning" half, keeping its RETURN and
     SUBDOMAIN handling. A user found only by `users.discord_id` gets its identity row
     created on the spot, so accounts made between the migration and the deploy self-heal.
   - Provider-neutral pending-signup cookie (still accepts the old Discord-shaped payload,
     10-minute lifetime) and `requester_identity`.
   - `club_requests.identity_provider` + `identity_subject`; duplicate detection and
     provisioning key on them. `discord_id`/`discord_name` kept, dual-written, for display.
   - Explicit `/auth/me` and `/auth/complete-signup` user shape (adds `display_name` and a
     resolved `name`; no `session_version`, no future sensitive columns).
   - Fix the `cta_oauth_*` cookies' missing `secure=True`.
   - **Order:** run the migration, then deploy. The migration only adds, so the old code keeps
     working against it. It is self-contained SQL (no `models` import) because it runs on the
     Fly machine before the new `models.py` exists there. Rehearsed 2026-09-15 on a throwaway
     Postgres 16 with the pre-Slab-0 tree; prod dry run: 111 identities to backfill, 0 requests.
1. **User merge.** Needs: 0 (identities move with the user). BUILT: `user_merge.py`
   (`plan_merge` / `merge_users`), `migrations/merge_users.py --keep --drop [--apply]`,
   `tests/test_user_merge.py`. Moves all 14 user-id columns; duplicate admin roles and venue
   staff collapse; the dropped account's identities join as secondary. Refuses, writing
   nothing, on: both own a player at one club (merge the players first), both in one
   tournament, super-admins of different clubs, or any user-id column missing from
   `USER_REFERENCES` (checked against live metadata, so a new column can't be silently
   orphaned). No schema change. Known cases once Slab 0 is live: Shaun (users 26, 60?, 69),
   and the 6 accounts owning no player. Link/unlink (Slab 6) calls it.
2. **`/account` page** (frontend, greenfield). Needs: 0. BUILT: `GET /auth/account` (the
   account, its identities, its player at each club, `can_add`) and web `/account`
   (sign-in methods with the primary marked "Tagged in posts" when there is more than one,
   player profiles per club with archived shown, sign out everywhere with a confirm step),
   linked from the account menu. **The nudge (way out 1 of the C/D conflict)** renders when
   `can_add` is non-empty, i.e. when `identity.AVAILABLE_PROVIDERS` lists a method the account
   lacks. Discord is the only entry, so it is hidden with no flag to flip. Its button goes to
   `/auth/<provider>/link`: **a provider must not join `AVAILABLE_PROVIDERS` until that route
   exists** (4/5 add the provider, 6 adds linking; add it to the list in 6, or give 4/5 their
   own link route). Display name is shown, not yet editable (Slab 3).
3. **Display name you own.** Needs: 0, 2. BUILT: `PATCH /auth/account {display_name}`
   (`identity.clean_display_name`: whitespace collapsed, 32 max, control/invisible characters
   refused, blank clears back to the Discord handle); an Edit control on `/account`. API sweep:
   every admin payload that gave only `discord_name` now also gives the resolved `name`
   (roles, super-admins, grantable users, platform super-admins/grantable, support search,
   provisioning's appointed admin, venue staff); the Players tab adds `linked` and
   `account_name` so "not linked" means no account, not no Discord; audit-log `actor_name`,
   audit detail strings and `reviewed_by_name` use the resolved name; support search matches
   `display_name`; tournament entry and venue booking name fallbacks try it before the handle.
   Web sweep: header, claim banner, claim page, signup "Almost there", request-club greet by
   `name`; admin pickers use `$lib/accountName.ts` `accountLabel` (roster or chosen name, with
   the Discord handle beside it when different). The profile page's Discord chip still shows
   the Discord handle: it is labelled as Discord.
   **Name moderation: no automatic filter (decided 2026-09-15).** A word-list check was built
   and briefly deployed (`cb4bac7`), then removed: Joel judged a blocked-word list in shipped
   code too problematic, and chose no automatic filtering over a package list or a hosted
   moderation API. What stays: account
   name changes are audit-logged (`account.name`); a club super-admin resets one from Players &
   blocks › Edit player (`POST /admin/players/{id}/reset-account-name`), a platform admin from
   user search (`POST /admin/platform/users/{id}/reset-name`), both logged as
   `account.name.reset`. The edit-player field formerly labelled "Display Name" is now
   "Roster name". The removed list is still in the repo's history; it was not rewritten.
4. **Google sign-in.** Needs: 0, 2, 3. Standard OIDC, one module. Also the §5 copy sweep and
   the privacy page, in the same release. Implements Decision C's "offer to link" on a
   verified-email match.
5. **Email magic link.** Needs: 0, 2, and new rate limiting. `login_tokens` table, single-use
   short-expiry hashed tokens, per-address and per-IP limits. No passwords. This is also the
   recovery path; recovery bumps `session_version`. Security-sensitive: review properly.
6. **Link / unlink.** Needs: 0, 1, 2, and at least one of 4/5. From a signed-in session on
   `/account`. Can't unlink your last identity. Linking an identity already on another
   account runs the Slab 1 merge after proving both. Unlinking Discord clears the mirror
   column and bumps `session_version`.
7. **Apple — deferred.** £79/yr developer program. Client secret is an ES256 JWT
   from a `.p8` key, max 6-month expiry → needs a rotation job or it dies silently. Name is
   only returned on the very first authorisation, ever. `response_mode=form_post` makes the
   callback a cross-site POST, which will NOT carry `cta_oauth_state` because every auth
   cookie is `samesite="lax"` → state check breaks; would need SameSite=None on that cookie or
   state carried outside cookies. The App Store rule requiring Sign in with Apple is for native
   iOS apps, not a web PWA.

---

## 8. Decisions (settled 2026-09-15)

**A. Guild gate with no Discord linked → let them through.** Keep today's fail-open at
`signups.py:379`. Accepted consequence: anyone can sidestep a gate by signing in another way.

**B. Display name → account name + `Player.name`.** A new `users.display_name` the user
owns (never written by a provider); `discord_name` keeps meaning the Discord handle;
`Player.name` stays per club and admin-renamed. Matcher untouched.

**C. Google email matches an existing account → offer to link.** Never auto-link. Prompt
the person to prove the other identity from the same session, then link.

**D. Discord `email` scope → don't collect.**

**C/D conflict → nudge on `/account`.** With no Discord emails, C's prompt can't fire for
Discord-only accounts. Every Discord-only account is nudged to add Google or an email while
signed in; from then on it is matchable.

**Follow-ups, all yes:** `users.display_name` column (not overloading `discord_name`);
revocable sessions in Slab 0; one authoritative Discord ID per user (`user_identities`);
user merge as its own slab before link/unlink.

---

## Reproducing the sweep

From `~/projects/call-to-arms-api`:

```bash
# total, and identity vs webhook split (exclude caches, tests, migrations)
grep -rn "discord" --include=*.py -i . | grep -v "__pycache__\|\.venv" | wc -l
grep -rn "discord_id\|discord_name\|discord_user\|DISCORD_CLIENT\|discord/login\|discord/callback\|pending_signup" \
  --include=*.py . | grep -v "__pycache__\|tests/\|migrations/\|\.venv" | cut -d: -f1 | sort | uniq -c | sort -rn
grep -rni "webhook" --include=*.py . | grep -v "__pycache__\|tests/\|migrations/\|\.venv" | cut -d: -f1 | sort | uniq -c | sort -rn

# the trap, the scope, the matcher key
grep -n "existing.discord_name\|existing.avatar_url\|SCOPES = " auth.py
grep -n "_normalize_name(.*player_name)" pairings_engine.py

# every user-id column a user merge must move (§2b.2)
grep -nE "user_id.*(foreign_key=\"users.id\"|: (Optional\[)?int)" models.py
```

From `~/projects/call-to-arms-web`:

```bash
grep -rn "Sign in with Discord" src
grep -rn "discord_name" src
grep -rn "loginHref\|discord/login" src
```

A published visual version of the 09-14 audit:
https://claude.ai/code/artifact/c0b2cd3f-c1d8-4ca1-9ff5-d30b7b2f6765 — out of date since
09-15; this file is the source of truth.
