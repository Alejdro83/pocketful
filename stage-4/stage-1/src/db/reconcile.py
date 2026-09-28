"""Reconciliation job: detect and repair drift between cached balance_cents and ledger sums."""

from __future__ import annotations

import json
import sqlite3


DRIFT_QUERY = """
SELECT
    a.id,
    a.balance_cents AS cached,
    COALESCE(SUM(le.amount_cents), 0) AS actual
FROM accounts a
LEFT JOIN ledger_entries le ON le.account_id = a.id
GROUP BY a.id
"""


def reconcile(db: sqlite3.Connection) -> dict:
    """Compare accounts.balance_cents vs SUM(ledger_entries.amount_cents).

    Returns ``{status, checked, drifted}`` with optional ``accounts`` list
    when drift is found.
    """
    rows = db.execute(DRIFT_QUERY).fetchall()
    drifted = []
    for r in rows:
        diff = r["cached"] - r["actual"]
        if diff != 0:
            drifted.append(
                {"id": r["id"], "cached": r["cached"], "actual": r["actual"], "diff": diff}
            )

    if not drifted:
        return {"status": "ok", "checked": len(rows), "drifted": 0}
    return {
        "status": "drift_detected",
        "checked": len(rows),
        "drifted": len(drifted),
        "accounts": drifted,
    }


def repair(db: sqlite3.Connection, *, dry_run: bool = True) -> dict:
    """Fix drift by setting balance_cents to the ledger sum.

    When *dry_run* is True (default) nothing is written; the return value
    describes what *would* change.
    """
    rows = db.execute(DRIFT_QUERY).fetchall()
    fixes = []
    for r in rows:
        diff = r["cached"] - r["actual"]
        if diff != 0:
            fixes.append(
                {"id": r["id"], "cached": r["cached"], "actual": r["actual"], "diff": diff}
            )

    if not fixes:
        return {"status": "ok", "checked": len(rows), "repaired": 0, "dry_run": dry_run}

    if not dry_run:
        for f in fixes:
            db.execute(
                "UPDATE accounts SET balance_cents = ? WHERE id = ?",
                (f["actual"], f["id"]),
            )

    return {
        "status": "repaired" if not dry_run else "drift_detected",
        "checked": len(rows),
        "repaired": len(fixes),
        "accounts": fixes,
        "dry_run": dry_run,
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from src.db.database import get_db

    conn = get_db()
    try:
        result = reconcile(conn)
        print(json.dumps(result, indent=2))
    finally:
        conn.close()
