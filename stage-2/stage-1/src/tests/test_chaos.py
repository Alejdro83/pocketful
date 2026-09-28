"""Chaos tests for Pocketful: real-world failure scenarios.

Simulates crashes, concurrent deadlocks, rapid-fire retries, and edge cases
to prove the system is resilient under stress.
"""

import os
import random
import sqlite3
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

from src.db.exceptions import InsufficientFunds
from src.db.reconcile import reconcile
from src.db.transfer import deposit, transfer, withdrawal

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

MIGRATION_SQL = (
    Path(__file__).resolve().parent.parent.parent / "migrations" / "001_initial.sql"
).read_text()


def _file_db(accounts: list[tuple[int, str, int]] | None = None) -> str:
    """Create a file-backed DB with schema applied, return its path.

    Each test gets its own isolated file so concurrent threads can each open
    independent connections (SQLite requires this for real concurrency).
    """
    tmpfile = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmpfile.close()
    db_path = tmpfile.name

    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(MIGRATION_SQL)

    if accounts:
        for acct_id, username, balance in accounts:
            conn.execute(
                "INSERT INTO accounts (id, username, balance_cents) VALUES (?, ?, ?)",
                (acct_id, username, balance),
            )
    conn.close()
    return db_path


def _connect(db_path: str, timeout: float = 10.0) -> sqlite3.Connection:
    """Open a connection to a file-backed DB with WAL mode."""
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _key() -> str:
    return uuid.uuid4().hex


def _balance(db_path: str, account_id: int) -> int:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    val = conn.execute(
        "SELECT balance_cents FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()["balance_cents"]
    conn.close()
    return val


def _transaction_count(db_path: str) -> int:
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    val = conn.execute("SELECT COUNT(*) AS c FROM transactions").fetchone()["c"]
    conn.close()
    return val


# ---------------------------------------------------------------------------
# 1. Crash between transaction INSERT and ledger INSERT
# ---------------------------------------------------------------------------


class TestTransferTimeoutMidTransaction:
    """Simulate: thread holds write lock (crash/long GC), another thread times out.

    Verifies: no orphan transactions, balances unchanged, idempotency key
    not consumed (transfer can be retried).
    """

    def test_transfer_timeout_mid_transaction(self):
        db_path = _file_db(
            [(1, "alice", 100_00), (2, "bob", 100_00)]
        )
        idem_key = _key()

        # Thread A: grab the write lock and hold it (simulates a hung/crashed process)
        lock_acquired = threading.Event()
        lock_released = threading.Event()

        def hold_lock():
            conn = _connect(db_path)
            conn.execute("BEGIN IMMEDIATE")
            lock_acquired.set()
            # Hold until main thread tells us to release (simulating crash/rollback)
            lock_released.wait(timeout=30)
            conn.execute("ROLLBACK")
            conn.close()

        # Thread B: attempt a transfer with a very short timeout → should fail
        transfer_error = [None]
        transfer_result = [None]

        def attempt_transfer():
            # Short timeout so we get SQLITE_BUSY quickly
            conn = sqlite3.connect(db_path, isolation_level=None, timeout=0.3)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                result = transfer(conn, 1, 2, 50_00, idempotency_key=idem_key)
                transfer_result[0] = result
            except Exception as e:
                transfer_error[0] = e
            finally:
                conn.close()

        t_lock = threading.Thread(target=hold_lock)
        t_transfer = threading.Thread(target=attempt_transfer)

        t_lock.start()
        lock_acquired.wait(timeout=5)  # wait for lock to be held

        t_transfer.start()
        t_transfer.join(timeout=10)
        assert t_transfer.is_alive() is False, "Transfer thread hung"

        # Release the lock (simulates crash/rollback)
        lock_released.set()
        t_lock.join(timeout=5)

        # Transfer should have failed (database locked)
        assert transfer_error[0] is not None, "Expected transfer to fail due to lock"
        assert isinstance(
            transfer_error[0], sqlite3.OperationalError
        ), f"Expected OperationalError, got {type(transfer_error[0])}"

        # Verify: no orphan transactions
        txn_count = _transaction_count(db_path)
        assert txn_count == 0, f"Expected 0 transactions, got {txn_count}"

        # Verify: balances unchanged
        assert _balance(db_path, 1) == 100_00
        assert _balance(db_path, 2) == 100_00

        # Verify: idempotency key not consumed — retry should succeed
        conn = _connect(db_path)
        result = transfer(conn, 1, 2, 50_00, idempotency_key=idem_key)
        assert result["status"] == "completed"
        conn.close()

        # Now balances should reflect the transfer
        assert _balance(db_path, 1) == 50_00
        assert _balance(db_path, 2) == 150_00

        os.unlink(db_path)


# ---------------------------------------------------------------------------
# 2. Concurrent deadlock prevention
# ---------------------------------------------------------------------------


class TestConcurrentDeadlockPrevention:
    """Thread 1: transfer A→B, Thread 2: transfer B→A simultaneously.

    The system locks accounts in ascending ID order, preventing deadlock.
    Both must complete. Total balance conserved.
    """

    def test_concurrent_deadlock_prevention(self):
        db_path = _file_db(
            [(1, "alice", 1_000_00), (2, "bob", 1_000_00)]
        )

        errors = []
        results = []

        def do_transfer(from_id, to_id, amount, key):
            try:
                conn = _connect(db_path)
                result = transfer(conn, from_id, to_id, amount, idempotency_key=key)
                results.append(result)
                conn.close()
            except Exception as e:
                errors.append(e)

        # Run multiple rounds to increase deadlock opportunity
        threads = []
        for i in range(20):
            key_a = _key()
            key_b = _key()
            t1 = threading.Thread(target=do_transfer, args=(1, 2, 100, key_a))
            t2 = threading.Thread(target=do_transfer, args=(2, 1, 100, key_b))
            threads.extend([t1, t2])

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        # No thread should still be alive (deadlock would cause hang)
        for t in threads:
            assert not t.is_alive(), "Thread hung — possible deadlock!"

        # All transfers should succeed
        assert len(errors) == 0, f"Errors during concurrent transfers: {errors}"
        assert len(results) == 40, f"Expected 40 results, got {len(results)}"

        # Total balance conserved: 1_000_00 + 1_000_00 = 2_000_00
        conn = _connect(db_path)
        total = conn.execute(
            "SELECT SUM(balance_cents) AS s FROM accounts WHERE id IN (1, 2)"
        ).fetchone()["s"]
        conn.close()
        assert total == 2_000_00, f"Total balance is {total}, expected 2_000_00"

        os.unlink(db_path)


# ---------------------------------------------------------------------------
# 3. Rapid-fire same idempotency key
# ---------------------------------------------------------------------------


class TestRapidFireSameKey:
    """50 threads all fire the same transfer with the same idempotency key.

    Exactly 1 transaction must be created. All 50 threads get the same result.
    """

    def test_rapid_fire_same_key(self):
        db_path = _file_db(
            [(1, "alice", 100_000_00), (2, "bob", 100_000_00)]
        )
        shared_key = _key()

        results = []
        errors = []
        results_lock = threading.Lock()

        def do_transfer():
            try:
                conn = sqlite3.connect(
                    db_path, isolation_level=None, timeout=30
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("PRAGMA foreign_keys = ON")
                result = transfer(conn, 1, 2, 1000, idempotency_key=shared_key)
                conn.close()
                with results_lock:
                    results.append(result)
            except Exception as e:
                with results_lock:
                    errors.append(e)

        threads = [threading.Thread(target=do_transfer) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        # All threads should have completed
        for t in threads:
            assert not t.is_alive(), "Thread hung!"

        # All should succeed (no errors from valid operations)
        assert len(errors) == 0, f"Unexpected errors: {errors}"
        assert len(results) == 50, f"Expected 50 results, got {len(results)}"

        # All results must be identical (same transaction_id, same status)
        txn_ids = {r["transaction_id"] for r in results}
        statuses = {r["status"] for r in results}
        assert len(txn_ids) == 1, f"Expected 1 unique txn_id, got {txn_ids}: {len(txn_ids)}"
        assert statuses == {"completed"}, f"Unexpected statuses: {statuses}"

        # Exactly 1 transaction in the DB
        txn_count = _transaction_count(db_path)
        assert txn_count == 1, f"Expected 1 transaction, got {txn_count}"

        # Balances: alice lost 1000, bob gained 1000
        assert _balance(db_path, 1) == 100_000_00 - 1000
        assert _balance(db_path, 2) == 100_000_00 + 1000

        os.unlink(db_path)


# ---------------------------------------------------------------------------
# 4. Wallet round trip
# ---------------------------------------------------------------------------


class TestWalletRoundTrip:
    """Deposit → transfer → withdraw. Verify final balances and total money."""

    def test_wallet_round_trip(self):
        db_path = _file_db(
            [(1, "alice", 0), (2, "bob", 0)]
        )

        # Deposit $1000 to alice
        conn = _connect(db_path)
        deposit(conn, 1, 100_000, idempotency_key=_key())
        conn.close()

        assert _balance(db_path, 1) == 100_000

        # Transfer $500 from alice to bob
        conn = _connect(db_path)
        transfer(conn, 1, 2, 50_000, idempotency_key=_key())
        conn.close()

        assert _balance(db_path, 1) == 50_000
        assert _balance(db_path, 2) == 50_000

        # Withdraw $250 from bob
        conn = _connect(db_path)
        withdrawal(conn, 2, 25_000, idempotency_key=_key())
        conn.close()

        assert _balance(db_path, 1) == 50_000
        assert _balance(db_path, 2) == 25_000

        # Total money in the system (excluding system account): $750
        conn = _connect(db_path)
        total = conn.execute(
            "SELECT SUM(balance_cents) AS s FROM accounts WHERE id != 0"
        ).fetchone()["s"]
        conn.close()
        assert total == 75_000, f"Total money is {total}, expected 75000 ($750)"

        os.unlink(db_path)


# ---------------------------------------------------------------------------
# 5. Reconcile after stress
# ---------------------------------------------------------------------------


class TestReconcileAfterStress:
    """100 random transfers across 5 accounts, then reconcile.

    Must show no drift between cached balances and ledger sums.
    """

    def test_reconcile_after_stress(self):
        # Fund via deposit() so ledger entries match balance_cents (reconcile
        # compares the two, so direct INSERT would show artificial drift).
        db_path = _file_db(
            [
                (1, "alice", 0),
                (2, "bob", 0),
                (3, "charlie", 0),
                (4, "diana", 0),
                (5, "eve", 0),
            ]
        )
        for acct_id in [1, 2, 3, 4, 5]:
            conn = _connect(db_path)
            deposit(conn, acct_id, 10_000_00, idempotency_key=_key())
            conn.close()

        account_ids = [1, 2, 3, 4, 5]
        rng = random.Random(42)  # deterministic seed for reproducibility

        errors = []
        successes = 0
        successes_lock = threading.Lock()

        def do_one_transfer(from_id, to_id, amount, key):
            nonlocal successes
            try:
                conn = _connect(db_path)
                transfer(conn, from_id, to_id, amount, idempotency_key=key)
                conn.close()
                with successes_lock:
                    successes += 1
            except (InsufficientFunds, ValueError):
                # Expected: insufficient funds or self-transfer — just skip
                pass
            except Exception as e:
                errors.append(e)

        # Fire 100 random transfers (sequentially is fine; the point is volume)
        threads = []
        for _ in range(100):
            frm, to = rng.sample(account_ids, 2)
            amount = rng.randint(1, 500)  # small amounts to avoid draining accounts
            t = threading.Thread(
                target=do_one_transfer, args=(frm, to, amount, _key())
            )
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        # No unexpected errors
        assert len(errors) == 0, f"Unexpected errors during stress: {errors}"
        assert successes > 0, "No transfers succeeded — something is wrong"

        # Reconcile
        conn = _connect(db_path)
        result = reconcile(conn)
        conn.close()

        assert result["status"] == "ok", (
            f"Drift detected after stress test: {result}"
        )
        assert result["drifted"] == 0

        os.unlink(db_path)


# ---------------------------------------------------------------------------
# 6. Deposit / withdrawal edge cases
# ---------------------------------------------------------------------------


class TestDepositWithdrawalEdgeCases:
    """Boundary conditions: exact balance withdrawal, over-withdrawal,
    zero deposit, self-transfer.
    """

    def test_withdraw_exact_balance(self):
        """Withdraw entire balance → balance becomes 0, succeeds."""
        db_path = _file_db([(1, "alice", 50_00)])
        conn = _connect(db_path)
        result = withdrawal(conn, 1, 50_00, idempotency_key=_key())
        conn.close()

        assert result["status"] == "completed"
        assert _balance(db_path, 1) == 0
        os.unlink(db_path)

    def test_withdraw_one_cent_over(self):
        """Withdraw 1 cent more than balance → InsufficientFunds."""
        db_path = _file_db([(1, "alice", 50_00)])
        conn = _connect(db_path)
        with pytest.raises(InsufficientFunds) as exc_info:
            withdrawal(conn, 1, 50_01, idempotency_key=_key())
        conn.close()

        # Balance unchanged
        assert _balance(db_path, 1) == 50_00
        # No orphan transaction
        assert _transaction_count(db_path) == 0
        os.unlink(db_path)

    def test_deposit_zero_fails(self):
        """Depositing 0 cents must fail validation."""
        db_path = _file_db([(1, "alice", 50_00)])
        conn = _connect(db_path)
        with pytest.raises(ValueError, match="positive"):
            deposit(conn, 1, 0, idempotency_key=_key())
        conn.close()

        # Balance unchanged
        assert _balance(db_path, 1) == 50_00
        os.unlink(db_path)

    def test_transfer_to_self_fails(self):
        """Transfer to the same account must fail."""
        db_path = _file_db([(1, "alice", 50_00)])
        conn = _connect(db_path)
        with pytest.raises(ValueError, match="same account"):
            transfer(conn, 1, 1, 10_00, idempotency_key=_key())
        conn.close()

        # Balance unchanged
        assert _balance(db_path, 1) == 50_00
        # No transaction created
        assert _transaction_count(db_path) == 0
        os.unlink(db_path)