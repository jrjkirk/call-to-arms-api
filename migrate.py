"""Apply the numbered schema migrations that haven't run yet.

Fly runs this before every deploy (fly.toml `release_command`), in a one-off
machine with the new image and the production secrets. If it fails, the
deploy stops and the old version keeps serving. So a change that needs a new
column ships as one push: the migration file and the code that uses it.

    python migrate.py              # list applied and pending, change nothing
    python migrate.py --apply      # apply pending ones, in order

A migration is `migrations/NNNN_short_name.py` (four digits, run in number
order) defining:

    def up(conn) -> None              # the change, via conn.execute(text(...))
    def verify(conn) -> list[str]     # optional: problems found, [] if fine

Each runs in its own transaction together with the row recording it in
`schema_migrations`, so a failure leaves neither the change nor the record.
Write `up` to be safe to run twice (IF NOT EXISTS), and remember the OLD code
is still serving while it runs: add things, don't remove or rename what the
old code reads in the same deploy.

The older scripts in migrations/ (no number) were run by hand before this
existed; they stay as a record and this runner ignores them.
"""
import importlib.util
import pathlib
import re
import sys
from datetime import datetime

from sqlalchemy import text

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent / "migrations"
NAME_RE = re.compile(r"^(\d{4})_[a-z0-9_]+\.py$")

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name        VARCHAR PRIMARY KEY,
    applied_at  TIMESTAMP NOT NULL
)
"""


def discover(directory: pathlib.Path = MIGRATIONS_DIR) -> list[pathlib.Path]:
    """Numbered migration files, in order. Two files with one number is an error."""
    found = sorted(p for p in directory.glob("*.py") if NAME_RE.match(p.name))
    numbers = [NAME_RE.match(p.name).group(1) for p in found]
    dupes = sorted({n for n in numbers if numbers.count(n) > 1})
    if dupes:
        raise SystemExit(f"Two migrations share a number: {', '.join(dupes)}")
    return found


def _load(path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not callable(getattr(module, "up", None)):
        raise SystemExit(f"{path.name} has no up(conn)")
    return module


def applied_names(engine, create: bool = False) -> set[str]:
    """Names already recorded. Only `create=True` (applying) makes the table,
    so listing what's pending writes nothing."""
    from sqlalchemy import inspect

    if create:
        with engine.begin() as conn:
            conn.execute(text(CREATE_TABLE))
    elif not inspect(engine).has_table("schema_migrations"):
        return set()
    with engine.connect() as conn:
        return {r[0] for r in conn.execute(text("SELECT name FROM schema_migrations")).all()}


def apply_pending(engine, directory: pathlib.Path = MIGRATIONS_DIR, log=print) -> list[str]:
    """Apply every pending migration in order; stop at the first failure.

    Returns the names applied. A failure raises, after rolling back that one
    migration; the ones before it stay applied and recorded.
    """
    done = applied_names(engine, create=True)
    applied = []
    for path in discover(directory):
        if path.stem in done:
            continue
        module = _load(path)
        log(f"applying {path.stem}")
        with engine.begin() as conn:
            module.up(conn)
            problems = module.verify(conn) if callable(getattr(module, "verify", None)) else []
            if problems:
                raise RuntimeError(f"{path.stem} failed verification, rolled back: {problems}")
            conn.execute(
                text("INSERT INTO schema_migrations (name, applied_at) VALUES (:n, :t)"),
                {"n": path.stem, "t": datetime.utcnow()},
            )
        applied.append(path.stem)
    return applied


def main() -> int:
    from database import engine

    if "--apply" in sys.argv:
        applied = apply_pending(engine)
        print(f"applied {len(applied)} migration(s)" + (f": {', '.join(applied)}" if applied else ""))
        return 0
    done = applied_names(engine)
    for path in discover():
        print(f"{'applied' if path.stem in done else 'PENDING'}  {path.stem}")
    if not discover():
        print("no numbered migrations yet")
    return 0


if __name__ == "__main__":
    sys.exit(main())
