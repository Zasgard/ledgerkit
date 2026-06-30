"""
Square payment driver.

Uses the Square Payments API (v2) to process charges and refunds.
Credentials required:
    access_token – Square OAuth or personal access token
    location_id  – Square location ID for the payment

API reference: https://developer.squareup.com/reference/square/payments-api
"""

from decimal import Decimal
from typing import Any, Dict
import uuid

import requests

from ledgerkit.drivers.base import BasePaymentDriver, DriverError, PaymentResult

_BASE_URL = "https://connect.squareup.com/v2"


class SquareDriver(BasePaymentDriver):
    """Square Payments API driver."""

    name = "square"

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": "Bearer " + str(self.credentials.get("access_token", "")),
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Square-Version": "2024-01-18",
        }

    def charge(
        self,
        amount: Decimal,
        currency: str,
        payment_method: Dict[str, Any],
        idempotency_key: str,
        **kwargs: Any,
    ) -> PaymentResult:
        # Square uses the smallest currency unit (cents for USD)
        amount_cents = int(amount * 100)

        payload: Dict[str, Any] = {
            "idempotency_key": idempotency_key,
            "amount_money": {
                "amount": amount_cents,
                "currency": currency.upper(),
            },
            "source_id": (
                payment_method.get("source_id")
                or payment_method.get("token")
                or payment_method.get("nonce", "")
            ),
            "location_id": str(self.credentials.get("location_id", "")),
        }

        if payment_method.get("customer_id"):
            payload["customer_id"] = payment_method["customer_id"]
        if kwargs.get("note"):
            payload["note"] = kwargs["note"]

        try:
            resp = requests.post(
                f"{_BASE_URL}/payments",
                json=payload,
                headers=self._headers(),
                timeout=30,
            )
            body: Dict[str, Any] = resp.json()
        except Exception as exc:
            raise DriverError(f"Square charge request failed: {exc}") from exc

        payment = body.get("payment", {})
        success = resp.status_code == 200 and payment.get("status") == "COMPLETED"

        if success:
            return PaymentResult(
                success=True,
                transaction_id=str(payment["id"]),
                amount=amount,
                currency=currency,
                driver_name=self.name,
                raw_response=body,
            )

        errors = body.get("errors", [{}])
        first_error = errors[0] if errors else {}
        return PaymentResult(
            success=False,
            transaction_id=None,
            amount=amount,
            currency=currency,
            driver_name=self.name,
            raw_response=body,
            error=first_error.get("detail") or "Charge declined",
            error_code=first_error.get("code"),
        )

    def refund(
        self,
        transaction_id: str,
        amount: Decimal,
        idempotency_key: str,
        **kwargs: Any,
    ) -> PaymentResult:
        currency = kwargs.get("currency", "USD")
        amount_cents = int(amount * 100)

        payload: Dict[str, Any] = {
            "idempotency_key": idempotency_key,
            "payment_id": transaction_id,
            "amount_money": {
                "amount": amount_cents,
                "currency": currency.upper(),
            },
        }
        if kwargs.get("reason"):
            payload["reason"] = kwargs["reason"]

        try:
            resp = requests.post(
                f"{_BASE_URL}/refunds",
                json=payload,
                headers=self._headers(),
                timeout=30,
            )
            body = resp.json()
        except Exception as exc:
            raise DriverError(f"Square refund request failed: {exc}") from exc

        refund = body.get("refund", {})
        success = resp.status_code == 200 and refund.get("status") in ("COMPLETED", "PENDING")

        errors = body.get("errors", [{}])
        first_error = errors[0] if errors else {}
        return PaymentResult(
            success=success,
            transaction_id=transaction_id,
            amount=amount,
            currency=currency,
            driver_name=self.name,
            raw_response=body,
            error=None if success else (first_error.get("detail") or "Refund failed"),
        )

    def verify(
        self,
        transaction_id: str,
        **kwargs: Any,
    ) -> PaymentResult:
        try:
            resp = requests.get(
                f"{_BASE_URL}/payments/{transaction_id}",
                headers=self._headers(),
                timeout=15,
            )
            body = resp.json()
        except Exception as exc:
            raise DriverError(f"Square verify request failed: {exc}") from exc

        payment = body.get("payment", {})
        success = resp.status_code == 200 and payment.get("status") == "COMPLETED"

        amount_raw = payment.get("amount_money", {}).get("amount")
        amount_decimal = Decimal(str(amount_raw)) / 100 if amount_raw is not None else Decimal("0")
        currency = payment.get("amount_money", {}).get("currency", "USD")

        errors = body.get("errors", [{}])
        first_error = errors[0] if errors else {}
        return PaymentResult(
            success=success,
            transaction_id=transaction_id if success else None,
            amount=amount_decimal,
            currency=currency,
            driver_name=self.name,
            raw_response=body,
            error=None if success else (first_error.get("detail") or "Transaction not found"),
        )
