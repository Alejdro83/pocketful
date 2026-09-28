"""Authentication helpers — signup, login, token management."""

from __future__ import annotations

import re
import secrets
from typing import Optional

import bcrypt


# ---------------------------------------------------------------------------
# In-memory token store: token -> user_id
# ---------------------------------------------------------------------------

_tokens: dict[str, str] = {}


def clear_tokens() -> None:
    """Wipe all tokens (called on /_test/reset)."""
    _tokens.clear()


# ---------------------------------------------------------------------------
# Handle derivation
# ---------------------------------------------------------------------------

def derive_handle(email: str) -> str:
    """Derive a handle from an email: local part, lowercase, non-alnum -> _."""
    local = email.split("@")[0].lower()
    handle = re.sub(r"[^a-z0-9_]", "_", local)
    return handle[:20]


# ---------------------------------------------------------------------------
# User creation
# ---------------------------------------------------------------------------

def create_user(
    db,
    email: str,
    password: str,
    display_name: str,
    currency: str,
    minor_units: int,
) -> dict:
    """Create a new user.  Returns {"user_id", "display_name", "token"}.

    Raises ValueError with code: email_taken | handle_taken | validation_failed
    """
    # Validate
    if not password or len(password) < 8:
        raise ValueError("validation_failed")
    if not email or "@" not in email:
        raise ValueError("validation_failed")

    handle = derive_handle(email)

    # Unique email?
    if db.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        raise ValueError("email_taken")

    # Unique handle?
    if db.execute("SELECT 1 FROM users WHERE handle = ?", (handle,)).fetchone():
        raise ValueError("handle_taken")

    # Hash password
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    # Generate user_id (next numeric u_N that doesn't collide)
    user_id = _next_user_id(db)

    db.execute(
        """INSERT INTO users (id, email, password_hash, display_name, handle)
           VALUES (?, ?, ?, ?, ?)""",
        (user_id, email, pw_hash, display_name, handle),
    )
    db.execute(
        """INSERT INTO wallets (user_id, currency, minor_units, cached_balance)
           VALUES (?, ?, ?, 0)""",
        (user_id, currency, minor_units),
    )
    db.commit()

    token = secrets.token_urlsafe(32)
    _tokens[token] = user_id

    return {"user_id": user_id, "display_name": display_name, "token": token}


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

def login_user(db, email: str, password: str) -> dict:
    """Verify credentials.  Returns {"user_id", "display_name", "token"}.

    Raises ValueError("unauthenticated") on failure.
    """
    row = db.execute(
        "SELECT id, password_hash, display_name FROM users WHERE email = ?",
        (email,),
    ).fetchone()

    if row is None:
        raise ValueError("unauthenticated")

    if not bcrypt.checkpw(password.encode(), row["password_hash"].encode()):
        raise ValueError("unauthenticated")

    token = secrets.token_urlsafe(32)
    _tokens[token] = row["id"]

    return {"user_id": row["id"], "display_name": row["display_name"], "token": token}


# ---------------------------------------------------------------------------
# Token verification
# ---------------------------------------------------------------------------

def verify_token(token: str) -> Optional[str]:
    """Return user_id for a valid bearer token, or None."""
    return _tokens.get(token)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _next_user_id(db) -> str:
    """Pick the next unused numeric u_N id."""
    rows = db.execute("SELECT id FROM users").fetchall()
    max_n = 0
    for row in rows:
        uid = row["id"]
        if uid.startswith("u_"):
            try:
                max_n = max(max_n, int(uid[2:]))
            except ValueError:
                pass
    return f"u_{max_n + 1}"