"""Tests for Stage 4: refunds and batch corrections."""
import pytest
from fastapi.testclient import TestClient

from src.db.schema import reset_db
from src.main import app

FIXTURE = {
    "currency": "EUR", "minor_units": 2,
    "users": [
        {"id": "u_ada", "email": "ada@example.com", "password": "correct horse",
         "display_name": "Ada", "handle": "ada", "balance": 10000},
        {"id": "u_bob", "email": "bob@example.com", "password": "correct horse",
         "display_name": "Bob", "handle": "bob", "balance": 2500},
    ],
    "settlement_operator_ids": ["u_ada"],
}

client = TestClient(app)


@pytest.fixture(autouse=True)
def setup_db():
    reset_db(FIXTURE)
    yield


def _login(email="ada@example.com"):
    return client.post("/auth/login", json={"email": email, "password": "correct horse"}).json()["token"]


def _auth(t):
    return {"Authorization": f"Bearer {t}"}


class TestRefunds:
    def test_refund_happy_path(self):
        t_ada = _login()
        t_bob = _login("bob@example.com")
        r = client.post("/payments", headers={**_auth(t_ada), "Idempotency-Key": "rp1"},
                        json={"to_handle": "bob", "amount": 1000})
        pid = r.json()["payment_id"]

        # Bob refunds 500
        r = client.post(f"/payments/{pid}/refunds",
                       headers={**_auth(t_bob), "Idempotency-Key": "rr1"},
                       json={"amount": 500})
        assert r.status_code == 201
        assert r.json()["refund_of"] == pid
        assert r.json()["amount"] == 500

    def test_refund_only_receiver(self):
        t_ada = _login()
        r = client.post("/payments", headers={**_auth(t_ada), "Idempotency-Key": "rp2"},
                        json={"to_handle": "bob", "amount": 1000})
        pid = r.json()["payment_id"]

        # Ada (sender) tries to refund — should fail
        r = client.post(f"/payments/{pid}/refunds",
                       headers={**_auth(t_ada), "Idempotency-Key": "rr2"},
                       json={"amount": 500})
        assert r.status_code == 403

    def test_refund_exceeds_payment(self):
        t_ada = _login()
        t_bob = _login("bob@example.com")
        r = client.post("/payments", headers={**_auth(t_ada), "Idempotency-Key": "rp3"},
                        json={"to_handle": "bob", "amount": 1000})
        pid = r.json()["payment_id"]

        r = client.post(f"/payments/{pid}/refunds",
                       headers={**_auth(t_bob), "Idempotency-Key": "rr3"},
                       json={"amount": 2000})
        assert r.status_code == 422

    def test_refund_insufficient_funds(self):
        t_ada = _login()
        t_bob = _login("bob@example.com")
        # Ada sends 10000 (all her money)
        r = client.post("/payments", headers={**_auth(t_ada), "Idempotency-Key": "rp4"},
                        json={"to_handle": "bob", "amount": 10000})
        pid = r.json()["payment_id"]

        # Bob tries to refund 3000 but only has 12500
        # This should succeed since Bob has enough
        r = client.post(f"/payments/{pid}/refunds",
                       headers={**_auth(t_bob), "Idempotency-Key": "rr4"},
                       json={"amount": 3000})
        assert r.status_code == 201


class TestCorrectionBatches:
    def test_batch_happy_path(self):
        t_ada = _login()
        r = client.post("/payments", headers={**_auth(t_ada), "Idempotency-Key": "bp1"},
                        json={"to_handle": "bob", "amount": 500})
        pid1 = r.json()["payment_id"]
        r = client.post("/payments", headers={**_auth(t_ada), "Idempotency-Key": "bp2"},
                        json={"to_handle": "bob", "amount": 300})
        pid2 = r.json()["payment_id"]

        r = client.post("/correction-batches",
                       headers={**_auth(t_ada), "Idempotency-Key": "br1"},
                       json={"corrections": [
                           {"payment_id": pid1, "expected_revision": 1, "amount": 0,
                            "effective_at": "2026-09-28T12:00:00+00:00", "reason": "reversal"},
                           {"payment_id": pid2, "expected_revision": 1, "amount": 0,
                            "effective_at": "2026-09-28T12:00:00+00:00", "reason": "reversal"},
                       ]})
        assert r.status_code == 201
        assert len(r.json()["revisions"]) == 2

    def test_batch_requires_operator(self):
        t_bob = _login("bob@example.com")
        r = client.post("/correction-batches",
                       headers={**_auth(t_bob), "Idempotency-Key": "br2"},
                       json={"corrections": []})
        assert r.status_code == 403
