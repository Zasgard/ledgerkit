"""
Basic Usage Example for LedgerKit

This example demonstrates the fundamental operations of the ledger:
- Creating accounts
- Creating entries (transactions)
- Handling idempotency
- Reversing entries
- Checking balances
"""

from decimal import Decimal
from ledgerkit import Ledger, AccountType

# Initialize the ledger (use PostgreSQL in production)
ledger = Ledger("sqlite:///example.db")
ledger.init_db()

print("=== LedgerKit Basic Usage Example ===\n")

# Step 1: Create accounts
print("1. Creating accounts...")
ledger.create_account("user_wallet", AccountType.ASSET)
ledger.create_account("merchant_account", AccountType.LIABILITY)
ledger.create_account("platform_revenue", AccountType.REVENUE)
print("   ✓ Created: user_wallet, merchant_account, platform_revenue\n")

# Step 2: Create an initial balance for user
print("2. Adding initial balance to user wallet...")
ledger.create_account("cash_on_hand", AccountType.ASSET)
entry1 = ledger.create_entry(
    idempotency_key="initial-deposit-001",
    description="Initial deposit from bank",
    lines=[
        {"account": "user_wallet", "amount": Decimal("1000.00")},
        {"account": "cash_on_hand", "amount": Decimal("-1000.00")},
    ],
    allow_negative_balance=True,  # Allow cash_on_hand to go negative
)
print(f"   ✓ Entry {entry1.id} created: $1000.00 deposited")
print(f"   User wallet balance: ${ledger.get_balance('user_wallet')}\n")

# Step 3: Create a purchase
# In this transaction: buyer pays, seller receives payment minus platform fee
print("3. Processing a purchase...")
# Create seller account
ledger.create_account("seller_wallet", AccountType.ASSET)

entry2 = ledger.create_entry(
    idempotency_key="purchase-12345",
    description="Purchase of product #456",
    lines=[
        {"account": "user_wallet", "amount": Decimal("-100.00")},     # Buyer pays $100
        {"account": "seller_wallet", "amount": Decimal("95.00")},     # Seller receives $95
        {"account": "platform_revenue", "amount": Decimal("-5.00")},  # Platform fee $5 (revenue)
        {"account": "merchant_account", "amount": Decimal("10.00")},  # Balance the entry
    ],
)
print(f"   ✓ Entry {entry2.id} created")
print(f"   User wallet: ${ledger.get_balance('user_wallet')}")
print(f"   Seller wallet: ${ledger.get_balance('seller_wallet')}")
print(f"   Platform revenue: ${ledger.get_balance('platform_revenue')}\n")

# Step 4: Demonstrate idempotency (retry the same purchase)
print("4. Testing idempotency (simulating a retry)...")
entry2_retry = ledger.create_entry(
    idempotency_key="purchase-12345",  # Same key!
    description="Purchase retry attempt",
    lines=[
        {"account": "user_wallet", "amount": Decimal("-100.00")},
        {"account": "seller_wallet", "amount": Decimal("95.00")},
        {"account": "platform_revenue", "amount": Decimal("-5.00")},
        {"account": "merchant_account", "amount": Decimal("10.00")},
    ],
)
print(f"   ✓ Same entry returned: {entry2.id == entry2_retry.id}")
print(f"   User NOT double-charged: ${ledger.get_balance('user_wallet')}\n")

# Step 5: Process a refund (reversal)
print("5. Processing a refund (reversal entry)...")
reversal = ledger.reverse_entry(
    original_idempotency_key="purchase-12345",
    reversal_idempotency_key="refund-12345",
    description="Refund for product #456",
)
print(f"   ✓ Reversal entry {reversal.id} created")
print(f"   User wallet (refunded): ${ledger.get_balance('user_wallet')}")
print(f"   Seller wallet: ${ledger.get_balance('seller_wallet')}")
print(f"   Platform revenue: ${ledger.get_balance('platform_revenue')}\n")

# Step 6: Verify balance integrity
print("6. Verifying balance integrity...")
for account_name in ["user_wallet", "seller_wallet", "platform_revenue"]:
    is_valid = ledger.verify_balance(account_name)
    status = "✓" if is_valid else "✗"
    print(f"   {status} {account_name}: valid={is_valid}")

print("\n=== Example Complete ===")
