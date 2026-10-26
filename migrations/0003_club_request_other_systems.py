"""Club requests: games the club plays that aren't in the catalogue yet.

The add-your-club form now has an "Other" choice with a free-text box. It is
read by the platform admin from the request notification and the review
queue; approving a request still sets up only catalogue systems, and the admin
adds the new one to the catalogue by hand."""
from sqlalchemy import text


def up(conn):
    conn.execute(text("ALTER TABLE club_requests ADD COLUMN IF NOT EXISTS other_systems VARCHAR"))


def verify(conn):
    cols = {r[0] for r in conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'club_requests'"))}
    return [] if "other_systems" in cols else ["club_requests.other_systems missing"]
