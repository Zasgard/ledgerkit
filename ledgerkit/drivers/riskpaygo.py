"""
RiskPayGo payment driver.

Uses the RiskPayGo REST API for high-risk transaction processing.
Credentials required:
    api_key    – RiskPayGo API key
    merchant_id – RiskPayGo merchant identifier

API reference: https://riskpaygo.com/documentation
"""

from decimal import Decimal
from typing import Any, Dict

import requests

from ledgerkit.drivers.base import BasePaymentDriver, DriverError, PaymentResult

_BASE_URL = "https://api.riskpaygo.com/v1"


class RiskPayGoDriver(BasePaymentDriver):
    """RiskPayGo REST API driver."""

    name = "riskpaygo"

    def _headers(self) -> Dict[str, str]:
        return {
            "X-Api-Key": str(self.credentials.get("api_key", "")),
            "X-Merchant-Id": str(self.credentials.get("merchant_id", "")),
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
            "merchantId": str(self.credentials.get("merchant_id", "")),
            "idempotencyKey": idempotency_key,
            "amount": str(amount),
            "currency": currency.upper(),
            "card": {
                "number": payment_method.get("card_number", ""),
                "expMonth": str(payment_method.get("exp_month", "")),
                "expYear": str(payment_method.get("exp_year", "")),
                "cvv": str(payment_method.get("cvv", "")),
                "holderName": payment_method.get("name", ""),
            },
            "billing": {
                "address": payment_method.get("address1", ""),
                "city": payment_method.get("city", ""),
                "state": payment_method.get("state", ""),
                "zip": payment_method.get("zip", ""),
                "country": payment_method.get("country", "US"),
            },
            "email": payment_method.get("email", ""),
        }
        payload.update(kwargs)

        try:
            resp = requests.post(
                f"{_BASE_URL}/charges",
                json=payload,
                headers=self._headers(),
                timeout=30,
            )
            body: Dict[str, Any] = resp.json()
        except Exception as exc:
            raise DriverError(f"RiskPayGo charge request failed: {exc}") from exc

        success = resp.status_code in (200, 201) and body.get("status") == "approved"
        return PaymentResult(
            success=success,
            transaction_id=str(body["id"]) if success and "id" in body else None,
            amount=amount,
            currency=currency,
            driver_name=self.name,
            raw_response=body,
            error=None if success else (body.get("message") or "Charge declined"),
            error_code=str(body.get("code", "")) if not success else None,
        )

    def refund(
        self,
        transaction_id: str,
        amount: Decimal,
        idempotency_key: str,
        **kwargs: Any,
    ) -> PaymentResult:
        payload: Dict[str, Any] = {
            "idempotencyKey": idempotency_key,
            "amount": str(amount),
        }
        payload.update(kwargs)

        try:
            resp = requests.post(
                f"{_BASE_URL}/charges/{transaction_id}/refunds",
                json=payload,
                headers=self._headers(),
                timeout=30,
            )
            body = resp.json()
        except Exception as exc:
            raise DriverError(f"RiskPayGo refund request failed: {exc}") from exc

        success = resp.status_code in (200, 201) and body.get("status") in ("approved", "refunded")
        return PaymentResult(
            success=success,
            transaction_id=transaction_id,
            amount=amount,
            currency=kwargs.get("currency", "USD"),
            driver_name=self.name,
            raw_response=body,
            error=None if success else (body.get("message") or "Refund failed"),
        )

    def verify(
        self,
        transaction_id: str,
        **kwargs: Any,
    ) -> PaymentResult:
        try:
            resp = requests.get(
                f"{_BASE_URL}/charges/{transaction_id}",
                headers=self._headers(),
                timeout=15,
            )
            body = resp.json()
        except Exception as exc:
            raise DriverError(f"RiskPayGo verify request failed: {exc}") from exc

        success = resp.status_code == 200 and body.get("status") in ("approved", "settled")
        amount_raw = body.get("amount") or "0"
        return PaymentResult(
            success=success,
            transaction_id=transaction_id if success else None,
            amount=Decimal(str(amount_raw)),
            currency=body.get("currency", "USD"),
            driver_name=self.name,
            raw_response=body,
            error=None if success else (body.get("message") or "Transaction not found"),
        )
