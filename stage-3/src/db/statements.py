"""Statement generation for Stage 3."""


def get_statement(db, user_id, from_time=None, to_time=None, limit=50, offset=0):
    """Generate a statement for a user within a time window."""
    # Get all payments where user is sender or receiver
    # within the window, ordered by created_at ASC

    conditions = ["(t.from_user_id = ? OR t.to_user_id = ?)"]
    params = [user_id, user_id]

    if from_time:
        conditions.append("t.created_at >= ?")
        params.append(from_time)
    if to_time:
        conditions.append("t.created_at < ?")
        params.append(to_time)

    where = " AND ".join(conditions)

    # Count total
    count_row = db.execute(
        f"SELECT COUNT(*) as cnt FROM transactions t WHERE {where}", params
    ).fetchone()
    total = count_row["cnt"] if count_row else 0

    # Get entries with pagination
    entries_params = params + [limit, offset]
    rows = db.execute(
        f"""SELECT t.id, t.from_user_id, t.to_user_id, t.amount, t.note, t.visibility,
                   t.request_id, t.settlement_id, t.created_at
            FROM transactions t
            WHERE {where}
            ORDER BY t.created_at ASC, t.id ASC
            LIMIT ? OFFSET ?""",
        entries_params
    ).fetchall()

    # Calculate opening balance: current balance - sum of all deltas in the window
    # This gives the balance before the window started
    wallet = db.execute("SELECT cached_balance FROM wallets WHERE user_id = ?", (user_id,)).fetchone()
    current_balance = wallet["cached_balance"] if wallet else 0

    # Get total deltas in the window
    delta_params = [user_id] + params  # user_id for CASE, then params for WHERE
    delta_row = db.execute(
        f"""SELECT COALESCE(SUM(
            CASE WHEN t.from_user_id = ? THEN -t.amount ELSE t.amount END
        ), 0) as total_delta
        FROM transactions t
        WHERE {where}""",
        delta_params
    ).fetchone()
    total_delta = delta_row["total_delta"] if delta_row else 0

    opening_balance = current_balance - total_delta

    # Build entries with delta and balance_after
    entries = []
    running_balance = opening_balance

    # We need ALL entries in the window to compute running balance correctly
    all_rows = db.execute(
        f"""SELECT t.id, t.from_user_id, t.to_user_id, t.amount, t.note, t.visibility,
                   t.request_id, t.settlement_id, t.created_at
            FROM transactions t
            WHERE {where}
            ORDER BY t.created_at ASC, t.id ASC""",
        params
    ).fetchall()

    for row in all_rows:
        is_sender = row["from_user_id"] == user_id
        delta = -row["amount"] if is_sender else row["amount"]
        running_balance += delta
        entry = {
            "payment": {
                "payment_id": row["id"],
                "from_user_id": row["from_user_id"],
                "to_user_id": row["to_user_id"],
                "amount": row["amount"],
                "note": row["note"],
                "visibility": row["visibility"],
                "request_id": row["request_id"],
                "created_at": row["created_at"],
            },
            "delta": delta,
            "balance_after": running_balance,
        }
        entries.append(entry)

    closing_balance = running_balance

    # Apply pagination
    paginated = entries[offset:offset + limit]
    has_more = (offset + limit) < len(entries)

    return {
        "opening_balance": opening_balance,
        "entries": paginated,
        "closing_balance": closing_balance,
        "has_more": has_more,
    }
