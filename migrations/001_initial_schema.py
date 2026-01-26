"""
Example PostgreSQL migration script for LedgerKit.

This script demonstrates how to set up the ledger tables in PostgreSQL
with all necessary indexes and constraints.

Usage:
    python migrations/001_initial_schema.py | psql -d your_database
    
Or apply this in your migration tool (Alembic, Django migrations, etc.)
"""

SQL_MIGRATION = """
-- Create custom types
CREATE TYPE account_type AS ENUM ('asset', 'liability', 'equity', 'revenue', 'expense');
CREATE TYPE webhook_delivery_status AS ENUM ('pending', 'delivered', 'failed');

-- Accounts table
CREATE TABLE accounts (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    type account_type NOT NULL,
    balance NUMERIC(20, 2) NOT NULL DEFAULT 0,
    version INTEGER NOT NULL DEFAULT 0,
    metadata JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_accounts_name ON accounts(name);
CREATE INDEX idx_accounts_type ON accounts(type);

-- Entries table
CREATE TABLE entries (
    id SERIAL PRIMARY KEY,
    idempotency_key VARCHAR(255) NOT NULL UNIQUE,
    description TEXT NOT NULL,
    reverses_entry_id INTEGER REFERENCES entries(id),
    metadata JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_entries_idempotency_key ON entries(idempotency_key);
CREATE INDEX idx_entries_created_at ON entries(created_at);
CREATE INDEX idx_entries_reverses ON entries(reverses_entry_id);

-- Lines table
CREATE TABLE lines (
    id SERIAL PRIMARY KEY,
    entry_id INTEGER NOT NULL REFERENCES entries(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(id),
    amount NUMERIC(20, 2) NOT NULL,
    metadata JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT check_amount_nonzero CHECK (amount != 0)
);

CREATE INDEX idx_lines_entry_id ON lines(entry_id);
CREATE INDEX idx_lines_account_id ON lines(account_id);
CREATE INDEX idx_lines_created_at ON lines(created_at);

-- Webhook deliveries table
CREATE TABLE webhook_deliveries (
    id SERIAL PRIMARY KEY,
    idempotency_key VARCHAR(255) NOT NULL UNIQUE,
    entry_id INTEGER NOT NULL REFERENCES entries(id),
    url VARCHAR(2048) NOT NULL,
    payload JSONB NOT NULL,
    status webhook_delivery_status NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    next_retry_at TIMESTAMP,
    last_response_status INTEGER,
    last_response_body TEXT,
    last_error TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
    delivered_at TIMESTAMP
);

CREATE INDEX idx_webhook_deliveries_idempotency_key ON webhook_deliveries(idempotency_key);
CREATE INDEX idx_webhook_deliveries_status ON webhook_deliveries(status);
CREATE INDEX idx_webhook_deliveries_next_retry ON webhook_deliveries(next_retry_at);
CREATE INDEX idx_webhook_deliveries_entry_id ON webhook_deliveries(entry_id);

-- Trigger to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

CREATE TRIGGER update_accounts_updated_at BEFORE UPDATE ON accounts
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_webhook_deliveries_updated_at BEFORE UPDATE ON webhook_deliveries
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
"""

if __name__ == "__main__":
    print(SQL_MIGRATION)
