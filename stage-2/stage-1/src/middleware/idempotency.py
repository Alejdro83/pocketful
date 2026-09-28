"""Idempotency-key enforcement middleware for Pocketful.

This dependency checks for the ``X-Idempotency-Key`` header on money-moving
POST endpoints (transfers, deposit, withdraw).  The actual idempotency
dedup logic lives in the transfer functions themselves — this layer only
enforces that the header is *present* so those functions always receive a key.
"""

from fastapi import Header, HTTPException, Request

# POST paths that require an idempotency key
_REQUIRES_KEY: set[str] = {
    "/transfers",
}

# Deposit/withdraw paths use account IDs, so we match by suffix
_KEY_SUFFIXES: tuple[str, ...] = (
    "/deposit",
    "/withdraw",
)


def _path_requires_key(path: str) -> bool:
    """Return True if the request path demands an idempotency key."""
    if path in _REQUIRES_KEY:
        return True
    return any(path.endswith(s) for s in _KEY_SUFFIXES)


async def require_idempotency_key(
    request: Request,
    x_idempotency_key: str | None = Header(default=None),
) -> str:
    """FastAPI dependency: enforce ``X-Idempotency-Key`` on money-moving POSTs.

    Returns the key value on success so route handlers can forward it
    directly to the transfer / deposit / withdrawal functions.
    """
    if request.method == "POST" and _path_requires_key(request.url.path):
        if not x_idempotency_key:
            raise HTTPException(
                status_code=422,
                detail="X-Idempotency-Key header is required for this endpoint",
            )
        return x_idempotency_key

    # Non-matching endpoints pass through unconditionally
    return x_idempotency_key or ""