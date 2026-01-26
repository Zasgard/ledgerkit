"""
Tests for webhook delivery system.

Tests cover:
- Webhook creation idempotency
- Retry logic with exponential backoff
- Delivery tracking
- Failed webhook handling
"""

import pytest
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ledgerkit.models import Base, AccountType, WebhookDeliveryStatus
from ledgerkit.ledger import Ledger
from ledgerkit.webhooks import WebhookManager


@pytest.fixture
def database_url():
    """Use in-memory SQLite for testing."""
    return "sqlite:///:memory:"


@pytest.fixture
def session_factory(database_url):
    """Create session factory for testing."""
    engine = create_engine(database_url)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)


@pytest.fixture
def ledger(database_url):
    """Create a fresh ledger for each test."""
    ledger = Ledger(database_url)
    ledger.init_db()
    return ledger


@pytest.fixture
def webhook_manager(session_factory):
    """Create webhook manager."""
    return WebhookManager(session_factory)


class TestWebhookCreation:
    """Test webhook creation."""

    def test_create_webhook(self, ledger, webhook_manager):
        """Test creating a webhook."""
        # Create an entry first
        ledger.create_account("cash", AccountType.ASSET)
        ledger.create_account("revenue", AccountType.REVENUE)
        entry = ledger.create_entry(
            idempotency_key="sale-1",
            description="Sale",
            lines=[
                {"account": "cash", "amount": Decimal("100.00")},
                {"account": "revenue", "amount": Decimal("-100.00")},
            ],
        )

        # Create webhook
        webhook = webhook_manager.create_webhook(
            idempotency_key="webhook-sale-1",
            entry_id=entry.id,
            url="https://example.com/webhook",
            payload={"event": "payment", "amount": "100.00"},
        )

        assert webhook.idempotency_key == "webhook-sale-1"
        assert webhook.entry_id == entry.id
        assert webhook.url == "https://example.com/webhook"
        assert webhook.status == WebhookDeliveryStatus.PENDING
        assert webhook.attempts == 0

    def test_webhook_creation_is_idempotent(self, ledger, webhook_manager):
        """Test that duplicate webhook creation returns existing webhook."""
        ledger.create_account("cash", AccountType.ASSET)
        ledger.create_account("revenue", AccountType.REVENUE)
        entry = ledger.create_entry(
            idempotency_key="sale-2",
            description="Sale",
            lines=[
                {"account": "cash", "amount": Decimal("100.00")},
                {"account": "revenue", "amount": Decimal("-100.00")},
            ],
        )

        # Create webhook
        webhook1 = webhook_manager.create_webhook(
            idempotency_key="webhook-sale-2",
            entry_id=entry.id,
            url="https://example.com/webhook",
            payload={"event": "payment"},
        )

        # Try to create again with same key
        webhook2 = webhook_manager.create_webhook(
            idempotency_key="webhook-sale-2",
            entry_id=entry.id,
            url="https://example.com/webhook",
            payload={"event": "payment"},
        )

        # Should return the same webhook
        assert webhook1.id == webhook2.id


class TestWebhookDelivery:
    """Test webhook delivery and tracking."""

    def test_get_pending_webhooks(self, ledger, webhook_manager):
        """Test retrieving pending webhooks."""
        ledger.create_account("cash", AccountType.ASSET)
        ledger.create_account("revenue", AccountType.REVENUE)

        # Create entry and webhooks
        entry = ledger.create_entry(
            idempotency_key="sale-3",
            description="Sale",
            lines=[
                {"account": "cash", "amount": Decimal("100.00")},
                {"account": "revenue", "amount": Decimal("-100.00")},
            ],
        )

        webhook_manager.create_webhook(
            idempotency_key="webhook-1",
            entry_id=entry.id,
            url="https://example.com/webhook1",
            payload={"event": "payment"},
        )

        webhook_manager.create_webhook(
            idempotency_key="webhook-2",
            entry_id=entry.id,
            url="https://example.com/webhook2",
            payload={"event": "payment"},
        )

        # Get pending webhooks
        pending = webhook_manager.get_pending_webhooks()
        assert len(pending) == 2

    def test_mark_delivered(self, ledger, webhook_manager):
        """Test marking webhook as delivered."""
        ledger.create_account("cash", AccountType.ASSET)
        ledger.create_account("revenue", AccountType.REVENUE)
        entry = ledger.create_entry(
            idempotency_key="sale-4",
            description="Sale",
            lines=[
                {"account": "cash", "amount": Decimal("100.00")},
                {"account": "revenue", "amount": Decimal("-100.00")},
            ],
        )

        webhook = webhook_manager.create_webhook(
            idempotency_key="webhook-3",
            entry_id=entry.id,
            url="https://example.com/webhook",
            payload={"event": "payment"},
        )

        # Mark as delivered
        webhook_manager.mark_delivered(webhook.id, 200, "OK")

        # Verify status
        pending = webhook_manager.get_pending_webhooks()
        assert len(pending) == 0  # Should not be in pending anymore

    def test_mark_failed_with_retry(self, ledger, webhook_manager):
        """Test marking webhook as failed and scheduling retry."""
        ledger.create_account("cash", AccountType.ASSET)
        ledger.create_account("revenue", AccountType.REVENUE)
        entry = ledger.create_entry(
            idempotency_key="sale-5",
            description="Sale",
            lines=[
                {"account": "cash", "amount": Decimal("100.00")},
                {"account": "revenue", "amount": Decimal("-100.00")},
            ],
        )

        webhook = webhook_manager.create_webhook(
            idempotency_key="webhook-4",
            entry_id=entry.id,
            url="https://example.com/webhook",
            payload={"event": "payment"},
        )

        # Mark as failed
        webhook_manager.mark_failed(webhook.id, "Connection timeout", 500, "Error")

        # Verify it's scheduled for retry
        with webhook_manager.session_factory() as session:
            from ledgerkit.models import WebhookDelivery
            updated_webhook = session.get(WebhookDelivery, webhook.id)
            assert updated_webhook.attempts == 1
            assert updated_webhook.status == WebhookDeliveryStatus.PENDING
            assert updated_webhook.next_retry_at is not None

    def test_permanent_failure_after_max_attempts(self, ledger, webhook_manager):
        """Test that webhook is marked as permanently failed after max attempts."""
        ledger.create_account("cash", AccountType.ASSET)
        ledger.create_account("revenue", AccountType.REVENUE)
        entry = ledger.create_entry(
            idempotency_key="sale-6",
            description="Sale",
            lines=[
                {"account": "cash", "amount": Decimal("100.00")},
                {"account": "revenue", "amount": Decimal("-100.00")},
            ],
        )

        webhook = webhook_manager.create_webhook(
            idempotency_key="webhook-5",
            entry_id=entry.id,
            url="https://example.com/webhook",
            payload={"event": "payment"},
            max_attempts=3,
        )

        # Fail it 3 times
        for i in range(3):
            webhook_manager.mark_failed(webhook.id, f"Attempt {i+1} failed")

        # Verify it's permanently failed
        with webhook_manager.session_factory() as session:
            from ledgerkit.models import WebhookDelivery
            failed_webhook = session.get(WebhookDelivery, webhook.id)
            assert failed_webhook.status == WebhookDeliveryStatus.FAILED
            assert failed_webhook.attempts == 3


class TestWebhookProcessing:
    """Test webhook processing with delivery function."""

    def test_process_pending_webhooks_success(self, ledger, webhook_manager):
        """Test processing webhooks with successful delivery."""
        ledger.create_account("cash", AccountType.ASSET)
        ledger.create_account("revenue", AccountType.REVENUE)
        entry = ledger.create_entry(
            idempotency_key="sale-7",
            description="Sale",
            lines=[
                {"account": "cash", "amount": Decimal("100.00")},
                {"account": "revenue", "amount": Decimal("-100.00")},
            ],
        )

        webhook_manager.create_webhook(
            idempotency_key="webhook-6",
            entry_id=entry.id,
            url="https://example.com/webhook",
            payload={"event": "payment"},
        )

        # Mock delivery function (always succeeds)
        def mock_deliver(url, payload):
            return (200, "Success")

        # Process webhooks
        processed = webhook_manager.process_pending_webhooks(mock_deliver)
        assert processed == 1

        # Verify no more pending
        pending = webhook_manager.get_pending_webhooks()
        assert len(pending) == 0

    def test_process_pending_webhooks_failure(self, ledger, webhook_manager):
        """Test processing webhooks with failed delivery."""
        ledger.create_account("cash", AccountType.ASSET)
        ledger.create_account("revenue", AccountType.REVENUE)
        entry = ledger.create_entry(
            idempotency_key="sale-8",
            description="Sale",
            lines=[
                {"account": "cash", "amount": Decimal("100.00")},
                {"account": "revenue", "amount": Decimal("-100.00")},
            ],
        )

        webhook_manager.create_webhook(
            idempotency_key="webhook-7",
            entry_id=entry.id,
            url="https://example.com/webhook",
            payload={"event": "payment"},
        )

        # Mock delivery function (always fails)
        def mock_deliver(url, payload):
            return (500, "Internal Server Error")

        # Process webhooks
        processed = webhook_manager.process_pending_webhooks(mock_deliver)
        assert processed == 1

        # Should still be pending (scheduled for retry)
        # Note: Won't be in immediate pending due to retry delay
        with webhook_manager.session_factory() as session:
            from ledgerkit.models import WebhookDelivery
            from sqlalchemy import select
            
            all_webhooks = list(session.scalars(select(WebhookDelivery)))
            assert len(all_webhooks) == 1
            assert all_webhooks[0].status == WebhookDeliveryStatus.PENDING
            assert all_webhooks[0].attempts == 1

    def test_retry_failed_webhook(self, ledger, webhook_manager):
        """Test manually retrying a failed webhook."""
        ledger.create_account("cash", AccountType.ASSET)
        ledger.create_account("revenue", AccountType.REVENUE)
        entry = ledger.create_entry(
            idempotency_key="sale-9",
            description="Sale",
            lines=[
                {"account": "cash", "amount": Decimal("100.00")},
                {"account": "revenue", "amount": Decimal("-100.00")},
            ],
        )

        webhook = webhook_manager.create_webhook(
            idempotency_key="webhook-8",
            entry_id=entry.id,
            url="https://example.com/webhook",
            payload={"event": "payment"},
            max_attempts=1,
        )

        # Fail it once to make it permanently failed
        webhook_manager.mark_failed(webhook.id, "Failed")

        # Verify it's failed
        with webhook_manager.session_factory() as session:
            from ledgerkit.models import WebhookDelivery
            failed_webhook = session.get(WebhookDelivery, webhook.id)
            assert failed_webhook.status == WebhookDeliveryStatus.FAILED

        # Retry manually
        webhook_manager.retry_failed_webhook(webhook.id)

        # Verify it's back to pending
        with webhook_manager.session_factory() as session:
            from ledgerkit.models import WebhookDelivery
            retried_webhook = session.get(WebhookDelivery, webhook.id)
            assert retried_webhook.status == WebhookDeliveryStatus.PENDING
            assert retried_webhook.attempts == 0  # Reset
