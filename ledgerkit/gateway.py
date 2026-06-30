"""
Payment gateway that routes charges through configured drivers.

Features:
- Round-robin rotation across multiple primary drivers
- Ordered fallback chain when all primaries fail
- Automatic driver instantiation from :class:`~ledgerkit.config.GatewayConfig`
- One-step ledger confirmation via :meth:`PaymentGateway.confirm_to_ledger`

Usage::

    from decimal import Decimal
    from ledgerkit.config import DriverConfig, GatewayConfig
    from ledgerkit.drivers import StripeDriver, PaymentCloudDriver
    from ledgerkit.gateway import PaymentGateway
    from ledgerkit.ledger import Ledger

    ledger = Ledger("postgresql://...")
    ledger.init_db()

    config = GatewayConfig(
        primaries=[
            DriverConfig("stripe", StripeDriver, {"api_key": "sk_..."}),
        ],
        fallbacks=[
            DriverConfig("payment_cloud", PaymentCloudDriver, {"security_key": "..."}),
        ],
    )

    gateway = PaymentGateway(config, ledger=ledger)

    result = gateway.charge(
        amount=Decimal("49.99"),
        currency="USD",
        payment_method={"token": "tok_visa"},
        idempotency_key="order-abc-001",
        description="Premium subscription",
    )

    if result.success:
        gateway.confirm_to_ledger(
            result,
            debit_account="receivables",
            credit_account="revenue",
            description="Premium subscription payment",
        )
"""

import logging
from decimal import Decimal
from typing import Any, Dict, List, Optional

from ledgerkit.config import GatewayConfig
from ledgerkit.drivers.base import BasePaymentDriver, PaymentResult
from ledgerkit.ledger import Ledger

logger = logging.getLogger(__name__)


class GatewayError(Exception):
    """Raised when every configured driver has failed or no drivers are configured."""

    pass


class PaymentGateway:
    """
    Routes payment requests across primary and fallback drivers.

    **Routing logic**

    1. Charge attempts start at the *next primary* in a round-robin cycle so
       load is spread evenly across all enabled primaries.
    2. If a primary returns ``success=False`` or raises an exception, the next
       primary is tried (wrapping around once through the full list).
    3. Only after all primaries are exhausted are fallback drivers tried, in the
       order they appear in the configuration.
    4. If every driver fails, :class:`GatewayError` is raised with a summary of
       all error messages.

    **Refund / verify routing**

    For :meth:`refund` and :meth:`verify`, supply ``driver_name`` to target a
    specific driver; otherwise all drivers are tried in primary-then-fallback
    order until one succeeds.
    """

    def __init__(
        self,
        config: GatewayConfig,
        ledger: Optional[Ledger] = None,
    ) -> None:
        """
        Initialise the gateway.

        Args:
            config: :class:`~ledgerkit.config.GatewayConfig` describing primaries
                    and fallbacks.
            ledger: Optional :class:`~ledgerkit.ledger.Ledger` instance used by
                    :meth:`confirm_to_ledger`.  Required only if you intend to call
                    that method.
        """
        self._config = config
        self._ledger = ledger

        self._primaries: List[BasePaymentDriver] = [
            dc.driver_class(**dc.credentials)
            for dc in config.primaries
            if dc.enabled
        ]
        self._fallbacks: List[BasePaymentDriver] = [
            dc.driver_class(**dc.credentials)
            for dc in config.fallbacks
            if dc.enabled
        ]

        # Index of the next primary to try (advances on every charge attempt)
        self._primary_index: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def charge(
        self,
        amount: Decimal,
        currency: str,
        payment_method: Dict[str, Any],
        idempotency_key: str,
        **kwargs: Any,
    ) -> PaymentResult:
        """
        Charge a payment method, automatically routing through drivers.

        Tries primaries in round-robin order, then falls back to fallback
        drivers if all primaries fail.

        Args:
            amount: Positive :class:`~decimal.Decimal` amount to charge.
            currency: ISO 4217 currency code (e.g. ``"USD"``).
            payment_method: Driver-specific payment details.  Common keys:

                - ``"token"``       – tokenised card (Stripe / Square)
                - ``"card_number"`` – raw card number
                - ``"exp_month"``   – two-digit expiry month
                - ``"exp_year"``    – two- or four-digit expiry year
                - ``"cvv"``         – card verification code
                - ``"name"``        – cardholder name
                - ``"email"``       – cardholder e-mail
                - ``"address1"``, ``"city"``, ``"state"``, ``"zip"``, ``"country"``

            idempotency_key: Unique key to prevent duplicate charges on retry.
            **kwargs: Extra parameters forwarded to the driver (e.g. ``description``).

        Returns:
            :class:`~ledgerkit.drivers.base.PaymentResult` from the first driver
            that succeeds.

        Raises:
            :class:`GatewayError`: If every configured driver fails.
        """
        errors: List[str] = []

        if self._primaries:
            start = self._primary_index
            self._primary_index = (self._primary_index + 1) % len(self._primaries)

            for i in range(len(self._primaries)):
                idx = (start + i) % len(self._primaries)
                driver = self._primaries[idx]
                result = self._try_charge(driver, amount, currency, payment_method, idempotency_key, kwargs)
                if result is not None and result.success:
                    logger.info("charge succeeded via primary driver '%s'", driver.name)
                    return result
                errors.append(
                    "{}: {}".format(driver.name, result.error if result else "exception")
                )

        for driver in self._fallbacks:
            result = self._try_charge(driver, amount, currency, payment_method, idempotency_key, kwargs)
            if result is not None and result.success:
                logger.info("charge succeeded via fallback driver '%s'", driver.name)
                return result
            errors.append(
                "{}: {}".format(driver.name, result.error if result else "exception")
            )

        raise GatewayError("All payment drivers failed: " + "; ".join(errors))

    def refund(
        self,
        transaction_id: str,
        amount: Decimal,
        idempotency_key: str,
        driver_name: Optional[str] = None,
        **kwargs: Any,
    ) -> PaymentResult:
        """
        Refund a previously charged transaction.

        Args:
            transaction_id: Processor transaction ID from the original charge.
            amount: Amount to refund.
            idempotency_key: Unique key for this refund.
            driver_name: If supplied, only the named driver is tried; otherwise
                         all drivers are tried in primary-then-fallback order.
            **kwargs: Extra parameters forwarded to the driver (e.g. ``currency``).

        Returns:
            :class:`~ledgerkit.drivers.base.PaymentResult` from the first
            successful driver.

        Raises:
            :class:`GatewayError`: If no driver succeeds or none matches ``driver_name``.
        """
        drivers = self._resolve_drivers(driver_name)
        errors: List[str] = []

        for driver in drivers:
            try:
                result = driver.refund(transaction_id, amount, idempotency_key, **kwargs)
                if result.success:
                    return result
                errors.append("{}: {}".format(driver.name, result.error))
            except Exception as exc:
                errors.append("{}: {}".format(driver.name, exc))
                logger.exception("driver '%s' raised during refund", driver.name)

        raise GatewayError("All refund attempts failed: " + "; ".join(errors))

    def verify(
        self,
        transaction_id: str,
        driver_name: Optional[str] = None,
        **kwargs: Any,
    ) -> PaymentResult:
        """
        Verify / look up a transaction across configured drivers.

        Args:
            transaction_id: Processor transaction ID to look up.
            driver_name: If supplied, only the named driver is tried.
            **kwargs: Extra parameters forwarded to the driver.

        Returns:
            :class:`~ledgerkit.drivers.base.PaymentResult` from the first
            driver that locates the transaction.

        Raises:
            :class:`GatewayError`: If no driver finds the transaction.
        """
        drivers = self._resolve_drivers(driver_name)
        errors: List[str] = []

        for driver in drivers:
            try:
                result = driver.verify(transaction_id, **kwargs)
                if result.success:
                    return result
                errors.append("{}: {}".format(driver.name, result.error))
            except Exception as exc:
                errors.append("{}: {}".format(driver.name, exc))
                logger.exception("driver '%s' raised during verify", driver.name)

        raise GatewayError("Transaction verification failed: " + "; ".join(errors))

    def confirm_to_ledger(
        self,
        result: PaymentResult,
        debit_account: str,
        credit_account: str,
        description: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Record a successful payment as a double-entry ledger transaction.

        This creates one balanced :class:`~ledgerkit.models.Entry` in the
        ledger with the payment amount flowing from *credit_account* to
        *debit_account*.  The call is **idempotent** – duplicate calls with
        the same ``result.transaction_id`` return silently without creating
        another entry.

        Args:
            result:          A successful :class:`~ledgerkit.drivers.base.PaymentResult`
                             (``result.success`` must be ``True``).
            debit_account:   Name of the ledger account to debit
                             (e.g. ``"receivables"``).
            credit_account:  Name of the ledger account to credit
                             (e.g. ``"revenue"``).
            description:     Human-readable description for the ledger entry.
            meta:            Optional extra metadata merged into the entry's
                             ``meta`` field.

        Raises:
            :exc:`ValueError`: If ``result.success`` is ``False`` or no ledger
                               was provided during construction.
        """
        if self._ledger is None:
            raise ValueError(
                "A Ledger instance must be supplied to PaymentGateway to use confirm_to_ledger."
            )
        if not result.success:
            raise ValueError("Cannot confirm a failed payment to the ledger.")

        entry_meta: Dict[str, Any] = {
            "payment_driver": result.driver_name,
            "payment_transaction_id": result.transaction_id,
            "payment_currency": result.currency,
        }
        if meta:
            entry_meta.update(meta)

        self._ledger.create_entry(
            idempotency_key="payment-{}".format(result.transaction_id),
            description=description,
            lines=[
                {"account": debit_account, "amount": result.amount},
                {"account": credit_account, "amount": -result.amount},
            ],
            meta=entry_meta,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _try_charge(
        self,
        driver: BasePaymentDriver,
        amount: Decimal,
        currency: str,
        payment_method: Dict[str, Any],
        idempotency_key: str,
        extra: Dict[str, Any],
    ) -> Optional[PaymentResult]:
        """Attempt a charge on *driver*, returning ``None`` on exception."""
        try:
            logger.debug("trying charge with driver '%s'", driver.name)
            return driver.charge(amount, currency, payment_method, idempotency_key, **extra)
        except Exception as exc:
            logger.exception("driver '%s' raised during charge", driver.name)
            return None

    def _resolve_drivers(self, driver_name: Optional[str]) -> List[BasePaymentDriver]:
        """Return the list of drivers to use for refund / verify operations."""
        all_drivers: List[BasePaymentDriver] = self._primaries + self._fallbacks
        if driver_name is None:
            return all_drivers
        matched = [d for d in all_drivers if d.name == driver_name]
        if not matched:
            raise GatewayError("No driver named '{}' is configured.".format(driver_name))
        return matched
