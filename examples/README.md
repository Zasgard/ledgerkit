# Examples

This directory contains example code demonstrating various uses of LedgerKit.

## Files

- **basic_usage.py** - Basic ledger operations: creating accounts, entries, reversals
- **fastapi_integration.py** - Complete FastAPI payment API with idempotency
- **webhook_worker.py** - Background worker for processing webhook deliveries

## Running the Examples

### Basic Usage

```bash
python examples/basic_usage.py
```

### FastAPI Integration

First install FastAPI and uvicorn:

```bash
pip install fastapi uvicorn pydantic
```

Then run the server:

```bash
python examples/fastapi_integration.py
```

Test with curl:

```bash
# Create accounts
curl -X POST http://localhost:8000/accounts \
  -H 'Content-Type: application/json' \
  -d '{"user_id": "alice", "initial_balance": "1000.00"}'

# Make a payment (idempotent)
curl -X POST http://localhost:8000/payments \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: payment-001' \
  -d '{"from_user_id": "alice", "to_user_id": "bob", "amount": "50.00", "description": "Test payment"}'

# Check balance
curl http://localhost:8000/accounts/alice/balance
```

### Webhook Worker

Update the DATABASE_URL in the file, then run:

```bash
python examples/webhook_worker.py
```

This worker will continuously poll for pending webhooks and deliver them with automatic retries.
