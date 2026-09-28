"""Split operations for Pocketful — create shared expense requests."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# ID helpers
# ---------------------------------------------------------------------------

def _next_request_id(db: sqlite3.Connection) -> str:
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


def build_request_dict(db: sqlite3.Connection, request_id: str) -> dict:
    """Build the API response dict for a request."""
    rq = db.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    rh = db.execute("SELECT handle FROM users WHERE id = ?", (rq["requester_id"],)).fetchone()
    ph = db.execute("SELECT handle FROM users WHERE id = ?", (rq["payer_id"],)).fetchone()
    w = db.execute("SELECT currency FROM wallets WHERE user_id = ?", (rq["requester_id"],)).fetchone()
    return {
        "request_id": rq["id"],
        "requester_id": rq["requester_id"],
        "requester_handle": rh["handle"],
        "payer_id": rq["payer_id"],
        "payer_handle": ph["handle"],
        "amount": rq["amount"],
        "currency": w["currency"],
        "note": rq["note"],
        "status": rq["status"],
        "payment_id": rq["payment_id"],
        "created_at": rq["created_at"],
    }


# ---------------------------------------------------------------------------
# create_split
# ---------------------------------------------------------------------------

def create_split(
    db: sqlite3.Connection,
    requester_id: str,
    amount: int,
    participant_handles: list[str],
    note: str = "",
) -> dict:
    """Create a split — caller already paid; generates requests for every
    participant *except* the caller.

    Rounding: ``base = amount // n``, ``remainder = amount % n``.
    The first *remainder* participants each pay ``base + 1``.

    Returns ``{split_id, amount, currency, note, shares, requests, created_at}``.
    """
    if len(participant_handles) < 2:
        raise ValueError("validation_failed")
    if amount <= 0:
        raise ValueError("validation_failed")

    # Resolve caller handle
    caller_row = db.execute(
        "SELECT handle FROM users WHERE id = ?", (requester_id,)
    ).fetchone()
    if caller_row is None:
        raise ValueError("not_found")
    caller_handle = caller_row["handle"]

    # Verify all participants exist
    for h in participant_handles:
        exists = db.execute("SELECT 1 FROM users WHERE handle = ?", (h,)).fetchone()
        if not exists:
            raise ValueError("not_found")

    # Calculate shares
    n = len(participant_handles)
    base = amount // n
    remainder = amount % n
    shares = []
    for i, h in enumerate(participant_handles):
        share_amount = base + (1 if i < remainder else 0)
        shares.append({"handle": h, "amount": share_amount})

    # Currency
    wallet = db.execute(
        "SELECT currency FROM wallets WHERE user_id = ?", (requester_id,)
    ).fetchone()
    currency = wallet["currency"] if wallet else "EUR"

    # Create requests for non-caller participants
    now = datetime.now(timezone.utc).isoformat()
    requests = []
    for share in shares:
        if share["handle"] == caller_handle:
            continue
        payer_row = db.execute(
            "SELECT id FROM users WHERE handle = ?", (share["handle"],)
        ).fetchone()
        if payer_row is None:
            raise ValueError("not_found")

        rq_id = _next_request_id(db)
        db.execute(
            """INSERT INTO requests (id, requester_id, payer_id, amount, note, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (rq_id, requester_id, payer_row["id"], share["amount"], note, now),
        )
        requests.append(build_request_dict(db, rq_id))

    db.commit()

    # Generate an ephemeral split_id (no splits table — just for the response)
    import uuid
    split_id = "sp_" + uuid.uuid4().hex[:8]

    return {
        "split_id": split_id,
        "amount": amount,
        "currency": currency,
        "note": note,
        "shares": shares,
        "requests": requests,
        "created_at": now,
    }
