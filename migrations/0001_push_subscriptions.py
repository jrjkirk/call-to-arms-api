"""Devices that have allowed push notifications (push.py, models.PushSubscription)."""
from sqlalchemy import text


def up(conn):
    conn.execute(text("""
        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id SERIAL PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id),
            endpoint VARCHAR NOT NULL UNIQUE,
            p256dh VARCHAR NOT NULL,
            auth VARCHAR NOT NULL,
            muted JSON NOT NULL DEFAULT '[]'::json,
            user_agent VARCHAR,
            created_at TIMESTAMP NOT NULL DEFAULT now(),
            last_sent_at TIMESTAMP
        )
    """))
    conn.execute(text(
        "CREATE INDEX IF NOT EXISTS ix_push_subscriptions_user_id ON push_subscriptions (user_id)"
    ))


def verify(conn):
    cols = {r[0] for r in conn.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'push_subscriptions'"))}
    need = {"id", "user_id", "endpoint", "p256dh", "auth", "muted", "user_agent", "created_at", "last_sent_at"}
    return [f"push_subscriptions.{c} missing" for c in sorted(need - cols)]
