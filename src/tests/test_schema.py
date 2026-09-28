"""Tests for pocketful extended database schema."""

import sqlite3
import sys
from pathlib import Path

import bcrypt
import pytest

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from src.db.schema import get_db, init_db, reset_db, derive_handle, DB_PATH


@pytest.fixture(autouse=True)
def clean_db():
    """Remove DB file before each test for isolation."""
    if DB_PATH.exists():
        DB_PATH.unlink()
    yield
    if DB_PATH.exists():
        DB_PATH.unlink()


SAMPLE_FIXTURE = {
    "currency": "EUR",
    "minor_units": 2,
    "users": [
        {
            "id": "u_ada",
            "email": "ada@example.com",
            "password": "correct horse",
            "display_name": "Ada",
            "handle": "ada",
            "balance": 10000,
        },
        {
            "id": "u_bob",
            "email": "bob@example.com",
            "password": "battery staple",
            "display_name": "Bob",
            "handle": "bob",
            "balance": 5000,
        },
    ],
    "payments": [
        {
            "id": "p_1",
            "from_user_id": "u_ada",
            "to_user_id": "u_bob",
            "amount": 500,
            "note": "coffee",
            "visibility": "public",
        }
    ],
    "requests": [
        {
            "id": "rq_1",
            "requester_id": "u_bob",
            "payer_id": "u_ada",
            "amount": 1200,
            "note": "taxi",
            "status": "pending",
        }
    ],
}


def test_create_users_with_bcrypt():
    """Users created via reset_db have bcrypt-hashed passwords."""
    reset_db(SAMPLE_FIXTURE)
    conn = get_db()
    try:
        rows = conn.execute("SELECT id, password_hash FROM users ORDER BY id").fetchall()
        assert len(rows) == 2
        for row in rows:
            pw_hash = row["password_hash"]
            # bcrypt hashes start with $2b$
            assert pw_hash.startswith("$2b$"), f"Expected bcrypt hash for {row['id']}"
    finally:
        conn.close()


def test_fixture_reset_loads_users_wallets_payments_requests():
    """reset_db correctly loads all fixture data."""
    reset_db(SAMPLE_FIXTURE)
    conn = get_db()
    try:
        users = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
        assert len(users) == 2
        assert users[0]["id"] == "u_ada"
        assert users[1]["id"] == "u_bob"

        wallets = conn.execute("SELECT * FROM wallets ORDER BY user_id").fetchall()
        assert len(wallets) == 2
        assert wallets[0]["currency"] == "EUR"
        assert wallets[0]["minor_units"] == 2

        txns = conn.execute("SELECT * FROM transactions").fetchall()
        assert len(txns) == 1
        assert txns[0]["id"] == "p_1"
        assert txns[0]["amount"] == 500

        entries = conn.execute("SELECT * FROM ledger_entries ORDER BY id").fetchall()
        assert len(entries) == 2
        # Debit then credit
        assert entries[0]["amount"] == -500
        assert entries[1]["amount"] == 500

        reqs = conn.execute("SELECT * FROM requests").fetchall()
        assert len(reqs) == 1
        assert reqs[0]["id"] == "rq_1"
        assert reqs[0]["status"] == "pending"
    finally:
        conn.close()


def test_sum_wallet_balances_after_reset():
    """SUM of wallet cached_balance matches expected total after reset."""
    reset_db(SAMPLE_FIXTURE)
    conn = get_db()
    try:
        row = conn.execute("SELECT SUM(cached_balance) as total FROM wallets").fetchone()
        expected = sum(u["balance"] for u in SAMPLE_FIXTURE["users"])
        assert row["total"] == expected
    finally:
        conn.close()


def test_handle_derivation_from_email():
    """Handle derivation: lowercase, replace non-alnum/_ with _, truncate to 20."""
    assert derive_handle("ada@example.com") == "ada"
    assert derive_handle("Bob.Smith@example.com") == "bob_smith"
    assert derive_handle("user+tag@example.com") == "user_tag"
    assert derive_handle("A" * 30 + "@example.com") == ("a" * 20)
    assert derive_handle("hello-world@example.com") == "hello_world"
    assert derive_handle("123@example.com") == "123"


def test_reset_db_idempotent():
    """Calling reset_db twice succeeds without errors."""
    reset_db(SAMPLE_FIXTURE)
    reset_db(SAMPLE_FIXTURE)
    conn = get_db()
    try:
        count = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()
        assert count["c"] == 2
    finally:
        conn.close()


def test_settlement_operators_loaded():
    """settlement_operators are inserted when in fixture."""
    fixture = {**SAMPLE_FIXTURE, "settlement_operators": ["u_ada"]}
    reset_db(fixture)
    conn = get_db()
    try:
        ops = conn.execute("SELECT * FROM settlement_operators").fetchall()
        assert len(ops) == 1
        assert ops[0]["user_id"] == "u_ada"
    finally:
        conn.close()


def test_init_db_creates_tables():
    """init_db creates all expected tables."""
    init_db()
    conn = get_db()
    try:
        tables = [
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        expected = {"users", "wallets", "transactions", "ledger_entries",
                     "requests", "settlements", "idempotency_records", "settlement_operators"}
        assert expected.issubset(set(tables)), f"Missing tables: {expected - set(tables)}"
    finally:
        conn.close()