-- Pocketful wallet: double-entry accounting schema
-- All amounts in BIGINT cents (no floats)

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    balance_cents BIGINT NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS transactions (
    id INTEGER PRIMARY KEY,
    idempotency_key TEXT UNIQUE NOT NULL,
    request_hash TEXT NOT NULL,
    type TEXT NOT NULL CHECK(type IN ('transfer','deposit','withdrawal')),
    from_account_id INTEGER REFERENCES accounts(id),
    to_account_id INTEGER REFERENCES accounts(id),
    amount_cents BIGINT NOT NULL CHECK(amount_cents > 0),
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id INTEGER PRIMARY KEY,
    transaction_id INTEGER NOT NULL REFERENCES transactions(id),
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    amount_cents BIGINT NOT NULL,  -- positive=credit, negative=debit
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Indexes
CREATE UNIQUE INDEX IF NOT EXISTS idx_accounts_username ON accounts(username);
CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_idempotency_key ON transactions(idempotency_key);
CREATE INDEX IF NOT EXISTS idx_ledger_entries_transaction_id ON ledger_entries(transaction_id);
CREATE INDEX IF NOT EXISTS idx_ledger_entries_account_id ON ledger_entries(account_id);

-- Partial index: fast lookup of pending transactions (for retry/timeout logic)
CREATE INDEX IF NOT EXISTS idx_transactions_pending ON transactions(created_at)
    WHERE status = 'pending';

-- Double-entry invariant: every transaction's ledger entries must sum to 0.
-- Enforced via a BEFORE UPDATE trigger on transactions when status is set to
-- 'completed'. Workflow: INSERT transaction (pending) -> INSERT ledger entries
-- -> UPDATE status to completed (trigger validates sum == 0).
CREATE TRIGGER IF NOT EXISTS trg_check_double_entry_on_complete
BEFORE UPDATE OF status ON transactions
WHEN NEW.status = 'completed'
BEGIN
    SELECT RAISE(ABORT, 'double-entry violation: ledger entries do not sum to 0')
    WHERE (
        SELECT COALESCE(SUM(le.amount_cents), 0)
        FROM ledger_entries le
        WHERE le.transaction_id = NEW.id
    ) != 0;
END;