# Architecture and Design

## Overview

LedgerKit is a PostgreSQL-backed, double-entry ledger system designed for production use in marketplaces, wallets, and internal credit systems. It prioritizes correctness, auditability, and operational resilience.

## Core Principles

### 1. Idempotency Everywhere

Every state-changing operation accepts an idempotency key. This makes the system resilient to:
- Network timeouts and retries
- Webhook delivery failures
- Queue worker restarts
- User double-clicks

**Implementation:**
- Unique constraint on `entries.idempotency_key`
- Check-before-insert pattern returns existing records
- No duplicate transactions ever created

### 2. Concurrency-Safe Spending

Multiple workers can process transactions simultaneously without race conditions.

**Implementation:**
- `SELECT FOR UPDATE` locks accounts during transaction processing
- Optimistic locking with version numbers detects concurrent modifications
- Balance checks only apply to ASSET/EXPENSE accounts (revenue/liability can be negative)

### 3. Append-Only Architecture

Entries are immutable. Corrections create reversal entries.

**Benefits:**
- Complete audit trail
- Balances always verifiable by summing lines
- No data loss from accidental deletes
- Simplified conflict resolution

**Implementation:**
- No DELETE or UPDATE on entries/lines
- Reversals reference original entry
- Each reversal creates opposite-sign lines

### 4. Double-Entry Bookkeeping

Every transaction must balance (debits = credits).

**Rules:**
- Sum of all line amounts must equal zero
- Enforced at application layer before commit
- Provides mathematical certainty of correctness

### 5. Operational Semantics

The system handles real-world operational challenges:
- Webhook delivery with exponential backoff
- Worker integration patterns
- Graceful degradation
- Observable behavior (logging, metrics)

## Data Model

### Account

Represents a ledger account (asset, liability, equity, revenue, expense).

**Key Fields:**
- `balance`: Cached balance for performance
- `version`: Optimistic lock counter
- `type`: Account category (affects balance validation)

**Invariants:**
- Balance always equals sum of related line amounts
- ASSET/EXPENSE accounts cannot go negative (without explicit override)

### Entry

Represents a transaction (group of balanced lines).

**Key Fields:**
- `idempotency_key`: Unique identifier for deduplication
- `reverses_entry_id`: Links reversals to originals
- `meta`: Extensible JSON metadata

**Invariants:**
- Immutable once created
- Lines must sum to zero
- Idempotency key is unique

### Line

Individual debit/credit within an entry.

**Key Fields:**
- `amount`: Positive for debit, negative for credit (by convention)
- `account_id`: Which account is affected

**Invariants:**
- Amount cannot be zero
- Immutable once created
- Always part of exactly one entry

### WebhookDelivery

Tracks webhook delivery attempts.

**Key Fields:**
- `status`: pending, delivered, failed
- `attempts`: Current attempt count
- `next_retry_at`: When to retry next
- `max_attempts`: Give up after this many tries

**Retry Logic:**
- Exponential backoff: 2^(attempt-1) minutes
- Capped at 240 minutes (4 hours)
- Permanently fails after max_attempts

## Transaction Flow

### Creating an Entry

1. **Validate**: Check entry balances (sum = 0)
2. **Check Idempotency**: Return existing if key exists
3. **Lock Accounts**: `SELECT FOR UPDATE` on all involved accounts
4. **Create Entry**: Insert entry record
5. **Create Lines**: Insert line records
6. **Update Balances**: Increment account balances
7. **Increment Versions**: Bump account version numbers
8. **Commit**: Atomic commit of all changes

### Handling Retries

```python
# First attempt
entry1 = ledger.create_entry(
    idempotency_key="payment-123",
    ...
)

# Retry (network timeout, etc)
entry2 = ledger.create_entry(
    idempotency_key="payment-123",  # Same key!
    ...
)

assert entry1.id == entry2.id  # Same entry returned
```

### Creating Reversals

```python
# Original
entry = ledger.create_entry(
    idempotency_key="sale-456",
    lines=[
        {"account": "cash", "amount": Decimal("100")},
        {"account": "revenue", "amount": Decimal("-100")},
    ]
)

# Reversal
reversal = ledger.reverse_entry(
    original_idempotency_key="sale-456",
    reversal_idempotency_key="refund-456",
)
# Creates entry with opposite signs:
# cash: -100, revenue: +100
```

## Performance Considerations

### Cached Balances

Accounts store cached balances for fast queries. These are:
- Updated transactionally with each entry
- Always verifiable by summing lines
- Protected by optimistic locking

### Indexes

Strategic indexes on:
- `entries.idempotency_key` (uniqueness + lookup)
- `lines.account_id` (balance calculation)
- `lines.entry_id` (entry detail retrieval)
- `webhook_deliveries.status` (pending webhook queries)
- `webhook_deliveries.next_retry_at` (retry scheduling)

### Connection Pooling

Use SQLAlchemy's connection pooling:

```python
ledger = Ledger(
    database_url,
    pool_size=20,
    max_overflow=0,
    pool_pre_ping=True,
)
```

## Webhook System

### Delivery Flow

1. Create webhook with idempotency key
2. Set `next_retry_at` to current time (immediate delivery)
3. Worker picks up pending webhooks
4. Attempt delivery via HTTP POST
5. On success: Mark delivered
6. On failure: Increment attempts, schedule retry
7. After max_attempts: Mark permanently failed

### Retry Schedule

| Attempt | Backoff |
|---------|---------|
| 1       | 1 min   |
| 2       | 2 min   |
| 3       | 4 min   |
| 4       | 8 min   |
| 5       | 16 min  |
| ...     | capped at 240 min |

### Worker Pattern

```python
webhook_mgr = WebhookManager(session_factory)

while True:
    processed = webhook_mgr.process_pending_webhooks(
        deliver_func=http_post,
        limit=100
    )
    if processed == 0:
        time.sleep(5)
```

## Error Handling

### InsufficientFundsError

Raised when ASSET/EXPENSE account would go negative.

**Recovery:**
- Return error to user
- No partial state (transaction rolled back)
- Retry safe due to idempotency

### ImbalancedEntryError

Raised when entry lines don't sum to zero.

**Prevention:**
- Validate in application code
- Double-check in tests
- Use helper functions for complex entries

### StaleDataError

Raised on optimistic lock failure (concurrent modification).

**Recovery:**
- Automatic retry usually succeeds
- Indicates high contention on specific account
- Consider sharding or rate limiting

## Security Considerations

### SQL Injection

- All queries use parameterized statements
- SQLAlchemy ORM provides protection
- No raw SQL with string interpolation

### Access Control

LedgerKit is a library, not a service. Implementing application must:
- Authenticate users
- Authorize account access
- Validate transaction permissions
- Audit access logs

### Data Validation

- Amounts validated as Decimal (no floating point errors)
- Account names validated (existence check)
- Entry balances validated (sum = 0)
- Idempotency keys validated (uniqueness)

## Testing Strategy

### Unit Tests

Test individual components in isolation:
- Account creation
- Entry validation
- Balance calculation

### Integration Tests

Test component interactions:
- Entry creation with balance updates
- Reversal creation
- Webhook delivery

### Concurrency Tests

Test race conditions:
- Concurrent entries with same idempotency key
- Concurrent balance updates
- Optimistic locking

### Property Tests

Verify invariants:
- Balance equals sum of lines
- All entries balance
- Reversals properly undo originals

## Production Deployment

### Database Setup

1. Use PostgreSQL 12+
2. Enable connection pooling
3. Set appropriate pool sizes
4. Monitor connection usage

### Migration Strategy

1. Use migration tool (Alembic, etc)
2. Apply schema from `migrations/001_initial_schema.py`
3. Create indexes before loading data
4. Monitor migration performance

### Monitoring

Key metrics to track:
- Entry creation rate
- Entry creation latency
- Balance query latency
- Webhook delivery success rate
- Webhook retry queue depth
- Database connection pool usage

### Backup and Recovery

- Regular PostgreSQL backups
- Point-in-time recovery enabled
- Test recovery procedures
- Document recovery SLAs

## Future Enhancements

Potential additions (not in scope):

- Multi-currency support
- Scheduled transactions
- Transaction approval workflows
- Reconciliation tools
- Reporting and analytics
- GraphQL API
- Event sourcing integration
