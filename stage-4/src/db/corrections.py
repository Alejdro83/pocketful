"""Payment corrections for Stage 3."""
import json
import uuid
from datetime import datetime, timezone


def get_current_revision(db, payment_id):
    """Get the current (latest) revision number for a payment."""
    row = db.execute(
        "SELECT MAX(revision_number) as max_rev FROM payment_revisions WHERE payment_id = ?",
        (payment_id,)
    ).fetchone()
    return row["max_rev"] if row and row["max_rev"] else 0


def get_payment_with_revision(db, payment_id):
    """Get payment with its current revision amount."""
    payment = db.execute(
        "SELECT * FROM transactions WHERE id = ?", (payment_id,)
    ).fetchone()
    if not payment:
        return None
    rev = db.execute(
        "SELECT * FROM payment_revisions WHERE payment_id = ? ORDER BY revision_number DESC LIMIT 1",
        (payment_id,)
    ).fetchone()
    return {
        "payment_id": payment["id"],
        "from_user_id": payment["from_user_id"],
        "to_user_id": payment["to_user_id"],
        "current_amount": rev["amount"] if rev else payment["amount"],
        "current_revision": rev["revision_number"] if rev else 1,
        "visibility": payment["visibility"],
        "note": payment["note"],
        "request_id": payment["request_id"],
        "settlement_id": payment["settlement_id"],
        "created_at": payment["created_at"],
    }


def create_correction(db, payment_id, user_id, expected_revision, new_amount, effective_at, reason, idempotency_key=None):
    """Create a payment correction (new revision)."""
    from src.db.idempotency import check_idempotency, save_idempotency, request_hash
    from src.db.exceptions import IdempotencyKeyReuse

    payment = get_payment_with_revision(db, payment_id)
    if not payment:
        return None, "not_found"

    if payment["from_user_id"] != user_id:
        return None, "forbidden"

    current_rev = payment["current_revision"]
    if expected_revision != current_rev:
        return None, "stale_revision"

    # Check idempotency
    if idempotency_key:
        body = {"payment_id": payment_id, "expected_revision": expected_revision,
                "amount": new_amount, "effective_at": effective_at, "reason": reason}
        action, data = check_idempotency(db, user_id, idempotency_key, body)
        if action == "replay":
            return {"status_code": 200, "body": json.loads(data)}, "replay"
        elif action == "conflict":
            return None, "idempotency_key_reuse"

    now = datetime.now(timezone.utc).isoformat()
    new_rev_number = current_rev + 1

    # Calculate balance adjustment
    old_amount = payment["current_amount"]
    delta = new_amount - old_amount  # positive = more money to receiver, negative = less

    # Insert revision
    corr_id = "cr_" + uuid.uuid4().hex[:8]
    db.execute(
        """INSERT INTO payment_revisions (id, payment_id, revision_number, amount, effective_at, recorded_at, reason)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (corr_id, payment_id, new_rev_number, new_amount, effective_at, now, reason)
    )

    # Adjust wallet balances if delta != 0
    if delta != 0:
        # Credit back to sender if delta < 0 (they get money back)
        # Credit to receiver if delta > 0 (they get more)
        from_user = payment["from_user_id"]
        to_user = payment["to_user_id"]
        db.execute("UPDATE wallets SET cached_balance = cached_balance - ? WHERE user_id = ?",
                   (delta, from_user))
        db.execute("UPDATE wallets SET cached_balance = cached_balance + ? WHERE user_id = ?",
                   (delta, to_user))

    result = {
        "correction_id": corr_id,
        "payment_id": payment_id,
        "revision_number": new_rev_number,
        "amount": new_amount,
        "effective_at": effective_at,
        "recorded_at": now,
        "reason": reason,
    }

    # Save idempotency
    if idempotency_key:
        save_idempotency(db, user_id, idempotency_key, body, result, 201)

    return result, "created"


def get_revisions(db, payment_id):
    """Get all revisions for a payment."""
    rows = db.execute(
        "SELECT * FROM payment_revisions WHERE payment_id = ? ORDER BY revision_number ASC",
        (payment_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_balance_as_of(db, user_id, as_of):
    """Get wallet balance as of a specific instant."""
    # Get current balance
    wallet = db.execute("SELECT cached_balance FROM wallets WHERE user_id = ?", (user_id,)).fetchone()
    if not wallet:
        return 0
    current = wallet["cached_balance"]

    # Sum corrections after as_of that affect this user
    # For corrections that changed the amount after as_of, we need to undo them
    # Simpler approach: replay from opening balance
    # Opening balance = wallet balance at creation (0 for new users, fixture balance for seeded)

    # Actually, let's compute from the ledger entries
    # Balance = SUM of all ledger entries for this user where transaction created_at <= as_of
    row = db.execute(
        """SELECT COALESCE(SUM(le.amount), 0) as balance
           FROM ledger_entries le
           JOIN transactions t ON le.transaction_id = t.id
           WHERE le.account_id = ? AND t.created_at <= ?""",
        (user_id, as_of)
    ).fetchone()
    return row["balance"] if row else 0
