"""Settlement operations for Pocketful — atomic multi-transfer settlements."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------

def _next_settlement_id(db: sqlite3.Connection) -> str:
    rows = db.execute("SELECT id FROM settlements").fetchall()
    max_n = 0
    for row in rows:
        sid = row["id"]
        if sid.startswith("st_"):
            try:
                max_n = max(max_n, int(sid[3:]))
            except ValueError:
                pass
    return f"st_{max_n + 1}"


def _next_txn_id(db: sqlite3.Connection) -> str:
    rows = db.execute("SELECT id FROM transactions").fetchall()
    max_n = 0
    for row in rows:
        tid = row["id"]
        if tid.startswith("txn_"):
            try:
                max_n = max(max_n, int(tid[4:]))
            except ValueError:
                pass
    return f"txn_{max_n + 1}"


def _build_payment_dict(db: sqlite3.Connection, txn_id: str) -> dict:
    txn = db.execute("SELECT * FROM transactions WHERE id = ?", (txn_id,)).fetchone()
    fu = db.execute("SELECT handle FROM users WHERE id = ?", (txn["from_user_id"],)).fetchone()
    tu = db.execute("SELECT handle FROM users WHERE id = ?", (txn["to_user_id"],)).fetchone()
    w = db.execute("SELECT currency FROM wallets WHERE user_id = ?", (txn["from_user_id"],)).fetchone()
    return {
        "payment_id": txn["id"],
        "from_user_id": txn["from_user_id"],
        "from_handle": fu["handle"],
        "to_user_id": txn["to_user_id"],
        "to_handle": tu["handle"],
        "amount": txn["amount"],
        "currency": w["currency"],
        "note": txn["note"],
        "visibility": txn["visibility"],
        "request_id": txn["request_id"],
        "created_at": txn["created_at"],
    }


# ---------------------------------------------------------------------------
# create_settlement
# ---------------------------------------------------------------------------

def create_settlement(
    db: sqlite3.Connection,
    operator_id: str,
    transfers: list[dict],
) -> dict:
    """Atomically execute all *transfers* as a single settlement.

    Simulates every transfer first; if any wallet would go negative the
    entire settlement is rejected (all-or-nothing).

    Returns ``{settlement_id, committed_at, payments}``.
    Raises ``PermissionError`` (not operator) or ``ValueError`` (not_found,
    insufficient_funds).
    """
    # 1. Operator check
    op = db.execute(
        "SELECT 1 FROM settlement_operators WHERE user_id = ?", (operator_id,)
    ).fetchone()
    if not op:
        raise PermissionError("forbidden")

    # 2. Resolve handles → user_ids, collect current balances
    resolved: list[dict] = []
    balances: dict[str, int] = {}

    for t in transfers:
        from_row = db.execute(
            "SELECT id FROM users WHERE handle = ?", (t["from_handle"],)
        ).fetchone()
        to_row = db.execute(
            "SELECT id FROM users WHERE handle = ?", (t["to_handle"],)
        ).fetchone()
        if not from_row or not to_row:
            raise ValueError("not_found")

        resolved.append({
            "from_user_id": from_row["id"],
            "to_user_id": to_row["id"],
            "amount": t["amount"],
            "note": t.get("note", ""),
            "visibility": t.get("visibility", "public"),
        })

        for uid in (from_row["id"], to_row["id"]):
            if uid not in balances:
                w = db.execute(
                    "SELECT cached_balance FROM wallets WHERE user_id = ?", (uid,)
                ).fetchone()
                if w is None:
                    raise ValueError("not_found")
                balances[uid] = w["cached_balance"]

    # 3. Simulate
    for t in resolved:
        balances[t["from_user_id"]] -= t["amount"]
        balances[t["to_user_id"]] += t["amount"]

    # 4. All wallets must stay ≥ 0
    for uid, bal in balances.items():
        if bal < 0:
            raise ValueError("insufficient_funds")

    # 5. Commit
    settlement_id = _next_settlement_id(db)
    now = datetime.now(timezone.utc).isoformat()

    db.execute(
        "INSERT INTO settlements (id, operator_id, committed_at) VALUES (?, ?, ?)",
        (settlement_id, operator_id, now),
    )

    payments = []
    for t in resolved:
        txn_id = _next_txn_id(db)
        db.execute(
            """INSERT INTO transactions
                   (id, type, from_user_id, to_user_id, amount, note, visibility,
                    settlement_id, created_at)
               VALUES (?, 'settlement_payment', ?, ?, ?, ?, ?, ?, ?)""",
            (txn_id, t["from_user_id"], t["to_user_id"], t["amount"],
             t["note"], t["visibility"], settlement_id, now),
        )
        db.execute(
            "INSERT INTO ledger_entries (transaction_id, account_id, amount) VALUES (?, ?, ?)",
            (txn_id, t["from_user_id"], -t["amount"]),
        )
        db.execute(
            "INSERT INTO ledger_entries (transaction_id, account_id, amount) VALUES (?, ?, ?)",
            (txn_id, t["to_user_id"], t["amount"]),
        )
        db.execute(
            "UPDATE wallets SET cached_balance = cached_balance - ? WHERE user_id = ?",
            (t["amount"], t["from_user_id"]),
        )
        db.execute(
            "UPDATE wallets SET cached_balance = cached_balance + ? WHERE user_id = ?",
            (t["amount"], t["to_user_id"]),
        )

        payments.append(_build_payment_dict(db, txn_id))

    db.commit()
    return {
        "settlement_id": settlement_id,
        "committed_at": now,
        "payments": payments,
    }
