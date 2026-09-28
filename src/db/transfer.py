"""Core money-movement functions for Pocketful.

Every public function:
  - Opens with BEGIN IMMEDIATE to grab the SQLite write lock.
  - Checks idempotency_key first (retry-safe).
  - Commits only after all invariants hold.
  - Rolls back on any exception so the DB is never left half-done.
"""

import hashlib
import sqlite3
from typing import Optional

from .exceptions import DuplicateTransaction, InsufficientFunds


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _request_hash(from_id: int, to_id: int, amount_cents: int) -> str:
    """Deterministic hash of transfer parameters (for idempotent dedup)."""
    raw = f"{from_id}:{to_id}:{amount_cents}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _existing_idempotent(
    db: sqlite3.Connection, idempotency_key: str
) -> Optional[dict]:
    """If idempotency_key already exists in transactions, return its result."""
    row = db.execute(
        "SELECT id, status FROM transactions WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if row is not None:
        return {"transaction_id": row["id"], "status": row["status"]}
    return None


# ---------------------------------------------------------------------------
# transfer
# ---------------------------------------------------------------------------

def transfer(
    db: sqlite3.Connection,
    from_id: int,
    to_id: int,
    amount_cents: int,
    idempotency_key: str,
) -> dict:
    """Move *amount_cents* from *from_id* to *to_id* atomically.

    Returns ``{"transaction_id": int, "status": "completed"}``.
    Raises ``InsufficientFunds`` or ``DuplicateTransaction`` on failure.
    """
    if from_id == to_id:
        raise ValueError("Cannot transfer to the same account")
    if amount_cents <= 0:
        raise ValueError("amount_cents must be positive")

    try:
        db.execute("BEGIN IMMEDIATE")

        # 1. Idempotency check
        existing = _existing_idempotent(db, idempotency_key)
        if existing is not None:
            db.execute("COMMIT")
            return existing

        # 2. Lock accounts in ascending id order (deadlock prevention)
        accounts = db.execute(
            "SELECT id, balance_cents FROM accounts WHERE id IN (?, ?) ORDER BY id",
            (from_id, to_id),
        ).fetchall()
        if len(accounts) < 2:
            db.execute("ROLLBACK")
            raise ValueError("Both from_id and to_id must exist")

        account_map = {row["id"]: row["balance_cents"] for row in accounts}
        available = account_map[from_id]

        # 3. Sufficient balance check
        if available < amount_cents:
            db.execute("ROLLBACK")
            raise InsufficientFunds(from_id, amount_cents, available)

        # 4. Insert transaction (pending)
        req_hash = _request_hash(from_id, to_id, amount_cents)
        cur = db.execute(
            """INSERT INTO transactions
               (idempotency_key, request_hash, type, from_account_id, to_account_id,
                amount_cents, status)
               VALUES (?, ?, 'transfer', ?, ?, ?, 'pending')""",
            (idempotency_key, req_hash, from_id, to_id, amount_cents),
        )
        txn_id = cur.lastrowid

        # 5. Insert ledger entries (debit sender, credit receiver)
        db.execute(
            "INSERT INTO ledger_entries (transaction_id, account_id, amount_cents) VALUES (?, ?, ?)",
            (txn_id, from_id, -amount_cents),
        )
        db.execute(
            "INSERT INTO ledger_entries (transaction_id, account_id, amount_cents) VALUES (?, ?, ?)",
            (txn_id, to_id, amount_cents),
        )

        # 6. Update balances
        db.execute(
            "UPDATE accounts SET balance_cents = balance_cents - ? WHERE id = ?",
            (amount_cents, from_id),
        )
        db.execute(
            "UPDATE accounts SET balance_cents = balance_cents + ? WHERE id = ?",
            (amount_cents, to_id),
        )

        # 7. Mark completed — fires the double-entry trigger (SUM == 0 check)
        db.execute(
            "UPDATE transactions SET status = 'completed' WHERE id = ?", (txn_id,)
        )

        db.execute("COMMIT")
        return {"transaction_id": txn_id, "status": "completed"}

    except Exception:
        # Ensure we don't leave an open transaction on unexpected errors
        try:
            db.execute("ROLLBACK")
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# deposit  (external money in)
# ---------------------------------------------------------------------------

def deposit(
    db: sqlite3.Connection,
    account_id: int,
    amount_cents: int,
    idempotency_key: str,
) -> dict:
    """Credit *amount_cents* into *account_id* from outside the system.

    Uses a virtual ``from_account_id = NULL`` (no debit side — money enters).
    To satisfy the double-entry trigger, we pair the credit with an equal
    debit against a synthetic ``system_account`` (id 0) whose balance is
    allowed to go negative (it represents external capital).
    """
    if amount_cents <= 0:
        raise ValueError("amount_cents must be positive")

    SYSTEM_ACCOUNT_ID = 0

    try:
        db.execute("BEGIN IMMEDIATE")

        existing = _existing_idempotent(db, idempotency_key)
        if existing is not None:
            db.execute("COMMIT")
            return existing

        # Ensure system account exists
        db.execute(
            "INSERT OR IGNORE INTO accounts (id, username, balance_cents) VALUES (?, '__system__', 0)",
            (SYSTEM_ACCOUNT_ID,),
        )

        # Ensure target account exists
        acct = db.execute(
            "SELECT id FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if acct is None:
            db.execute("ROLLBACK")
            raise ValueError(f"Account {account_id} does not exist")

        req_hash = _request_hash(0, account_id, amount_cents)
        cur = db.execute(
            """INSERT INTO transactions
               (idempotency_key, request_hash, type, from_account_id, to_account_id,
                amount_cents, status)
               VALUES (?, ?, 'deposit', ?, ?, ?, 'pending')""",
            (idempotency_key, req_hash, SYSTEM_ACCOUNT_ID, account_id, amount_cents),
        )
        txn_id = cur.lastrowid

        # Ledger: debit system, credit user
        db.execute(
            "INSERT INTO ledger_entries (transaction_id, account_id, amount_cents) VALUES (?, ?, ?)",
            (txn_id, SYSTEM_ACCOUNT_ID, -amount_cents),
        )
        db.execute(
            "INSERT INTO ledger_entries (transaction_id, account_id, amount_cents) VALUES (?, ?, ?)",
            (txn_id, account_id, amount_cents),
        )

        # Balances
        db.execute(
            "UPDATE accounts SET balance_cents = balance_cents - ? WHERE id = ?",
            (amount_cents, SYSTEM_ACCOUNT_ID),
        )
        db.execute(
            "UPDATE accounts SET balance_cents = balance_cents + ? WHERE id = ?",
            (amount_cents, account_id),
        )

        db.execute(
            "UPDATE transactions SET status = 'completed' WHERE id = ?", (txn_id,)
        )

        db.execute("COMMIT")
        return {"transaction_id": txn_id, "status": "completed"}

    except Exception:
        try:
            db.execute("ROLLBACK")
        except Exception:
            pass
        raise


# ---------------------------------------------------------------------------
# withdrawal  (money out of the system)
# ---------------------------------------------------------------------------

def withdrawal(
    db: sqlite3.Connection,
    account_id: int,
    amount_cents: int,
    idempotency_key: str,
) -> dict:
    """Debit *amount_cents* from *account_id* out of the system.

    Mirror image of deposit — credits the system account.
    """
    if amount_cents <= 0:
        raise ValueError("amount_cents must be positive")

    SYSTEM_ACCOUNT_ID = 0

    try:
        db.execute("BEGIN IMMEDIATE")

        existing = _existing_idempotent(db, idempotency_key)
        if existing is not None:
            db.execute("COMMIT")
            return existing

        db.execute(
            "INSERT OR IGNORE INTO accounts (id, username, balance_cents) VALUES (?, '__system__', 0)",
            (SYSTEM_ACCOUNT_ID,),
        )

        acct = db.execute(
            "SELECT id, balance_cents FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if acct is None:
            db.execute("ROLLBACK")
            raise ValueError(f"Account {account_id} does not exist")

        if acct["balance_cents"] < amount_cents:
            db.execute("ROLLBACK")
            raise InsufficientFunds(account_id, amount_cents, acct["balance_cents"])

        req_hash = _request_hash(account_id, 0, amount_cents)
        cur = db.execute(
            """INSERT INTO transactions
               (idempotency_key, request_hash, type, from_account_id, to_account_id,
                amount_cents, status)
               VALUES (?, ?, 'withdrawal', ?, ?, ?, 'pending')""",
            (idempotency_key, req_hash, account_id, SYSTEM_ACCOUNT_ID, amount_cents),
        )
        txn_id = cur.lastrowid

        db.execute(
            "INSERT INTO ledger_entries (transaction_id, account_id, amount_cents) VALUES (?, ?, ?)",
            (txn_id, account_id, -amount_cents),
        )
        db.execute(
            "INSERT INTO ledger_entries (transaction_id, account_id, amount_cents) VALUES (?, ?, ?)",
            (txn_id, SYSTEM_ACCOUNT_ID, amount_cents),
        )

        db.execute(
            "UPDATE accounts SET balance_cents = balance_cents - ? WHERE id = ?",
            (amount_cents, account_id),
        )
        db.execute(
            "UPDATE accounts SET balance_cents = balance_cents + ? WHERE id = ?",
            (amount_cents, SYSTEM_ACCOUNT_ID),
        )

        db.execute(
            "UPDATE transactions SET status = 'completed' WHERE id = ?", (txn_id,)
        )

        db.execute("COMMIT")
        return {"transaction_id": txn_id, "status": "completed"}

    except Exception:
        try:
            db.execute("ROLLBACK")
        except Exception:
            pass
        raise
