"""Concurrent invariant test harness for Pocketful wallet.

This is THE hackathon proof that the wallet is correct under concurrent load.
Every test verifies that the money invariant holds when multiple threads
hammer the wallet simultaneously.

Invariants tested:
  1. Total money (SUM of balances, excl. system account) is conserved
  2. Double-entry: every transaction's ledger entries sum to 0
  3. Idempotency keys prevent double-spending under contention
  4. No account ever goes negative
  5. Mixed operations (deposit, transfer, withdrawal) conserve money
"""

import os
import random
import sqlite3
import tempfile
import threading
import uuid
from pathlib import Path

import pytest

import src.db.database as db_module
from src.db.database import get_db
from src.db.exceptions import InsufficientFunds
from src.db.transfer import deposit, transfer, withdrawal

MIGRATION_SQL = (
    Path(__file__).resolve().parent.parent.parent / "migrations" / "001_initial.sql"
).read_text()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _fresh_file_db() -> str:
    """Create a fresh file-backed SQLite DB with the full schema. Return path."""
    tmpdir = tempfile.mkdtemp(prefix="pocketful_inv_")
    db_path = os.path.join(tmpdir, "test.db")
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(MIGRATION_SQL)
    conn.close()
    return db_path


def _get_db_with_timeout() -> sqlite3.Connection:
    """get_db() variant with a long busy_timeout for high-contention tests."""
    conn = sqlite3.connect(str(db_module.DB_PATH), timeout=60.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 60000")
    return conn


def _key() -> str:
    """Unique idempotency key."""
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# test suite
# ---------------------------------------------------------------------------

class TestConcurrentInvariant:
    """Money invariant holds under concurrent load."""

    # ------------------------------------------------------------------
    # 1. Total money constant under concurrent transfers
    # ------------------------------------------------------------------
    def test_total_money_constant_under_concurrent_transfers(self):
        """
        10 accounts, each with 100 000 cents.  20 threads × 50 random
        transfers between accounts.  After all threads join:
          - SUM(balance_cents) WHERE id != 0 == 1 000 000
          - Every ledger_entries transaction sums to 0
        """
        db_path = _fresh_file_db()
        old_path = db_module.DB_PATH
        db_module.DB_PATH = db_path
        try:
            # Seed 10 accounts
            conn = sqlite3.connect(db_path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            for i in range(1, 11):
                conn.execute(
                    "INSERT INTO accounts (id, username, balance_cents) VALUES (?, ?, ?)",
                    (i, f"user_{i}", 100_000),
                )
            conn.close()

            errors: list[Exception] = []
            NUM_THREADS = 20
            TRANSFERS_PER_THREAD = 50

            def worker():
                try:
                    db = _get_db_with_timeout()
                    for _ in range(TRANSFERS_PER_THREAD):
                        src, dst = random.sample(range(1, 11), 2)
                        amount = random.randint(1, 1_000)
                        try:
                            transfer(db, src, dst, amount, idempotency_key=_key())
                        except InsufficientFunds:
                            pass  # normal under concurrent load
                    db.close()
                except Exception as e:
                    errors.append(e)

            threads = [threading.Thread(target=worker) for _ in range(NUM_THREADS)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)

            assert not errors, f"Thread errors: {errors}"

            # Verify total money conserved (excluding system account)
            conn = sqlite3.connect(db_path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            total = conn.execute(
                "SELECT SUM(balance_cents) AS s FROM accounts WHERE id != 0"
            ).fetchone()["s"]
            assert total == 1_000_000, f"Total balance is {total}, expected 1_000_000"

            # Verify every transaction's ledger entries sum to 0
            rows = conn.execute(
                """SELECT t.id, SUM(le.amount_cents) AS s
                   FROM transactions t
                   JOIN ledger_entries le ON le.transaction_id = t.id
                   WHERE t.status = 'completed'
                   GROUP BY t.id"""
            ).fetchall()
            for r in rows:
                assert r["s"] == 0, (
                    f"Transaction {r['id']} ledger sum is {r['s']}, expected 0"
                )

            # No negative balances (excl. system account)
            neg = conn.execute(
                "SELECT id, balance_cents FROM accounts WHERE balance_cents < 0 AND id != 0"
            ).fetchall()
            assert len(neg) == 0, f"Negative balances found: {[(r['id'], r['balance_cents']) for r in neg]}"

            conn.close()
        finally:
            db_module.DB_PATH = old_path
            _cleanup(db_path)

    # ------------------------------------------------------------------
    # 2. Idempotency under contention
    # ------------------------------------------------------------------
    def test_idempotency_under_contention(self):
        """
        2 accounts with 50 000 cents each.  10 threads all try the SAME
        transfer with the SAME idempotency_key.  Exactly 1 transaction
        in DB; balances reflect single transfer.
        """
        db_path = _fresh_file_db()
        old_path = db_module.DB_PATH
        db_module.DB_PATH = db_path
        try:
            conn = sqlite3.connect(db_path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("INSERT INTO accounts (id, username, balance_cents) VALUES (1, 'alice', 50000)")
            conn.execute("INSERT INTO accounts (id, username, balance_cents) VALUES (2, 'bob', 50000)")
            conn.close()

            shared_key = _key()
            results: list[dict] = []
            errors: list[Exception] = []
            lock = threading.Lock()

            def worker():
                try:
                    db = _get_db_with_timeout()
                    result = transfer(db, 1, 2, 1000, idempotency_key=shared_key)
                    with lock:
                        results.append(result)
                    db.close()
                except Exception as e:
                    with lock:
                        errors.append(e)

            threads = [threading.Thread(target=worker) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)

            assert not errors, f"Thread errors: {errors}"
            assert len(results) == 10, f"Expected 10 results, got {len(results)}"

            # All results should be the same (idempotent)
            txn_ids = set(r["transaction_id"] for r in results)
            assert len(txn_ids) == 1, f"Multiple transaction IDs: {txn_ids}"

            conn = sqlite3.connect(db_path, isolation_level=None)
            conn.row_factory = sqlite3.Row

            # Exactly 1 transaction in DB
            count = conn.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
            assert count == 1, f"Expected 1 transaction, got {count}"

            # Balances: alice=49000, bob=51000
            a = conn.execute("SELECT balance_cents FROM accounts WHERE id=1").fetchone()["balance_cents"]
            b = conn.execute("SELECT balance_cents FROM accounts WHERE id=2").fetchone()["balance_cents"]
            assert a == 49_000, f"Alice balance: {a}, expected 49000"
            assert b == 51_000, f"Bob balance: {b}, expected 51000"
            assert a + b == 100_000

            conn.close()
        finally:
            db_module.DB_PATH = old_path
            _cleanup(db_path)

    # ------------------------------------------------------------------
    # 3. No negative balances
    # ------------------------------------------------------------------
    def test_no_negative_balances(self):
        """
        A has 100 cents, B has 0.  10 threads each try to transfer 100
        from A to B (each with a different idempotency key).
        A.balance_cents >= 0 always; total money conserved.
        """
        db_path = _fresh_file_db()
        old_path = db_module.DB_PATH
        db_module.DB_PATH = db_path
        try:
            conn = sqlite3.connect(db_path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("INSERT INTO accounts (id, username, balance_cents) VALUES (1, 'alice', 100)")
            conn.execute("INSERT INTO accounts (id, username, balance_cents) VALUES (2, 'bob', 0)")
            conn.close()

            success_count = [0]
            errors: list[Exception] = []
            lock = threading.Lock()

            def worker():
                try:
                    db = _get_db_with_timeout()
                    try:
                        transfer(db, 1, 2, 100, idempotency_key=_key())
                        with lock:
                            success_count[0] += 1
                    except InsufficientFunds:
                        pass  # expected: only 1 should succeed
                    db.close()
                except Exception as e:
                    with lock:
                        errors.append(e)

            threads = [threading.Thread(target=worker) for _ in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)

            assert not errors, f"Thread errors: {errors}"

            conn = sqlite3.connect(db_path, isolation_level=None)
            conn.row_factory = sqlite3.Row

            # Exactly 1 transfer succeeded
            assert success_count[0] == 1, f"Expected 1 success, got {success_count[0]}"

            a = conn.execute("SELECT balance_cents FROM accounts WHERE id=1").fetchone()["balance_cents"]
            b = conn.execute("SELECT balance_cents FROM accounts WHERE id=2").fetchone()["balance_cents"]
            assert a >= 0, f"A has negative balance: {a}"
            assert a == 0, f"A balance: {a}, expected 0"
            assert b == 100, f"B balance: {b}, expected 100"
            assert a + b == 100

            conn.close()
        finally:
            db_module.DB_PATH = old_path
            _cleanup(db_path)

    # ------------------------------------------------------------------
    # 4. Retries don't double-spend
    # ------------------------------------------------------------------
    def test_retries_dont_double_spend(self):
        """
        Same idempotency_key sent 5 times from different threads (simulating
        network retries).  Only 1 ledger entry pair; balances reflect single
        transfer.
        """
        db_path = _fresh_file_db()
        old_path = db_module.DB_PATH
        db_module.DB_PATH = db_path
        try:
            conn = sqlite3.connect(db_path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("INSERT INTO accounts (id, username, balance_cents) VALUES (1, 'alice', 10000)")
            conn.execute("INSERT INTO accounts (id, username, balance_cents) VALUES (2, 'bob', 10000)")
            conn.close()

            shared_key = _key()
            results: list[dict] = []
            errors: list[Exception] = []
            lock = threading.Lock()

            def worker():
                try:
                    db = _get_db_with_timeout()
                    result = transfer(db, 1, 2, 5000, idempotency_key=shared_key)
                    with lock:
                        results.append(result)
                    db.close()
                except Exception as e:
                    with lock:
                        errors.append(e)

            threads = [threading.Thread(target=worker) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=15)

            assert not errors, f"Thread errors: {errors}"
            assert len(results) == 5, f"Expected 5 results, got {len(results)}"

            conn = sqlite3.connect(db_path, isolation_level=None)
            conn.row_factory = sqlite3.Row

            # Only 1 transaction
            txn_count = conn.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
            assert txn_count == 1, f"Expected 1 transaction, got {txn_count}"

            # Only 2 ledger entries (one debit, one credit)
            ledger_count = conn.execute("SELECT COUNT(*) AS c FROM ledger_entries").fetchone()["c"]
            assert ledger_count == 2, f"Expected 2 ledger entries, got {ledger_count}"

            # Ledger entries sum to 0
            ledger_sum = conn.execute(
                "SELECT SUM(amount_cents) AS s FROM ledger_entries"
            ).fetchone()["s"]
            assert ledger_sum == 0, f"Ledger sum: {ledger_sum}, expected 0"

            # Balances: alice=5000, bob=15000
            a = conn.execute("SELECT balance_cents FROM accounts WHERE id=1").fetchone()["balance_cents"]
            b = conn.execute("SELECT balance_cents FROM accounts WHERE id=2").fetchone()["balance_cents"]
            assert a == 5_000, f"Alice: {a}, expected 5000"
            assert b == 15_000, f"Bob: {b}, expected 15000"
            assert a + b == 20_000

            conn.close()
        finally:
            db_module.DB_PATH = old_path
            _cleanup(db_path)

    # ------------------------------------------------------------------
    # 5. Invariant after mixed operations
    # ------------------------------------------------------------------
    def test_invariant_after_mixed_operations(self):
        """
        Mix of deposits, transfers, withdrawals across 5 accounts with
        10 threads.  Verify:
          - total balance = total deposits − total withdrawals
          - every ledger entry transaction sums to 0
        """
        db_path = _fresh_file_db()
        old_path = db_module.DB_PATH
        db_module.DB_PATH = db_path
        try:
            conn = sqlite3.connect(db_path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            for i in range(1, 6):
                conn.execute(
                    "INSERT INTO accounts (id, username, balance_cents) VALUES (?, ?, 0)",
                    (i, f"user_{i}"),
                )
            conn.close()

            deposit_total = [0]
            withdrawal_total = [0]
            errors: list[Exception] = []
            lock = threading.Lock()

            def worker(thread_id: int):
                try:
                    db = _get_db_with_timeout()
                    for _ in range(20):
                        op = random.choice(["deposit", "transfer", "withdrawal"])
                        if op == "deposit":
                            acct = random.randint(1, 5)
                            amt = random.randint(100, 5_000)
                            deposit(db, acct, amt, idempotency_key=_key())
                            with lock:
                                deposit_total[0] += amt
                        elif op == "withdrawal":
                            acct = random.randint(1, 5)
                            amt = random.randint(100, 1_000)
                            try:
                                withdrawal(db, acct, amt, idempotency_key=_key())
                                with lock:
                                    withdrawal_total[0] += amt
                            except InsufficientFunds:
                                pass
                        else:  # transfer
                            src, dst = random.sample(range(1, 6), 2)
                            amt = random.randint(1, 500)
                            try:
                                transfer(db, src, dst, amt, idempotency_key=_key())
                            except InsufficientFunds:
                                pass
                    db.close()
                except Exception as e:
                    with lock:
                        errors.append(e)

            threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=60)

            assert not errors, f"Thread errors: {errors}"

            conn = sqlite3.connect(db_path, isolation_level=None)
            conn.row_factory = sqlite3.Row

            # Total balance = deposits − withdrawals
            total_balance = conn.execute(
                "SELECT SUM(balance_cents) AS s FROM accounts WHERE id != 0"
            ).fetchone()["s"]
            expected = deposit_total[0] - withdrawal_total[0]
            assert total_balance == expected, (
                f"Total balance {total_balance} != "
                f"deposits({deposit_total[0]}) − withdrawals({withdrawal_total[0]}) = {expected}"
            )

            # Every transaction's ledger entries sum to 0
            rows = conn.execute(
                """SELECT t.id, SUM(le.amount_cents) AS s
                   FROM transactions t
                   JOIN ledger_entries le ON le.transaction_id = t.id
                   WHERE t.status = 'completed'
                   GROUP BY t.id"""
            ).fetchall()
            for r in rows:
                assert r["s"] == 0, (
                    f"Transaction {r['id']} ledger sum is {r['s']}, expected 0"
                )

            # No negative balances (excl. system account)
            neg = conn.execute(
                "SELECT id, balance_cents FROM accounts WHERE balance_cents < 0 AND id != 0"
            ).fetchall()
            assert len(neg) == 0, (
                f"Negative balances: {[(r['id'], r['balance_cents']) for r in neg]}"
            )

            conn.close()
        finally:
            db_module.DB_PATH = old_path
            _cleanup(db_path)


# ---------------------------------------------------------------------------
# cleanup helper
# ---------------------------------------------------------------------------

def _cleanup(db_path: str) -> None:
    """Remove the temp DB file and its WAL/SHM companions."""
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(db_path + suffix)
        except OSError:
            pass
    try:
        os.rmdir(os.path.dirname(db_path))
    except OSError:
        pass
