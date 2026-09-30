"""Pocketful FastAPI application — Stage 1 gate endpoints."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, HTMLResponse
from fastapi.staticfiles import StaticFiles

from src.auth import clear_tokens, create_user, login_user, verify_token
from src.db.schema import init_db
from src.db.export_import import export_state, import_state
from src.db.exceptions import IdempotencyKeyReuse, InsufficientFunds, SelfPayment
from src.db.idempotency import check_idempotency, save_idempotency
from src.db.payments import get_activity, get_balance, make_payment
from src.db.schema import SCHEMA_SQL, TABLES, get_db, reset_db
from src.db.settlements import create_settlement
from src.db.splits import build_request_dict, create_split
from src.errors import (
    EMAIL_TAKEN,
    FORBIDDEN,
    HANDLE_TAKEN,
    IDEMPOTENCY_KEY_REUSE,
    INSUFFICIENT_FUNDS,
    MALFORMED_REQUEST,
    MISSING_IDEMPOTENCY_KEY,
    NOT_FOUND,
    REQUEST_NOT_PENDING,
    SELF_PAYMENT,
    SELF_REQUEST,
    UNAUTHENTICATED,
    VALIDATION_FAILED,
    error_response,
)

# ---------------------------------------------------------------------------
# Config (set by /_test/reset, used by /auth/signup)
# ---------------------------------------------------------------------------

_config: dict = {"currency": "EUR", "minor_units": 2}

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    yield


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Pocketful", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

PUBLIC_PATHS = frozenset({
    "/health",
    "/_test/reset",
    "/_test/export",
    "/_test/import",
    "/auth/signup",
    "/auth/login",
    "/",
})


@app.middleware("http")
async def _auth_middleware(request: Request, call_next):
    if request.url.path in PUBLIC_PATHS:
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return error_response(401, UNAUTHENTICATED, "Missing or invalid authorization header")
    token = auth[7:]
    user_id = verify_token(token)
    if user_id is None:
        return error_response(401, UNAUTHENTICATED, "Invalid token")
    request.state.user_id = user_id
    return await call_next(request)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_idempotency_key(request: Request) -> str | None:
    """Return the Idempotency-Key header value, or None."""
    return request.headers.get("Idempotency-Key") or request.headers.get("idempotency-key")


def _next_request_id(db) -> str:
    rows = db.execute("SELECT id FROM requests").fetchall()
    max_n = 0
    for row in rows:
        rid = row["id"]
        if rid.startswith("rq_"):
            try:
                max_n = max(max_n, int(rid[3:]))
            except ValueError:
                pass
    return f"rq_{max_n + 1}"


def _lookup_user_by_handle(db, handle: str) -> dict | None:
    """Return {id, handle} for a user looked up by handle, or None."""
    row = db.execute(
        "SELECT id, handle FROM users WHERE handle = ?", (handle,)
    ).fetchone()
    if row is None:
        return None
    return {"id": row["id"], "handle": row["handle"]}


# ===================================================================
# 1. GET /health
# ===================================================================


@app.get("/")
async def index(request: Request):
    accept = request.headers.get("accept", "")
    if "text/html" in accept:
        import os
        html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static", "index.html")
        if os.path.exists(html_path):
            with open(html_path) as f:
                return HTMLResponse(f.read())
    return JSONResponse({"message": "Pocketful API"})

@app.get("/health")
def health():
    return {"status": "ok"}


# ===================================================================
# 2. POST /_test/reset
# ===================================================================


@app.post("/_test/reset", status_code=204)
async def test_reset(request: Request):
    fixture = await request.json()
    reset_db(fixture)
    _config["currency"] = fixture.get("currency", "EUR")
    _config["minor_units"] = fixture.get("minor_units", 2)
    clear_tokens()
    return Response(status_code=204)


# ===================================================================
# 3. POST /auth/signup
# ===================================================================


@app.post("/auth/signup", status_code=201)
async def signup(request: Request):
    body = await request.json()
    email: str = body.get("email", "")
    password: str = body.get("password", "")
    display_name: str = body.get("display_name", "")

    if not password or len(password) < 8:
        return error_response(422, VALIDATION_FAILED, "Password must be at least 8 characters")
    if not email or "@" not in email:
        return error_response(422, VALIDATION_FAILED, "Invalid email address")

    db = get_db()
    try:
        result = create_user(
            db, email, password, display_name,
            _config["currency"], _config["minor_units"],
        )
        return result
    except ValueError as exc:
        code = str(exc)
        if code == EMAIL_TAKEN:
            return error_response(409, EMAIL_TAKEN, "Email already taken")
        if code == HANDLE_TAKEN:
            return error_response(409, HANDLE_TAKEN, "Handle already taken")
        return error_response(422, VALIDATION_FAILED, code)
    finally:
        db.close()


# ===================================================================
# 4. POST /auth/login
# ===================================================================


@app.post("/auth/login")
async def login(request: Request):
    body = await request.json()
    email: str = body.get("email", "")
    password: str = body.get("password", "")

    db = get_db()
    try:
        result = login_user(db, email, password)
        return result
    except ValueError:
        return error_response(401, UNAUTHENTICATED, "Invalid email or password")
    finally:
        db.close()


# ===================================================================
# 5. GET /me
# ===================================================================


@app.get("/me")
def me(request: Request):
    user_id: str = request.state.user_id
    db = get_db()
    try:
        user = db.execute(
            "SELECT id, display_name, handle FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if user is None:
            return error_response(404, NOT_FOUND, "User not found")
        wallet = db.execute(
            "SELECT cached_balance, currency, minor_units FROM wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return {
            "user_id": user["id"],
            "display_name": user["display_name"],
            "handle": user["handle"],
            "balance": wallet["cached_balance"] if wallet else 0,
            "currency": wallet["currency"] if wallet else "EUR",
            "minor_units": wallet["minor_units"] if wallet else 2,
        }
    finally:
        db.close()


# ===================================================================
# 6. GET /balance
# ===================================================================


@app.get("/balance")
def balance(request: Request):
    user_id: str = request.state.user_id
    db = get_db()
    try:
        row = db.execute(
            "SELECT cached_balance FROM wallets WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if row is None:
            return error_response(404, NOT_FOUND, "User not found")
        return {"balance": row["cached_balance"]}
    finally:
        db.close()


# ===================================================================
# 7. POST /payments — idempotent
# ===================================================================


@app.post("/payments", status_code=201)
async def post_payment(request: Request):
    user_id: str = request.state.user_id
    key = _require_idempotency_key(request)
    if not key:
        return error_response(400, MISSING_IDEMPOTENCY_KEY, "Idempotency-Key header required")

    body = await request.json()
    to_handle: str = body.get("to_handle", "")
    amount: int = body.get("amount", 0)
    note: str = body.get("note", "")
    visibility: str = body.get("visibility", "public")

    if not to_handle or amount <= 0:
        return error_response(422, VALIDATION_FAILED, "to_handle and positive amount required")

    db = get_db()
    try:
        # Idempotency check
        action, cached = check_idempotency(db, user_id, key, body)
        if action == "replay":
            return JSONResponse(status_code=201, content=cached)
        if action == "conflict":
            return error_response(409, IDEMPOTENCY_KEY_REUSE, "Idempotency key already used with different payload")

        # Lookup recipient
        recipient = _lookup_user_by_handle(db, to_handle)
        if recipient is None:
            save_idempotency(db, user_id, key, body,
                             {"error": {"code": NOT_FOUND, "message": "Recipient not found"}}, 404)
            return error_response(404, NOT_FOUND, "Recipient not found")

        # Self-payment check
        if recipient["id"] == user_id:
            save_idempotency(db, user_id, key, body,
                             {"error": {"code": SELF_PAYMENT, "message": "Cannot pay yourself"}}, 409)
            return error_response(409, SELF_PAYMENT, "Cannot pay yourself")

        # Execute payment
        try:
            result = make_payment(
                db, user_id, recipient["id"], amount,
                note=note, visibility=visibility,
            )
        except SelfPayment:
            save_idempotency(db, user_id, key, body,
                             {"error": {"code": SELF_PAYMENT, "message": "Cannot pay yourself"}}, 409)
            return error_response(409, SELF_PAYMENT, "Cannot pay yourself")
        except InsufficientFunds:
            save_idempotency(db, user_id, key, body,
                             {"error": {"code": INSUFFICIENT_FUNDS, "message": "Insufficient funds"}}, 409)
            return error_response(409, INSUFFICIENT_FUNDS, "Insufficient funds")
        except ValueError as exc:
            code = str(exc)
            if code == "not_found":
                return error_response(404, NOT_FOUND, "User not found")
            return error_response(422, VALIDATION_FAILED, code)

        save_idempotency(db, user_id, key, body, result, 201)
        return JSONResponse(status_code=201, content=result)
    finally:
        db.close()


# ===================================================================
# 8. GET /activity
# ===================================================================


@app.get("/activity")
def activity(request: Request):
    user_id: str = request.state.user_id
    limit = int(request.query_params.get("limit", "50"))
    offset = int(request.query_params.get("offset", "0"))
    db = get_db()
    try:
        result = get_activity(db, user_id, limit=limit, offset=offset)
        return result
    finally:
        db.close()


# ===================================================================
# 9. POST /requests — idempotent
# ===================================================================


@app.post("/requests", status_code=201)
async def post_request(request: Request):
    user_id: str = request.state.user_id
    key = _require_idempotency_key(request)
    if not key:
        return error_response(400, MISSING_IDEMPOTENCY_KEY, "Idempotency-Key header required")

    body = await request.json()
    payer_handle: str = body.get("payer_handle", "")
    amount: int = body.get("amount", 0)
    note: str = body.get("note", "")

    if not payer_handle or amount <= 0:
        return error_response(422, VALIDATION_FAILED, "payer_handle and positive amount required")

    db = get_db()
    try:
        action, cached = check_idempotency(db, user_id, key, body)
        if action == "replay":
            return JSONResponse(status_code=201, content=cached)
        if action == "conflict":
            return error_response(409, IDEMPOTENCY_KEY_REUSE, "Idempotency key already used with different payload")

        # Lookup payer
        payer = _lookup_user_by_handle(db, payer_handle)
        if payer is None:
            save_idempotency(db, user_id, key, body,
                             {"error": {"code": NOT_FOUND, "message": "Payer not found"}}, 404)
            return error_response(404, NOT_FOUND, "Payer not found")

        if payer["id"] == user_id:
            save_idempotency(db, user_id, key, body,
                             {"error": {"code": SELF_REQUEST, "message": "Cannot request from yourself"}}, 422)
            return error_response(422, SELF_REQUEST, "Cannot request from yourself")

        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        rq_id = _next_request_id(db)

        db.execute(
            """INSERT INTO requests (id, requester_id, payer_id, amount, note, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (rq_id, user_id, payer["id"], amount, note, now),
        )
        db.commit()

        result = build_request_dict(db, rq_id)
        save_idempotency(db, user_id, key, body, result, 201)
        return JSONResponse(status_code=201, content=result)
    finally:
        db.close()


# ===================================================================
# 10. GET /requests
# ===================================================================


@app.get("/requests")
def list_requests(request: Request):
    user_id: str = request.state.user_id
    direction = request.query_params.get("direction", "")
    status_filter = request.query_params.get("status", "")
    limit = int(request.query_params.get("limit", "50"))
    offset = int(request.query_params.get("offset", "0"))

    db = get_db()
    try:
        conditions = []
        params: list = []

        if direction == "incoming":
            conditions.append("r.payer_id = ?")
            params.append(user_id)
        elif direction == "outgoing":
            conditions.append("r.requester_id = ?")
            params.append(user_id)
        else:
            conditions.append("(r.payer_id = ? OR r.requester_id = ?)")
            params.extend([user_id, user_id])

        if status_filter:
            conditions.append("r.status = ?")
            params.append(status_filter)

        where = " AND ".join(conditions) if conditions else "1=1"
        params.extend([limit + 1, offset])

        rows = db.execute(
            f"""SELECT r.id FROM requests r
                WHERE {where}
                ORDER BY r.created_at DESC
                LIMIT ? OFFSET ?""",
            params,
        ).fetchall()

        has_more = len(rows) > limit
        rows = rows[:limit]

        requests_list = [build_request_dict(db, r["id"]) for r in rows]
        return {"requests": requests_list, "has_more": has_more}
    finally:
        db.close()


# ===================================================================
# 11. POST /requests/{id}/pay — idempotent
# ===================================================================


@app.post("/requests/{request_id}/pay", status_code=201)
async def pay_request(request: Request, request_id: str):
    user_id: str = request.state.user_id
    key = _require_idempotency_key(request)
    if not key:
        return error_response(400, MISSING_IDEMPOTENCY_KEY, "Idempotency-Key header required")

    body = await request.json()
    visibility: str = body.get("visibility", "public")

    db = get_db()
    try:
        action, cached = check_idempotency(db, user_id, key, body)
        if action == "replay":
            return JSONResponse(status_code=201, content=cached)
        if action == "conflict":
            return error_response(409, IDEMPOTENCY_KEY_REUSE, "Idempotency key already used with different payload")

        # Fetch request
        rq = db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        if rq is None:
            return error_response(404, NOT_FOUND, "Request not found")

        # Only payer can pay
        if rq["payer_id"] != user_id:
            save_idempotency(db, user_id, key, body,
                             {"error": {"code": FORBIDDEN, "message": "Only the payer can pay"}}, 403)
            return error_response(403, FORBIDDEN, "Only the payer can pay this request")

        # Must be pending
        if rq["status"] != "pending":
            save_idempotency(db, user_id, key, body,
                             {"error": {"code": REQUEST_NOT_PENDING, "message": "Request is not pending"}}, 409)
            return error_response(409, REQUEST_NOT_PENDING, "Request is not pending")

        # Execute payment (payer → requester)
        try:
            payment = make_payment(
                db, rq["payer_id"], rq["requester_id"], rq["amount"],
                note=rq["note"], visibility=visibility,
                request_id=request_id, txn_type="request_payment",
            )
        except InsufficientFunds:
            save_idempotency(db, user_id, key, body,
                             {"error": {"code": INSUFFICIENT_FUNDS, "message": "Insufficient funds"}}, 409)
            return error_response(409, INSUFFICIENT_FUNDS, "Insufficient funds")

        # Update request status
        db.execute(
            "UPDATE requests SET status = 'paid', payment_id = ? WHERE id = ?",
            (payment["payment_id"], request_id),
        )
        db.commit()

        save_idempotency(db, user_id, key, body, payment, 201)
        return JSONResponse(status_code=201, content=payment)
    finally:
        db.close()


# ===================================================================
# 12. POST /requests/{id}/decline — NO idempotency key
# ===================================================================


@app.post("/requests/{request_id}/decline")
async def decline_request(request: Request, request_id: str):
    user_id: str = request.state.user_id

    db = get_db()
    try:
        rq = db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        if rq is None:
            return error_response(404, NOT_FOUND, "Request not found")

        # Only payer can decline
        if rq["payer_id"] != user_id:
            return error_response(403, FORBIDDEN, "Only the payer can decline this request")

        # Already declined → idempotent success
        if rq["status"] == "declined":
            return build_request_dict(db, request_id)

        # Must be pending
        if rq["status"] != "pending":
            return error_response(409, REQUEST_NOT_PENDING, "Request is not pending")

        db.execute(
            "UPDATE requests SET status = 'declined' WHERE id = ?", (request_id,)
        )
        db.commit()

        return build_request_dict(db, request_id)
    finally:
        db.close()


# ===================================================================
# 13. POST /requests/{id}/cancel — NO idempotency key
# ===================================================================


@app.post("/requests/{request_id}/cancel")
async def cancel_request(request: Request, request_id: str):
    user_id: str = request.state.user_id

    db = get_db()
    try:
        rq = db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
        if rq is None:
            return error_response(404, NOT_FOUND, "Request not found")

        # Only requester can cancel
        if rq["requester_id"] != user_id:
            return error_response(403, FORBIDDEN, "Only the requester can cancel this request")

        # Already cancelled → idempotent success
        if rq["status"] == "cancelled":
            return build_request_dict(db, request_id)

        # Must be pending
        if rq["status"] != "pending":
            return error_response(409, REQUEST_NOT_PENDING, "Request is not pending")

        db.execute(
            "UPDATE requests SET status = 'cancelled' WHERE id = ?", (request_id,)
        )
        db.commit()

        return build_request_dict(db, request_id)
    finally:
        db.close()


# ===================================================================
# 14. POST /splits — idempotent
# ===================================================================


@app.post("/splits", status_code=201)
async def post_split(request: Request):
    user_id: str = request.state.user_id
    key = _require_idempotency_key(request)
    if not key:
        return error_response(400, MISSING_IDEMPOTENCY_KEY, "Idempotency-Key header required")

    body = await request.json()
    amount: int = body.get("amount", 0)
    participant_handles: list[str] = body.get("participant_handles", [])
    note: str = body.get("note", "")

    if amount <= 0 or len(participant_handles) < 2:
        return error_response(422, VALIDATION_FAILED, "amount and at least 2 participant_handles required")

    db = get_db()
    try:
        action, cached = check_idempotency(db, user_id, key, body)
        if action == "replay":
            return JSONResponse(status_code=201, content=cached)
        if action == "conflict":
            return error_response(409, IDEMPOTENCY_KEY_REUSE, "Idempotency key already used with different payload")

        try:
            result = create_split(db, user_id, amount, participant_handles, note)
        except ValueError as exc:
            code = str(exc)
            if code == "not_found":
                return error_response(404, NOT_FOUND, "Participant not found")
            return error_response(422, VALIDATION_FAILED, code)

        save_idempotency(db, user_id, key, body, result, 201)
        return JSONResponse(status_code=201, content=result)
    finally:
        db.close()


# ===================================================================
# 15. POST /settlements — idempotent + operator
# ===================================================================


@app.post("/settlements", status_code=201)
async def post_settlement(request: Request):
    user_id: str = request.state.user_id
    key = _require_idempotency_key(request)
    if not key:
        return error_response(400, MISSING_IDEMPOTENCY_KEY, "Idempotency-Key header required")

    body = await request.json()
    transfers: list[dict] = body.get("transfers", [])

    if not transfers:
        return error_response(422, VALIDATION_FAILED, "transfers list required")

    db = get_db()
    try:
        action, cached = check_idempotency(db, user_id, key, body)
        if action == "replay":
            return JSONResponse(status_code=201, content=cached)
        if action == "conflict":
            return error_response(409, IDEMPOTENCY_KEY_REUSE, "Idempotency key already used with different payload")

        try:
            result = create_settlement(db, user_id, transfers)
        except PermissionError:
            save_idempotency(db, user_id, key, body,
                             {"error": {"code": FORBIDDEN, "message": "Not a settlement operator"}}, 403)
            return error_response(403, FORBIDDEN, "Not a settlement operator")
        except ValueError as exc:
            code = str(exc)
            if code == "not_found":
                return error_response(404, NOT_FOUND, "User not found")
            if code == "insufficient_funds":
                return error_response(409, INSUFFICIENT_FUNDS, "Insufficient funds in one or more wallets")
            return error_response(422, VALIDATION_FAILED, code)

        save_idempotency(db, user_id, key, body, result, 201)
        return JSONResponse(status_code=201, content=result)
    finally:
        db.close()


# ===================================================================
# 16. GET /_test/export — unauthenticated
# ===================================================================


@app.get("/_test/export")
async def test_export():
    db = get_db()
    try:
        return export_state(db)
    finally:
        db.close()


# ===================================================================
# 17. POST /_test/import — unauthenticated
# ===================================================================


@app.post("/_test/import", status_code=204)
async def test_import(request: Request):
    body = await request.json()

    if body.get("track") != "pocketful" or body.get("format_version") != 1:
        return error_response(422, VALIDATION_FAILED, "Invalid track or format_version")

    db = get_db()
    try:
        import_state(db, body)
        clear_tokens()
        return Response(status_code=204)
    except Exception as exc:
        return error_response(422, VALIDATION_FAILED, str(exc))
    finally:
        db.close()
