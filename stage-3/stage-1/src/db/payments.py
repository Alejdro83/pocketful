"""Core payment functions for Pocketful — new transfer engine for hackathon schema.

Every public function:
  - Opens with BEGIN IMMEDIATE to grab the SQLite write lock.
  - Checks idempotency_key first (retry-safe).
  - Commits only after all invariants hold.
  - Rolls back on any exception so the DB is never left half-done.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Optional

from .exceptions import IdempotencyKeyReuse, InsufficientFunds, SelfPayment
from .idempotency import check_idempotency, save_idempotency


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _payment_id() -> str:
    return "p_" + uuid.uuid4().hex[:8]


def _row_to_payment(row: sqlite3.Row) -> dict:
    """Convert a transaction row to a payment dict."""
    return {
        "payment_id": row["id"],
        "from_user_id": row["from_user_id"],
        "from_handle": row["from_handle"],
        "to_user_id": row["to_user_id"],
        "to_handle": row["to_handle"],
        "amount": row["amount"],
        "currency": row["currency"],
        "note": row["note"],
        "visibility": row["visibility"],
        "request_id": row["request_id"],
        "created_at": row["created_at"],
    }


# ---------------------------------------------------------------------------
# make_payment
# ---------------------------------------------------------------------------

def make_payment(
    db: sqlite3.Connection,
    from_user_id: str,
    to_user_id: str,
    amount: int,
    note: str = "",
    visibility: str = "public",
    request_id: Optional[str] = None,
    settlement_id: Optional[str] = None,
    txn_type: str = "payment",
    idempotency_key: Optional[str] = None,
    user_id_for_idempotency: Optional[str] = None,
) -> dict:
    """Atomically move *amount* (minor units) from one user to another.

    Returns the payment dict on success.
    Raises InsufficientFunds, SelfPayment, or IdempotencyKeyReuse on failure.
    """
    # --- pre-tx validations ---
    if from_user_id == to_user_id:
        raise SelfPayment(from_user_id)
    if amount <= 0:
        raise ValueError("amount must be positive")

    # Build the request body for idempotency hashing
    req_body = {
        "from_user_id": from_user_id,
        "to_user_id": to_user_id,
        "amount": amount,
        "note": note,
        "visibility": visibility,
        "request_id": request_id,
        "settlement_id": settlement_id,
    }

    try:
        db.execute("BEGIN IMMEDIATE")

        # --- idempotency check ---
        if idempotency_key and user_id_for_idempotency:
            action, data = check_idempotency(db, user_id_for_idempotency, idempotency_key, req_body)
            if action == "replay":
                db.execute("COMMIT")
                return data
            if action == "conflict":
                db.execute("ROLLBACK")
                raise IdempotencyKeyReuse(user_id_for_idempotency, idempotency_key)
            # action == "proceed" → fall through

        # --- lock wallets in ascending user_id order ---
        wallets = db.execute(
            "SELECT user_id, currency, cached_balance FROM wallets "
            "WHERE user_id IN (?, ?) ORDER BY user_id",
            (from_user_id, to_user_id),
        ).fetchall()
        if len(wallets) < 2:
            db.execute("ROLLBACK")
            raise ValueError("Both from_user_id and to_user_id must have wallets")

        wmap = {w["user_id"]: w for w in wallets}
        from_wallet = wmap[from_user_id]
        to_wallet = wmap[to_user_id]

        # --- balance check ---
        if from_wallet["cached_balance"] < amount:
            db.execute("ROLLBACK")
            raise InsufficientFunds(from_user_id, amount, from_wallet["cached_balance"])

        # --- insert transaction ---
        payment_id = _payment_id()
        now = _now_iso()

        db.execute(
            """INSERT INTO transactions
               (id, type, from_user_id, to_user_id, amount, note, visibility,
                request_id, settlement_id, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (payment_id, txn_type, from_user_id, to_user_id, amount, note, visibility,
             request_id, settlement_id, now),
        )

        # --- ledger entries ---
        db.execute(
            "INSERT INTO ledger_entries (transaction_id, account_id, amount) VALUES (?, ?, ?)",
            (payment_id, from_user_id, -amount),
        )
        db.execute(
            "INSERT INTO ledger_entries (transaction_id, account_id, amount) VALUES (?, ?, ?)",
            (payment_id, to_user_id, amount),
        )

        # --- update wallet balances ---
        db.execute(
            "UPDATE wallets SET cached_balance = cached_balance - ? WHERE user_id = ?",
            (amount, from_user_id),
        )
        db.execute(
            "UPDATE wallets SET cached_balance = cached_balance + ? WHERE user_id = ?",
            (amount, to_user_id),
        )

        # --- resolve handles for response ---
        from_handle = db.execute(
            "SELECT handle FROM users WHERE id = ?", (from_user_id,)
        ).fetchone()["handle"]
        to_handle = db.execute(
            "SELECT handle FROM users WHERE id = ?", (to_user_id,)
        ).fetchone()["handle"]

        result = {
            "payment_id": payment_id,
            "from_user_id": from_user_id,
            "from_handle": from_handle,
            "to_user_id": to_user_id,
            "to_handle": to_handle,
            "amount": amount,
            "currency": from_wallet["currency"],
            "note": note,
            "visibility": visibility,
            "request_id": request_id,
            "created_at": now,
        }

        # --- persist idempotency record (only on success, inside tx) ---
        if idempotency_key and user_id_for_idempotency:
            save_idempotency(db, user_id_for_idempotency, idempotency_key,
                             req_body, result, 200)

        db.execute("COMMIT")
        return result

    except Exception:
        try:
            db.execute("ROLLBACK")
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# get_balance
# ---------------------------------------------------------------------------

def get_balance(db: sqlite3.Connection, user_id: str) -> int:
    """Return cached_balance for user_id from wallets."""
    row = db.execute(
        "SELECT cached_balance FROM wallets WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"No wallet for user {user_id}")
    return row["cached_balance"]


# ---------------------------------------------------------------------------
# get_activity
# ---------------------------------------------------------------------------

def get_activity(
    db: sqlite3.Connection,
    user_id: str,
    limit: int = 50,
    offset: int = 0,
) -> dict:
    """Return payments visible to user_id.

    Visibility rules:
      - public payments visible to everyone
      - private payments visible only to sender or receiver
    """
    rows = db.execute(
        """SELECT t.id, t.type, t.from_user_id, fu.handle AS from_handle,
                  t.to_user_id, tu.handle AS to_handle,
                  t.amount, w.currency, t.note, t.visibility,
                  t.request_id, t.created_at
           FROM transactions t
           JOIN users fu ON fu.id = t.from_user_id
           JOIN users tu ON tu.id = t.to_user_id
           JOIN wallets w ON w.user_id = t.from_user_id
           WHERE (t.visibility = 'public'
                  OR t.from_user_id = ?
                  OR t.to_user_id = ?)
           ORDER BY t.created_at DESC
           LIMIT ? OFFSET ?""",
        (user_id, user_id, limit + 1, offset),
    ).fetchall()

    has_more = len(rows) > limit
    rows = rows[:limit]

    payments = []
    for r in rows:
        payments.append({
            "payment_id": r["id"],
            "from_user_id": r["from_user_id"],
            "from_handle": r["from_handle"],
            "to_user_id": r["to_user_id"],
            "to_handle": r["to_handle"],
            "amount": r["amount"],
            "currency": r["currency"],
            "note": r["note"],
            "visibility": r["visibility"],
            "request_id": r["request_id"],
            "created_at": r["created_at"],
        })

    return {"payments": payments, "has_more": has_more}


# ---------------------------------------------------------------------------
# get_payment
# ---------------------------------------------------------------------------

def get_payment(db: sqlite3.Connection, payment_id: str) -> Optional[dict]:
    """Return a single payment dict by payment_id, or None."""
    row = db.execute(
        """SELECT t.id, t.type, t.from_user_id, fu.handle AS from_handle,
                  t.to_user_id, tu.handle AS to_handle,
                  t.amount, w.currency, t.note, t.visibility,
                  t.request_id, t.created_at
           FROM transactions t
           JOIN users fu ON fu.id = t.from_user_id
           JOIN users tu ON tu.id = t.to_user_id
           JOIN wallets w ON w.user_id = t.from_user_id
           WHERE t.id = ? AND t.type = 'payment'""",
        (payment_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "payment_id": row["id"],
        "from_user_id": row["from_user_id"],
        "from_handle": row["from_handle"],
        "to_user_id": row["to_user_id"],
        "to_handle": row["to_handle"],
        "amount": row["amount"],
        "currency": row["currency"],
        "note": row["note"],
        "visibility": row["visibility"],
        "request_id": row["request_id"],
        "created_at": row["created_at"],
    }