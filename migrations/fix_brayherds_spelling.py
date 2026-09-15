"""Correct the Old World faction "Beastmen Brayheards" to "Beastmen Brayherds".

The faction name is stored on each row, not looked up, so the old spelling
lives in history as well as in the faction lists. Left alone, the icon would
vanish from those rows (its filename is built from the name) and league and
faction stats would count one army as two.

    signups.faction                  11 rows at the time of writing
    pairings.a_faction / b_faction   4 / 6
    league_results.player_1_faction / player_2_faction   3 / 2

Nothing in systems.faction_list, players.default_faction, tournament_entries
or call_outs carried it. Checked across every text and JSON column in public.

Run AFTER the deploy that renames the faction and its icon, so the form and
the rows agree the moment the rows change. Safe to run twice.

    PYTHONPATH=. python migrations/fix_brayherds_spelling.py
    PYTHONPATH=. python migrations/fix_brayherds_spelling.py --verify-only
"""
import sys

from sqlalchemy import text

from database import engine

OLD = "Beastmen Brayheards"
NEW = "Beastmen Brayherds"

COLUMNS = [
    ("signups", "faction"),
    ("pairings", "a_faction"),
    ("pairings", "b_faction"),
    ("league_results", "player_1_faction"),
    ("league_results", "player_2_faction"),
]


def report(conn, header):
    print(header)
    total = 0
    for table, col in COLUMNS:
        n = conn.execute(
            text(f"SELECT count(*) FROM {table} WHERE {col} = :old"), {"old": OLD}
        ).scalar() or 0
        total += n
        print(f"  {table}.{col:<18} {n}")
    return total


def main() -> None:
    verify_only = "--verify-only" in sys.argv
    with engine.begin() as conn:
        remaining = report(conn, f"Rows still saying {OLD!r}:")
        if verify_only:
            print("\nClean." if remaining == 0 else f"\n{remaining} row(s) left.")
            sys.exit(0 if remaining == 0 else 1)
        if remaining == 0:
            print("\nNothing to do.")
            return
        print()
        for table, col in COLUMNS:
            n = conn.execute(
                text(f"UPDATE {table} SET {col} = :new WHERE {col} = :old"),
                {"new": NEW, "old": OLD},
            ).rowcount
            print(f"  {table}.{col:<18} updated {n}")
    with engine.connect() as conn:
        left = report(conn, "\nAfter:")
        print("\nClean." if left == 0 else f"\n{left} row(s) left.")


if __name__ == "__main__":
    main()
