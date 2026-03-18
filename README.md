# LedgerKit

A PostgreSQL-backed, double-entry ledger core for building safe, auditable wallet and marketplace systems.

## Features

✅ **Idempotency Everywhere** - All operations use idempotency keys to safely handle retries, webhooks, and double-clicks  
✅ **Concurrency-Safe Spending** - Optimistic locking prevents race conditions on account balances  
✅ **Append-Only + Reversals** - Immutable ledger entries with proper reversal transactions for audit trails  
✅ **Webhook Retries** - Built-in webhook delivery system with exponential backoff  
✅ **Framework-Agnostic** - Pure Python library that works with FastAPI, Flask, Django, or any framework  
✅ **Performance** - Cached balances with database-level constraints for speed and correctness  

## Installation

```bash
pip install ledgerkit
```

Or with development dependencies:

```bash
pip install ledgerkit[dev]
```

## Quick Start

### 1. Initialize the Ledger

```python
from ledgerkit import Ledger, AccountType
from decimal import Decimal

# Connect to PostgreSQL
ledger = Ledger("postgresql://user:pass@localhost/mydb")
ledger.init_db()  # Create tables

# Create accounts
ledger.create_account("user_wallet", AccountType.ASSET)
ledger.create_account("merchant_wallet", AccountType.ASSET)
ledger.create_account("platform_revenue", AccountType.REVENUE)
```

### 2. Create Entries (Idempotent)

```python
# Process a payment - idempotent by design
entry = ledger.create_entry(
    idempotency_key="payment-abc-123",  # Unique key prevents duplicates
    description="Purchase of product #456",
    lines=[
        {"account": "user_wallet", "amount": Decimal("-100.00")},
        {"account": "merchant_wallet", "amount": Decimal("95.00")},
        {"account": "platform_revenue", "amount": Decimal("-5.00")},
    ]
)

# Retry with same key returns existing entry - no duplicate charge!
retry = ledger.create_entry(
    idempotency_key="payment-abc-123",  # Same key
    description="Purchase of product #456",
    lines=[
        {"account": "user_wallet", "amount": Decimal("-100.00")},
        {"account": "merchant_wallet", "amount": Decimal("95.00")},
        {"account": "platform_revenue", "amount": Decimal("-5.00")},
    ]
)
assert entry.id == retry.id  # True - same entry returned
```

### 3. Handle Refunds with Reversals

```python
# Reverse an entry (append-only, creates new entry)
reversal = ledger.reverse_entry(
    original_idempotency_key="payment-abc-123",
    reversal_idempotency_key="refund-abc-123",
    description="Refund for product #456"
)

# Check balances
print(ledger.get_balance("user_wallet"))  # Back to original
```

### 4. Concurrency-Safe Operations

```python
# Multiple workers trying to spend from same account
# Only one will succeed if insufficient funds
try:
    ledger.create_entry(
        idempotency_key="spend-xyz-789",
        description="Purchase",
        lines=[
            {"account": "user_wallet", "amount": Decimal("-50.00")},
            {"account": "merchant_wallet", "amount": Decimal("50.00")},
        ]
    )
except InsufficientFundsError:
    print("Not enough funds!")
```

### 5. Webhook Delivery with Retries

```python
from ledgerkit.webhooks import WebhookManager

# Initialize webhook manager
webhook_mgr = WebhookManager(ledger.Session)

# Create webhook (idempotent)
webhook = webhook_mgr.create_webhook(
    idempotency_key="webhook-payment-abc-123",
    entry_id=entry.id,
    url="https://merchant.example.com/webhooks/payment",
    payload={
        "event": "payment.completed",
        "amount": "100.00",
        "entry_id": entry.id,
    }
)

# In your worker process, deliver pending webhooks
import requests

def deliver_webhook(url, payload):
    response = requests.post(url, json=payload)
    return (response.status_code, response.text)

# Process webhooks with automatic retries
processed = webhook_mgr.process_pending_webhooks(deliver_webhook)
print(f"Processed {processed} webhooks")
```

## Core Concepts

### Double-Entry Bookkeeping

Every entry must balance (debits = credits). Amounts are signed:
- Positive amounts represent debits
- Negative amounts represent credits

For asset accounts like wallets:
- Positive = money coming in
- Negative = money going out

### Idempotency

All operations that modify state accept an `idempotency_key`. Calling the same operation multiple times with the same key is safe - it returns the existing result instead of creating duplicates.

This handles:
- **Payment webhook retries** - If webhook times out, retry won't double-charge
- **User double-clicks** - Multiple form submissions don't create duplicate transactions
- **Queue worker retries** - Workers can safely retry failed jobs

### Concurrency Safety

The ledger uses optimistic locking (version numbers on accounts) to detect concurrent modifications. Combined with PostgreSQL's transaction isolation, this prevents:
- Race conditions on balance checks
- Lost updates from concurrent transactions
- Double-spending

### Append-Only Architecture

Entries are never deleted or modified. Instead:
- Corrections create new reversal entries
- Audit trail is complete and immutable
- Balances can always be verified by summing all lines

## Database Schema

The ledger uses these tables:

- **accounts** - Account definitions with cached balances and version numbers
- **entries** - Ledger entries (transactions) with idempotency keys
- **lines** - Individual line items within entries
- **webhook_deliveries** - Webhook delivery tracking with retry state

All tables have proper indexes for performance and constraints for correctness.

## Testing

Run the test suite:

```bash
pip install -e ".[dev]"
pytest
```

Run with coverage:

```bash
pytest --cov=ledgerkit --cov-report=html
```

## Production Deployment

### Database Setup

Use PostgreSQL 12+ in production:

```python
ledger = Ledger(
    "postgresql://user:pass@localhost/ledger_prod",
    pool_size=20,
    max_overflow=0,
    pool_pre_ping=True,
)
```

### Worker Setup

Run webhook workers as separate processes:

```python
# worker.py
import time
from ledgerkit.webhooks import WebhookManager
from sqlalchemy.orm import sessionmaker
from sqlalchemy import create_engine

engine = create_engine("postgresql://...")
Session = sessionmaker(bind=engine)
webhook_mgr = WebhookManager(Session)

def deliver(url, payload):
    import requests
    resp = requests.post(url, json=payload, timeout=10)
    return (resp.status_code, resp.text)

while True:
    processed = webhook_mgr.process_pending_webhooks(deliver, limit=100)
    if processed == 0:
        time.sleep(5)
```

### Integration Example (FastAPI)

```python
from fastapi import FastAPI, HTTPException
from ledgerkit import Ledger, InsufficientFundsError
from decimal import Decimal

app = FastAPI()
ledger = Ledger("postgresql://...")

@app.post("/payments")
async def create_payment(
    user_id: str,
    amount: str,
    idempotency_key: str,
):
    try:
        entry = ledger.create_entry(
            idempotency_key=idempotency_key,
            description=f"Payment from user {user_id}",
            lines=[
                {"account": f"user_{user_id}_wallet", "amount": Decimal(f"-{amount}")},
                {"account": "merchant_wallet", "amount": Decimal(amount)},
            ]
        )
        return {"entry_id": entry.id, "status": "completed"}
    except InsufficientFundsError:
        raise HTTPException(status_code=400, detail="Insufficient funds")
```

## License

MIT License - see LICENSE file for details.

## Contributing

Contributions welcome! Please open an issue or PR on GitHub.
