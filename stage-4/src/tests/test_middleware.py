"""Tests for Pocketful middleware: idempotency-key enforcement and validation."""

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.middleware.idempotency import require_idempotency_key
from src.middleware.validation import (
    CreateAccount,
    DepositWithdrawRequest,
    TransferRequest,
)

# ---------------------------------------------------------------------------
# Helper: build a minimal FastAPI app wired with the idempotency dependency
# ---------------------------------------------------------------------------

def _make_app() -> FastAPI:
    app = FastAPI()

    @app.post("/transfers")
    async def do_transfer(key: str = Depends(require_idempotency_key)):
        return {"key": key, "ok": True}

    @app.post("/accounts/{account_id}/deposit")
    async def do_deposit(account_id: int, key: str = Depends(require_idempotency_key)):
        return {"account_id": account_id, "key": key, "ok": True}

    @app.post("/accounts/{account_id}/withdraw")
    async def do_withdraw(account_id: int, key: str = Depends(require_idempotency_key)):
        return {"account_id": account_id, "key": key, "ok": True}

    @app.get("/accounts/{account_id}/balance")
    async def get_balance(account_id: int):
        return {"account_id": account_id, "balance_cents": 0}

    return app


@pytest.fixture()
def client():
    return TestClient(_make_app())


# ---------------------------------------------------------------------------
# Idempotency-key header enforcement
# ---------------------------------------------------------------------------

class TestIdempotencyMiddleware:
    def test_post_transfers_without_key_returns_422(self, client):
        resp = client.post("/transfers", json={"from_account_id": 1, "to_account_id": 2, "amount_cents": 100})
        assert resp.status_code == 422
        assert "X-Idempotency-Key" in resp.json()["detail"]

    def test_post_transfers_with_key_passes(self, client):
        resp = client.post(
            "/transfers",
            json={"from_account_id": 1, "to_account_id": 2, "amount_cents": 100},
            headers={"X-Idempotency-Key": "abc-123"},
        )
        assert resp.status_code == 200
        assert resp.json()["key"] == "abc-123"

    def test_post_deposit_without_key_returns_422(self, client):
        resp = client.post("/accounts/1/deposit", json={"amount_cents": 500})
        assert resp.status_code == 422
        assert "X-Idempotency-Key" in resp.json()["detail"]

    def test_post_withdraw_without_key_returns_422(self, client):
        resp = client.post("/accounts/1/withdraw", json={"amount_cents": 500})
        assert resp.status_code == 422
        assert "X-Idempotency-Key" in resp.json()["detail"]

    def test_get_balance_does_not_require_key(self, client):
        resp = client.get("/accounts/1/balance")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Pydantic validation — TransferRequest
# ---------------------------------------------------------------------------

class TestTransferValidation:
    def test_valid_transfer(self):
        t = TransferRequest(from_account_id=1, to_account_id=2, amount_cents=1000)
        assert t.from_account_id == 1

    def test_from_account_zero_rejected(self):
        with pytest.raises(ValidationError, match="positive"):
            TransferRequest(from_account_id=0, to_account_id=2, amount_cents=1000)

    def test_to_account_negative_rejected(self):
        with pytest.raises(ValidationError, match="positive"):
            TransferRequest(from_account_id=1, to_account_id=-1, amount_cents=1000)

    def test_amount_zero_rejected(self):
        with pytest.raises(ValidationError, match="positive"):
            TransferRequest(from_account_id=1, to_account_id=2, amount_cents=0)

    def test_amount_negative_rejected(self):
        with pytest.raises(ValidationError, match="positive"):
            TransferRequest(from_account_id=1, to_account_id=2, amount_cents=-500)

    def test_same_account_rejected(self):
        with pytest.raises(ValidationError, match="must differ"):
            TransferRequest(from_account_id=1, to_account_id=1, amount_cents=1000)


# ---------------------------------------------------------------------------
# Pydantic validation — DepositWithdrawRequest
# ---------------------------------------------------------------------------

class TestDepositWithdrawValidation:
    def test_valid(self):
        d = DepositWithdrawRequest(amount_cents=500)
        assert d.amount_cents == 500

    def test_zero_rejected(self):
        with pytest.raises(ValidationError, match="positive"):
            DepositWithdrawRequest(amount_cents=0)

    def test_negative_rejected(self):
        with pytest.raises(ValidationError, match="positive"):
            DepositWithdrawRequest(amount_cents=-1)


# ---------------------------------------------------------------------------
# Pydantic validation — CreateAccount
# ---------------------------------------------------------------------------

class TestCreateAccountValidation:
    def test_valid_username(self):
        a = CreateAccount(username="alice_99")
        assert a.username == "alice_99"

    def test_too_short(self):
        with pytest.raises(ValidationError, match="3-50"):
            CreateAccount(username="ab")

    def test_too_long(self):
        with pytest.raises(ValidationError, match="3-50"):
            CreateAccount(username="a" * 51)

    def test_special_chars_rejected(self):
        with pytest.raises(ValidationError, match="alphanumeric"):
            CreateAccount(username="alice@bob")

    def test_spaces_rejected(self):
        with pytest.raises(ValidationError, match="alphanumeric"):
            CreateAccount(username="alice bob")

    def test_hyphens_rejected(self):
        with pytest.raises(ValidationError, match="alphanumeric"):
            CreateAccount(username="alice-bob")