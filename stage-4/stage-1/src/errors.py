"""Standard error response helpers for Pocketful API."""

from fastapi.responses import JSONResponse

# Error codes
MALFORMED_REQUEST = "malformed_request"
MISSING_IDEMPOTENCY_KEY = "missing_idempotency_key"
UNAUTHENTICATED = "unauthenticated"
FORBIDDEN = "forbidden"
NOT_FOUND = "not_found"
INSUFFICIENT_FUNDS = "insufficient_funds"
IDEMPOTENCY_KEY_REUSE = "idempotency_key_reuse"
REQUEST_NOT_PENDING = "request_not_pending"
SELF_PAYMENT = "self_payment"
VALIDATION_FAILED = "validation_failed"
SELF_REQUEST = "self_request"
HANDLE_TAKEN = "handle_taken"
EMAIL_TAKEN = "email_taken"


def error_response(status_code: int, code: str, message: str) -> JSONResponse:
    """Return a standardised JSON error response."""
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
    )