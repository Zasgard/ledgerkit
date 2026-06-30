"""
CCBill payment driver.

Uses the CCBill REST API (v2) to process transactions.
Credentials required:
    client_id     – OAuth2 client ID
    client_secret – OAuth2 client secret
    merchant_id   – Merchant account number
    sub_account   – Sub-account number (default "0000")

API reference: https://ccbill.com/doc/ccbill-rest-api
"""

from decimal import Decimal
from typing import Any, Dict, Optional

import requests

from ledgerkit.drivers.base import BasePaymentDriver, DriverError, PaymentResult

_BASE_URL = "https://api.ccbill.com"
_TOKEN_URL = f"{_BASE_URL}/ccbill-auth/oauth/token"
_CHARGE_URL = f"{_BASE_URL}/transactions/charge"


class CCBillDriver(BasePaymentDriver):
    """CCBill REST API driver."""

    name = "ccbill"

    # ---------- helpers ----------

    def _get_token(self) -> str:
        """Exchange client credentials for an OAuth2 bearer token."""
        resp = requests.post(
            _TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": self.credentials.get("client_id", ""),
                "client_secret": self.credentials.get("client_secret", ""),
            },
            timeout=15,
        )
        body = resp.json()
        if resp.status_code != 200 or "access_token" not in body:
            raise DriverError(f"CCBill token request failed: {body}")
        return str(body["access_token"])

    def _headers(self) -> Dict[str, str]:
        token = self._get_token()
        return {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ---------- interface ----------

    def charge(
        self,
        amount: Decimal,
        currency: str,
        payment_method: Dict[str, Any],
        idempotency_key: str,
        **kwargs: Any,
    ) -> PaymentResult:
        payload: Dict[str, Any] = {
            "clientAccnum": str(self.credentials.get("merchant_id", "")),
            "clientSubacc": str(self.credentials.get("sub_account", "0000")),
            "currencyCode": currency.upper(),
            "initialPrice": str(amount),
            "initialPeriod": 30,
            "cardNum": payment_method.get("card_number", ""),
            "cardMonth": str(payment_method.get("exp_month", "")),
            "cardYear": str(payment_method.get("exp_year", "")),
            "cardCvv2": str(payment_method.get("cvv", "")),
            "nameOnCard": payment_method.get("name", ""),
            "address1": payment_method.get("address1", ""),
            "city": payment_method.get("city", ""),
            "state": payment_method.get("state", ""),
            "zipCode": payment_method.get("zip", ""),
            "country": payment_method.get("country", "US"),
            "email": payment_method.get("email", ""),
            "ipAddress": payment_method.get("ip_address", ""),
        }
        payload.update(kwargs)

        try:
            resp = requests.post(
                _CHARGE_URL,
                json=payload,
                headers=self._headers(),
                timeout=30,
            )
            body: Dict[str, Any] = resp.json()
        except Exception as exc:
            raise DriverError(f"CCBill charge request failed: {exc}") from exc

        approved = body.get("approved") is True or body.get("approved") == "1"
        if resp.status_code in (200, 201) and approved:
            return PaymentResult(
                success=True,
                transaction_id=str(body.get("transactionId", "")),
                amount=amount,
                currency=currency,
                driver_name=self.name,
                raw_response=body,
            )
        return PaymentResult(
            success=False,
            transaction_id=None,
            amount=amount,
            currency=currency,
            driver_name=self.name,
            raw_response=body,
            error=body.get("declineError") or body.get("reason") or "Charge declined",
            error_code=str(body.get("declineCode", "")),
        )

    def refund(
        self,
        transaction_id: str,
        amount: Decimal,
        idempotency_key: str,
        **kwargs: Any,
    ) -> PaymentResult:
        url = f"{_BASE_URL}/transactions/{transaction_id}/void-or-refund"
        payload: Dict[str, Any] = {"amount": str(amount)}
        payload.update(kwargs)

        try:
            resp = requests.post(
                url,
                json=payload,
                headers=self._headers(),
                timeout=30,
            )
            body = resp.json()
        except Exception as exc:
            raise DriverError(f"CCBill refund request failed: {exc}") from exc

        success = resp.status_code in (200, 201) and (
            body.get("approved") is True or body.get("approved") == "1"
        )
        return PaymentResult(
            success=success,
            transaction_id=transaction_id,
            amount=amount,
            currency=kwargs.get("currency", "USD"),
            driver_name=self.name,
            raw_response=body,
            error=None if success else (body.get("reason") or "Refund failed"),
        )

    def verify(
        self,
        transaction_id: str,
        **kwargs: Any,
    ) -> PaymentResult:
        url = f"{_BASE_URL}/transactions/{transaction_id}"
        try:
            resp = requests.get(url, headers=self._headers(), timeout=15)
            body = resp.json()
        except Exception as exc:
            raise DriverError(f"CCBill verify request failed: {exc}") from exc

        success = resp.status_code == 200 and bool(body.get("transactionId"))
        amount_raw = body.get("initialPrice") or body.get("amount") or "0"
        return PaymentResult(
            success=success,
            transaction_id=transaction_id if success else None,
            amount=Decimal(str(amount_raw)),
            currency=body.get("currencyCode", "USD"),
            driver_name=self.name,
            raw_response=body,
            error=None if success else (body.get("reason") or "Transaction not found"),
        )
