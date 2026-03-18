"""
Core ledger implementation with double-entry bookkeeping.

Features:
- Idempotent operations via idempotency keys
- Concurrency-safe with optimistic locking
- Append-only with reversals
- Automatic balance validation
"""

from typing import List, Dict, Optional, Any
from decimal import Decimal
from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.exc import IntegrityError

from ledgerkit.models import Account, Entry, Line, AccountType, Base


class LedgerError(Exception):
    """Base exception for ledger errors."""
    pass


class InsufficientFundsError(LedgerError):
    """Raised when an account doesn't have sufficient balance."""
    pass


class DuplicateEntryError(LedgerError):
    """Raised when an entry with the same idempotency key already exists."""
    pass


class ImbalancedEntryError(LedgerError):
    """Raised when entry debits don't equal credits."""
    pass


class StaleDataError(LedgerError):
    """Raised when optimistic locking detects concurrent modification."""
    pass


class Ledger:
    """
    Main ledger interface for double-entry bookkeeping.
    
    Usage:
        ledger = Ledger(database_url)
        ledger.create_account("cash", AccountType.ASSET)
        ledger.create_account("revenue", AccountType.REVENUE)
        
        # Create entry (idempotent)
        entry = ledger.create_entry(
            idempotency_key="sale-123",
            description="Sale of product",
            lines=[
                {"account": "cash", "amount": Decimal("100.00")},
                {"account": "revenue", "amount": Decimal("-100.00")},
            ]
        )
    """

    def __init__(self, database_url: str, **engine_kwargs: Any):
        """
        Initialize ledger with database connection.
        
        Args:
            database_url: PostgreSQL connection string
            **engine_kwargs: Additional arguments for SQLAlchemy engine
        """
        self.engine = create_engine(database_url, **engine_kwargs)
        self.Session = sessionmaker(bind=self.engine)

    def init_db(self) -> None:
        """Create all database tables."""
        Base.metadata.create_all(self.engine)

    def create_account(
        self,
        name: str,
        account_type: AccountType,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Account:
        """
        Create a new account.
        
        Args:
            name: Unique account name
            account_type: Type of account (asset, liability, etc.)
            meta: Optional metadata dictionary
            
        Returns:
            Created account
            
        Raises:
            IntegrityError: If account with same name exists
        """
        with self.Session() as session:
            account = Account(
                name=name,
                type=account_type,
                meta=meta,
                balance=Decimal("0.00"),
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            return account

    def get_account(self, name: str) -> Optional[Account]:
        """Get account by name."""
        with self.Session() as session:
            stmt = select(Account).where(Account.name == name)
            return session.scalar(stmt)

    def create_entry(
        self,
        idempotency_key: str,
        description: str,
        lines: List[Dict[str, Any]],
        meta: Optional[Dict[str, Any]] = None,
        allow_negative_balance: bool = False,
    ) -> Entry:
        """
        Create a new ledger entry (transaction).
        
        This operation is idempotent - calling with the same idempotency_key
        will return the existing entry without creating a duplicate.
        
        Args:
            idempotency_key: Unique key for this entry (e.g., "payment-123")
            description: Human-readable description
            lines: List of line items, each with:
                - account: Account name (str)
                - amount: Amount as Decimal (positive for debit, negative for credit)
                - meta: Optional metadata dict
            meta: Optional metadata for the entry
            allow_negative_balance: If False, raises error on insufficient funds
            
        Returns:
            Created or existing entry
            
        Raises:
            DuplicateEntryError: If idempotency key exists (with same entry)
            ImbalancedEntryError: If debits don't equal credits
            InsufficientFundsError: If balance would go negative (when not allowed)
            StaleDataError: If concurrent modification detected
        """
        with self.Session() as session:
            # Check for existing entry (idempotency)
            existing = session.scalar(
                select(Entry).where(Entry.idempotency_key == idempotency_key)
            )
            if existing:
                # Return existing entry - this is idempotent behavior
                return existing

            # Validate entry balances
            total = sum(Decimal(str(line["amount"])) for line in lines)
            if total != 0:
                raise ImbalancedEntryError(
                    f"Entry must balance (debits = credits). Current total: {total}"
                )

            # Create entry
            entry = Entry(
                idempotency_key=idempotency_key,
                description=description,
                meta=meta,
            )
            session.add(entry)
            session.flush()  # Get entry.id

            # Lock accounts and create lines
            account_map: Dict[str, Account] = {}
            account_deltas: Dict[int, Decimal] = {}

            for line_data in lines:
                account_name = line_data["account"]
                amount = Decimal(str(line_data["amount"]))

                # Get and lock account (SELECT FOR UPDATE)
                if account_name not in account_map:
                    account = session.scalar(
                        select(Account)
                        .where(Account.name == account_name)
                        .with_for_update()
                    )
                    if not account:
                        raise LedgerError(f"Account not found: {account_name}")
                    account_map[account_name] = account
                    account_deltas[account.id] = Decimal("0")

                account = account_map[account_name]

                # Create line
                line = Line(
                    entry_id=entry.id,
                    account_id=account.id,
                    amount=amount,
                    meta=line_data.get("meta"),
                )
                session.add(line)

                # Track balance changes
                account_deltas[account.id] += amount

            # Update account balances with optimistic locking
            for account_id, delta in account_deltas.items():
                account = account_map[
                    next(name for name, acc in account_map.items() if acc.id == account_id)
                ]
                
                new_balance = account.balance + delta
                
                # Check for negative balance if not allowed
                # Only enforce for asset and expense accounts (revenue/liability can be negative)
                if (
                    not allow_negative_balance 
                    and new_balance < 0
                    and account.type in (AccountType.ASSET, AccountType.EXPENSE)
                ):
                    raise InsufficientFundsError(
                        f"Insufficient funds in account '{account.name}'. "
                        f"Current: {account.balance}, Requested: {delta}, "
                        f"Would result in: {new_balance}"
                    )

                # Update with version check (optimistic locking)
                old_version = account.version
                account.balance = new_balance
                account.version = old_version + 1
                account.updated_at = datetime.utcnow()

                # Verify no concurrent modification (this happens on commit)
                session.flush()

            try:
                session.commit()
                session.refresh(entry)
                # Eagerly load lines to avoid detached instance issues
                _ = entry.lines  # Force load
                return entry
            except IntegrityError as e:
                session.rollback()
                # Check if this was due to duplicate idempotency key
                existing = session.scalar(
                    select(Entry).where(Entry.idempotency_key == idempotency_key)
                )
                if existing:
                    # Another concurrent request created this entry
                    return existing
                raise LedgerError(f"Database integrity error: {e}")

    def reverse_entry(
        self,
        original_idempotency_key: str,
        reversal_idempotency_key: str,
        description: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Entry:
        """
        Create a reversal entry for an existing entry.
        
        This creates a new entry that reverses all the lines of the original,
        effectively undoing the original transaction while maintaining the
        append-only audit trail.
        
        Args:
            original_idempotency_key: Idempotency key of entry to reverse
            reversal_idempotency_key: Unique key for the reversal entry
            description: Description for reversal (defaults to "Reversal of ...")
            meta: Optional metadata
            
        Returns:
            The reversal entry
            
        Raises:
            LedgerError: If original entry not found
        """
        with self.Session() as session:
            # Get original entry
            original = session.scalar(
                select(Entry).where(Entry.idempotency_key == original_idempotency_key)
            )
            if not original:
                raise LedgerError(f"Original entry not found: {original_idempotency_key}")

            # Check if reversal already exists (idempotency)
            existing_reversal = session.scalar(
                select(Entry).where(Entry.idempotency_key == reversal_idempotency_key)
            )
            if existing_reversal:
                return existing_reversal

            # Create reversal lines (opposite signs)
            reversal_lines = []
            for line in original.lines:
                account = session.get(Account, line.account_id)
                reversal_lines.append({
                    "account": account.name,
                    "amount": -line.amount,
                    "meta": {"reversal_of_line_id": line.id},
                })

            # Use create_entry for the reversal (ensures idempotency)
            if description is None:
                description = f"Reversal of: {original.description}"

            reversal_meta = meta or {}
            reversal_meta["reverses_entry_id"] = original.id

            # Create the reversal entry
            reversal = self.create_entry(
                idempotency_key=reversal_idempotency_key,
                description=description,
                lines=reversal_lines,
                meta=reversal_meta,
                allow_negative_balance=True,  # Reversals should always succeed
            )

            # Link the reversal to the original
            session.execute(
                select(Entry)
                .where(Entry.id == reversal.id)
            )
            reversal_obj = session.get(Entry, reversal.id)
            reversal_obj.reverses_entry_id = original.id
            session.commit()

            return reversal

    def get_balance(self, account_name: str) -> Decimal:
        """
        Get current balance of an account.
        
        Args:
            account_name: Name of the account
            
        Returns:
            Current balance
            
        Raises:
            LedgerError: If account not found
        """
        with self.Session() as session:
            account = session.scalar(
                select(Account).where(Account.name == account_name)
            )
            if not account:
                raise LedgerError(f"Account not found: {account_name}")
            return account.balance

    def verify_balance(self, account_name: str) -> bool:
        """
        Verify account balance by recalculating from lines.
        
        This is useful for detecting data corruption or bugs.
        
        Args:
            account_name: Name of the account
            
        Returns:
            True if balance matches, False otherwise
        """
        with self.Session() as session:
            account = session.scalar(
                select(Account).where(Account.name == account_name)
            )
            if not account:
                raise LedgerError(f"Account not found: {account_name}")

            # Calculate actual balance from lines
            calculated_balance = Decimal("0")
            for line in account.lines:
                calculated_balance += line.amount

            return calculated_balance == account.balance
