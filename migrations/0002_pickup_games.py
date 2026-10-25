"""Pick-up games (pickups.py): requests to be matched outside club night, and
the matches the app proposes between them."""
from sqlalchemy import text


def up(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS pickup_requests (
            id SERIAL PRIMARY KEY,
            club_id INTEGER NOT NULL REFERENCES clubs(id),
            system VARCHAR NOT NULL,
            player_id INTEGER NOT NULL REFERENCES players(id),
            player_name VARCHAR NOT NULL,
            window_start TIMESTAMP NOT NULL,
            window_end TIMESTAMP NOT NULL,
            venue VARCHAR NOT NULL DEFAULT 'club',
            venue_text VARCHAR,
            faction VARCHAR,
            points INTEGER,
            vibe VARCHAR,
            notes VARCHAR,
            status VARCHAR NOT NULL DEFAULT 'open',
            created_at TIMESTAMP NOT NULL DEFAULT now()
        )
    """))
    for col in ("club_id", "system", "player_id", "status"):
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_pickup_requests_{col} ON pickup_requests ({col})"))
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS pickup_matches (
            id SERIAL PRIMARY KEY,
            club_id INTEGER NOT NULL REFERENCES clubs(id),
            system VARCHAR NOT NULL,
            a_request_id INTEGER NOT NULL REFERENCES pickup_requests(id),
            b_request_id INTEGER NOT NULL REFERENCES pickup_requests(id),
            a_answer VARCHAR NOT NULL DEFAULT 'pending',
            b_answer VARCHAR NOT NULL DEFAULT 'pending',
            starts_at TIMESTAMP NOT NULL,
            status VARCHAR NOT NULL DEFAULT 'proposed',
            decline_reason VARCHAR,
            declined_by_player_id INTEGER,
            proposed_at TIMESTAMP NOT NULL DEFAULT now(),
            expires_at TIMESTAMP NOT NULL,
            decided_at TIMESTAMP
        )
    """))
    for col in ("club_id", "system", "a_request_id", "b_request_id", "status"):
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_pickup_matches_{col} ON pickup_matches ({col})"))


def verify(conn):
    missing = []
    for table in ("pickup_requests", "pickup_matches"):
        n = conn.execute(text(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = :t"), {"t": table}).scalar()
        if not n:
            missing.append(f"{table} missing")
    return missing
