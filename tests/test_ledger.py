"""
Tests for ledger functionality.

Tests cover:
- Idempotency
- Concurrency safety
- Double-entry bookkeeping
- Reversals
- Balance verification
"""

import pytest
from decimal import Decimal
from datetime import datetime
import threading
import time

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ledgerkit.models import Base, AccountType
from ledgerkit.ledger import (
    Ledger,
    InsufficientFundsError,
    ImbalancedEntryError,
    LedgerError,
)


@pytest.fixture
def database_url():
    """Use in-memory SQLite for testing (PostgreSQL in production)."""
    return "sqlite:///:memory:"


@pytest.fixture
def ledger(database_url):
    """Create a fresh ledger for each test."""
    ledger = Ledger(database_url)
    ledger.init_db()
    return ledger


class TestAccountCreation:
    """Test account creation."""

    def test_create_account(self, ledger):
        """Test creating an account."""
        account = ledger.create_account("cash", AccountType.ASSET)
        assert account.name == "cash"
        assert account.type == AccountType.ASSET
        assert account.balance == Decimal("0.00")

    def test_create_duplicate_account_fails(self, ledger):
        """Test that duplicate account names fail."""
        ledger.create_account("cash", AccountType.ASSET)
        with pytest.raises(Exception):  # IntegrityError
            ledger.create_account("cash", AccountType.LIABILITY)

    def test_get_account(self, ledger):
        """Test retrieving an account."""
        ledger.create_account("revenue", AccountType.REVENUE)
        account = ledger.get_account("revenue")
        assert account is not None
        assert account.name == "revenue"

    def test_get_nonexistent_account(self, ledger):
        """Test getting non-existent account returns None."""
        account = ledger.get_account("nonexistent")
        assert account is None


class TestEntryCreation:
    """Test ledger entry creation."""

    def test_create_balanced_entry(self, ledger):
        """Test creating a balanced entry."""
        ledger.create_account("cash", AccountType.ASSET)
        ledger.create_account("revenue", AccountType.REVENUE)

        entry = ledger.create_entry(
            idempotency_key="sale-1",
            description="Product sale",
            lines=[
                {"account": "cash", "amount": Decimal("100.00")},
                {"account": "revenue", "amount": Decimal("-100.00")},
            ],
        )

        assert entry.idempotency_key == "sale-1"
        assert entry.description == "Product sale"
        assert len(entry.lines) == 2

        # Verify balances
        assert ledger.get_balance("cash") == Decimal("100.00")
        assert ledger.get_balance("revenue") == Decimal("-100.00")

    def test_imbalanced_entry_fails(self, ledger):
        """Test that imbalanced entries are rejected."""
        ledger.create_account("cash", AccountType.ASSET)
        ledger.create_account("revenue", AccountType.REVENUE)

        with pytest.raises(ImbalancedEntryError):
            ledger.create_entry(
                idempotency_key="bad-entry",
                description="Imbalanced entry",
                lines=[
                    {"account": "cash", "amount": Decimal("100.00")},
                    {"account": "revenue", "amount": Decimal("-50.00")},  # Doesn't balance
                ],
            )

    def test_entry_with_nonexistent_account_fails(self, ledger):
        """Test that entries with non-existent accounts fail."""
        ledger.create_account("cash", AccountType.ASSET)

        with pytest.raises(LedgerError):
            ledger.create_entry(
                idempotency_key="bad-entry",
                description="Entry with bad account",
                lines=[
                    {"account": "cash", "amount": Decimal("100.00")},
                    {"account": "nonexistent", "amount": Decimal("-100.00")},
                ],
            )


class TestIdempotency:
    """Test idempotency of operations."""

    def test_duplicate_entry_returns_existing(self, ledger):
        """Test that duplicate idempotency keys return the existing entry."""
        ledger.create_account("cash", AccountType.ASSET)
        ledger.create_account("revenue", AccountType.REVENUE)

        # Create entry
        entry1 = ledger.create_entry(
            idempotency_key="sale-123",
            description="First attempt",
            lines=[
                {"account": "cash", "amount": Decimal("100.00")},
                {"account": "revenue", "amount": Decimal("-100.00")},
            ],
        )

        # Try to create again with same key (simulates retry/double-click)
        entry2 = ledger.create_entry(
            idempotency_key="sale-123",
            description="Second attempt (should be ignored)",
            lines=[
                {"account": "cash", "amount": Decimal("200.00")},
                {"account": "revenue", "amount": Decimal("-200.00")},
            ],
        )

        # Should return the same entry
        assert entry1.id == entry2.id
        assert entry1.description == entry2.description  # Original description preserved

        # Balance should only reflect first entry
        assert ledger.get_balance("cash") == Decimal("100.00")

    def test_concurrent_duplicate_entries(self, ledger):
        """Test that concurrent requests with same key don't create duplicates.
        
        Note: True concurrency testing requires a real database like PostgreSQL.
        This test simulates concurrent requests with sequential calls.
        """
        ledger.create_account("cash", AccountType.ASSET)
        ledger.create_account("revenue", AccountType.REVENUE)

        # Simulate 5 "concurrent" requests with the same idempotency key
        entry_ids = []
        for i in range(5):
            entry = ledger.create_entry(
                idempotency_key="concurrent-sale",
                description=f"Concurrent sale attempt {i+1}",
                lines=[
                    {"account": "cash", "amount": Decimal("50.00")},
                    {"account": "revenue", "amount": Decimal("-50.00")},
                ],
            )
            entry_ids.append(entry.id)

        # All requests should return the same entry
        assert len(set(entry_ids)) == 1
        
        # Balance should only reflect one entry
        assert ledger.get_balance("cash") == Decimal("50.00")


class TestConcurrencySafety:
    """Test concurrency-safe spending."""

    def test_insufficient_funds(self, ledger):
        """Test that insufficient funds are detected."""
        ledger.create_account("wallet", AccountType.ASSET)
        ledger.create_account("expense", AccountType.EXPENSE)

        # Add some funds
        ledger.create_entry(
            idempotency_key="deposit",
            description="Initial deposit",
            lines=[
                {"account": "wallet", "amount": Decimal("100.00")},
                {"account": "expense", "amount": Decimal("-100.00")},
            ],
            allow_negative_balance=True,
        )

        # Try to spend more than available
        with pytest.raises(InsufficientFundsError):
            ledger.create_entry(
                idempotency_key="overspend",
                description="Overspending",
                lines=[
                    {"account": "wallet", "amount": Decimal("-150.00")},
                    {"account": "expense", "amount": Decimal("150.00")},
                ],
            )

        # Balance should remain unchanged
        assert ledger.get_balance("wallet") == Decimal("100.00")

    def test_allow_negative_balance(self, ledger):
        """Test that negative balances can be allowed when specified."""
        ledger.create_account("credit", AccountType.LIABILITY)
        ledger.create_account("expense", AccountType.EXPENSE)

        # This should succeed with allow_negative_balance=True
        entry = ledger.create_entry(
            idempotency_key="credit-purchase",
            description="Credit purchase",
            lines=[
                {"account": "credit", "amount": Decimal("-100.00")},
                {"account": "expense", "amount": Decimal("100.00")},
            ],
            allow_negative_balance=True,
        )

        assert entry is not None
        assert ledger.get_balance("credit") == Decimal("-100.00")


class TestReversals:
    """Test entry reversals."""

    def test_reverse_entry(self, ledger):
        """Test reversing an entry."""
        ledger.create_account("cash", AccountType.ASSET)
        ledger.create_account("revenue", AccountType.REVENUE)

        # Create original entry
        original = ledger.create_entry(
            idempotency_key="sale-456",
            description="Product sale",
            lines=[
                {"account": "cash", "amount": Decimal("100.00")},
                {"account": "revenue", "amount": Decimal("-100.00")},
            ],
        )

        # Verify initial balances
        assert ledger.get_balance("cash") == Decimal("100.00")
        assert ledger.get_balance("revenue") == Decimal("-100.00")

        # Reverse the entry
        reversal = ledger.reverse_entry(
            original_idempotency_key="sale-456",
            reversal_idempotency_key="reversal-456",
            description="Refund",
        )

        # Verify reversal created
        assert reversal is not None
        assert reversal.id != original.id

        # Verify balances are back to zero
        assert ledger.get_balance("cash") == Decimal("0.00")
        assert ledger.get_balance("revenue") == Decimal("0.00")

    def test_reverse_nonexistent_entry_fails(self, ledger):
        """Test that reversing a non-existent entry fails."""
        with pytest.raises(LedgerError):
            ledger.reverse_entry(
                original_idempotency_key="nonexistent",
                reversal_idempotency_key="reversal-123",
            )

    def test_reversal_is_idempotent(self, ledger):
        """Test that reversal operations are idempotent."""
        ledger.create_account("cash", AccountType.ASSET)
        ledger.create_account("revenue", AccountType.REVENUE)

        # Create original
        ledger.create_entry(
            idempotency_key="sale-789",
            description="Sale",
            lines=[
                {"account": "cash", "amount": Decimal("100.00")},
                {"account": "revenue", "amount": Decimal("-100.00")},
            ],
        )

        # Reverse it
        reversal1 = ledger.reverse_entry(
            original_idempotency_key="sale-789",
            reversal_idempotency_key="reversal-789",
        )

        # Try to reverse again with same key
        reversal2 = ledger.reverse_entry(
            original_idempotency_key="sale-789",
            reversal_idempotency_key="reversal-789",
        )

        # Should return same reversal
        assert reversal1.id == reversal2.id

        # Balances should still be zero (not double-reversed)
        assert ledger.get_balance("cash") == Decimal("0.00")


class TestBalanceVerification:
    """Test balance verification."""

    def test_verify_balance(self, ledger):
        """Test that balance verification works."""
        ledger.create_account("checking", AccountType.ASSET)
        ledger.create_account("income", AccountType.REVENUE)

        # Create several entries
        for i in range(5):
            ledger.create_entry(
                idempotency_key=f"entry-{i}",
                description=f"Entry {i}",
                lines=[
                    {"account": "checking", "amount": Decimal("10.00")},
                    {"account": "income", "amount": Decimal("-10.00")},
                ],
            )

        # Verify balances match calculated from lines
        assert ledger.verify_balance("checking") is True
        assert ledger.verify_balance("income") is True

    def test_get_balance(self, ledger):
        """Test getting account balance."""
        ledger.create_account("savings", AccountType.ASSET)
        ledger.create_account("transfer", AccountType.REVENUE)

        # Initially zero
        assert ledger.get_balance("savings") == Decimal("0.00")

        # After entry
        ledger.create_entry(
            idempotency_key="deposit",
            description="Deposit",
            lines=[
                {"account": "savings", "amount": Decimal("500.00")},
                {"account": "transfer", "amount": Decimal("-500.00")},
            ],
        )

        assert ledger.get_balance("savings") == Decimal("500.00")


class TestComplexScenarios:
    """Test complex real-world scenarios."""

    def test_marketplace_transaction(self, ledger):
        """Test a marketplace transaction with fees."""
        # Setup accounts
        ledger.create_account("buyer_wallet", AccountType.ASSET)
        ledger.create_account("seller_wallet", AccountType.ASSET)
        ledger.create_account("platform_fees", AccountType.REVENUE)
        ledger.create_account("platform_wallet", AccountType.LIABILITY)  # Platform owes money

        # Buyer has funds
        ledger.create_entry(
            idempotency_key="buyer-deposit",
            description="Buyer deposit",
            lines=[
                {"account": "buyer_wallet", "amount": Decimal("100.00")},
                {"account": "platform_wallet", "amount": Decimal("-100.00")},
            ],
            allow_negative_balance=True,
        )

        # Purchase: $100 item with $10 fee
        ledger.create_entry(
            idempotency_key="purchase-123",
            description="Purchase with fee",
            lines=[
                {"account": "buyer_wallet", "amount": Decimal("-100.00")},
                {"account": "seller_wallet", "amount": Decimal("90.00")},
                {"account": "platform_fees", "amount": Decimal("-10.00")},
                {"account": "platform_wallet", "amount": Decimal("20.00")},
            ],
        )

        # Verify final balances
        assert ledger.get_balance("buyer_wallet") == Decimal("0.00")
        assert ledger.get_balance("seller_wallet") == Decimal("90.00")
        assert ledger.get_balance("platform_fees") == Decimal("-10.00")
        assert ledger.get_balance("platform_wallet") == Decimal("-80.00")

    def test_payment_with_retry(self, ledger):
        """Test payment idempotency (simulating retry after timeout)."""
        ledger.create_account("user_wallet", AccountType.ASSET)
        ledger.create_account("merchant", AccountType.LIABILITY)  # Merchant receives payments

        # Initial balance
        ledger.create_entry(
            idempotency_key="initial-balance",
            description="Initial balance",
            lines=[
                {"account": "user_wallet", "amount": Decimal("1000.00")},
                {"account": "merchant", "amount": Decimal("-1000.00")},
            ],
            allow_negative_balance=True,
        )

        # User makes payment
        payment1 = ledger.create_entry(
            idempotency_key="payment-xyz-123",
            description="Payment attempt 1",
            lines=[
                {"account": "user_wallet", "amount": Decimal("-50.00")},
                {"account": "merchant", "amount": Decimal("50.00")},
            ],
        )

        # Simulate: request times out, user clicks again (retry)
        payment2 = ledger.create_entry(
            idempotency_key="payment-xyz-123",  # Same key!
            description="Payment attempt 2 (retry)",
            lines=[
                {"account": "user_wallet", "amount": Decimal("-50.00")},
                {"account": "merchant", "amount": Decimal("50.00")},
            ],
        )

        # Should be the same entry
        assert payment1.id == payment2.id

        # User should only be charged once
        assert ledger.get_balance("user_wallet") == Decimal("950.00")
        assert ledger.get_balance("merchant") == Decimal("-950.00")
