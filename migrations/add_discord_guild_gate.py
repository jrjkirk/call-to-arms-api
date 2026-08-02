"""Discord-guild gate (2026-08-02): expand + grandfather for the two additive
columns behind the "must be in the club's Discord to sign up" check.

One-off script, not a long-lived migration tool (this repo doesn't manage
migrations — see CLAUDE.md / models.py docstring). Both tables already exist
with real rows, so SQLModel's create_all() won't alter them; this runs the
ALTER TABLEs by hand, then grandfathers existing players.

Columns added (both NULLABLE by design — no contract/SET NOT NULL step):
  - clubs.discord_guild_id      -> the club's Discord server snowflake, as
                                   text. NULL = gate OFF for that club, which
                                   is every club until an admin sets one. No
                                   backfill: there is nothing to derive it
                                   from without calling Discord.
  - players.discord_verified_at -> stamped once the player is confirmed to be
                                   in their club's Discord. NULL = not yet
                                   checked.

GRANDFATHERING (--grandfather) stamps every EXISTING active player as already
verified, so the new requirement only ever applies to people who join after
the gate goes live. Without it, flipping a club to "enforce" would block
long-standing members who simply aren't in the Discord — the exact surprise
that makes people abandon an app. Run this BEFORE any club is switched on.

Run manually, in order:

    PYTHONPATH=. python migrations/add_discord_guild_gate.py --add-columns
    PYTHONPATH=. python migrations/add_discord_guild_gate.py --grandfather
    PYTHONPATH=. python migrations/add_discord_guild_gate.py --verify

Safe to re-run: ALTER TABLE uses IF NOT EXISTS; grandfathering only touches
rows that are still NULL.
"""
import sys
from datetime import datetime

from sqlalchemy import text
from sqlmodel import Session, select

from database import engine
from models import Club, Player


def add_columns():
    with engine.begin() as conn:
        conn.execute(text(
            "ALTER TABLE clubs ADD COLUMN IF NOT EXISTS discord_guild_id VARCHAR"
        ))
        conn.execute(text(
            "ALTER TABLE players ADD COLUMN IF NOT EXISTS discord_verified_at TIMESTAMP"
        ))
    print("Added clubs.discord_guild_id, players.discord_verified_at (or already present).")


def grandfather():
    """Mark every existing active player as already-verified."""
    with Session(engine) as session:
        players = session.exec(
            select(Player)
            .where(Player.active == True)
            .where(Player.discord_verified_at.is_(None))
        ).all()
        stamp = datetime.utcnow()
        for p in players:
            p.discord_verified_at = stamp
            session.add(p)
        session.commit()
        print(f"Grandfathered {len(players)} existing active player(s) at {stamp:%Y-%m-%d %H:%M:%S}.")

        # Inactive players are deliberately left NULL: they can't sign up
        # anyway (_require_linked_player rejects them), and if one is ever
        # reactivated they should be checked like anyone else.
        inactive = session.exec(
            select(Player)
            .where(Player.active == False)
            .where(Player.discord_verified_at.is_(None))
        ).all()
        print(f"Left {len(inactive)} inactive player(s) unstamped (deliberate).")


def verify():
    with Session(engine) as session:
        clubs = session.exec(select(Club)).all()
        players = session.exec(select(Player)).all()
        active = [p for p in players if p.active]
        verified = [p for p in active if p.discord_verified_at is not None]

        print(f"clubs: {len(clubs)}")
        for c in clubs:
            state = c.discord_guild_id or "(none — gate OFF)"
            print(f"  - {c.slug}: discord_guild_id={state}")
        print(f"active players: {len(active)}, grandfathered: {len(verified)}, unstamped: {len(active) - len(verified)}")

        ok = True
        if any(c.discord_guild_id for c in clubs):
            print("NOTE: at least one club has a guild id set — the gate is live for it "
                  "(subject to its discord_gate_mode setting).")
        if len(verified) != len(active):
            print("WARNING: some active players are unstamped — run --grandfather before "
                  "switching any club to 'enforce'.")
            ok = False
        print("OK" if ok else "CHECK THE WARNINGS ABOVE")
        return ok


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if not args & {"--add-columns", "--grandfather", "--verify"}:
        print(__doc__)
        sys.exit(1)
    if "--add-columns" in args:
        add_columns()
    if "--grandfather" in args:
        grandfather()
    if "--verify" in args:
        verify()
