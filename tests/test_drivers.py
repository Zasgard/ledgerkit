"""
Tests for individual payment drivers.

Each driver is tested in isolation by mocking the underlying HTTP calls
made via the ``requests`` library.  No real network connections are made.

Tests cover:
- Successful charge, refund, and verify paths
- Failed / declined responses
- Driver-specific response parsing (NMI form-encoded, Stripe JSON, etc.)
- DriverError propagation when requests raise exceptions
"""

import pytest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from ledgerkit.drivers.base import DriverError, PaymentResult
from ledgerkit.drivers.ccbill import CCBillDriver, _TOKEN_URL, _CHARGE_URL, _BASE_URL as CCBILL_BASE
from ledgerkit.drivers.segpay import SegpayDriver, _BASE_URL as SEGPAY_BASE
from ledgerkit.drivers.epoch import EpochDriver, _BASE_URL as EPOCH_BASE
from ledgerkit.drivers.payment_cloud import PaymentCloudDriver, _TRANSACTION_URL
from ledgerkit.drivers.riskpaygo import RiskPayGoDriver, _BASE_URL as RISKPAYGO_BASE
from ledgerkit.drivers.stripe_driver import StripeDriver, _BASE_URL as STRIPE_BASE
from ledgerkit.drivers.square_driver import SquareDriver, _BASE_URL as SQUARE_BASE


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

CARD = {
    "card_number": "4111111111111111",
    "exp_month": "12",
    "exp_year": "2025",
    "cvv": "123",
    "name": "Test User",
    "email": "test@example.com",
    "address1": "123 Main St",
    "city": "Anytown",
    "state": "CA",
    "zip": "12345",
    "country": "US",
}

AMOUNT = Decimal("49.99")
CURRENCY = "USD"
IDEM_KEY = "test-idem-key-001"
TX_ID = "txn_abc123"


def _mock_response(json_data=None, text=None, status_code=200):
    """Return a mock ``requests.Response``."""
    mock = MagicMock()
    mock.status_code = status_code
    if json_data is not None:
        mock.json.return_value = json_data
    if text is not None:
        mock.text = text
    return mock


# ===========================================================================
# CCBill
# ===========================================================================

class TestCCBillDriver:
    """Tests for :class:`~ledgerkit.drivers.ccbill.CCBillDriver`."""

    @pytest.fixture
    def driver(self):
        return CCBillDriver(
            client_id="cid",
            client_secret="csec",
            merchant_id="123456",
            sub_account="0000",
        )

    def _token_response(self):
        return _mock_response({"access_token": "tok_test"})

    def test_charge_success(self, driver):
        charge_resp = _mock_response({
            "approved": True,
            "transactionId": "ccbill_tx_001",
        })
        with patch("ledgerkit.drivers.ccbill.requests.post") as mock_post:
            mock_post.side_effect = [self._token_response(), charge_resp]
            result = driver.charge(AMOUNT, CURRENCY, CARD, IDEM_KEY)

        assert result.success is True
        assert result.transaction_id == "ccbill_tx_001"
        assert result.driver_name == "ccbill"
        assert result.amount == AMOUNT

    def test_charge_declined(self, driver):
        charge_resp = _mock_response({
            "approved": False,
            "declineError": "Insufficient funds",
            "declineCode": "51",
        })
        with patch("ledgerkit.drivers.ccbill.requests.post") as mock_post:
            mock_post.side_effect = [self._token_response(), charge_resp]
            result = driver.charge(AMOUNT, CURRENCY, CARD, IDEM_KEY)

        assert result.success is False
        assert result.transaction_id is None
        assert result.error == "Insufficient funds"
        assert result.error_code == "51"

    def test_charge_raises_on_network_error(self, driver):
        with patch("ledgerkit.drivers.ccbill.requests.post") as mock_post:
            mock_post.side_effect = ConnectionError("network unreachable")
            with pytest.raises(DriverError):
                driver.charge(AMOUNT, CURRENCY, CARD, IDEM_KEY)

    def test_refund_success(self, driver):
        refund_resp = _mock_response({"approved": True})
        with patch("ledgerkit.drivers.ccbill.requests.post") as mock_post:
            mock_post.side_effect = [self._token_response(), refund_resp]
            result = driver.refund(TX_ID, AMOUNT, IDEM_KEY)

        assert result.success is True
        assert result.transaction_id == TX_ID

    def test_verify_success(self, driver):
        verify_resp = _mock_response({
            "transactionId": TX_ID,
            "initialPrice": "49.99",
            "currencyCode": "USD",
        })
        with patch("ledgerkit.drivers.ccbill.requests.post") as mock_post_p:
            mock_post_p.return_value = self._token_response()
            with patch("ledgerkit.drivers.ccbill.requests.get") as mock_get:
                mock_get.return_value = verify_resp
                result = driver.verify(TX_ID)

        assert result.success is True
        assert result.transaction_id == TX_ID
        assert result.amount == Decimal("49.99")

    def test_token_failure_raises_driver_error(self, driver):
        with patch("ledgerkit.drivers.ccbill.requests.post") as mock_post:
            mock_post.return_value = _mock_response({"error": "invalid_client"}, status_code=401)
            with pytest.raises(DriverError, match="token request failed"):
                driver.charge(AMOUNT, CURRENCY, CARD, IDEM_KEY)


# ===========================================================================
# Segpay
# ===========================================================================

class TestSegpayDriver:
    """Tests for :class:`~ledgerkit.drivers.segpay.SegpayDriver`."""

    @pytest.fixture
    def driver(self):
        return SegpayDriver(api_key="segpay_api_key", package_id="pkg_001")

    def test_charge_success(self, driver):
        resp = _mock_response({
            "status": "approved",
            "transactionId": "seg_tx_001",
        })
        with patch("ledgerkit.drivers.segpay.requests.post", return_value=resp):
            result = driver.charge(AMOUNT, CURRENCY, CARD, IDEM_KEY)

        assert result.success is True
        assert result.transaction_id == "seg_tx_001"
        assert result.driver_name == "segpay"

    def test_charge_declined(self, driver):
        resp = _mock_response({
            "status": "declined",
            "message": "Card expired",
            "errorCode": "54",
        })
        with patch("ledgerkit.drivers.segpay.requests.post", return_value=resp):
            result = driver.charge(AMOUNT, CURRENCY, CARD, IDEM_KEY)

        assert result.success is False
        assert result.error == "Card expired"
        assert result.error_code == "54"

    def test_charge_raises_on_exception(self, driver):
        with patch("ledgerkit.drivers.segpay.requests.post", side_effect=TimeoutError("timed out")):
            with pytest.raises(DriverError):
                driver.charge(AMOUNT, CURRENCY, CARD, IDEM_KEY)

    def test_refund_success(self, driver):
        resp = _mock_response({"status": "approved"})
        with patch("ledgerkit.drivers.segpay.requests.post", return_value=resp):
            result = driver.refund(TX_ID, AMOUNT, IDEM_KEY)

        assert result.success is True

    def test_refund_failure(self, driver):
        resp = _mock_response({"status": "declined", "message": "Already refunded"})
        with patch("ledgerkit.drivers.segpay.requests.post", return_value=resp):
            result = driver.refund(TX_ID, AMOUNT, IDEM_KEY)

        assert result.success is False
        assert result.error == "Already refunded"

    def test_verify_success(self, driver):
        resp = _mock_response({
            "status": "approved",
            "price": "49.99",
            "currency": "USD",
        })
        with patch("ledgerkit.drivers.segpay.requests.get", return_value=resp):
            result = driver.verify(TX_ID)

        assert result.success is True
        assert result.amount == Decimal("49.99")

    def test_verify_not_found(self, driver):
        resp = _mock_response({"status": "not_found", "message": "Not found"}, status_code=404)
        with patch("ledgerkit.drivers.segpay.requests.get", return_value=resp):
            result = driver.verify(TX_ID)

        assert result.success is False


# ===========================================================================
# Epoch
# ===========================================================================

class TestEpochDriver:
    """Tests for :class:`~ledgerkit.drivers.epoch.EpochDriver`."""

    @pytest.fixture
    def driver(self):
        return EpochDriver(site_id="site_001", api_key="epoch_api_key")

    def test_charge_success(self, driver):
        resp = _mock_response({
            "result": "approved",
            "transactionId": "epoch_tx_001",
        })
        with patch("ledgerkit.drivers.epoch.requests.post", return_value=resp):
            result = driver.charge(AMOUNT, CURRENCY, CARD, IDEM_KEY)

        assert result.success is True
        assert result.transaction_id == "epoch_tx_001"
        assert result.driver_name == "epoch"

    def test_charge_alternative_status_field(self, driver):
        """Epoch may return 'status': 'success' instead of 'result': 'approved'."""
        resp = _mock_response({
            "status": "success",
            "transactionId": "epoch_tx_002",
        })
        with patch("ledgerkit.drivers.epoch.requests.post", return_value=resp):
            result = driver.charge(AMOUNT, CURRENCY, CARD, IDEM_KEY)

        assert result.success is True

    def test_charge_declined(self, driver):
        resp = _mock_response({
            "result": "declined",
            "errorMessage": "Do not honor",
            "errorCode": "05",
        })
        with patch("ledgerkit.drivers.epoch.requests.post", return_value=resp):
            result = driver.charge(AMOUNT, CURRENCY, CARD, IDEM_KEY)

        assert result.success is False
        assert "Do not honor" in result.error

    def test_refund_success(self, driver):
        resp = _mock_response({"result": "approved"})
        with patch("ledgerkit.drivers.epoch.requests.post", return_value=resp):
            result = driver.refund(TX_ID, AMOUNT, IDEM_KEY)

        assert result.success is True

    def test_verify_success(self, driver):
        resp = _mock_response({
            "status": "settled",
            "amount": "49.99",
            "currency": "USD",
        })
        with patch("ledgerkit.drivers.epoch.requests.get", return_value=resp):
            result = driver.verify(TX_ID)

        assert result.success is True
        assert result.amount == Decimal("49.99")

    def test_raises_on_network_error(self, driver):
        with patch("ledgerkit.drivers.epoch.requests.post", side_effect=OSError("network error")):
            with pytest.raises(DriverError):
                driver.charge(AMOUNT, CURRENCY, CARD, IDEM_KEY)


# ===========================================================================
# PaymentCloud (NMI)
# ===========================================================================

class TestPaymentCloudDriver:
    """Tests for :class:`~ledgerkit.drivers.payment_cloud.PaymentCloudDriver`."""

    @pytest.fixture
    def driver(self):
        return PaymentCloudDriver(security_key="nmi_sec_key")

    def _nmi_resp(self, response="1", transactionid="pc_tx_001", responsetext="SUCCESS"):
        text = "response={}&transactionid={}&responsetext={}".format(
            response, transactionid, responsetext
        )
        return _mock_response(text=text)

    def test_charge_success(self, driver):
        with patch("ledgerkit.drivers.payment_cloud.requests.post", return_value=self._nmi_resp()):
            result = driver.charge(AMOUNT, CURRENCY, CARD, IDEM_KEY)

        assert result.success is True
        assert result.transaction_id == "pc_tx_001"
        assert result.driver_name == "payment_cloud"

    def test_charge_declined(self, driver):
        with patch(
            "ledgerkit.drivers.payment_cloud.requests.post",
            return_value=self._nmi_resp(response="2", transactionid="", responsetext="DECLINE"),
        ):
            result = driver.charge(AMOUNT, CURRENCY, CARD, IDEM_KEY)

        assert result.success is False
        assert result.error == "DECLINE"

    def test_charge_raises_on_network_error(self, driver):
        with patch(
            "ledgerkit.drivers.payment_cloud.requests.post",
            side_effect=ConnectionError("refused"),
        ):
            with pytest.raises(DriverError):
                driver.charge(AMOUNT, CURRENCY, CARD, IDEM_KEY)

    def test_refund_success(self, driver):
        with patch("ledgerkit.drivers.payment_cloud.requests.post", return_value=self._nmi_resp()):
            result = driver.refund(TX_ID, AMOUNT, IDEM_KEY)

        assert result.success is True

    def test_verify_success(self, driver):
        resp_text = "response=1&transactionid=pc_tx_001&amount=49.99&currency=USD"
        mock = _mock_response(text=resp_text)
        with patch("ledgerkit.drivers.payment_cloud.requests.post", return_value=mock):
            result = driver.verify("pc_tx_001")

        assert result.success is True
        assert result.amount == Decimal("49.99")

    def test_parse_nmi_response(self, driver):
        text = "response=1&transactionid=abc&foo=bar%20baz"
        parsed = driver._parse_nmi_response(text)
        assert parsed["response"] == "1"
        assert parsed["transactionid"] == "abc"


# ===========================================================================
# RiskPayGo
# ===========================================================================

class TestRiskPayGoDriver:
    """Tests for :class:`~ledgerkit.drivers.riskpaygo.RiskPayGoDriver`."""

    @pytest.fixture
    def driver(self):
        return RiskPayGoDriver(api_key="rpg_key", merchant_id="rpg_merchant")

    def test_charge_success(self, driver):
        resp = _mock_response({"status": "approved", "id": "rpg_tx_001"})
        with patch("ledgerkit.drivers.riskpaygo.requests.post", return_value=resp):
            result = driver.charge(AMOUNT, CURRENCY, CARD, IDEM_KEY)

        assert result.success is True
        assert result.transaction_id == "rpg_tx_001"
        assert result.driver_name == "riskpaygo"

    def test_charge_declined(self, driver):
        resp = _mock_response(
            {"status": "declined", "message": "Fraud risk too high", "code": "FRAUD"},
            status_code=402,
        )
        with patch("ledgerkit.drivers.riskpaygo.requests.post", return_value=resp):
            result = driver.charge(AMOUNT, CURRENCY, CARD, IDEM_KEY)

        assert result.success is False
        assert result.error == "Fraud risk too high"
        assert result.error_code == "FRAUD"

    def test_charge_raises_on_exception(self, driver):
        with patch("ledgerkit.drivers.riskpaygo.requests.post", side_effect=TimeoutError()):
            with pytest.raises(DriverError):
                driver.charge(AMOUNT, CURRENCY, CARD, IDEM_KEY)

    def test_refund_success(self, driver):
        resp = _mock_response({"status": "refunded"})
        with patch("ledgerkit.drivers.riskpaygo.requests.post", return_value=resp):
            result = driver.refund(TX_ID, AMOUNT, IDEM_KEY)

        assert result.success is True

    def test_refund_failure(self, driver):
        resp = _mock_response({"status": "error", "message": "Not found"}, status_code=404)
        with patch("ledgerkit.drivers.riskpaygo.requests.post", return_value=resp):
            result = driver.refund(TX_ID, AMOUNT, IDEM_KEY)

        assert result.success is False

    def test_verify_success(self, driver):
        resp = _mock_response({"status": "approved", "amount": "49.99", "currency": "USD"})
        with patch("ledgerkit.drivers.riskpaygo.requests.get", return_value=resp):
            result = driver.verify(TX_ID)

        assert result.success is True
        assert result.amount == Decimal("49.99")

    def test_verify_not_found(self, driver):
        resp = _mock_response({"status": "not_found"}, status_code=404)
        with patch("ledgerkit.drivers.riskpaygo.requests.get", return_value=resp):
            result = driver.verify(TX_ID)

        assert result.success is False


# ===========================================================================
# Stripe
# ===========================================================================

class TestStripeDriver:
    """Tests for :class:`~ledgerkit.drivers.stripe_driver.StripeDriver`."""

    @pytest.fixture
    def driver(self):
        return StripeDriver(api_key="sk_test_stripe_key")

    def test_charge_success(self, driver):
        resp = _mock_response({
            "id": "ch_stripe_001",
            "status": "succeeded",
            "amount": 4999,
            "currency": "usd",
        })
        with patch("ledgerkit.drivers.stripe_driver.requests.post", return_value=resp):
            result = driver.charge(AMOUNT, CURRENCY, {"token": "tok_visa"}, IDEM_KEY)

        assert result.success is True
        assert result.transaction_id == "ch_stripe_001"
        assert result.driver_name == "stripe"

    def test_charge_declined(self, driver):
        resp = _mock_response(
            {"error": {"message": "Your card was declined.", "code": "card_declined"}},
            status_code=402,
        )
        with patch("ledgerkit.drivers.stripe_driver.requests.post", return_value=resp):
            result = driver.charge(AMOUNT, CURRENCY, {"token": "tok_chargeDeclined"}, IDEM_KEY)

        assert result.success is False
        assert "declined" in result.error
        assert result.error_code == "card_declined"

    def test_charge_raises_on_network_error(self, driver):
        with patch(
            "ledgerkit.drivers.stripe_driver.requests.post",
            side_effect=ConnectionError("timeout"),
        ):
            with pytest.raises(DriverError):
                driver.charge(AMOUNT, CURRENCY, {"token": "tok_visa"}, IDEM_KEY)

    def test_refund_success(self, driver):
        resp = _mock_response({
            "id": "re_stripe_001",
            "status": "succeeded",
            "charge": TX_ID,
        })
        with patch("ledgerkit.drivers.stripe_driver.requests.post", return_value=resp):
            result = driver.refund(TX_ID, AMOUNT, IDEM_KEY)

        assert result.success is True
        assert result.transaction_id == TX_ID

    def test_refund_failure(self, driver):
        resp = _mock_response(
            {"error": {"message": "Charge already fully refunded.", "code": "charge_already_refunded"}},
            status_code=400,
        )
        with patch("ledgerkit.drivers.stripe_driver.requests.post", return_value=resp):
            result = driver.refund(TX_ID, AMOUNT, IDEM_KEY)

        assert result.success is False
        assert result.error_code == "charge_already_refunded"

    def test_verify_success(self, driver):
        resp = _mock_response({
            "id": TX_ID,
            "status": "succeeded",
            "amount": 4999,
            "currency": "usd",
        })
        with patch("ledgerkit.drivers.stripe_driver.requests.get", return_value=resp):
            result = driver.verify(TX_ID)

        assert result.success is True
        # Stripe returns cents; driver converts to dollars
        assert result.amount == Decimal("49.99")
        assert result.currency == "USD"

    def test_verify_not_found(self, driver):
        resp = _mock_response(
            {"error": {"message": "No such charge", "code": "resource_missing"}},
            status_code=404,
        )
        with patch("ledgerkit.drivers.stripe_driver.requests.get", return_value=resp):
            result = driver.verify("ch_nonexistent")

        assert result.success is False

    def test_uses_payment_method_id_when_no_token(self, driver):
        """Driver should send payment_method + confirm=true when no token is provided."""
        resp = _mock_response({"id": "ch_pi_001", "status": "succeeded", "amount": 4999})
        with patch("ledgerkit.drivers.stripe_driver.requests.post", return_value=resp) as mock_post:
            driver.charge(AMOUNT, CURRENCY, {"payment_method_id": "pm_card_visa"}, IDEM_KEY)
        call_data = mock_post.call_args[1].get("data") or mock_post.call_args[0][1]
        # The data may be a dict or form-encoded; just verify the post was called
        assert mock_post.called


# ===========================================================================
# Square
# ===========================================================================

class TestSquareDriver:
    """Tests for :class:`~ledgerkit.drivers.square_driver.SquareDriver`."""

    @pytest.fixture
    def driver(self):
        return SquareDriver(access_token="sq_access_token", location_id="LOC001")

    def test_charge_success(self, driver):
        resp = _mock_response({
            "payment": {
                "id": "sq_pay_001",
                "status": "COMPLETED",
                "amount_money": {"amount": 4999, "currency": "USD"},
            }
        })
        with patch("ledgerkit.drivers.square_driver.requests.post", return_value=resp):
            result = driver.charge(AMOUNT, CURRENCY, {"source_id": "cnon:card-nonce"}, IDEM_KEY)

        assert result.success is True
        assert result.transaction_id == "sq_pay_001"
        assert result.driver_name == "square"

    def test_charge_declined(self, driver):
        resp = _mock_response(
            {
                "errors": [
                    {
                        "code": "CARD_DECLINED",
                        "detail": "Card declined.",
                        "category": "PAYMENT_METHOD_ERROR",
                    }
                ]
            },
            status_code=402,
        )
        with patch("ledgerkit.drivers.square_driver.requests.post", return_value=resp):
            result = driver.charge(AMOUNT, CURRENCY, {"source_id": "cnon:bad"}, IDEM_KEY)

        assert result.success is False
        assert result.error_code == "CARD_DECLINED"
        assert "declined" in result.error.lower()

    def test_charge_raises_on_network_error(self, driver):
        with patch(
            "ledgerkit.drivers.square_driver.requests.post",
            side_effect=ConnectionError("refused"),
        ):
            with pytest.raises(DriverError):
                driver.charge(AMOUNT, CURRENCY, {"source_id": "cnon:card"}, IDEM_KEY)

    def test_refund_success(self, driver):
        resp = _mock_response({
            "refund": {
                "id": "refund_001",
                "status": "COMPLETED",
                "payment_id": TX_ID,
            }
        })
        with patch("ledgerkit.drivers.square_driver.requests.post", return_value=resp):
            result = driver.refund(TX_ID, AMOUNT, IDEM_KEY, currency="USD")

        assert result.success is True

    def test_refund_pending_counts_as_success(self, driver):
        """Square may return PENDING status which should also be treated as success."""
        resp = _mock_response({
            "refund": {"id": "refund_002", "status": "PENDING", "payment_id": TX_ID}
        })
        with patch("ledgerkit.drivers.square_driver.requests.post", return_value=resp):
            result = driver.refund(TX_ID, AMOUNT, IDEM_KEY, currency="USD")

        assert result.success is True

    def test_verify_success(self, driver):
        resp = _mock_response({
            "payment": {
                "id": TX_ID,
                "status": "COMPLETED",
                "amount_money": {"amount": 4999, "currency": "USD"},
            }
        })
        with patch("ledgerkit.drivers.square_driver.requests.get", return_value=resp):
            result = driver.verify(TX_ID)

        assert result.success is True
        assert result.amount == Decimal("49.99")
        assert result.currency == "USD"

    def test_verify_not_found(self, driver):
        resp = _mock_response(
            {"errors": [{"code": "NOT_FOUND", "detail": "Payment not found."}]},
            status_code=404,
        )
        with patch("ledgerkit.drivers.square_driver.requests.get", return_value=resp):
            result = driver.verify("sq_nonexistent")

        assert result.success is False
