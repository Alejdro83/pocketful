"""Unit tests for pocketful transfer / deposit / withdrawal."""

import sqlite3
import threading
import uuid
from pathlib import Path

import pytest

from src.db.exceptions import InsufficientFunds
from src.db.transfer import deposit, transfer, withdrawal

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

MIGRATION_SQL = (Path(__file__).resolve().parent.parent.parent / "migrations" / "001_initial.sql").read_text()


def _fresh_db() -> sqlite3.Connection:
    """Return an in-memory DB with the full schema applied."""
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode = WAL")
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(MIGRATION_SQL)
    return db


def _key() -> str:
    """Unique idempotency key per test."""
    return uuid.uuid4().hex


@pytest.fixture()
def db():
    conn = _fresh_db()
    yield conn
    conn.close()


@pytest.fixture()
def funded_db(db):
    """DB with two accounts, each holding 10 000 cents."""
    db.execute("INSERT INTO accounts (id, username, balance_cents) VALUES (1, 'alice', 10000)")
    db.execute("INSERT INTO accounts (id, username, balance_cents) VALUES (2, 'bob', 10000)")
    return db


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------

class TestTransferHappyPath:
    def test_basic_transfer(self, funded_db):
        result = transfer(funded_db, from_id=1, to_id=2, amount_cents=3000, idempotency_key=_key())

        assert result["status"] == "completed"
        assert isinstance(result["transaction_id"], int)

        a = funded_db.execute("SELECT balance_cents FROM accounts WHERE id=1").fetchone()
        b = funded_db.execute("SELECT balance_cents FROM accounts WHERE id=2").fetchone()
        assert a["balance_cents"] == 7000
        assert b["balance_cents"] == 13000

    def test_ledger_sums_to_zero(self, funded_db):
        """Double-entry invariant: every transaction's ledger entries sum to 0."""
        key = _key()
        transfer(funded_db, from_id=1, to_id=2, amount_cents=5000, idempotency_key=key)

        row = funded_db.execute(
            """SELECT SUM(le.amount_cents) AS total
               FROM ledger_entries le
               JOIN transactions t ON t.id = le.transaction_id
               WHERE t.idempotency_key = ?""",
            (key,),
        ).fetchone()
        assert row["total"] == 0

    def test_total_balance_constant_across_transfers(self, funded_db):
        """Money is neither created nor destroyed across N transfers."""
        initial_total = funded_db.execute("SELECT SUM(balance_cents) AS s FROM accounts WHERE id IN (1,2)").fetchone()["s"]

        for _ in range(20):
            transfer(funded_db, 1, 2, 100, idempotency_key=_key())

        final_total = funded_db.execute("SELECT SUM(balance_cents) AS s FROM accounts WHERE id IN (1,2)").fetchone()["s"]
        assert final_total == initial_total

    def test_all_ledger_entries_sum_to_zero_globally(self, funded_db):
        """Every completed transaction's ledger must balance individually."""
        for _ in range(10):
            transfer(funded_db, 1, 2, 100, idempotency_key=_key())

        rows = funded_db.execute(
            """SELECT t.id, SUM(le.amount_cents) AS s
               FROM transactions t
               JOIN ledger_entries le ON le.transaction_id = t.id
               GROUP BY t.id"""
        ).fetchall()
        for r in rows:
            assert r["s"] == 0, f"Transaction {r['id']} ledger sum is {r['s']}, expected 0"


# ---------------------------------------------------------------------------
# insufficient funds
# ---------------------------------------------------------------------------

class TestInsufficientFunds:
    def test_raises_on_overdraft(self, funded_db):
        with pytest.raises(InsufficientFunds) as exc_info:
            transfer(funded_db, 1, 2, 99999, idempotency_key=_key())
        assert exc_info.value.from_id == 1
        assert exc_info.value.available_cents == 10000

    def test_balances_unchanged_after_failure(self, funded_db):
        with pytest.raises(InsufficientFunds):
            transfer(funded_db, 1, 2, 99999, idempotency_key=_key())

        a = funded_db.execute("SELECT balance_cents FROM accounts WHERE id=1").fetchone()
        b = funded_db.execute("SELECT balance_cents FROM accounts WHERE id=2").fetchone()
        assert a["balance_cents"] == 10000
        assert b["balance_cents"] == 10000

    def test_no_orphan_transaction_on_failure(self, funded_db):
        with pytest.raises(InsufficientFunds):
            transfer(funded_db, 1, 2, 99999, idempotency_key=_key())

        count = funded_db.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
        assert count == 0


# ---------------------------------------------------------------------------
# idempotency
# ---------------------------------------------------------------------------

class TestIdempotency:
    def test_same_key_returns_same_result(self, funded_db):
        key = _key()
        r1 = transfer(funded_db, 1, 2, 1000, idempotency_key=key)
        r2 = transfer(funded_db, 1, 2, 1000, idempotency_key=key)

        assert r1 == r2

    def test_replay_does_not_double_debit(self, funded_db):
        key = _key()
        transfer(funded_db, 1, 2, 1000, idempotency_key=key)
        transfer(funded_db, 1, 2, 1000, idempotency_key=key)

        a = funded_db.execute("SELECT balance_cents FROM accounts WHERE id=1").fetchone()
        b = funded_db.execute("SELECT balance_cents FROM accounts WHERE id=2").fetchone()
        assert a["balance_cents"] == 9000
        assert b["balance_cents"] == 11000


# ---------------------------------------------------------------------------
# concurrent transfers (threading)
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_concurrent_transfers_preserve_total(self, funded_db):
        """Run many transfers between two accounts concurrently and verify
        the total balance is conserved (no double-spend, no lost money)."""
        db_path = ":memory:"
        # We need a file-backed DB for threads to share it
        import tempfile, os
        tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmpfile.close()
        db_path = tmpfile.name

        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(MIGRATION_SQL)
        conn.execute("INSERT INTO accounts (id, username, balance_cents) VALUES (1, 'alice', 100000)")
        conn.execute("INSERT INTO accounts (id, username, balance_cents) VALUES (2, 'bob', 100000)")
        conn.close()

        errors = []

        def do_transfer(from_id, to_id, amount, key):
            try:
                c = sqlite3.connect(db_path, isolation_level=None)
                c.row_factory = sqlite3.Row
                c.execute("PRAGMA journal_mode = WAL")
                c.execute("PRAGMA foreign_keys = ON")
                transfer(c, from_id, to_id, amount, idempotency_key=key)
                c.close()
            except Exception as e:
                errors.append(e)

        threads = []
        N = 40
        for i in range(N):
            # Alternate direction each time
            frm, to = (1, 2) if i % 2 == 0 else (2, 1)
            t = threading.Thread(target=do_transfer, args=(frm, to, 100, uuid.uuid4().hex))
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # Verify totals
        conn = sqlite3.connect(db_path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        total = conn.execute("SELECT SUM(balance_cents) AS s FROM accounts WHERE id IN (1,2)").fetchone()["s"]
        assert total == 200000, f"Total balance is {total}, expected 200000. Errors: {errors}"

        # Verify every transaction's ledger is balanced
        rows = conn.execute(
            """SELECT t.id, SUM(le.amount_cents) AS s
               FROM transactions t
               JOIN ledger_entries le ON le.transaction_id = t.id
               WHERE t.status = 'completed'
               GROUP BY t.id"""
        ).fetchall()
        for r in rows:
            assert r["s"] == 0, f"Transaction {r['id']} ledger sum is {r['s']}"

        conn.close()
        os.unlink(db_path)


# ---------------------------------------------------------------------------
# deposit
# ---------------------------------------------------------------------------

class TestDeposit:
    def test_deposit_increases_balance(self, funded_db):
        result = deposit(funded_db, account_id=1, amount_cents=5000, idempotency_key=_key())
        assert result["status"] == "completed"
        bal = funded_db.execute("SELECT balance_cents FROM accounts WHERE id=1").fetchone()["balance_cents"]
        assert bal == 15000

    def test_deposit_idempotent(self, funded_db):
        key = _key()
        r1 = deposit(funded_db, 1, 5000, key)
        r2 = deposit(funded_db, 1, 5000, key)
        assert r1 == r2
        bal = funded_db.execute("SELECT balance_cents FROM accounts WHERE id=1").fetchone()["balance_cents"]
        assert bal == 15000

    def test_deposit_ledger_sums_zero(self, funded_db):
        key = _key()
        deposit(funded_db, 1, 5000, key)
        row = funded_db.execute(
            """SELECT SUM(le.amount_cents) AS total
               FROM ledger_entries le JOIN transactions t ON t.id = le.transaction_id
               WHERE t.idempotency_key = ?""",
            (key,),
        ).fetchone()
        assert row["total"] == 0


# ---------------------------------------------------------------------------
# withdrawal
# ---------------------------------------------------------------------------

class TestWithdrawal:
    def test_withdrawal_decreases_balance(self, funded_db):
        result = withdrawal(funded_db, account_id=1, amount_cents=4000, idempotency_key=_key())
        assert result["status"] == "completed"
        bal = funded_db.execute("SELECT balance_cents FROM accounts WHERE id=1").fetchone()["balance_cents"]
        assert bal == 6000

    def test_withdrawal_insufficient_funds(self, funded_db):
        with pytest.raises(InsufficientFunds):
            withdrawal(funded_db, 1, 99999, idempotency_key=_key())

    def test_withdrawal_idempotent(self, funded_db):
        key = _key()
        r1 = withdrawal(funded_db, 1, 2000, key)
        r2 = withdrawal(funded_db, 1, 2000, key)
        assert r1 == r2
        bal = funded_db.execute("SELECT balance_cents FROM accounts WHERE id=1").fetchone()["balance_cents"]
        assert bal == 8000
