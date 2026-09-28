"""Pydantic request models for Pocketful API endpoints."""

import re

from pydantic import BaseModel, field_validator, model_validator


class TransferRequest(BaseModel):
    """Body for POST /transfers."""

    from_account_id: int
    to_account_id: int
    amount_cents: int

    @field_validator("from_account_id", "to_account_id")
    @classmethod
    def account_id_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("account_id must be a positive integer")
        return v

    @field_validator("amount_cents")
    @classmethod
    def amount_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("amount_cents must be a positive integer")
        return v

    @model_validator(mode="after")
    def accounts_differ(self) -> "TransferRequest":
        if self.from_account_id == self.to_account_id:
            raise ValueError("from_account_id and to_account_id must differ")
        return self


class DepositWithdrawRequest(BaseModel):
    """Body for POST /accounts/<id>/deposit and /withdraw."""

    amount_cents: int

    @field_validator("amount_cents")
    @classmethod
    def amount_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("amount_cents must be a positive integer")
        return v


_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{3,50}$")


class CreateAccount(BaseModel):
    """Body for POST /accounts."""

    username: str

    @field_validator("username")
    @classmethod
    def username_valid(cls, v: str) -> str:
        if not _USERNAME_RE.match(v):
            raise ValueError(
                "username must be 3-50 characters, alphanumeric and underscores only"
            )
        return v