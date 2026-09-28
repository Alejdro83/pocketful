"""Tests for src.db.reconcile — drift detection and repair."""

import sqlite3
from pathlib import Path

import pytest

from src.db.reconcile import reconcile, repair

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

MIGRATION_SQL = (Path(__file__).resolve().parent.parent.parent / "migrations" / "001_initial.sql").read_text()


def _fresh_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(MIGRATION_SQL)
    return db


@pytest.fixture()
def db():
    conn = _fresh_db()
    yield conn
    conn.close()


@pytest.fixture()
def seeded_db(db):
    """DB with two funded accounts whose balances match the ledger."""
    db.execute("INSERT INTO accounts (id, username, balance_cents) VALUES (1, 'alice', 10000)")
    db.execute("INSERT INTO accounts (id, username, balance_cents) VALUES (2, 'bob', 5000)")
    # Seed matching ledger entries
    db.execute("INSERT INTO transactions (id, idempotency_key, request_hash, type, from_account_id, to_account_id, amount_cents, status) VALUES (1, 'k1', 'h1', 'deposit', NULL, 1, 10000, 'completed')")
    db.execute("INSERT INTO ledger_entries (transaction_id, account_id, amount_cents) VALUES (1, 1, 10000)")
    db.execute("INSERT INTO transactions (id, idempotency_key, request_hash, type, from_account_id, to_account_id, amount_cents, status) VALUES (2, 'k2', 'h2', 'deposit', NULL, 2, 5000, 'completed')")
    db.execute("INSERT INTO ledger_entries (transaction_id, account_id, amount_cents) VALUES (2, 2, 5000)")
    return db


# ---------------------------------------------------------------------------
# reconcile
# ---------------------------------------------------------------------------

class TestReconcile:
    def test_no_drift_returns_ok(self, seeded_db):
        result = reconcile(seeded_db)
        assert result["status"] == "ok"
        assert result["checked"] == 2
        assert result["drifted"] == 0

    def test_detects_drift(self, seeded_db):
        seeded_db.execute("UPDATE accounts SET balance_cents = 9999 WHERE id = 1")
        result = reconcile(seeded_db)
        assert result["status"] == "drift_detected"
        assert result["drifted"] == 1
        assert result["accounts"][0]["id"] == 1
        assert result["accounts"][0]["cached"] == 9999
        assert result["accounts"][0]["actual"] == 10000
        assert result["accounts"][0]["diff"] == -1

    def test_system_account_id_0_included(self, seeded_db):
        """Account id=0 (system) should be checked like any other."""
        seeded_db.execute("INSERT INTO accounts (id, username, balance_cents) VALUES (0, 'system', 0)")
        result = reconcile(seeded_db)
        assert result["checked"] == 3
        # All should be ok
        assert result["status"] == "ok"

    def test_system_account_drift_detected(self, seeded_db):
        seeded_db.execute("INSERT INTO accounts (id, username, balance_cents) VALUES (0, 'system', 999)")
        result = reconcile(seeded_db)
        assert result["status"] == "drift_detected"
        assert any(a["id"] == 0 for a in result["accounts"])


# ---------------------------------------------------------------------------
# repair
# ---------------------------------------------------------------------------

class TestRepair:
    def test_repair_dry_run_does_not_change_db(self, seeded_db):
        seeded_db.execute("UPDATE accounts SET balance_cents = 1 WHERE id = 1")
        bal_before = seeded_db.execute("SELECT balance_cents FROM accounts WHERE id=1").fetchone()["balance_cents"]

        result = repair(seeded_db, dry_run=True)
        assert result["dry_run"] is True
        assert result["repaired"] == 1

        bal_after = seeded_db.execute("SELECT balance_cents FROM accounts WHERE id=1").fetchone()["balance_cents"]
        assert bal_before == bal_after == 1, "dry_run must not modify the database"

    def test_repair_fixes_drift(self, seeded_db):
        seeded_db.execute("UPDATE accounts SET balance_cents = 1 WHERE id = 1")
        result = repair(seeded_db, dry_run=False)
        assert result["dry_run"] is False
        assert result["repaired"] == 1

        bal = seeded_db.execute("SELECT balance_cents FROM accounts WHERE id=1").fetchone()["balance_cents"]
        assert bal == 10000

    def test_repair_then_reconcile_ok(self, seeded_db):
        seeded_db.execute("UPDATE accounts SET balance_cents = 0 WHERE id = 1")
        seeded_db.execute("UPDATE accounts SET balance_cents = 0 WHERE id = 2")
        repair(seeded_db, dry_run=False)

        result = reconcile(seeded_db)
        assert result["status"] == "ok"
        assert result["drifted"] == 0
