"""
LedgerKit - A PostgreSQL-backed, double-entry ledger core.

Framework-agnostic library for building safe, auditable wallet and marketplace systems.
"""

from ledgerkit.models import Account, Entry, Line, WebhookDelivery, AccountType, WebhookDeliveryStatus
from ledgerkit.ledger import Ledger, InsufficientFundsError, DuplicateEntryError
from ledgerkit.config import DriverConfig, GatewayConfig
from ledgerkit.gateway import PaymentGateway, GatewayError
from ledgerkit.drivers import (
    BasePaymentDriver,
    DriverError,
    PaymentResult,
    CCBillDriver,
    EpochDriver,
    PaymentCloudDriver,
    RiskPayGoDriver,
    SegpayDriver,
    SquareDriver,
    StripeDriver,
)

__version__ = "0.1.0"
__all__ = [
    # Core ledger
    "Ledger",
    "Account",
    "Entry",
    "Line",
    "WebhookDelivery",
    "AccountType",
    "WebhookDeliveryStatus",
    "InsufficientFundsError",
    "DuplicateEntryError",
    # Gateway & config
    "PaymentGateway",
    "GatewayError",
    "GatewayConfig",
    "DriverConfig",
    # Driver base
    "BasePaymentDriver",
    "DriverError",
    "PaymentResult",
    # Drivers
    "CCBillDriver",
    "EpochDriver",
    "PaymentCloudDriver",
    "RiskPayGoDriver",
    "SegpayDriver",
    "SquareDriver",
    "StripeDriver",
]
