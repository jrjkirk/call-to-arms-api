"""Create the new `call_outs` table (ad-hoc "call to arms" feature).

One-off script, not a long-lived migration tool (this repo doesn't manage
migrations — see CLAUDE.md / models.py docstring). Brand-new table (like
create_club_settings_table.py), so this also verifies the expected columns,
primary key, and FKs were generated.

Run manually (from repo root, so repo-root imports resolve):

    PYTHONPATH=. python migrations/add_call_outs_table.py             # create + verify
    PYTHONPATH=. python migrations/add_call_outs_table.py --verify-only

Safe to re-run: table creation uses checkfirst (CREATE TABLE IF NOT EXISTS).
"""
import sys

from sqlalchemy import text
from sqlmodel import Session

from database import engine
from models import CallOut


def create_table():
    CallOut.metadata.create_all(engine, tables=[CallOut.__table__], checkfirst=True)
    print("Created call_outs table (or already present).")


def verify() -> list[str]:
    problems: list[str] = []
    with Session(engine) as session:
        exists = session.exec(text(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'call_outs'"
        )).first()
        if not exists:
            problems.append("call_outs table does not exist")
            return problems

        pk_cols = session.exec(text(
            """
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
            WHERE tc.table_name = 'call_outs'
              AND tc.constraint_type = 'PRIMARY KEY'
            ORDER BY kcu.ordinal_position
            """
        )).all()
        pk_cols = [row[0] for row in pk_cols]
        if pk_cols != ["id"]:
            problems.append(f"Expected PK (id), got {pk_cols}")

        cols = session.exec(text(
            """
            SELECT column_name, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'call_outs'
            """
        )).all()
        cols_by_name = {row[0]: row[1] for row in cols}
        expected_cols = {
            "id", "club_id", "system", "creator_player_id", "creator_name",
            "game_at", "vibe", "faction", "points", "notes",
            "status", "taker_player_id", "taker_name", "taken_at",
            "created_at", "last_reminder_at", "updated_at",
        }
        missing = expected_cols - set(cols_by_name)
        if missing:
            problems.append(f"Missing columns: {missing}")
        for not_null in ("club_id", "system", "creator_player_id", "creator_name", "game_at", "status"):
            if cols_by_name.get(not_null) != "NO":
                problems.append(f"{not_null} should be NOT NULL, is_nullable={cols_by_name.get(not_null)}")

        fks = session.exec(text(
            """
            SELECT kcu.column_name, ccu.table_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.constraint_column_usage ccu
              ON tc.constraint_name = ccu.constraint_name
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
            WHERE tc.table_name = 'call_outs'
              AND tc.constraint_type = 'FOREIGN KEY'
            """
        )).all()
        fk_map = {row[0]: row[1] for row in fks}
        if fk_map.get("club_id") != "clubs":
            problems.append(f"Expected club_id FK -> clubs, got {fk_map.get('club_id')}")
        if fk_map.get("creator_player_id") != "players":
            problems.append(f"Expected creator_player_id FK -> players, got {fk_map.get('creator_player_id')}")

    return problems


def main():
    verify_only = "--verify-only" in sys.argv
    if not verify_only:
        print("Creating call_outs table (idempotent)...")
        create_table()

    problems = verify()
    if problems:
        print(f"\nVERIFICATION FAILED ({len(problems)} mismatch(es)):")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    else:
        print("\nVerification passed: call_outs exists with expected columns, PK (id), and FKs to clubs/players.")


if __name__ == "__main__":
    main()
