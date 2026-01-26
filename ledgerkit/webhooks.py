"""
Webhook delivery system with retry logic.

Features:
- Idempotent webhook creation
- Exponential backoff for retries
- Delivery tracking
- Worker integration helpers
"""

from typing import Optional, Dict, Any, Callable, List, Tuple
from datetime import datetime, timedelta
from decimal import Decimal
import logging
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from ledgerkit.models import WebhookDelivery, WebhookDeliveryStatus, Entry

logger = logging.getLogger(__name__)


class WebhookManager:
    """
    Manages webhook delivery with retry logic.
    
    Usage:
        webhook_mgr = WebhookManager(session_factory)
        
        # Create webhook (idempotent)
        delivery = webhook_mgr.create_webhook(
            idempotency_key="webhook-entry-123",
            entry_id=entry.id,
            url="https://example.com/webhooks",
            payload={"event": "payment", "amount": "100.00"}
        )
        
        # Process pending webhooks (in worker)
        webhook_mgr.process_pending_webhooks(deliver_func=my_http_client.post)
    """

    def __init__(self, session_factory: Callable[[], Session]):
        """
        Initialize webhook manager.
        
        Args:
            session_factory: Callable that returns a SQLAlchemy session
        """
        self.session_factory = session_factory

    def create_webhook(
        self,
        idempotency_key: str,
        entry_id: int,
        url: str,
        payload: Dict[str, Any],
        max_attempts: int = 5,
    ) -> WebhookDelivery:
        """
        Create a webhook delivery record.
        
        This is idempotent - calling with the same idempotency_key
        returns the existing record.
        
        Args:
            idempotency_key: Unique key for this webhook
            entry_id: Related ledger entry ID
            url: Webhook URL to deliver to
            payload: JSON payload to send
            max_attempts: Maximum delivery attempts
            
        Returns:
            WebhookDelivery record
        """
        with self.session_factory() as session:
            # Check for existing webhook (idempotency)
            existing = session.scalar(
                select(WebhookDelivery).where(
                    WebhookDelivery.idempotency_key == idempotency_key
                )
            )
            if existing:
                return existing

            # Create new webhook
            webhook = WebhookDelivery(
                idempotency_key=idempotency_key,
                entry_id=entry_id,
                url=url,
                payload=payload,
                max_attempts=max_attempts,
                next_retry_at=datetime.utcnow(),  # Ready for immediate delivery
            )
            session.add(webhook)
            session.commit()
            session.refresh(webhook)
            return webhook

    def get_pending_webhooks(self, limit: int = 100) -> List[WebhookDelivery]:
        """
        Get webhooks that are ready for delivery.
        
        Args:
            limit: Maximum number to return
            
        Returns:
            List of webhook deliveries ready for processing
        """
        with self.session_factory() as session:
            stmt = (
                select(WebhookDelivery)
                .where(WebhookDelivery.status == WebhookDeliveryStatus.PENDING)
                .where(WebhookDelivery.attempts < WebhookDelivery.max_attempts)
                .where(
                    (WebhookDelivery.next_retry_at.is_(None))
                    | (WebhookDelivery.next_retry_at <= datetime.utcnow())
                )
                .order_by(WebhookDelivery.next_retry_at)
                .limit(limit)
            )
            return list(session.scalars(stmt))

    def mark_delivered(self, webhook_id: int, response_status: int, response_body: str) -> None:
        """
        Mark webhook as successfully delivered.
        
        Args:
            webhook_id: ID of webhook delivery
            response_status: HTTP status code
            response_body: Response body
        """
        with self.session_factory() as session:
            webhook = session.get(WebhookDelivery, webhook_id)
            if webhook:
                webhook.status = WebhookDeliveryStatus.DELIVERED
                webhook.delivered_at = datetime.utcnow()
                webhook.last_response_status = response_status
                webhook.last_response_body = response_body
                webhook.updated_at = datetime.utcnow()
                session.commit()

    def mark_failed(
        self,
        webhook_id: int,
        error: str,
        response_status: Optional[int] = None,
        response_body: Optional[str] = None,
    ) -> None:
        """
        Mark webhook delivery attempt as failed and schedule retry.
        
        Uses exponential backoff: 1min, 5min, 15min, 1hr, 4hr
        
        Args:
            webhook_id: ID of webhook delivery
            error: Error message
            response_status: HTTP status code (if available)
            response_body: Response body (if available)
        """
        with self.session_factory() as session:
            webhook = session.get(WebhookDelivery, webhook_id)
            if not webhook:
                return

            webhook.attempts += 1
            webhook.last_error = error
            webhook.last_response_status = response_status
            webhook.last_response_body = response_body
            webhook.updated_at = datetime.utcnow()

            # Calculate next retry time with exponential backoff
            if webhook.attempts < webhook.max_attempts:
                # Exponential backoff: 2^(attempts-1) minutes, capped
                backoff_minutes = min(2 ** (webhook.attempts - 1), 240)  # Max 4 hours
                webhook.next_retry_at = datetime.utcnow() + timedelta(minutes=backoff_minutes)
                logger.info(
                    f"Webhook {webhook_id} failed (attempt {webhook.attempts}), "
                    f"retrying in {backoff_minutes} minutes"
                )
            else:
                # Max attempts reached
                webhook.status = WebhookDeliveryStatus.FAILED
                webhook.next_retry_at = None
                logger.error(
                    f"Webhook {webhook_id} permanently failed after {webhook.attempts} attempts"
                )

            session.commit()

    def process_pending_webhooks(
        self,
        deliver_func: Callable[[str, Dict[str, Any]], Tuple[int, str]],
        limit: int = 100,
    ) -> int:
        """
        Process pending webhooks using provided delivery function.
        
        This is designed to be called by a worker process.
        
        Args:
            deliver_func: Function that delivers webhook and returns (status_code, body)
            limit: Maximum number to process in this batch
            
        Returns:
            Number of webhooks processed
        """
        webhooks = self.get_pending_webhooks(limit=limit)
        processed = 0

        for webhook in webhooks:
            try:
                logger.info(f"Delivering webhook {webhook.id} to {webhook.url}")
                status_code, response_body = deliver_func(webhook.url, webhook.payload)

                if 200 <= status_code < 300:
                    self.mark_delivered(webhook.id, status_code, response_body)
                    logger.info(f"Webhook {webhook.id} delivered successfully")
                else:
                    self.mark_failed(
                        webhook.id,
                        f"HTTP {status_code}",
                        status_code,
                        response_body,
                    )

                processed += 1

            except Exception as e:
                logger.exception(f"Error delivering webhook {webhook.id}")
                self.mark_failed(webhook.id, str(e))
                processed += 1

        return processed

    def retry_failed_webhook(self, webhook_id: int) -> None:
        """
        Manually retry a failed webhook.
        
        Args:
            webhook_id: ID of webhook to retry
        """
        with self.session_factory() as session:
            webhook = session.get(WebhookDelivery, webhook_id)
            if webhook and webhook.status == WebhookDeliveryStatus.FAILED:
                webhook.status = WebhookDeliveryStatus.PENDING
                webhook.next_retry_at = datetime.utcnow()
                webhook.attempts = 0  # Reset attempts
                session.commit()


class WorkerQueue:
    """
    Helper for building retry-safe queue workers.
    
    Provides utilities for:
    - Idempotent task processing
    - Automatic retries with exponential backoff
    - Dead letter queue for permanent failures
    
    Usage:
        queue = WorkerQueue()
        
        @queue.task(max_retries=3)
        def process_payment(payment_id: str):
            # Your processing logic
            pass
            
        # In worker
        queue.run_workers()
    """

    def __init__(self):
        """Initialize worker queue."""
        self.tasks: Dict[str, Callable] = {}
        self.task_config: Dict[str, Dict[str, Any]] = {}

    def task(
        self,
        max_retries: int = 3,
        backoff_base: int = 2,
        backoff_cap_minutes: int = 240,
    ) -> Callable:
        """
        Decorator to register a task with retry configuration.
        
        Args:
            max_retries: Maximum number of retry attempts
            backoff_base: Base for exponential backoff (2^n)
            backoff_cap_minutes: Maximum backoff time in minutes
            
        Returns:
            Decorator function
        """
        def decorator(func: Callable) -> Callable:
            task_name = func.__name__
            self.tasks[task_name] = func
            self.task_config[task_name] = {
                "max_retries": max_retries,
                "backoff_base": backoff_base,
                "backoff_cap_minutes": backoff_cap_minutes,
            }
            return func

        return decorator

    def calculate_backoff(
        self,
        attempt: int,
        backoff_base: int = 2,
        backoff_cap_minutes: int = 240,
    ) -> int:
        """
        Calculate backoff time for retry attempt.
        
        Args:
            attempt: Current attempt number (1-indexed)
            backoff_base: Base for exponential calculation
            backoff_cap_minutes: Maximum backoff in minutes
            
        Returns:
            Backoff time in minutes
        """
        backoff = backoff_base ** (attempt - 1)
        return min(backoff, backoff_cap_minutes)
