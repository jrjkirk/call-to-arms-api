"""app_settings/club_settings split (last piece of Phase 1's 10-table club_id
rollout): create the new `club_settings` table.

One-off script, not a long-lived migration tool (this repo doesn't manage
migrations — see CLAUDE.md / models.py docstring). Unlike every prior Phase 1
table, this isn't an ALTER TABLE on an existing table — it's a brand-new
table with a composite (club_id, key) primary key, so this script also
verifies SQLModel/SQLAlchemy actually generated that composite PK correctly
(not assumed).

Run manually:

    python create_club_settings_table.py            # create + verify
    python create_club_settings_table.py --verify-only

Safe to re-run: table creation uses SQLModel's checkfirst (CREATE TABLE IF
NOT EXISTS equivalent).
"""
import sys

from sqlalchemy import text
from sqlmodel import Session

from database import engine
from models import ClubSetting


def create_table():
    ClubSetting.metadata.create_all(engine, tables=[ClubSetting.__table__], checkfirst=True)
    print("Created club_settings table (or already present).")


def verify() -> list[str]:
    problems: list[str] = []
    with Session(engine) as session:
        exists = session.exec(text(
            "SELECT 1 FROM information_schema.tables WHERE table_name = 'club_settings'"
        )).first()
        if not exists:
            problems.append("club_settings table does not exist")
            return problems

        pk_cols = session.exec(text(
            """
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
            WHERE tc.table_name = 'club_settings'
              AND tc.constraint_type = 'PRIMARY KEY'
            ORDER BY kcu.ordinal_position
            """
        )).all()
        pk_cols = [row[0] for row in pk_cols]
        if pk_cols != ["club_id", "key"]:
            problems.append(f"Expected composite PK (club_id, key), got {pk_cols}")

        fk = session.exec(text(
            """
            SELECT ccu.table_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.constraint_column_usage ccu
              ON tc.constraint_name = ccu.constraint_name
            JOIN information_schema.key_column_usage kcu
              ON tc.constraint_name = kcu.constraint_name
            WHERE tc.table_name = 'club_settings'
              AND tc.constraint_type = 'FOREIGN KEY'
              AND kcu.column_name = 'club_id'
            """
        )).first()
        if not fk or fk[0] != "clubs":
            problems.append(f"Expected club_id FK -> clubs, got {fk}")

        cols = session.exec(text(
            """
            SELECT column_name, is_nullable
            FROM information_schema.columns
            WHERE table_name = 'club_settings'
            """
        )).all()
        cols_by_name = {row[0]: row[1] for row in cols}
        expected_cols = {"club_id", "key", "value", "updated_at"}
        missing = expected_cols - set(cols_by_name)
        if missing:
            problems.append(f"Missing columns: {missing}")
        if cols_by_name.get("club_id") != "NO":
            problems.append(f"club_id should be NOT NULL, is_nullable={cols_by_name.get('club_id')}")
        if cols_by_name.get("key") != "NO":
            problems.append(f"key should be NOT NULL, is_nullable={cols_by_name.get('key')}")

    return problems


def main():
    verify_only = "--verify-only" in sys.argv
    if not verify_only:
        print("Creating club_settings table (idempotent)...")
        create_table()

    problems = verify()
    if problems:
        print(f"\nVERIFICATION FAILED ({len(problems)} mismatch(es)):")
        for p in problems:
            print(f"  - {p}")
        sys.exit(1)
    else:
        print("\nVerification passed: club_settings exists with composite PK (club_id, key), FK to clubs, correct columns.")


if __name__ == "__main__":
    main()
