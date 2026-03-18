"""
Database models for the double-entry ledger system.

These models implement:
- Append-only ledger entries
- Idempotency through unique keys
- Concurrency-safe balance tracking
- Webhook delivery tracking for retries
"""

from datetime import datetime
from typing import Optional
from decimal import Decimal

from sqlalchemy import (
    Column,
    String,
    Integer,
    Numeric,
    DateTime,
    ForeignKey,
    Index,
    CheckConstraint,
    UniqueConstraint,
    Text,
    Enum as SQLEnum,
    JSON,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import declarative_base, relationship
import enum

Base = declarative_base()


class AccountType(enum.Enum):
    """Account types in double-entry bookkeeping."""
    ASSET = "asset"
    LIABILITY = "liability"
    EQUITY = "equity"
    REVENUE = "revenue"
    EXPENSE = "expense"


class Account(Base):
    """
    Represents a ledger account.
    
    Accounts track balances with concurrency control via version numbers.
    The balance is cached for performance but always verifiable from lines.
    """
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False, unique=True)
    type = Column(SQLEnum(AccountType), nullable=False)
    
    # Cached balance with optimistic locking
    balance = Column(Numeric(precision=20, scale=2), nullable=False, default=0)
    version = Column(Integer, nullable=False, default=0)
    
    # Metadata (using 'meta' instead of 'metadata' which is reserved by SQLAlchemy)
    # Use JSON for compatibility, will be JSONB on PostgreSQL
    meta = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    lines = relationship("Line", back_populates="account")

    __table_args__ = (
        Index("idx_accounts_name", "name"),
        Index("idx_accounts_type", "type"),
    )


class Entry(Base):
    """
    Represents a ledger entry (transaction).
    
    Entries are immutable (append-only) and support idempotency through
    unique idempotency_key. Each entry must balance (sum of debits = sum of credits).
    """
    __tablename__ = "entries"

    id = Column(Integer, primary_key=True)
    
    # Idempotency key ensures duplicate requests don't create duplicate entries
    idempotency_key = Column(String(255), nullable=False, unique=True)
    
    description = Column(Text, nullable=False)
    
    # Optional reference to original entry if this is a reversal
    reverses_entry_id = Column(Integer, ForeignKey("entries.id"), nullable=True)
    
    # Metadata for extensibility (using 'meta' instead of 'metadata' which is reserved)
    # Use JSON for compatibility, will be JSONB on PostgreSQL
    meta = Column(JSON, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    lines = relationship("Line", back_populates="entry", cascade="all, delete-orphan")
    reverses_entry = relationship("Entry", remote_side=[id], foreign_keys=[reverses_entry_id])

    __table_args__ = (
        Index("idx_entries_idempotency_key", "idempotency_key"),
        Index("idx_entries_created_at", "created_at"),
        Index("idx_entries_reverses", "reverses_entry_id"),
    )


class Line(Base):
    """
    Represents a line item within an entry.
    
    Each line affects one account. Lines are immutable (append-only).
    The amount is positive for debits and negative for credits (or vice versa
    depending on account type - this is handled by the business logic).
    """
    __tablename__ = "lines"

    id = Column(Integer, primary_key=True)
    entry_id = Column(Integer, ForeignKey("entries.id"), nullable=False)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    
    # Amount: positive for debit, negative for credit
    amount = Column(Numeric(precision=20, scale=2), nullable=False)
    
    # Metadata (using 'meta' instead of 'metadata' which is reserved)
    # Use JSON for compatibility, will be JSONB on PostgreSQL
    meta = Column(JSON, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    # Relationships
    entry = relationship("Entry", back_populates="lines")
    account = relationship("Account", back_populates="lines")

    __table_args__ = (
        Index("idx_lines_entry_id", "entry_id"),
        Index("idx_lines_account_id", "account_id"),
        Index("idx_lines_created_at", "created_at"),
        CheckConstraint("amount != 0", name="check_amount_nonzero"),
    )


class WebhookDeliveryStatus(enum.Enum):
    """Status of webhook delivery attempts."""
    PENDING = "pending"
    DELIVERED = "delivered"
    FAILED = "failed"


class WebhookDelivery(Base):
    """
    Tracks webhook delivery attempts for external integrations.
    
    Supports retry logic with exponential backoff. Each delivery attempt
    is recorded with its own idempotency to prevent duplicate processing.
    """
    __tablename__ = "webhook_deliveries"

    id = Column(Integer, primary_key=True)
    
    # Idempotency for webhook creation
    idempotency_key = Column(String(255), nullable=False, unique=True)
    
    # Related entry that triggered this webhook
    entry_id = Column(Integer, ForeignKey("entries.id"), nullable=False)
    
    # Webhook details
    url = Column(String(2048), nullable=False)
    # Use JSON for compatibility, will be JSONB on PostgreSQL
    payload = Column(JSON, nullable=False)
    
    # Delivery tracking
    status = Column(SQLEnum(WebhookDeliveryStatus), nullable=False, default=WebhookDeliveryStatus.PENDING)
    attempts = Column(Integer, nullable=False, default=0)
    max_attempts = Column(Integer, nullable=False, default=5)
    
    # Next retry time (NULL if no retry scheduled)
    next_retry_at = Column(DateTime, nullable=True)
    
    # Last response details
    last_response_status = Column(Integer, nullable=True)
    last_response_body = Column(Text, nullable=True)
    last_error = Column(Text, nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
    delivered_at = Column(DateTime, nullable=True)

    # Relationships
    entry = relationship("Entry")

    __table_args__ = (
        Index("idx_webhook_deliveries_idempotency_key", "idempotency_key"),
        Index("idx_webhook_deliveries_status", "status"),
        Index("idx_webhook_deliveries_next_retry", "next_retry_at"),
        Index("idx_webhook_deliveries_entry_id", "entry_id"),
    )
