"""The migration runner (migrate.py): numbered files, applied once, in order.

Fly runs it before every deploy, so what has to hold is what makes that safe:
  - pending migrations apply in number order, and each is recorded;
  - running it again applies nothing;
  - a failing migration rolls back its own change and its record, keeps the
    earlier ones, and stops the run so later ones don't apply out of order;
  - a failed verify() counts as a failure;
  - listing what's pending writes nothing;
  - the unnumbered historical scripts are ignored, and a duplicated number is
    refused.

Run: PYTHONPATH=. python tests/test_migrate.py
"""
import os
import pathlib
import sys
import tempfile

_DB = pathlib.Path(tempfile.mkdtemp()) / "migrate.db"
os.environ["DATABASE_URL"] = f"sqlite:///{_DB}"

from sqlalchemy import create_engine, event, inspect  # noqa: E402

import migrate  # noqa: E402

FAILURES = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + ("" if cond else f"  {detail}"))
    if not cond:
        FAILURES.append(label)


engine = create_engine(f"sqlite:///{_DB}", connect_args={"isolation_level": None})


# Python's sqlite3 driver commits on its own before every CREATE TABLE, so out
# of the box a failed migration's table would survive its rollback here and
# nowhere else: Postgres DDL is transactional. SQLAlchemy's documented recipe
# hands transaction control back, so this test sees what production will.
@event.listens_for(engine, "begin")
def _begin(conn):
    conn.exec_driver_sql("BEGIN")
work = pathlib.Path(tempfile.mkdtemp())


def write(name, body):
    (work / name).write_text(body)


def tables():
    return set(inspect(engine).get_table_names())


write("0001_make_widgets.py", """
from sqlalchemy import text
def up(conn):
    conn.execute(text("CREATE TABLE IF NOT EXISTS widgets (id INTEGER PRIMARY KEY)"))
""")
write("0002_make_gadgets.py", """
from sqlalchemy import text
def up(conn):
    conn.execute(text("CREATE TABLE IF NOT EXISTS gadgets (id INTEGER PRIMARY KEY)"))
def verify(conn):
    return []
""")
write("add_something_by_hand.py", "raise SystemExit('the runner must never import this')\n")
write("notes.txt", "not a migration\n")


print("\n1. Listing writes nothing")
check("no schema_migrations table before applying",
      migrate.applied_names(engine) == set() and "schema_migrations" not in tables())


print("\n2. Pending migrations apply in order and are recorded")
order = []
applied = migrate.apply_pending(engine, work, log=order.append)
check("both applied, in number order", applied == ["0001_make_widgets", "0002_make_gadgets"], str(applied))
check("their tables exist", {"widgets", "gadgets"} <= tables(), str(tables()))
check("and both are recorded", migrate.applied_names(engine) == set(applied))
check("the unnumbered script was ignored", "add_something_by_hand" not in " ".join(order))


print("\n3. Running again applies nothing")
check("nothing pending", migrate.apply_pending(engine, work, log=lambda *_: None) == [])


print("\n4. A failure rolls back that migration and stops the run")
write("0003_half_done.py", """
from sqlalchemy import text
def up(conn):
    conn.execute(text("CREATE TABLE half (id INTEGER PRIMARY KEY)"))
    conn.execute(text("THIS IS NOT SQL"))
""")
write("0004_after_the_failure.py", """
from sqlalchemy import text
def up(conn):
    conn.execute(text("CREATE TABLE later (id INTEGER PRIMARY KEY)"))
""")
try:
    migrate.apply_pending(engine, work, log=lambda *_: None)
    check("the run raised", False, "no exception")
except Exception:
    check("the run raised", True)
check("the failed migration's change was rolled back", "half" not in tables(), str(tables()))
check("and it was not recorded", "0003_half_done" not in migrate.applied_names(engine))
check("the earlier ones are still recorded",
      {"0001_make_widgets", "0002_make_gadgets"} <= migrate.applied_names(engine))
check("the later one did not run out of order", "later" not in tables())

(work / "0003_half_done.py").unlink()
(work / "0004_after_the_failure.py").unlink()


print("\n5. A failed verify() is a failure too")
write("0005_checked.py", """
from sqlalchemy import text
def up(conn):
    conn.execute(text("CREATE TABLE checked (id INTEGER PRIMARY KEY)"))
def verify(conn):
    return ["checked table is missing a column"]
""")
try:
    migrate.apply_pending(engine, work, log=lambda *_: None)
    check("refused", False, "no exception")
except RuntimeError as e:
    check("refused, naming the problem", "missing a column" in str(e), str(e))
check("and rolled back", "checked" not in tables() and "0005_checked" not in migrate.applied_names(engine))
(work / "0005_checked.py").unlink()


print("\n6. Two files with one number are refused")
write("0002_another_two.py", "def up(conn):\n    pass\n")
try:
    migrate.discover(work)
    check("refused", False, "no exception")
except SystemExit as e:
    check("refused", "0002" in str(e), str(e))


print("\n7. The real migrations folder is well formed")
check("discover() accepts it", isinstance(migrate.discover(), list))


print()
if FAILURES:
    print(f"{len(FAILURES)} FAILED")
    sys.exit(1)
print("ALL PASS")
