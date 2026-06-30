"""
Stripe payment driver.

Uses the Stripe REST API to process charges and refunds.
Credentials required:
    api_key – Stripe secret key (``sk_live_...`` or ``sk_test_...``)

API reference: https://stripe.com/docs/api
"""

from decimal import Decimal
from typing import Any, Dict

import requests

from ledgerkit.drivers.base import BasePaymentDriver, DriverError, PaymentResult

_BASE_URL = "https://api.stripe.com/v1"


class StripeDriver(BasePaymentDriver):
    """Stripe REST API driver (no Stripe SDK dependency)."""

    name = "stripe"

    def _auth(self) -> tuple:  # type: ignore[type-arg]
        """Return HTTP Basic-Auth tuple (Stripe uses API key as username)."""
        return (str(self.credentials.get("api_key", "")), "")

    def charge(
        self,
        amount: Decimal,
        currency: str,
        payment_method: Dict[str, Any],
        idempotency_key: str,
        **kwargs: Any,
    ) -> PaymentResult:
        # Stripe amounts are in the smallest currency unit (cents for USD)
        amount_cents = int(amount * 100)

        data: Dict[str, Any] = {
            "amount": amount_cents,
            "currency": currency.lower(),
            "description": kwargs.get("description", ""),
        }

        # Accept either a tokenised source/payment_method or raw card data
        if payment_method.get("token"):
            data["source"] = payment_method["token"]
        elif payment_method.get("payment_method_id"):
            data["payment_method"] = payment_method["payment_method_id"]
            data["confirm"] = "true"
        else:
            # Card object (test mode only; Stripe does not accept raw card data in live mode)
            data["source"] = {
                "object": "card",
                "number": payment_method.get("card_number", ""),
                "exp_month": str(payment_method.get("exp_month", "")),
                "exp_year": str(payment_method.get("exp_year", "")),
                "cvc": str(payment_method.get("cvv", "")),
                "name": payment_method.get("name", ""),
            }

        headers = {"Idempotency-Key": idempotency_key}

        try:
            resp = requests.post(
                f"{_BASE_URL}/charges",
                data=data,
                headers=headers,
                auth=self._auth(),
                timeout=30,
            )
            body: Dict[str, Any] = resp.json()
        except Exception as exc:
            raise DriverError(f"Stripe charge request failed: {exc}") from exc

        success = resp.status_code == 200 and body.get("status") == "succeeded"
        if success:
            return PaymentResult(
                success=True,
                transaction_id=str(body["id"]),
                amount=amount,
                currency=currency,
                driver_name=self.name,
                raw_response=body,
            )

        err = body.get("error", {})
        return PaymentResult(
            success=False,
            transaction_id=None,
            amount=amount,
            currency=currency,
            driver_name=self.name,
            raw_response=body,
            error=err.get("message") or "Charge declined",
            error_code=err.get("code"),
        )

    def refund(
        self,
        transaction_id: str,
        amount: Decimal,
        idempotency_key: str,
        **kwargs: Any,
    ) -> PaymentResult:
        amount_cents = int(amount * 100)
        data: Dict[str, Any] = {
            "charge": transaction_id,
            "amount": amount_cents,
        }
        data.update(kwargs)

        headers = {"Idempotency-Key": idempotency_key}

        try:
            resp = requests.post(
                f"{_BASE_URL}/refunds",
                data=data,
                headers=headers,
                auth=self._auth(),
                timeout=30,
            )
            body = resp.json()
        except Exception as exc:
            raise DriverError(f"Stripe refund request failed: {exc}") from exc

        success = resp.status_code == 200 and body.get("status") == "succeeded"
        err = body.get("error", {})
        return PaymentResult(
            success=success,
            transaction_id=transaction_id,
            amount=amount,
            currency=kwargs.get("currency", "USD"),
            driver_name=self.name,
            raw_response=body,
            error=None if success else (err.get("message") or "Refund failed"),
            error_code=err.get("code") if not success else None,
        )

    def verify(
        self,
        transaction_id: str,
        **kwargs: Any,
    ) -> PaymentResult:
        try:
            resp = requests.get(
                f"{_BASE_URL}/charges/{transaction_id}",
                auth=self._auth(),
                timeout=15,
            )
            body = resp.json()
        except Exception as exc:
            raise DriverError(f"Stripe verify request failed: {exc}") from exc

        success = resp.status_code == 200 and body.get("status") == "succeeded"
        amount_raw = body.get("amount")
        amount_decimal = Decimal(str(amount_raw)) / 100 if amount_raw is not None else Decimal("0")
        err = body.get("error", {})
        return PaymentResult(
            success=success,
            transaction_id=transaction_id if success else None,
            amount=amount_decimal,
            currency=body.get("currency", "usd").upper(),
            driver_name=self.name,
            raw_response=body,
            error=None if success else (err.get("message") or "Transaction not found"),
        )
