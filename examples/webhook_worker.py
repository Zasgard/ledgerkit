"""
Webhook Worker Example

This example shows how to run a background worker that processes
pending webhooks with automatic retry logic.
"""

import time
import logging
import signal
import sys
import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ledgerkit.webhooks import WebhookManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Database connection
DATABASE_URL = "postgresql://user:password@localhost/ledgerkit_prod"
# For testing: "sqlite:///webhooks.db"

# Create session factory
engine = create_engine(DATABASE_URL, pool_size=10, max_overflow=20)
SessionFactory = sessionmaker(bind=engine)

# Initialize webhook manager
webhook_manager = WebhookManager(SessionFactory)

# Graceful shutdown flag
shutdown_requested = False


def signal_handler(sig, frame):
    """Handle SIGINT and SIGTERM for graceful shutdown."""
    global shutdown_requested
    logger.info("Shutdown signal received, finishing current batch...")
    shutdown_requested = True


def deliver_webhook(url: str, payload: dict) -> tuple[int, str]:
    """
    Deliver a webhook via HTTP POST.
    
    Args:
        url: Webhook URL
        payload: JSON payload
        
    Returns:
        Tuple of (status_code, response_body)
    """
    try:
        response = requests.post(
            url,
            json=payload,
            timeout=30,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "LedgerKit-Webhook/1.0",
            }
        )
        return (response.status_code, response.text[:1000])  # Limit response size
        
    except requests.exceptions.Timeout:
        logger.warning(f"Timeout delivering webhook to {url}")
        return (0, "Request timeout")
        
    except requests.exceptions.RequestException as e:
        logger.error(f"Error delivering webhook to {url}: {e}")
        return (0, str(e))


def run_worker(batch_size: int = 100, sleep_seconds: int = 5):
    """
    Run the webhook worker.
    
    Args:
        batch_size: Maximum webhooks to process per batch
        sleep_seconds: Seconds to sleep when no webhooks are pending
    """
    logger.info("Starting webhook worker...")
    logger.info(f"Batch size: {batch_size}, Sleep interval: {sleep_seconds}s")
    
    while not shutdown_requested:
        try:
            # Process pending webhooks
            processed = webhook_manager.process_pending_webhooks(
                deliver_func=deliver_webhook,
                limit=batch_size,
            )
            
            if processed > 0:
                logger.info(f"Processed {processed} webhooks")
            else:
                # No webhooks to process, sleep
                time.sleep(sleep_seconds)
                
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
            break
            
        except Exception as e:
            logger.exception(f"Error in worker loop: {e}")
            time.sleep(sleep_seconds)
    
    logger.info("Webhook worker stopped")


def main():
    """Main entry point."""
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Run the worker
    try:
        run_worker(batch_size=100, sleep_seconds=5)
    except Exception as e:
        logger.exception(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
