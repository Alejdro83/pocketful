"""Pocketful extended database schema — users, wallets, transactions, ledger, requests, settlements."""

import sqlite3
import re
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "pocketful.db"

SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    handle TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    display_name TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS wallets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL REFERENCES users(id) UNIQUE,
    currency TEXT NOT NULL,
    minor_units INTEGER NOT NULL CHECK(minor_units IN (0, 2, 3)),
    cached_balance BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK(type IN ('payment','request_payment','settlement_payment','split_payment','refund')),
    from_user_id TEXT NOT NULL REFERENCES users(id),
    to_user_id TEXT NOT NULL REFERENCES users(id),
    amount BIGINT NOT NULL CHECK(amount > 0),
    note TEXT NOT NULL DEFAULT '',
    visibility TEXT NOT NULL DEFAULT 'public' CHECK(visibility IN ('public','private')),
    request_id TEXT,
    settlement_id TEXT,\n        refund_of TEXT,
    created_at TIMESTAMP NOT NULL,
    UNIQUE(from_user_id, id)
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id TEXT NOT NULL REFERENCES transactions(id),
    account_id TEXT NOT NULL,
    amount BIGINT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    requester_id TEXT NOT NULL REFERENCES users(id),
    payer_id TEXT NOT NULL REFERENCES users(id),
    amount BIGINT NOT NULL CHECK(amount > 0),
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','paid','declined','cancelled')),
    payment_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settlements (
    id TEXT PRIMARY KEY,
    operator_id TEXT NOT NULL REFERENCES users(id),
    committed_at TIMESTAMP NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS idempotency_records (
    user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    response_body TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, idempotency_key)
);

CREATE TABLE IF NOT EXISTS settlement_operators (
    user_id TEXT PRIMARY KEY REFERENCES users(id)
);

    CREATE TABLE IF NOT EXISTS payment_revisions (
        id TEXT PRIMARY KEY,
        payment_id TEXT NOT NULL,
        revision_number INTEGER NOT NULL,
        amount BIGINT NOT NULL,
        effective_at TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        reason TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(payment_id, revision_number)
    );
    

-- Indexes
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_users_handle ON users(handle);
CREATE INDEX IF NOT EXISTS idx_wallets_user_id ON wallets(user_id);
CREATE INDEX IF NOT EXISTS idx_transactions_from_user ON transactions(from_user_id);
CREATE INDEX IF NOT EXISTS idx_transactions_to_user ON transactions(to_user_id);
CREATE INDEX IF NOT EXISTS idx_transactions_created_at ON transactions(created_at);
CREATE INDEX IF NOT EXISTS idx_ledger_entries_txn ON ledger_entries(transaction_id);
CREATE INDEX IF NOT EXISTS idx_ledger_entries_account ON ledger_entries(account_id);
CREATE INDEX IF NOT EXISTS idx_requests_requester ON requests(requester_id);
CREATE INDEX IF NOT EXISTS idx_requests_payer ON requests(payer_id);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status);
CREATE INDEX IF NOT EXISTS idx_payment_revisions_payment ON payment_revisions(payment_id);
"""

TABLES = [
    "idempotency_records",
    "ledger_entries",
    "requests",
    "transactions",
    "payment_revisions",
        "settlement_operators",
    "settlements",
    "wallets",
    "users",
]


def get_db() -> sqlite3.Connection:
    """Return a new SQLite connection with WAL mode and manual transactions."""
    conn = sqlite3.connect(str(DB_PATH), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    """Create all tables from SCHEMA_SQL."""
    conn = get_db()
    try:
        conn.executescript(SCHEMA_SQL)
    finally:
        conn.close()


def derive_handle(email: str) -> str:
    """Derive a handle from email: local part, lowercase, replace non-alnum/_ with _, truncate to 20."""
    local = email.split("@")[0]
    local = local.lower()
    local = re.sub(r"[^a-z0-9_]", "_", local)
    return local[:20]


def reset_db(fixture: dict) -> None:
    """Drop all tables, recreate, and seed from fixture dict.

    Fixture format:
    {
        "currency": "EUR",
        "minor_units": 2,
        "users": [{"id": "u_ada", "email": "...", "password": "...", "display_name": "...", "handle": "...", "balance": 10000}],
        "payments": [{"id": "p_1", "from_user_id": "u_ada", "to_user_id": "u_bob", "amount": 500, "note": "...", "visibility": "public"}],
        "requests": [{"id": "rq_1", "requester_id": "u_bob", "payer_id": "u_ada", "amount": 1200, "note": "...", "status": "pending"}],
        "payment_revisions",
        "settlement_operators": ["u_ada"]  # optional
    }
    """
    import bcrypt
    from datetime import datetime, timezone

    conn = get_db()
    try:
        # 1. Drop all tables
        conn.execute("PRAGMA foreign_keys = OFF")
        for table in TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        conn.execute("PRAGMA foreign_keys = ON")

        # 2. Recreate schema
        conn.executescript(SCHEMA_SQL)

        currency = fixture["currency"]
        minor_units = fixture["minor_units"]

        # 3. Insert users with bcrypt-hashed passwords
        for u in fixture["users"]:
            pw_hash = bcrypt.hashpw(u["password"].encode(), bcrypt.gensalt()).decode()
            handle = u.get("handle") or derive_handle(u["email"])
            conn.execute(
                "INSERT INTO users (id, email, handle, password_hash, display_name) VALUES (?, ?, ?, ?, ?)",
                (u["id"], u["email"], handle, pw_hash, u["display_name"]),
            )

        # 4. Insert wallets (balance is the final balance after all seeded payments)
        for u in fixture["users"]:
            conn.execute(
                "INSERT INTO wallets (user_id, currency, minor_units, cached_balance) VALUES (?, ?, ?, ?)",
                (u["id"], currency, minor_units, u["balance"]),
            )

        # 5. Insert seeded payments into transactions + ledger_entries
        now = datetime.now(timezone.utc).isoformat()
        for p in fixture.get("payments", []):
            conn.execute(
                "INSERT INTO transactions (id, type, from_user_id, to_user_id, amount, note, visibility, created_at) "
                "VALUES (?, 'payment', ?, ?, ?, ?, ?, ?)",
                (p["id"], p["from_user_id"], p["to_user_id"], p["amount"],
                 p.get("note", ""), p.get("visibility", "public"), now),
            )
            # Debit sender
            conn.execute(
                "INSERT INTO ledger_entries (transaction_id, account_id, amount) VALUES (?, ?, ?)",
                (p["id"], p["from_user_id"], -p["amount"]),
            )
            # Credit receiver
            conn.execute(
                "INSERT INTO ledger_entries (transaction_id, account_id, amount) VALUES (?, ?, ?)",
                (p["id"], p["to_user_id"], p["amount"]),
            )

        # 6. Insert seeded requests
        for r in fixture.get("requests", []):
            conn.execute(
                "INSERT INTO requests (id, requester_id, payer_id, amount, note, status) VALUES (?, ?, ?, ?, ?, ?)",
                (r["id"], r["requester_id"], r["payer_id"], r["amount"],
                 r.get("note", ""), r.get("status", "pending")),
            )

        # 7. Set settlement operators
        for uid in fixture.get("settlement_operator_ids", fixture.get("settlement_operators", [])):
            conn.execute("INSERT OR IGNORE INTO settlement_operators (user_id) VALUES (?)", (uid,))

        # 8. Validate: SUM(wallet balances) == expected total from fixture balances
        expected_total = sum(u["balance"] for u in fixture["users"])
        row = conn.execute("SELECT COALESCE(SUM(cached_balance), 0) as total FROM wallets").fetchone()
        actual_total = row["total"]
        if actual_total != expected_total:
            raise ValueError(
                f"Wallet balance validation failed: expected {expected_total}, got {actual_total}"
            )

    finally:
        conn.close()
