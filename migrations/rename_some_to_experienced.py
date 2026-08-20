"""Rename the middle experience tier: "Some" -> "Experienced" (2026-08-20).

"Some" was the original name and reads oddly on its own — a pairing card
saying just SOME tells a player nothing. The tier has been called
"Experienced" everywhere new since experience became derived; this brings the
~287 historical signup rows into line so the two names stop coexisting.

Safe to run before deploying: the code already treats both names identically
(pairings_engine.exp_map maps each to weight 1, and every renderer accepts
both), so rows flip with no behavioural change either side of the deploy.

    PYTHONPATH=. python migrations/rename_some_to_experienced.py --run
    PYTHONPATH=. python migrations/rename_some_to_experienced.py --verify

    fly ssh console -C "sh -c 'cd /app && PYTHONPATH=. python migrations/rename_some_to_experienced.py --run --verify'"
"""
import sys

from sqlalchemy import text

from database import engine


def run():
    with engine.begin() as conn:
        r = conn.execute(text(
            "UPDATE signups SET experience = 'Experienced' WHERE experience = 'Some'"
        ))
        print(f"Renamed {r.rowcount} signup row(s) from 'Some' to 'Experienced'.")


def verify():
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT experience, count(*) FROM signups GROUP BY experience ORDER BY 2 DESC"
        )).all()
        for v, n in rows:
            print(f"   {str(v):14} {n}")
        left = [n for v, n in rows if v == "Some"]
        print("OK" if not left else f"WARNING: {left[0]} rows still say 'Some'")


if __name__ == "__main__":
    args = set(sys.argv[1:])
    if not args & {"--run", "--verify"}:
        print(__doc__)
        sys.exit(1)
    if "--run" in args:
        run()
    if "--verify" in args:
        verify()
