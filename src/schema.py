"""Database schema management — reset_db for fixture loading."""

from __future__ import annotations

import bcrypt as _bcrypt

from src.db.database import get_db

# Module-level config set by reset_db, read by signup endpoint
_current_config: dict = {"currency": "EUR", "minor_units": 2}


def get_config() -> dict:
    return _current_config.copy()


def reset_db(fixture: dict) -> None:
    """Drop everything and recreate from a fixture dict.

    Fixture format:
    {
      "currency": "EUR",
      "minor_units": 2,
      "users": [{"id","email","password","display_name","handle","balance"}, ...],
      "payments": [...],
      "requests": [...]
    }
    """
    global _current_config
    _current_config = {
        "currency": fixture.get("currency", "EUR"),
        "minor_units": fixture.get("minor_units", 2),
    }

    db = get_db()
    try:
        # Wipe old tables (including legacy ones from the v1 migration)
        for tbl in ("requests", "payments", "users", "ledger_entries", "transactions", "accounts"):
            db.execute(f"DROP TABLE IF EXISTS {tbl}")

        # --- New schema -------------------------------------------------------
        db.execute("""
            CREATE TABLE users (
                id            TEXT PRIMARY KEY,
                email         TEXT UNIQUE NOT NULL,
                password_hash TEXT    NOT NULL,
                display_name  TEXT    NOT NULL,
                handle        TEXT UNIQUE NOT NULL,
                balance       INTEGER NOT NULL DEFAULT 0,
                currency      TEXT    NOT NULL,
                minor_units   INTEGER NOT NULL DEFAULT 2
            )
        """)

        db.execute("""
            CREATE TABLE payments (
                id              TEXT PRIMARY KEY,
                from_user_id    TEXT NOT NULL REFERENCES users(id),
                to_user_id      TEXT NOT NULL REFERENCES users(id),
                amount          INTEGER NOT NULL,
                note            TEXT,
                status          TEXT NOT NULL DEFAULT 'completed',
                idempotency_key TEXT UNIQUE,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        db.execute("""
            CREATE TABLE requests (
                id              TEXT PRIMARY KEY,
                from_user_id    TEXT NOT NULL REFERENCES users(id),
                to_user_id      TEXT NOT NULL REFERENCES users(id),
                amount          INTEGER NOT NULL,
                note            TEXT,
                status          TEXT NOT NULL DEFAULT 'pending',
                idempotency_key TEXT UNIQUE,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # --- Seed data --------------------------------------------------------
        currency = fixture.get("currency", "EUR")
        minor_units = fixture.get("minor_units", 2)

        for u in fixture.get("users", []):
            pw_hash = _bcrypt.hashpw(
                u["password"].encode(), _bcrypt.gensalt()
            ).decode()
            db.execute(
                """INSERT INTO users (id, email, password_hash, display_name, handle, balance, currency, minor_units)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (u["id"], u["email"], pw_hash, u["display_name"],
                 u["handle"], u["balance"], currency, minor_units),
            )

        for p in fixture.get("payments", []):
            db.execute(
                """INSERT INTO payments (id, from_user_id, to_user_id, amount, note, status, idempotency_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (p["id"], p["from_user_id"], p["to_user_id"],
                 p["amount"], p.get("note"), p.get("status", "completed"),
                 p.get("idempotency_key")),
            )

        for r in fixture.get("requests", []):
            db.execute(
                """INSERT INTO requests (id, from_user_id, to_user_id, amount, note, status, idempotency_key)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (r["id"], r["from_user_id"], r["to_user_id"],
                 r["amount"], r.get("note"), r.get("status", "pending"),
                 r.get("idempotency_key")),
            )

        db.commit()
    finally:
        db.close()

    # Also clear in-memory auth tokens
    from src.auth import clear_tokens
    clear_tokens()