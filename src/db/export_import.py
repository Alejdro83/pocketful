"""Export / Import helpers for Pocketful — full state snapshot."""

from __future__ import annotations

import json
import sqlite3

from src.db.schema import SCHEMA_SQL, TABLES


# ---------------------------------------------------------------------------
# export_state
# ---------------------------------------------------------------------------

def export_state(db: sqlite3.Connection) -> dict:
    """Return the complete DB state as a JSON-safe dict."""
    def _rows(table: str) -> list[dict]:
        return [dict(r) for r in db.execute(f"SELECT * FROM {table}").fetchall()]

    operators = [
        r["user_id"] for r in db.execute("SELECT * FROM settlement_operators").fetchall()
    ]

    return {
        "track": "pocketful",
        "format_version": 1,
        "state": {
            "users": _rows("users"),
            "wallets": _rows("wallets"),
            "transactions": _rows("transactions"),
            "ledger_entries": _rows("ledger_entries"),
            "requests": _rows("requests"),
            "settlements": _rows("settlements"),
            "idempotency_records": _rows("idempotency_records"),
            "settlement_operators": operators,
        },
    }


# ---------------------------------------------------------------------------
# import_state
# ---------------------------------------------------------------------------

def import_state(db: sqlite3.Connection, body: dict) -> None:
    """Atomically replace all state from *body*.

    Raises ``ValueError`` if track/format_version mismatch.
    """
    if body.get("track") != "pocketful":
        raise ValueError("bad_track")
    if body.get("format_version") != 1:
        raise ValueError("bad_format_version")

    state = body.get("state", {})

    # 1. Drop everything
    db.execute("PRAGMA foreign_keys = OFF")
    for table in TABLES:
        db.execute(f"DROP TABLE IF EXISTS {table}")
    db.execute("PRAGMA foreign_keys = ON")

    # 2. Recreate schema
    db.executescript(SCHEMA_SQL)

    # 3. Insert data table-by-table
    for u in state.get("users", []):
        db.execute(
            "INSERT INTO users (id, email, handle, password_hash, display_name, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (u["id"], u["email"], u["handle"], u["password_hash"],
             u["display_name"], u.get("created_at")),
        )

    for w in state.get("wallets", []):
        db.execute(
            "INSERT INTO wallets (id, user_id, currency, minor_units, cached_balance, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (w.get("id"), w["user_id"], w["currency"], w["minor_units"],
             w["cached_balance"], w.get("created_at")),
        )

    for t in state.get("transactions", []):
        db.execute(
            """INSERT INTO transactions
                   (id, type, from_user_id, to_user_id, amount, note, visibility,
                    request_id, settlement_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (t["id"], t["type"], t["from_user_id"], t["to_user_id"],
             t["amount"], t.get("note", ""), t.get("visibility", "public"),
             t.get("request_id"), t.get("settlement_id"), t["created_at"]),
        )

    for le in state.get("ledger_entries", []):
        db.execute(
            "INSERT INTO ledger_entries (id, transaction_id, account_id, amount, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (le.get("id"), le["transaction_id"], le["account_id"],
             le["amount"], le.get("created_at")),
        )

    for r in state.get("requests", []):
        db.execute(
            """INSERT INTO requests (id, requester_id, payer_id, amount, note, status, payment_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (r["id"], r["requester_id"], r["payer_id"], r["amount"],
             r.get("note", ""), r.get("status", "pending"),
             r.get("payment_id"), r.get("created_at")),
        )

    for s in state.get("settlements", []):
        db.execute(
            "INSERT INTO settlements (id, operator_id, committed_at, created_at) VALUES (?, ?, ?, ?)",
            (s["id"], s["operator_id"], s["committed_at"], s.get("created_at")),
        )

    for ir in state.get("idempotency_records", []):
        db.execute(
            """INSERT INTO idempotency_records
                   (user_id, idempotency_key, request_hash, response_body, status_code, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (ir["user_id"], ir["idempotency_key"], ir["request_hash"],
             ir["response_body"], ir["status_code"], ir.get("created_at")),
        )

    for uid in state.get("settlement_operators", []):
        db.execute(
            "INSERT OR IGNORE INTO settlement_operators (user_id) VALUES (?)", (uid,)
        )

    db.commit()
