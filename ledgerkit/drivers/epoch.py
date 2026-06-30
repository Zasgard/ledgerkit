"""
Epoch payment driver.

Uses Epoch's REST API for transaction processing.
Credentials required:
    site_id    – Epoch site / merchant ID
    api_key    – Epoch API key

API reference: https://epoch.com/billing/developers
"""

from decimal import Decimal
from typing import Any, Dict

import requests

from ledgerkit.drivers.base import BasePaymentDriver, DriverError, PaymentResult

_BASE_URL = "https://api.epoch.com/v1"


class EpochDriver(BasePaymentDriver):
    """Epoch REST API driver."""

    name = "epoch"

    def _headers(self) -> Dict[str, str]:
        return {
            "X-Epoch-Site-Id": str(self.credentials.get("site_id", "")),
            "X-Epoch-Api-Key": str(self.credentials.get("api_key", "")),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def charge(
        self,
        amount: Decimal,
        currency: str,
        payment_method: Dict[str, Any],
        idempotency_key: str,
        **kwargs: Any,
    ) -> PaymentResult:
        payload: Dict[str, Any] = {
            "siteId": str(self.credentials.get("site_id", "")),
            "referenceId": idempotency_key,
            "amount": str(amount),
            "currency": currency.upper(),
            "cardNumber": payment_method.get("card_number", ""),
            "cardExpireMonth": str(payment_method.get("exp_month", "")),
            "cardExpireYear": str(payment_method.get("exp_year", "")),
            "cardCvv": str(payment_method.get("cvv", "")),
            "cardholderName": payment_method.get("name", ""),
            "address": payment_method.get("address1", ""),
            "city": payment_method.get("city", ""),
            "state": payment_method.get("state", ""),
            "zip": payment_method.get("zip", ""),
            "country": payment_method.get("country", "US"),
            "email": payment_method.get("email", ""),
        }
        payload.update(kwargs)

        try:
            resp = requests.post(
                f"{_BASE_URL}/charge",
                json=payload,
                headers=self._headers(),
                timeout=30,
            )
            body: Dict[str, Any] = resp.json()
        except Exception as exc:
            raise DriverError(f"Epoch charge request failed: {exc}") from exc

        approved = body.get("result") == "approved" or body.get("status") == "success"
        success = resp.status_code in (200, 201) and approved
        return PaymentResult(
            success=success,
            transaction_id=str(body["transactionId"]) if success and "transactionId" in body else None,
            amount=amount,
            currency=currency,
            driver_name=self.name,
            raw_response=body,
            error=None if success else (body.get("errorMessage") or body.get("message") or "Charge declined"),
            error_code=str(body.get("errorCode", "")) if not success else None,
        )

    def refund(
        self,
        transaction_id: str,
        amount: Decimal,
        idempotency_key: str,
        **kwargs: Any,
    ) -> PaymentResult:
        payload: Dict[str, Any] = {
            "transactionId": transaction_id,
            "amount": str(amount),
            "referenceId": idempotency_key,
        }
        payload.update(kwargs)

        try:
            resp = requests.post(
                f"{_BASE_URL}/refund",
                json=payload,
                headers=self._headers(),
                timeout=30,
            )
            body = resp.json()
        except Exception as exc:
            raise DriverError(f"Epoch refund request failed: {exc}") from exc

        success = resp.status_code in (200, 201) and (
            body.get("result") == "approved" or body.get("status") == "success"
        )
        return PaymentResult(
            success=success,
            transaction_id=transaction_id,
            amount=amount,
            currency=kwargs.get("currency", "USD"),
            driver_name=self.name,
            raw_response=body,
            error=None if success else (body.get("errorMessage") or "Refund failed"),
        )

    def verify(
        self,
        transaction_id: str,
        **kwargs: Any,
    ) -> PaymentResult:
        try:
            resp = requests.get(
                f"{_BASE_URL}/transactions/{transaction_id}",
                headers=self._headers(),
                timeout=15,
            )
            body = resp.json()
        except Exception as exc:
            raise DriverError(f"Epoch verify request failed: {exc}") from exc

        success = resp.status_code == 200 and body.get("status") in ("approved", "settled", "success")
        amount_raw = body.get("amount") or "0"
        return PaymentResult(
            success=success,
            transaction_id=transaction_id if success else None,
            amount=Decimal(str(amount_raw)),
            currency=body.get("currency", "USD"),
            driver_name=self.name,
            raw_response=body,
            error=None if success else (body.get("errorMessage") or "Transaction not found"),
        )
