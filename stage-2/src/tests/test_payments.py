"""Tests for core payment functions."""

from __future__ import annotations

import concurrent.futures
import sqlite3
import threading

import pytest

from ..db.exceptions import IdempotencyKeyReuse, InsufficientFunds, SelfPayment
from ..db.payments import get_activity, get_balance, get_payment, make_payment
from ..db.schema import get_db, reset_db


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

FIXTURE = {
    "currency": "EUR",
    "minor_units": 2,
    "users": [
        {"id": "u_ada", "email": "ada@test.com", "password": "password123",
         "display_name": "Ada", "handle": "ada", "balance": 10000},
        {"id": "u_bob", "email": "bob@test.com", "password": "password123",
         "display_name": "Bob", "handle": "bob", "balance": 5000},
        {"id": "u_carol", "email": "carol@test.com", "password": "password123",
         "display_name": "Carol", "handle": "carol", "balance": 3000},
    ],
}


@pytest.fixture(autouse=True)
def _setup_db():
    """Reset DB before each test."""
    reset_db(FIXTURE)
    yield


def _db() -> sqlite3.Connection:
    return get_db()


# ---------------------------------------------------------------------------
# make_payment — happy path
# ---------------------------------------------------------------------------

class TestMakePaymentHappyPath:
    def test_basic_transfer(self):
        db = _db()
        try:
            result = make_payment(db, "u_ada", "u_bob", 1000, note="lunch")
            assert result["payment_id"].startswith("p_")
            assert result["from_user_id"] == "u_ada"
            assert result["to_user_id"] == "u_bob"
            assert result["from_handle"] == "ada"
            assert result["to_handle"] == "bob"
            assert result["amount"] == 1000
            assert result["currency"] == "EUR"
            assert result["note"] == "lunch"
            assert result["visibility"] == "public"
            assert result["request_id"] is None
            assert result["created_at"] is not None

            # Verify balances
            assert get_balance(db, "u_ada") == 9000
            assert get_balance(db, "u_bob") == 6000
        finally:
            db.close()

    def test_payment_persists(self):
        db = _db()
        try:
            result = make_payment(db, "u_ada", "u_bob", 500)
            fetched = get_payment(db, result["payment_id"])
            assert fetched is not None
            assert fetched["payment_id"] == result["payment_id"]
            assert fetched["amount"] == 500
        finally:
            db.close()

    def test_ledger_entries_created(self):
        db = _db()
        try:
            result = make_payment(db, "u_ada", "u_bob", 200)
            rows = db.execute(
                "SELECT * FROM ledger_entries WHERE transaction_id = ?",
                (result["payment_id"],),
            ).fetchall()
            assert len(rows) == 2
            amounts = sorted([rows[0]["amount"], rows[1]["amount"]])
            assert amounts == [-200, 200]
        finally:
            db.close()


# ---------------------------------------------------------------------------
# make_payment — insufficient funds
# ---------------------------------------------------------------------------

class TestInsufficientFunds:
    def test_rejects_overdraft(self):
        db = _db()
        try:
            with pytest.raises(InsufficientFunds) as exc_info:
                make_payment(db, "u_bob", "u_ada", 99999)
            assert exc_info.value.user_id == "u_bob"
            assert exc_info.value.available == 5000

            # Balances unchanged
            assert get_balance(db, "u_bob") == 5000
            assert get_balance(db, "u_ada") == 10000
        finally:
            db.close()

    def test_exact_balance_works(self):
        db = _db()
        try:
            make_payment(db, "u_bob", "u_ada", 5000)
            assert get_balance(db, "u_bob") == 0
            assert get_balance(db, "u_ada") == 15000
        finally:
            db.close()


# ---------------------------------------------------------------------------
# self-payment
# ---------------------------------------------------------------------------

class TestSelfPayment:
    def test_self_payment_rejected(self):
        db = _db()
        try:
            with pytest.raises(SelfPayment):
                make_payment(db, "u_ada", "u_ada", 100)
        finally:
            db.close()


# ---------------------------------------------------------------------------
# idempotency — replay
# ---------------------------------------------------------------------------

class TestIdempotencyReplay:
    def test_same_key_same_body_returns_same_response(self):
        db = _db()
        try:
            result1 = make_payment(
                db, "u_ada", "u_bob", 1000,
                idempotency_key="key-1",
                user_id_for_idempotency="u_ada",
            )
            result2 = make_payment(
                db, "u_ada", "u_bob", 1000,
                idempotency_key="key-1",
                user_id_for_idempotency="u_ada",
            )
            assert result1["payment_id"] == result2["payment_id"]
            assert result1["amount"] == result2["amount"]

            # Only one payment should exist
            assert get_balance(db, "u_ada") == 9000
            assert get_balance(db, "u_bob") == 6000
        finally:
            db.close()


# ---------------------------------------------------------------------------
# idempotency — conflict
# ---------------------------------------------------------------------------

class TestIdempotencyConflict:
    def test_same_key_different_body_raises_409(self):
        db = _db()
        try:
            make_payment(
                db, "u_ada", "u_bob", 1000,
                idempotency_key="key-2",
                user_id_for_idempotency="u_ada",
            )
            with pytest.raises(IdempotencyKeyReuse):
                make_payment(
                    db, "u_ada", "u_bob", 2000,
                    idempotency_key="key-2",
                    user_id_for_idempotency="u_ada",
                )
            # Balances unchanged (second call was rejected)
            assert get_balance(db, "u_ada") == 9000
            assert get_balance(db, "u_bob") == 6000
        finally:
            db.close()


# ---------------------------------------------------------------------------
# idempotency — reuse after failure
# ---------------------------------------------------------------------------

class TestIdempotencyReuseAfterFailure:
    def test_failed_key_is_reusable(self):
        db = _db()
        try:
            # First attempt — insufficient funds (no idempotency persisted)
            with pytest.raises(InsufficientFunds):
                make_payment(
                    db, "u_bob", "u_ada", 99999,
                    idempotency_key="key-3",
                    user_id_for_idempotency="u_bob",
                )

            # Second attempt with same key — should succeed (key was never persisted)
            result = make_payment(
                db, "u_bob", "u_ada", 1000,
                idempotency_key="key-3",
                user_id_for_idempotency="u_bob",
            )
            assert result["amount"] == 1000
            assert get_balance(db, "u_bob") == 4000
        finally:
            db.close()


# ---------------------------------------------------------------------------
# get_activity — visibility
# ---------------------------------------------------------------------------

class TestGetActivityVisibility:
    def test_public_visible_to_all(self):
        db = _db()
        try:
            make_payment(db, "u_ada", "u_bob", 100, visibility="public")

            # Carol (not a participant) should see it
            activity = get_activity(db, "u_carol")
            assert len(activity["payments"]) == 1
            assert activity["payments"][0]["payment_id"].startswith("p_")
        finally:
            db.close()

    def test_private_only_to_participants(self):
        db = _db()
        try:
            make_payment(db, "u_ada", "u_bob", 100, visibility="private")

            # Ada (sender) should see it
            activity_ada = get_activity(db, "u_ada")
            assert len(activity_ada["payments"]) == 1

            # Bob (receiver) should see it
            activity_bob = get_activity(db, "u_bob")
            assert len(activity_bob["payments"]) == 1

            # Carol (outsider) should NOT see it
            activity_carol = get_activity(db, "u_carol")
            assert len(activity_carol["payments"]) == 0
        finally:
            db.close()

    def test_has_more_pagination(self):
        db = _db()
        try:
            for i in range(5):
                make_payment(db, "u_ada", "u_bob", 10, note=f"p{i}")

            activity = get_activity(db, "u_ada", limit=3)
            assert len(activity["payments"]) == 3
            assert activity["has_more"] is True

            activity2 = get_activity(db, "u_ada", limit=10)
            assert len(activity2["payments"]) == 5
            assert activity2["has_more"] is False
        finally:
            db.close()


# ---------------------------------------------------------------------------
# concurrent payments — invariant preservation
# ---------------------------------------------------------------------------

class TestConcurrentPayments:
    def test_concurrent_payments_preserve_balances(self):
        """Multiple threads making payments should not corrupt balances."""
        results = []
        errors = []

        def do_payment(from_uid, to_uid, amount, key):
            db = _db()
            try:
                r = make_payment(db, from_uid, to_uid, amount,
                                 idempotency_key=key,
                                 user_id_for_idempotency=from_uid)
                results.append(r)
            except Exception as e:
                errors.append(e)
            finally:
                db.close()

        threads = []
        for i in range(5):
            t = threading.Thread(
                target=do_payment,
                args=("u_ada", "u_bob", 100, f"conc-{i}"),
            )
            threads.append(t)

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All should succeed
        assert len(errors) == 0, f"Errors: {errors}"
        assert len(results) == 5

        db = _db()
        try:
            assert get_balance(db, "u_ada") == 9500  # 10000 - 5*100
            assert get_balance(db, "u_bob") == 5500  # 5000 + 5*100
        finally:
            db.close()


# ---------------------------------------------------------------------------
# SUM(wallet balances) invariant
# ---------------------------------------------------------------------------

class TestBalanceInvariant:
    def test_sum_constant_after_n_payments(self):
        """Total money in the system must not change after N payments."""
        db = _db()
        try:
            initial_total = (
                get_balance(db, "u_ada")
                + get_balance(db, "u_bob")
                + get_balance(db, "u_carol")
            )
            assert initial_total == 18000

            # Make several payments in a chain
            make_payment(db, "u_ada", "u_bob", 3000)
            make_payment(db, "u_bob", "u_carol", 1500)
            make_payment(db, "u_carol", "u_ada", 500)
            make_payment(db, "u_ada", "u_carol", 1000)

            final_total = (
                get_balance(db, "u_ada")
                + get_balance(db, "u_bob")
                + get_balance(db, "u_carol")
            )
            assert final_total == initial_total
        finally:
            db.close()