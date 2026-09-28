"""Idempotency helpers — scoped per-user, persisted only on success."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Optional, Tuple


def request_hash(body) -> str:
    """SHA-256 hex digest of a request body (dict or str)."""
    if isinstance(body, dict):
        raw = json.dumps(body, sort_keys=True, separators=(",", ":"))
    else:
        raw = str(body)
    return hashlib.sha256(raw.encode()).hexdigest()


def check_idempotency(
    db: sqlite3.Connection,
    user_id: str,
    key: str,
    request_body,
) -> Tuple[str, Optional[dict]]:
    """Check idempotency_records for (user_id, key).

    Returns:
        ('replay', stored_response)  — same key + same body → replay
        ('conflict', None)           — same key + different body → 409
        ('proceed', None)            — key not found or was failed → proceed
    """
    row = db.execute(
        "SELECT request_hash, response_body, status_code FROM idempotency_records "
        "WHERE user_id = ? AND idempotency_key = ?",
        (user_id, key),
    ).fetchone()

    if row is None:
        return ("proceed", None)

    # Previously failed — key is reusable
    if row["status_code"] >= 400:
        return ("proceed", None)

    # Previously succeeded (status_code == 200)
    new_hash = request_hash(request_body)
    if row["request_hash"] == new_hash:
        stored = json.loads(row["response_body"])
        return ("replay", stored)
    else:
        return ("conflict", None)


def save_idempotency(
    db: sqlite3.Connection,
    user_id: str,
    key: str,
    request_body,
    response_body: dict,
    status_code: int,
) -> None:
    """Persist an idempotency record (inside an existing transaction)."""
    db.execute(
        "INSERT OR REPLACE INTO idempotency_records "
        "(user_id, idempotency_key, request_hash, response_body, status_code) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            user_id,
            key,
            request_hash(request_body),
            json.dumps(response_body, sort_keys=True, separators=(",", ":")),
            status_code,
        ),
    )