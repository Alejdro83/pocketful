"""Integration tests for Pocketful Stage 1 API endpoints."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Ensure src is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fastapi.testclient import TestClient
from src.main import app
from src.db.schema import DB_PATH

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_FIXTURE = {
    "currency": "EUR",
    "minor_units": 2,
    "users": [
        {
            "id": "u_alice",
            "email": "alice@example.com",
            "password": "correct horse",
            "display_name": "Alice",
            "handle": "alice",
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
        {
            "id": "u_charlie",
            "email": "charlie@example.com",
            "password": "correct horse",
            "display_name": "Charlie",
            "handle": "charlie",
            "balance": 0,
        },
    ],
    "settlement_operators": ["u_alice"],
}


@pytest.fixture(autouse=True)
def _reset_db():
    """Reset DB before each test."""
    if DB_PATH.exists():
        DB_PATH.unlink()
    yield
    if DB_PATH.exists():
        DB_PATH.unlink()


@pytest.fixture()
def client():
    return TestClient(app)


def _reset(client, fixture=None):
    """POST /_test/reset and return response."""
    return client.post("/_test/reset", json=fixture or SAMPLE_FIXTURE)


def _login(client, email, password):
    """Login and return token."""
    resp = client.post("/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _auth(token):
    """Return Authorization header dict."""
    return {"Authorization": f"Bearer {token}"}


def _auth_idem(token, key):
    """Return auth + idempotency key headers."""
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": key}


# ===================================================================
# Test: full flow — reset → signup → login → pay → balance → activity
# ===================================================================


class TestFullFlow:
    def test_signup_login_pay_balance_activity(self, client):
        # Reset
        _reset(client)

        # Signup a new user
        resp = client.post("/auth/signup", json={
            "email": "dave@example.com",
            "password": "long enough pw",
            "display_name": "Dave",
        })
        assert resp.status_code == 201, resp.text
        dave = resp.json()
        assert dave["user_id"]
        dave_token = dave["token"]

        # Dave's balance should be 0
        resp = client.get("/balance", headers=_auth(dave_token))
        assert resp.status_code == 200
        assert resp.json()["balance"] == 0

        # Login as alice
        alice_token = _login(client, "alice@example.com", "correct horse")

        # Alice pays Bob 1000
        resp = client.post("/payments", json={
            "to_handle": "bob",
            "amount": 1000,
            "note": "coffee",
            "visibility": "public",
        }, headers=_auth_idem(alice_token, "pay-1"))
        assert resp.status_code == 201, resp.text
        payment = resp.json()
        assert payment["from_handle"] == "alice"
        assert payment["to_handle"] == "bob"
        assert payment["amount"] == 1000
        assert payment["currency"] == "EUR"
        assert payment["note"] == "coffee"
        assert payment["visibility"] == "public"

        # Alice balance should be 9000
        resp = client.get("/balance", headers=_auth(alice_token))
        assert resp.json()["balance"] == 9000

        # Bob balance should be 6000
        bob_token = _login(client, "bob@example.com", "battery staple")
        resp = client.get("/balance", headers=_auth(bob_token))
        assert resp.json()["balance"] == 6000

        # Activity should show the payment for both
        resp = client.get("/activity", headers=_auth(alice_token))
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["payments"]) >= 1
        assert data["payments"][0]["payment_id"] == payment["payment_id"]

        # Bob should also see it (it's public)
        resp = client.get("/activity", headers=_auth(bob_token))
        data = resp.json()
        assert any(p["payment_id"] == payment["payment_id"] for p in data["payments"])

    def test_idempotent_payment_replay(self, client):
        _reset(client)
        alice_token = _login(client, "alice@example.com", "correct horse")

        # First call
        resp1 = client.post("/payments", json={
            "to_handle": "bob", "amount": 500,
        }, headers=_auth_idem(alice_token, "idem-pay-1"))
        assert resp1.status_code == 201

        # Replay with same key → same response
        resp2 = client.post("/payments", json={
            "to_handle": "bob", "amount": 500,
        }, headers=_auth_idem(alice_token, "idem-pay-1"))
        assert resp2.status_code == 201
        assert resp2.json()["payment_id"] == resp1.json()["payment_id"]

        # Balance should reflect only one payment
        resp = client.get("/balance", headers=_auth(alice_token))
        assert resp.json()["balance"] == 9500


# ===================================================================
# Test: requests — create → pay → verify
# ===================================================================


class TestRequests:
    def test_create_pay_request(self, client):
        _reset(client)
        alice_token = _login(client, "alice@example.com", "correct horse")
        bob_token = _login(client, "bob@example.com", "battery staple")

        # Alice creates a request for Bob
        resp = client.post("/requests", json={
            "payer_handle": "bob",
            "amount": 2000,
            "note": "dinner",
        }, headers=_auth_idem(alice_token, "req-1"))
        assert resp.status_code == 201, resp.text
        rq = resp.json()
        assert rq["requester_handle"] == "alice"
        assert rq["payer_handle"] == "bob"
        assert rq["amount"] == 2000
        assert rq["status"] == "pending"
        assert rq["currency"] == "EUR"

        # List incoming requests for Bob
        resp = client.get("/requests?direction=incoming", headers=_auth(bob_token))
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["requests"]) >= 1
        assert any(r["request_id"] == rq["request_id"] for r in data["requests"])

        # List outgoing requests for Alice
        resp = client.get("/requests?direction=outgoing", headers=_auth(alice_token))
        data = resp.json()
        assert any(r["request_id"] == rq["request_id"] for r in data["requests"])

        # Bob pays the request
        resp = client.post(f"/requests/{rq['request_id']}/pay", json={
            "visibility": "public",
        }, headers=_auth_idem(bob_token, "pay-req-1"))
        assert resp.status_code == 201, resp.text
        payment = resp.json()
        assert payment["from_handle"] == "bob"
        assert payment["to_handle"] == "alice"
        assert payment["amount"] == 2000

        # Verify request is now paid
        resp = client.get("/requests?status=paid", headers=_auth(alice_token))
        data = resp.json()
        paid = [r for r in data["requests"] if r["request_id"] == rq["request_id"]]
        assert len(paid) == 1
        assert paid[0]["status"] == "paid"
        assert paid[0]["payment_id"] is not None

    def test_decline_request(self, client):
        _reset(client)
        alice_token = _login(client, "alice@example.com", "correct horse")
        bob_token = _login(client, "bob@example.com", "battery staple")

        # Alice requests from Bob
        resp = client.post("/requests", json={
            "payer_handle": "bob", "amount": 500,
        }, headers=_auth_idem(alice_token, "req-d-1"))
        rq_id = resp.json()["request_id"]

        # Bob declines
        resp = client.post(f"/requests/{rq_id}/decline", headers=_auth(bob_token))
        assert resp.status_code == 200
        assert resp.json()["status"] == "declined"

        # Decline again → idempotent 200
        resp = client.post(f"/requests/{rq_id}/decline", headers=_auth(bob_token))
        assert resp.status_code == 200
        assert resp.json()["status"] == "declined"

    def test_cancel_request(self, client):
        _reset(client)
        alice_token = _login(client, "alice@example.com", "correct horse")
        bob_token = _login(client, "bob@example.com", "battery staple")

        # Alice requests from Bob
        resp = client.post("/requests", json={
            "payer_handle": "bob", "amount": 500,
        }, headers=_auth_idem(alice_token, "req-c-1"))
        rq_id = resp.json()["request_id"]

        # Alice cancels
        resp = client.post(f"/requests/{rq_id}/cancel", headers=_auth(alice_token))
        assert resp.status_code == 200
        assert resp.json()["status"] == "cancelled"

        # Cancel again → idempotent 200
        resp = client.post(f"/requests/{rq_id}/cancel", headers=_auth(alice_token))
        assert resp.status_code == 200


# ===================================================================
# Test: splits
# ===================================================================


class TestSplits:
    def test_create_split(self, client):
        _reset(client)
        alice_token = _login(client, "alice@example.com", "correct horse")

        # Alice splits 1000 between 3 people
        resp = client.post("/splits", json={
            "amount": 1000,
            "participant_handles": ["alice", "bob", "charlie"],
            "note": "lunch",
        }, headers=_auth_idem(alice_token, "split-1"))
        assert resp.status_code == 201, resp.text
        split = resp.json()

        assert split["amount"] == 1000
        assert split["currency"] == "EUR"
        assert split["note"] == "lunch"
        assert len(split["shares"]) == 3

        # Rounding: 1000 // 3 = 333, remainder = 1
        # First participant gets 334, rest get 333
        amounts = [s["amount"] for s in split["shares"]]
        assert sum(amounts) == 1000
        assert amounts[0] == 334  # alice gets extra cent
        assert amounts[1] == 333
        assert amounts[2] == 333

        # Only 2 requests created (alice excluded)
        assert len(split["requests"]) == 2
        for rq in split["requests"]:
            assert rq["requester_handle"] == "alice"
            assert rq["status"] == "pending"

        # Bob should see the request
        bob_token = _login(client, "bob@example.com", "battery staple")
        resp = client.get("/requests?direction=incoming&status=pending", headers=_auth(bob_token))
        data = resp.json()
        assert len(data["requests"]) >= 1

    def test_split_rounding_even(self, client):
        _reset(client)
        alice_token = _login(client, "alice@example.com", "correct horse")

        # 1000 split between 2: each gets 500
        resp = client.post("/splits", json={
            "amount": 1000,
            "participant_handles": ["alice", "bob"],
        }, headers=_auth_idem(alice_token, "split-even"))
        assert resp.status_code == 201
        split = resp.json()
        amounts = [s["amount"] for s in split["shares"]]
        assert amounts == [500, 500]


# ===================================================================
# Test: settlements
# ===================================================================


class TestSettlements:
    def test_operator_settlement(self, client):
        _reset(client)
        alice_token = _login(client, "alice@example.com", "correct horse")

        # Alice (operator) settles: bob → charlie 1000
        resp = client.post("/settlements", json={
            "transfers": [
                {"from_handle": "bob", "to_handle": "charlie", "amount": 1000},
            ],
        }, headers=_auth_idem(alice_token, "settle-1"))
        assert resp.status_code == 201, resp.text
        settlement = resp.json()

        assert settlement["settlement_id"]
        assert settlement["committed_at"]
        assert len(settlement["payments"]) == 1
        assert settlement["payments"][0]["from_handle"] == "bob"
        assert settlement["payments"][0]["to_handle"] == "charlie"
        assert settlement["payments"][0]["amount"] == 1000

        # Bob balance: 5000 - 1000 = 4000
        bob_token = _login(client, "bob@example.com", "battery staple")
        resp = client.get("/balance", headers=_auth(bob_token))
        assert resp.json()["balance"] == 4000

        # Charlie balance: 0 + 1000 = 1000
        charlie_token = _login(client, "charlie@example.com", "correct horse")
        resp = client.get("/balance", headers=_auth(charlie_token))
        assert resp.json()["balance"] == 1000

    def test_settlement_atomicity(self, client):
        """If any transfer would make a wallet negative, none execute."""
        _reset(client)
        alice_token = _login(client, "alice@example.com", "correct horse")

        # Bob only has 5000, try to move 9999
        resp = client.post("/settlements", json={
            "transfers": [
                {"from_handle": "bob", "to_handle": "charlie", "amount": 9999},
            ],
        }, headers=_auth_idem(alice_token, "settle-fail"))
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "insufficient_funds"

        # Bob's balance unchanged
        bob_token = _login(client, "bob@example.com", "battery staple")
        resp = client.get("/balance", headers=_auth(bob_token))
        assert resp.json()["balance"] == 5000

    def test_settlement_non_operator_forbidden(self, client):
        _reset(client)
        bob_token = _login(client, "bob@example.com", "battery staple")

        resp = client.post("/settlements", json={
            "transfers": [
                {"from_handle": "alice", "to_handle": "charlie", "amount": 100},
            ],
        }, headers=_auth_idem(bob_token, "settle-nope"))
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"


# ===================================================================
# Test: export / import
# ===================================================================


class TestExportImport:
    def test_export_import_preserves_state(self, client):
        _reset(client)
        alice_token = _login(client, "alice@example.com", "correct horse")

        # Make a payment
        resp = client.post("/payments", json={
            "to_handle": "bob", "amount": 1234, "note": "test",
        }, headers=_auth_idem(alice_token, "exp-pay"))
        assert resp.status_code == 201

        # Export
        resp = client.get("/_test/export")
        assert resp.status_code == 200
        exported = resp.json()
        assert exported["track"] == "pocketful"
        assert exported["format_version"] == 1
        assert "state" in exported

        # Verify export contains data
        assert len(exported["state"]["users"]) == 3
        assert len(exported["state"]["wallets"]) == 3
        assert len(exported["state"]["transactions"]) >= 1

        # Reset (wipe everything)
        _reset(client, {"currency": "EUR", "minor_units": 2, "users": []})

        # Import
        resp = client.post("/_test/import", json=exported)
        assert resp.status_code == 204, resp.text

        # Login again (tokens cleared)
        alice_token = _login(client, "alice@example.com", "correct horse")

        # Verify balance preserved: 10000 - 1234 = 8766
        resp = client.get("/balance", headers=_auth(alice_token))
        assert resp.json()["balance"] == 8766

        # Activity preserved
        resp = client.get("/activity", headers=_auth(alice_token))
        data = resp.json()
        assert len(data["payments"]) >= 1

    def test_import_bad_track(self, client):
        _reset(client)
        resp = client.post("/_test/import", json={
            "track": "wrong", "format_version": 1, "state": {},
        })
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_failed"


# ===================================================================
# Test: error codes
# ===================================================================


class TestErrorCodes:
    def test_self_payment(self, client):
        _reset(client)
        token = _login(client, "alice@example.com", "correct horse")
        resp = client.post("/payments", json={
            "to_handle": "alice", "amount": 100,
        }, headers=_auth_idem(token, "self-pay"))
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "self_payment"

    def test_not_found_recipient(self, client):
        _reset(client)
        token = _login(client, "alice@example.com", "correct horse")
        resp = client.post("/payments", json={
            "to_handle": "nobody", "amount": 100,
        }, headers=_auth_idem(token, "nf-pay"))
        assert resp.status_code == 404
        assert resp.json()["error"]["code"] == "not_found"

    def test_insufficient_funds(self, client):
        _reset(client)
        token = _login(client, "charlie@example.com", "correct horse")
        resp = client.post("/payments", json={
            "to_handle": "bob", "amount": 100,
        }, headers=_auth_idem(token, "no-funds"))
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "insufficient_funds"

    def test_missing_idempotency_key(self, client):
        _reset(client)
        token = _login(client, "alice@example.com", "correct horse")
        resp = client.post("/payments", json={
            "to_handle": "bob", "amount": 100,
        }, headers=_auth(token))
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == "missing_idempotency_key"

    def test_self_request(self, client):
        _reset(client)
        token = _login(client, "alice@example.com", "correct horse")
        resp = client.post("/requests", json={
            "payer_handle": "alice", "amount": 100,
        }, headers=_auth_idem(token, "self-req"))
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "self_request"

    def test_request_not_pending(self, client):
        _reset(client)
        alice_token = _login(client, "alice@example.com", "correct horse")
        bob_token = _login(client, "bob@example.com", "battery staple")

        # Create request
        resp = client.post("/requests", json={
            "payer_handle": "bob", "amount": 100,
        }, headers=_auth_idem(alice_token, "rnp-create"))
        rq_id = resp.json()["request_id"]

        # Bob pays it
        client.post(f"/requests/{rq_id}/pay", json={},
                    headers=_auth_idem(bob_token, "rnp-pay"))

        # Try to pay again
        resp = client.post(f"/requests/{rq_id}/pay", json={},
                           headers=_auth_idem(bob_token, "rnp-pay2"))
        assert resp.status_code == 409
        assert resp.json()["error"]["code"] == "request_not_pending"

    def test_forbidden_request_actions(self, client):
        _reset(client)
        alice_token = _login(client, "alice@example.com", "correct horse")
        bob_token = _login(client, "bob@example.com", "battery staple")
        charlie_token = _login(client, "charlie@example.com", "correct horse")

        # Alice requests from Bob
        resp = client.post("/requests", json={
            "payer_handle": "bob", "amount": 100,
        }, headers=_auth_idem(alice_token, "forb-create"))
        rq_id = resp.json()["request_id"]

        # Charlie tries to pay (not the payer) → 403
        resp = client.post(f"/requests/{rq_id}/pay", json={},
                           headers=_auth_idem(charlie_token, "forb-pay"))
        assert resp.status_code == 403
        assert resp.json()["error"]["code"] == "forbidden"

        # Charlie tries to decline (not the payer) → 403
        resp = client.post(f"/requests/{rq_id}/decline",
                           headers=_auth(charlie_token))
        assert resp.status_code == 403

        # Bob tries to cancel (not the requester) → 403
        resp = client.post(f"/requests/{rq_id}/cancel",
                           headers=_auth(bob_token))
        assert resp.status_code == 403

    def test_unauthenticated(self, client):
        _reset(client)
        resp = client.get("/balance")
        assert resp.status_code == 401
        assert resp.json()["error"]["code"] == "unauthenticated"

    def test_signup_validation(self, client):
        _reset(client)
        # Short password
        resp = client.post("/auth/signup", json={
            "email": "test@example.com", "password": "short",
        })
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "validation_failed"

    def test_login_invalid(self, client):
        _reset(client)
        resp = client.post("/auth/login", json={
            "email": "alice@example.com", "password": "wrong password",
        })
        assert resp.status_code == 401


# ===================================================================
# Test: /me and /balance
# ===================================================================


class TestMeAndBalance:
    def test_me_endpoint(self, client):
        _reset(client)
        token = _login(client, "alice@example.com", "correct horse")
        resp = client.get("/me", headers=_auth(token))
        assert resp.status_code == 200
        data = resp.json()
        assert data["handle"] == "alice"
        assert data["balance"] == 10000
        assert data["currency"] == "EUR"

    def test_balance_endpoint(self, client):
        _reset(client)
        token = _login(client, "alice@example.com", "correct horse")
        resp = client.get("/balance", headers=_auth(token))
        assert resp.status_code == 200
        assert resp.json() == {"balance": 10000}


# ===================================================================
# Test: activity visibility
# ===================================================================


class TestActivity:
    def test_private_payment_visible_only_to_parties(self, client):
        _reset(client)
        alice_token = _login(client, "alice@example.com", "correct horse")
        charlie_token = _login(client, "charlie@example.com", "correct horse")

        # Alice pays Bob privately
        client.post("/payments", json={
            "to_handle": "bob", "amount": 100, "visibility": "private",
        }, headers=_auth_idem(alice_token, "priv-pay"))

        # Charlie (not involved) should NOT see it
        resp = client.get("/activity", headers=_auth(charlie_token))
        data = resp.json()
        assert not any(p.get("note") == "" and p["amount"] == 100 for p in data["payments"]
                       if p["from_handle"] == "alice" and p["to_handle"] == "bob")

        # Alice should see it
        resp = client.get("/activity", headers=_auth(alice_token))
        data = resp.json()
        assert any(p["visibility"] == "private" and p["amount"] == 100
                   for p in data["payments"])
