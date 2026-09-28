"""Pydantic response schemas for Pocketful API."""

from typing import List, Optional

from pydantic import BaseModel


class AccountResponse(BaseModel):
    id: int
    username: str
    balance_cents: int


class TransferResponse(BaseModel):
    transaction_id: int
    status: str


class TransactionItem(BaseModel):
    id: int
    transaction_id: int
    account_id: int
    amount_cents: int
    type: Optional[str] = None
    status: Optional[str] = None
    created_at: Optional[str] = None


class TransactionListResponse(BaseModel):
    transactions: List[TransactionItem]