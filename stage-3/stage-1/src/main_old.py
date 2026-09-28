"""Pocketful FastAPI application."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.db.database import get_db, init_db
from src.db.exceptions import DuplicateTransaction, InsufficientFunds
from src.db.transfer import deposit, transfer, withdrawal
from src.middleware.idempotency import require_idempotency_key
from src.middleware.validation import (
    CreateAccount,
    DepositWithdrawRequest,
    TransferRequest,
)
from src.schemas import (
    AccountResponse,
    TransactionItem,
    TransactionListResponse,
    TransferResponse,
)

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Pocketful", lifespan=lifespan)

# CORS — wide open for hackathon
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_account_or_404(account_id: int) -> dict:
    db = get_db()
    try:
        row = db.execute(
            "SELECT id, username, balance_cents FROM accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
    finally:
        db.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"Account {account_id} not found")
    return dict(row)


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    db = get_db()
    try:
        db.execute("SELECT 1")
    finally:
        db.close()
    return {"status": "ok", "db": "connected"}


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


@app.post("/accounts", response_model=AccountResponse, status_code=201)
def create_account(body: CreateAccount):
    db = get_db()
    try:
        cur = db.execute(
            "INSERT INTO accounts (username, balance_cents) VALUES (?, 0)",
            (body.username,),
        )
        db.commit()
        account_id: int = cur.lastrowid  # type: ignore[assignment]
    finally:
        db.close()
    return AccountResponse(id=account_id, username=body.username, balance_cents=0)


@app.get("/accounts", response_model=list[AccountResponse])
def list_accounts():
    db = get_db()
    try:
        rows = db.execute(
            "SELECT id, username, balance_cents FROM accounts ORDER BY id"
        ).fetchall()
    finally:
        db.close()
    return [dict(r) for r in rows]


@app.get("/accounts/{account_id}", response_model=AccountResponse)
def get_account(account_id: int):
    return _get_account_or_404(account_id)


@app.get("/accounts/{account_id}/balance")
def get_balance(account_id: int):
    acct = _get_account_or_404(account_id)
    return {"balance_cents": acct["balance_cents"]}


# ---------------------------------------------------------------------------
# Deposit / Withdraw
# ---------------------------------------------------------------------------


@app.post("/accounts/{account_id}/deposit", response_model=TransferResponse)
def account_deposit(
    account_id: int,
    body: DepositWithdrawRequest,
    idem_key: str = Depends(require_idempotency_key),
):
    _get_account_or_404(account_id)
    db = get_db()
    try:
        result = deposit(db, account_id, body.amount_cents, idem_key)
    except InsufficientFunds as e:
        raise HTTPException(status_code=400, detail=str(e))
    except DuplicateTransaction as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        db.close()
    return TransferResponse(**result)


@app.post("/accounts/{account_id}/withdraw", response_model=TransferResponse)
def account_withdraw(
    account_id: int,
    body: DepositWithdrawRequest,
    idem_key: str = Depends(require_idempotency_key),
):
    _get_account_or_404(account_id)
    db = get_db()
    try:
        result = withdrawal(db, account_id, body.amount_cents, idem_key)
    except InsufficientFunds as e:
        raise HTTPException(status_code=400, detail=str(e))
    except DuplicateTransaction as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        db.close()
    return TransferResponse(**result)


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------


@app.post("/transfers", response_model=TransferResponse, status_code=201)
def create_transfer(
    body: TransferRequest,
    idem_key: str = Depends(require_idempotency_key),
):
    db = get_db()
    try:
        result = transfer(
            db, body.from_account_id, body.to_account_id,
            body.amount_cents, idem_key,
        )
    except InsufficientFunds as e:
        raise HTTPException(status_code=400, detail=str(e))
    except DuplicateTransaction as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        db.close()
    return TransferResponse(**result)


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


@app.get("/accounts/{account_id}/transactions", response_model=TransactionListResponse)
def list_transactions(account_id: int):
    _get_account_or_404(account_id)
    db = get_db()
    try:
        rows = db.execute(
            """SELECT le.id, le.transaction_id, le.account_id, le.amount_cents,
                      t.type, t.status, le.created_at
               FROM ledger_entries le
               JOIN transactions t ON t.id = le.transaction_id
               WHERE le.account_id = ?
               ORDER BY le.id""",
            (account_id,),
        ).fetchall()
    finally:
        db.close()
    items = [TransactionItem(**dict(r)) for r in rows]
    return TransactionListResponse(transactions=items)