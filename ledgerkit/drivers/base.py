"""
Abstract base class for all payment drivers.

Every driver must implement charge(), refund(), and verify().
Instances are constructed once from credentials stored in DriverConfig.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, Optional


@dataclass
class PaymentResult:
    """
    Unified result returned by every driver operation.

    Attributes:
        success: Whether the operation succeeded.
        transaction_id: Processor-assigned transaction identifier (None on failure).
        amount: Amount that was charged / refunded.
        currency: ISO 4217 currency code.
        driver_name: Name of the driver that produced this result.
        raw_response: Full response body from the processor (dict or str).
        error: Human-readable error message (None on success).
        error_code: Processor-specific error code (None on success).
    """

    success: bool
    transaction_id: Optional[str]
    amount: Decimal
    currency: str
    driver_name: str
    raw_response: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    error_code: Optional[str] = None


class DriverError(Exception):
    """Raised when a driver cannot complete an operation due to configuration or network issues."""

    pass


class BasePaymentDriver(ABC):
    """
    Abstract base for payment processor drivers.

    Subclasses receive processor credentials as keyword arguments to ``__init__``
    and implement ``charge``, ``refund``, and ``verify``.
    """

    name: str = ""  # Must be overridden in every subclass

    def __init__(self, **credentials: Any) -> None:
        self.credentials = credentials

    @abstractmethod
    def charge(
        self,
        amount: Decimal,
        currency: str,
        payment_method: Dict[str, Any],
        idempotency_key: str,
        **kwargs: Any,
    ) -> PaymentResult:
        """
        Charge a payment method.

        Args:
            amount: Positive decimal amount to charge.
            currency: ISO 4217 currency code (e.g. ``"USD"``).
            payment_method: Driver-specific payment details (card data, token, etc.).
            idempotency_key: Unique key that prevents duplicate charges on retry.
            **kwargs: Extra driver-specific parameters (e.g. ``description``).

        Returns:
            :class:`PaymentResult` with ``success=True`` and ``transaction_id`` set
            on success, or ``success=False`` with ``error`` set on failure.
        """

    @abstractmethod
    def refund(
        self,
        transaction_id: str,
        amount: Decimal,
        idempotency_key: str,
        **kwargs: Any,
    ) -> PaymentResult:
        """
        Refund a previously successful charge, fully or partially.

        Args:
            transaction_id: Processor transaction ID from the original charge.
            amount: Amount to refund (must be <= original charge amount).
            idempotency_key: Unique key for this refund to prevent duplicates.
            **kwargs: Extra driver-specific parameters.

        Returns:
            :class:`PaymentResult` reflecting the refund outcome.
        """

    @abstractmethod
    def verify(
        self,
        transaction_id: str,
        **kwargs: Any,
    ) -> PaymentResult:
        """
        Retrieve and verify the current status of a transaction.

        Args:
            transaction_id: Processor transaction ID to look up.
            **kwargs: Extra driver-specific parameters.

        Returns:
            :class:`PaymentResult` reflecting the current transaction status.
        """
