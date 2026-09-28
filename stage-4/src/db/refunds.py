"""Refunds for Stage 4."""
import uuid
from datetime import datetime, timezone
import json


def create_refund(db, payment_id, user_id, amount, idempotency_key=None):
    """Create a refund for a payment."""
    from src.db.idempotency import check_idempotency, save_idempotency

    # Get original payment
    payment = db.execute("SELECT * FROM transactions WHERE id = ?", (payment_id,)).fetchone()
    if not payment:
        return None, "not_found"

    # Only receiver can refund
    if payment["to_user_id"] != user_id:
        return None, "forbidden"

    # Can't refund a refund
    if payment["type"] == "refund":
        return None, "invalid_refund_target"

    # Check cumulative refunds don't exceed payment amount
    refunded_row = db.execute(
        """SELECT COALESCE(SUM(t.amount), 0) as total_refunded
           FROM transactions t WHERE t.refund_of = ?""",
        (payment_id,)
    ).fetchone()
    total_refunded = refunded_row["total_refunded"] if refunded_row else 0

    # Get current corrected amount
    rev_row = db.execute(
        "SELECT amount FROM payment_revisions WHERE payment_id = ? ORDER BY revision_number DESC LIMIT 1",
        (payment_id,)
    ).fetchone()
    current_amount = rev_row["amount"] if rev_row else payment["amount"]

    if total_refunded + amount > current_amount:
        return None, "refund_exceeds_payment"

    # Check idempotency
    if idempotency_key:
        body = {"payment_id": payment_id, "amount": amount}
        action, data = check_idempotency(db, user_id, idempotency_key, body)
        if action == "replay":
            return {"status_code": 200, "body": json.loads(data)}, "replay"
        elif action == "conflict":
            return None, "idempotency_key_reuse"

    # Check sender has enough funds (refund sends money back to original sender)
    sender_wallet = db.execute("SELECT cached_balance FROM wallets WHERE user_id = ?",
                               (payment["to_user_id"],)).fetchone()
    if sender_wallet["cached_balance"] < amount:
        return None, "insufficient_funds"

    now = datetime.now(timezone.utc).isoformat()
    refund_id = "p_" + uuid.uuid4().hex[:8]

    # Create refund as a new payment in opposite direction
    db.execute(
        """INSERT INTO transactions (id, type, from_user_id, to_user_id, amount, note, visibility,
           request_id, settlement_id, refund_of, created_at)
           VALUES (?, 'refund', ?, ?, ?, ?, ?, NULL, NULL, ?, ?)""",
        (refund_id, payment["to_user_id"], payment["from_user_id"], amount,
         payment["note"], payment["visibility"], payment_id, now)
    )

    # Ledger entries
    db.execute("INSERT INTO ledger_entries (transaction_id, account_id, amount) VALUES (?, ?, ?)",
               (refund_id, payment["to_user_id"], -amount))
    db.execute("INSERT INTO ledger_entries (transaction_id, account_id, amount) VALUES (?, ?, ?)",
               (refund_id, payment["from_user_id"], amount))

    # Update balances
    db.execute("UPDATE wallets SET cached_balance = cached_balance - ? WHERE user_id = ?",
               (amount, payment["to_user_id"]))
    db.execute("UPDATE wallets SET cached_balance = cached_balance + ? WHERE user_id = ?",
               (amount, payment["from_user_id"]))

    result = {
        "payment_id": refund_id,
        "from_user_id": payment["to_user_id"],
        "to_user_id": payment["from_user_id"],
        "amount": amount,
        "currency": "EUR",
        "note": payment["note"],
        "visibility": payment["visibility"],
        "refund_of": payment_id,
        "request_id": None,
        "created_at": now,
    }

    if idempotency_key:
        save_idempotency(db, user_id, idempotency_key, body, result, 201)

    return result, "created"


def create_correction_batch(db, operator_id, corrections, idempotency_key=None):
    """Create a batch of corrections atomically."""
    from src.db.corrections import create_correction
    import json

    if not corrections or len(corrections) > 32:
        return None, "validation_failed"

    # Check distinct payment_ids
    pids = [c["payment_id"] for c in corrections]
    if len(set(pids)) != len(pids):
        return None, "validation_failed"

    # Check idempotency
    if idempotency_key:
        body = {"corrections": corrections}
        from src.db.idempotency import check_idempotency, save_idempotency
        action, data = check_idempotency(db, operator_id, idempotency_key, body)
        if action == "replay":
            return {"status_code": 200, "body": json.loads(data)}, "replay"
        elif action == "conflict":
            return None, "idempotency_key_reuse"

    now = datetime.now(timezone.utc).isoformat()
    batch_id = "cb_" + uuid.uuid4().hex[:8]
    all_revisions = []

    for corr in corrections:
        result, status = create_correction(
            db, corr["payment_id"], operator_id,
            corr["expected_revision"], corr["amount"],
            corr["effective_at"], corr["reason"]
        )
        if status == "not_found":
            return None, "not_found"
        elif status == "stale_revision":
            return None, "stale_revision"
        elif status == "forbidden":
            return None, "forbidden"
        result["correction_batch_id"] = batch_id
        all_revisions.append(result)

    response = {
        "correction_batch_id": batch_id,
        "recorded_at": now,
        "revisions": all_revisions,
    }

    if idempotency_key:
        save_idempotency(db, operator_id, idempotency_key, body, response, 201)

    return response, "created"
