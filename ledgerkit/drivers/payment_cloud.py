"""
PaymentCloud payment driver.

PaymentCloud routes through the NMI gateway.
Uses form-encoded POST requests to the NMI transaction endpoint.
Credentials required:
    security_key – NMI gateway security key provided by PaymentCloud

NMI API reference: https://secure.nmi.com/merchants/resources/integration/integration_portal.php
"""

from decimal import Decimal
from typing import Any, Dict

import requests

from ledgerkit.drivers.base import BasePaymentDriver, DriverError, PaymentResult

_TRANSACTION_URL = "https://secure.paymentcloud.com/api/transact.php"


class PaymentCloudDriver(BasePaymentDriver):
    """PaymentCloud / NMI gateway driver."""

    name = "payment_cloud"

    def _parse_nmi_response(self, text: str) -> Dict[str, str]:
        """Parse NMI key=value response body into a dict."""
        result: Dict[str, str] = {}
        for pair in text.strip().split("&"):
            if "=" in pair:
                k, _, v = pair.partition("=")
                result[k] = v
        return result

    def charge(
        self,
        amount: Decimal,
        currency: str,
        payment_method: Dict[str, Any],
        idempotency_key: str,
        **kwargs: Any,
    ) -> PaymentResult:
        data: Dict[str, Any] = {
            "security_key": str(self.credentials.get("security_key", "")),
            "type": "sale",
            "amount": str(amount),
            "currency": currency.upper(),
            "ccnumber": payment_method.get("card_number", ""),
            "ccexp": "{:02d}{:02d}".format(
                int(payment_method.get("exp_month", 0)),
                int(str(payment_method.get("exp_year", "00"))[-2:]),
            ),
            "cvv": str(payment_method.get("cvv", "")),
            "first_name": payment_method.get("first_name", ""),
            "last_name": payment_method.get("last_name", "") or payment_method.get("name", ""),
            "address1": payment_method.get("address1", ""),
            "city": payment_method.get("city", ""),
            "state": payment_method.get("state", ""),
            "zip": payment_method.get("zip", ""),
            "country": payment_method.get("country", "US"),
            "email": payment_method.get("email", ""),
            "orderid": idempotency_key,
        }
        data.update(kwargs)

        try:
            resp = requests.post(
                _TRANSACTION_URL,
                data=data,
                timeout=30,
            )
            body = self._parse_nmi_response(resp.text)
        except Exception as exc:
            raise DriverError(f"PaymentCloud charge request failed: {exc}") from exc

        # NMI: response=1 approved, response=2 declined, response=3 error
        success = body.get("response") == "1"
        return PaymentResult(
            success=success,
            transaction_id=body.get("transactionid") if success else None,
            amount=amount,
            currency=currency,
            driver_name=self.name,
            raw_response=dict(body),
            error=None if success else (body.get("responsetext") or "Charge declined"),
            error_code=body.get("response_code") if not success else None,
        )

    def refund(
        self,
        transaction_id: str,
        amount: Decimal,
        idempotency_key: str,
        **kwargs: Any,
    ) -> PaymentResult:
        data: Dict[str, Any] = {
            "security_key": str(self.credentials.get("security_key", "")),
            "type": "refund",
            "transactionid": transaction_id,
            "amount": str(amount),
            "orderid": idempotency_key,
        }
        data.update(kwargs)

        try:
            resp = requests.post(
                _TRANSACTION_URL,
                data=data,
                timeout=30,
            )
            body = self._parse_nmi_response(resp.text)
        except Exception as exc:
            raise DriverError(f"PaymentCloud refund request failed: {exc}") from exc

        success = body.get("response") == "1"
        return PaymentResult(
            success=success,
            transaction_id=transaction_id,
            amount=amount,
            currency=kwargs.get("currency", "USD"),
            driver_name=self.name,
            raw_response=dict(body),
            error=None if success else (body.get("responsetext") or "Refund failed"),
        )

    def verify(
        self,
        transaction_id: str,
        **kwargs: Any,
    ) -> PaymentResult:
        data: Dict[str, Any] = {
            "security_key": str(self.credentials.get("security_key", "")),
            "type": "query",
            "transactionid": transaction_id,
        }
        data.update(kwargs)

        try:
            resp = requests.post(
                _TRANSACTION_URL,
                data=data,
                timeout=15,
            )
            body = self._parse_nmi_response(resp.text)
        except Exception as exc:
            raise DriverError(f"PaymentCloud verify request failed: {exc}") from exc

        success = body.get("response") == "1"
        amount_raw = body.get("amount") or "0"
        return PaymentResult(
            success=success,
            transaction_id=transaction_id if success else None,
            amount=Decimal(str(amount_raw)),
            currency=body.get("currency", "USD"),
            driver_name=self.name,
            raw_response=dict(body),
            error=None if success else (body.get("responsetext") or "Transaction not found"),
        )
