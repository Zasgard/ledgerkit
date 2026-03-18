"""
FastAPI Integration Example

This example shows how to integrate LedgerKit with a FastAPI application
to build a payment API with proper idempotency handling.
"""

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from decimal import Decimal
from typing import Optional
import uvicorn

from ledgerkit import Ledger, AccountType, InsufficientFundsError

# Initialize FastAPI app
app = FastAPI(title="Payment API with LedgerKit")

# Initialize ledger (use PostgreSQL in production with connection pooling)
ledger = Ledger(
    "sqlite:///payments.db",
    pool_size=20,
    max_overflow=0,
)

# Create tables on startup
@app.on_event("startup")
async def startup():
    ledger.init_db()
    # Create some demo accounts
    try:
        ledger.create_account("platform_wallet", AccountType.ASSET)
        ledger.create_account("revenue", AccountType.REVENUE)
    except:
        pass  # Accounts already exist


# Request/Response models
class CreateAccountRequest(BaseModel):
    user_id: str
    initial_balance: Optional[Decimal] = Decimal("0.00")


class PaymentRequest(BaseModel):
    from_user_id: str
    to_user_id: str
    amount: Decimal
    description: str


class PaymentResponse(BaseModel):
    entry_id: int
    status: str
    from_balance: Decimal
    to_balance: Decimal


class BalanceResponse(BaseModel):
    user_id: str
    balance: Decimal


# Endpoints
@app.post("/accounts", status_code=201)
async def create_user_account(request: CreateAccountRequest):
    """Create a new user wallet account."""
    account_name = f"user_{request.user_id}"
    
    try:
        account = ledger.create_account(account_name, AccountType.ASSET)
        
        # Add initial balance if specified
        if request.initial_balance > 0:
            ledger.create_entry(
                idempotency_key=f"initial-balance-{request.user_id}",
                description=f"Initial balance for user {request.user_id}",
                lines=[
                    {"account": account_name, "amount": request.initial_balance},
                    {"account": "platform_wallet", "amount": -request.initial_balance},
                ],
                allow_negative_balance=True,
            )
        
        return {
            "user_id": request.user_id,
            "account_name": account.name,
            "balance": ledger.get_balance(account_name),
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/payments", response_model=PaymentResponse)
async def create_payment(
    request: PaymentRequest,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    """
    Create a payment from one user to another.
    
    This endpoint is idempotent - send the same Idempotency-Key header
    to safely retry the request without creating duplicate payments.
    """
    from_account = f"user_{request.from_user_id}"
    to_account = f"user_{request.to_user_id}"
    
    # Validate amount
    if request.amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    
    try:
        # Create the entry (idempotent)
        entry = ledger.create_entry(
            idempotency_key=idempotency_key,
            description=request.description,
            lines=[
                {"account": from_account, "amount": -request.amount},
                {"account": to_account, "amount": request.amount},
            ],
        )
        
        # Return success with current balances
        return PaymentResponse(
            entry_id=entry.id,
            status="completed",
            from_balance=ledger.get_balance(from_account),
            to_balance=ledger.get_balance(to_account),
        )
        
    except InsufficientFundsError:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient funds in account {from_account}",
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/payments/{entry_id}/refund")
async def refund_payment(
    entry_id: int,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
):
    """
    Refund a payment by creating a reversal entry.
    
    This is also idempotent via the Idempotency-Key header.
    """
    try:
        # Note: In a real app, you'd look up the original entry by entry_id
        # and get its idempotency_key from the database
        original_key = f"payment-{entry_id}"  # Simplified for example
        
        reversal = ledger.reverse_entry(
            original_idempotency_key=original_key,
            reversal_idempotency_key=idempotency_key,
            description=f"Refund for entry {entry_id}",
        )
        
        return {
            "reversal_entry_id": reversal.id,
            "status": "refunded",
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/accounts/{user_id}/balance", response_model=BalanceResponse)
async def get_balance(user_id: str):
    """Get current balance for a user account."""
    account_name = f"user_{user_id}"
    
    try:
        balance = ledger.get_balance(account_name)
        return BalanceResponse(user_id=user_id, balance=balance)
    except Exception as e:
        raise HTTPException(status_code=404, detail="Account not found")


@app.get("/health")
async def health_check():
    """Health check endpoint."""
    return {"status": "healthy", "service": "payment-api"}


if __name__ == "__main__":
    # Run the server
    print("Starting Payment API server...")
    print("Try these commands:")
    print("  curl -X POST http://localhost:8000/accounts -H 'Content-Type: application/json' -d '{\"user_id\": \"alice\", \"initial_balance\": \"1000.00\"}'")
    print("  curl -X POST http://localhost:8000/accounts -H 'Content-Type: application/json' -d '{\"user_id\": \"bob\", \"initial_balance\": \"500.00\"}'")
    print("  curl -X POST http://localhost:8000/payments -H 'Content-Type: application/json' -H 'Idempotency-Key: payment-001' -d '{\"from_user_id\": \"alice\", \"to_user_id\": \"bob\", \"amount\": \"100.00\", \"description\": \"Payment for services\"}'")
    print("  curl http://localhost:8000/accounts/alice/balance")
    
    uvicorn.run(app, host="0.0.0.0", port=8000)
