"""Tests for Stage 3: statements and payment corrections."""
import json
import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from src.db.schema import get_db, init_db, reset_db
from src.main import app
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def setup_db():
    """Reset DB before each test."""
    init_db()
    db = get_db()
    fixture = {
        "currency": "EUR", "minor_units": 2,
        "users": [
            {"id": "u_ada", "email": "ada@example.com", "password": "correct horse",
             "display_name": "Ada", "handle": "ada", "balance": 10000},
            {"id": "u_bob", "email": "bob@example.com", "password": "correct horse",
             "display_name": "Bob", "handle": "bob", "balance": 2500},
        ],
        "settlement_operator_ids": ["u_ada"],
    }
    reset_db(fixture)
    db.close()
    yield


client = TestClient(app)


def _login(email="ada@example.com", password="correct horse"):
    r = client.post("/auth/login", json={"email": email, "password": password})
    return r.json()["token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def _reset():
    fixture = {
        "currency": "EUR", "minor_units": 2,
        "users": [
            {"id": "u_ada", "email": "ada@example.com", "password": "correct horse",
             "display_name": "Ada", "handle": "ada", "balance": 10000},
            {"id": "u_bob", "email": "bob@example.com", "password": "correct horse",
             "display_name": "Bob", "handle": "bob", "balance": 2500},
        ],
        "settlement_operator_ids": ["u_ada"],
    }
    client.post("/_test/reset", json=fixture)


class TestMeAsOf:
    def test_me_current_balance(self):
        token = _login()
        r = client.get("/me", headers=_auth(token))
        assert r.status_code == 200
        assert r.json()["balance"] == 10000

    def test_me_with_as_of(self):
        token = _login()
        r = client.get("/me?as_of=2026-09-28T13:20:00%2B00:00", headers=_auth(token))
        assert r.status_code == 200
        assert "as_of" in r.json()

    def test_me_invalid_as_of(self):
        token = _login()
        r = client.get("/me?as_of=not-a-date", headers=_auth(token))
        assert r.status_code == 422


class TestStatements:
    def test_empty_statement(self):
        token = _login()
        r = client.get("/statement", headers=_auth(token))
        assert r.status_code == 200
        data = r.json()
        assert "opening_balance" in data
        assert "entries" in data
        assert "closing_balance" in data
        assert "has_more" in data

    def test_statement_with_payments(self):
        token_ada = _login()
        token_bob = _login("bob@example.com")
        # Ada pays Bob
        client.post("/payments", headers={**_auth(token_ada), "Idempotency-Key": "p1"},
                    json={"to_handle": "bob", "amount": 500})
        # Bob pays Ada
        client.post("/payments", headers={**_auth(token_bob), "Idempotency-Key": "p2"},
                    json={"to_handle": "ada", "amount": 200})

        r = client.get("/statement", headers=_auth(token_ada))
        assert r.status_code == 200
        data = r.json()
        assert len(data["entries"]) == 2
        # Ada sent 500, received 200, balance = 10000 - 500 + 200 = 9700
        assert data["closing_balance"] == 9700

    def test_statement_pagination(self):
        token_ada = _login()
        token_bob = _login("bob@example.com")
        for i in range(5):
            client.post("/payments", headers={**_auth(token_ada), "Idempotency-Key": f"p{i}"},
                        json={"to_handle": "bob", "amount": 100})

        r = client.get("/statement?limit=2&offset=0", headers=_auth(token_ada))
        data = r.json()
        assert len(data["entries"]) == 2
        assert data["has_more"] is True

        r2 = client.get("/statement?limit=2&offset=2", headers=_auth(token_ada))
        data2 = r2.json()
        assert len(data2["entries"]) == 2


class TestCorrections:
    def test_correction_happy_path(self):
        token_ada = _login()
        token_bob = _login("bob@example.com")
        # Ada pays Bob 500
        r = client.post("/payments", headers={**_auth(token_ada), "Idempotency-Key": "cp1"},
                        json={"to_handle": "bob", "amount": 500})
        payment_id = r.json()["payment_id"]

        # Correct to 300
        r = client.post(f"/payments/{payment_id}/corrections",
                       headers={**_auth(token_ada), "Idempotency-Key": "cr1"},
                       json={"expected_revision": 1, "amount": 300,
                             "effective_at": "2026-09-28T12:00:00+00:00", "reason": "corrected amount"})
        assert r.status_code == 201
        data = r.json()
        assert data["revision_number"] == 2
        assert data["amount"] == 300

    def test_correction_stale_revision(self):
        token_ada = _login()
        r = client.post("/payments", headers={**_auth(token_ada), "Idempotency-Key": "cp2"},
                        json={"to_handle": "bob", "amount": 500})
        payment_id = r.json()["payment_id"]

        r = client.post(f"/payments/{payment_id}/corrections",
                       headers={**_auth(token_ada), "Idempotency-Key": "cr2"},
                       json={"expected_revision": 99, "amount": 300,
                             "effective_at": "2026-09-28T12:00:00+00:00", "reason": "test"})
        assert r.status_code == 409

    def test_correction_forbidden_for_non_sender(self):
        token_ada = _login()
        token_bob = _login("bob@example.com")
        r = client.post("/payments", headers={**_auth(token_ada), "Idempotency-Key": "cp3"},
                        json={"to_handle": "bob", "amount": 500})
        payment_id = r.json()["payment_id"]

        # Bob tries to correct Ada's payment
        r = client.post(f"/payments/{payment_id}/corrections",
                       headers={**_auth(token_bob), "Idempotency-Key": "cr3"},
                       json={"expected_revision": 1, "amount": 300,
                             "effective_at": "2026-09-28T12:00:00+00:00", "reason": "test"})
        assert r.status_code == 403

    def test_revisions_endpoint(self):
        token_ada = _login()
        r = client.post("/payments", headers={**_auth(token_ada), "Idempotency-Key": "cp4"},
                        json={"to_handle": "bob", "amount": 500})
        payment_id = r.json()["payment_id"]

        r = client.get(f"/payments/{payment_id}/revisions", headers=_auth(token_ada))
        assert r.status_code == 200
        data = r.json()
        assert len(data["revisions"]) >= 1
        assert data["revisions"][0]["revision_number"] == 1
