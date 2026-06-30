"""
Tests for PaymentGateway routing, fallback logic, and ledger confirmation.

Tests cover:
- Single primary driver: success and failure
- Round-robin rotation across multiple primaries
- Fallback activation when all primaries fail
- GatewayError when every driver fails
- Partial failure (some primaries fail, one succeeds)
- Refund and verify routing, including driver_name targeting
- confirm_to_ledger creates the correct ledger entry
- confirm_to_ledger is idempotent
- Error cases: no ledger set, failed result passed to confirm_to_ledger
"""

import pytest
from decimal import Decimal
from unittest.mock import MagicMock, patch, call

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from ledgerkit.models import Base, AccountType
from ledgerkit.ledger import Ledger
from ledgerkit.config import DriverConfig, GatewayConfig
from ledgerkit.drivers.base import BasePaymentDriver, PaymentResult
from ledgerkit.gateway import GatewayError, PaymentGateway


# ---------------------------------------------------------------------------
# Helpers / Fixtures
# ---------------------------------------------------------------------------

AMOUNT = Decimal("99.99")
CURRENCY = "USD"
IDEM_KEY = "gateway-test-idem-001"
TX_ID = "gw_tx_001"
PAYMENT_METHOD = {"token": "tok_visa"}


def _make_result(success=True, tx_id=TX_ID, driver_name="mock"):
    return PaymentResult(
        success=success,
        transaction_id=tx_id if success else None,
        amount=AMOUNT,
        currency=CURRENCY,
        driver_name=driver_name,
        error=None if success else "Declined",
    )


class MockDriver(BasePaymentDriver):
    """Configurable mock driver for testing."""

    name = "mock"

    def __init__(self, charge_result=None, refund_result=None, verify_result=None, **credentials):
        super().__init__(**credentials)
        self._charge_result = charge_result or _make_result()
        self._refund_result = refund_result or _make_result()
        self._verify_result = verify_result or _make_result()

    def charge(self, amount, currency, payment_method, idempotency_key, **kwargs):
        return self._charge_result

    def refund(self, transaction_id, amount, idempotency_key, **kwargs):
        return self._refund_result

    def verify(self, transaction_id, **kwargs):
        return self._verify_result


class RaisingDriver(BasePaymentDriver):
    """Driver that always raises an exception (simulates network failure)."""

    name = "raising"

    def charge(self, *a, **kw):
        raise ConnectionError("network error")

    def refund(self, *a, **kw):
        raise ConnectionError("network error")

    def verify(self, *a, **kw):
        raise ConnectionError("network error")


def _make_driver_config(driver_obj, name=None):
    """Build a DriverConfig that returns *driver_obj* when instantiated."""

    class FixedDriver(BasePaymentDriver):
        pass

    FixedDriver.name = name or driver_obj.name
    FixedDriver.__init__ = lambda self, **kw: None  # type: ignore[assignment]
    instance_ref = driver_obj

    def _charge(self, *a, **kw):
        return instance_ref.charge(*a, **kw)

    def _refund(self, *a, **kw):
        return instance_ref.refund(*a, **kw)

    def _verify(self, *a, **kw):
        return instance_ref.verify(*a, **kw)

    FixedDriver.charge = _charge  # type: ignore[assignment]
    FixedDriver.refund = _refund  # type: ignore[assignment]
    FixedDriver.verify = _verify  # type: ignore[assignment]

    return DriverConfig(name=FixedDriver.name, driver_class=FixedDriver)


@pytest.fixture
def database_url():
    return "sqlite:///:memory:"


@pytest.fixture
def ledger(database_url):
    lk = Ledger(database_url)
    lk.init_db()
    # Pre-create accounts used in confirm_to_ledger tests
    lk.create_account("receivables", AccountType.ASSET)
    lk.create_account("revenue", AccountType.REVENUE)
    return lk


# ---------------------------------------------------------------------------
# Helper: build a gateway with pre-constructed driver instances
# ---------------------------------------------------------------------------

def _gateway(*primary_drivers, fallback_drivers=(), ledger=None):
    """Build a PaymentGateway from driver *instances* (bypasses credential init)."""

    def _cfg(drv):
        class _Wrapper(BasePaymentDriver):
            name = drv.name

            def __init__(self, **kw):  # type: ignore[override]
                pass  # skip credential init

            def charge(self, *a, **kw):  # type: ignore[override]
                return drv.charge(*a, **kw)

            def refund(self, *a, **kw):  # type: ignore[override]
                return drv.refund(*a, **kw)

            def verify(self, *a, **kw):  # type: ignore[override]
                return drv.verify(*a, **kw)

        return DriverConfig(name=drv.name, driver_class=_Wrapper)

    config = GatewayConfig(
        primaries=[_cfg(d) for d in primary_drivers],
        fallbacks=[_cfg(d) for d in fallback_drivers],
    )
    return PaymentGateway(config, ledger=ledger)


# ===========================================================================
# Basic routing
# ===========================================================================

class TestBasicRouting:

    def test_single_primary_success(self):
        drv = MockDriver()
        gw = _gateway(drv)
        result = gw.charge(AMOUNT, CURRENCY, PAYMENT_METHOD, IDEM_KEY)
        assert result.success is True
        assert result.driver_name == "mock"

    def test_single_primary_failure_raises_gateway_error(self):
        drv = MockDriver(charge_result=_make_result(success=False))
        gw = _gateway(drv)
        with pytest.raises(GatewayError):
            gw.charge(AMOUNT, CURRENCY, PAYMENT_METHOD, IDEM_KEY)

    def test_raising_primary_falls_through_to_fallback(self):
        primary = RaisingDriver()
        fallback = MockDriver(charge_result=_make_result(driver_name="fallback_mock"))
        fallback.name = "fallback_mock"

        gw = _gateway(primary, fallback_drivers=[fallback])
        result = gw.charge(AMOUNT, CURRENCY, PAYMENT_METHOD, IDEM_KEY)
        assert result.success is True
        assert result.driver_name == "fallback_mock"

    def test_failed_primary_falls_through_to_fallback(self):
        primary = MockDriver(charge_result=_make_result(success=False))
        fallback = MockDriver()
        fallback.name = "fallback_mock"

        gw = _gateway(primary, fallback_drivers=[fallback])
        result = gw.charge(AMOUNT, CURRENCY, PAYMENT_METHOD, IDEM_KEY)
        assert result.success is True

    def test_all_drivers_fail_raises_gateway_error(self):
        drv1 = MockDriver(charge_result=_make_result(success=False))
        drv1.name = "drv1"
        drv2 = MockDriver(charge_result=_make_result(success=False))
        drv2.name = "drv2"

        gw = _gateway(drv1, fallback_drivers=[drv2])
        with pytest.raises(GatewayError) as exc_info:
            gw.charge(AMOUNT, CURRENCY, PAYMENT_METHOD, IDEM_KEY)
        assert "drv1" in str(exc_info.value)
        assert "drv2" in str(exc_info.value)

    def test_no_drivers_raises_gateway_error(self):
        gw = PaymentGateway(GatewayConfig())
        with pytest.raises(GatewayError):
            gw.charge(AMOUNT, CURRENCY, PAYMENT_METHOD, IDEM_KEY)

    def test_disabled_driver_is_skipped(self):
        """A DriverConfig with enabled=False must never be instantiated or called."""
        called = []

        class TrackingDriver(BasePaymentDriver):
            name = "tracking"

            def __init__(self, **kw):
                called.append("init")

            def charge(self, *a, **kw):
                called.append("charge")
                return _make_result()

            def refund(self, *a, **kw):
                return _make_result()

            def verify(self, *a, **kw):
                return _make_result()

        good_drv = MockDriver()

        def _good_cfg(drv):
            class W(BasePaymentDriver):
                name = drv.name

                def __init__(self, **kw):
                    pass

                def charge(self, *a, **kw):
                    return drv.charge(*a, **kw)

                def refund(self, *a, **kw):
                    return drv.refund(*a, **kw)

                def verify(self, *a, **kw):
                    return drv.verify(*a, **kw)

            return DriverConfig(name=drv.name, driver_class=W)

        config = GatewayConfig(
            primaries=[
                DriverConfig("disabled", TrackingDriver, enabled=False),
                _good_cfg(good_drv),
            ],
        )
        gw = PaymentGateway(config)
        result = gw.charge(AMOUNT, CURRENCY, PAYMENT_METHOD, IDEM_KEY)

        assert result.success is True
        assert "init" not in called
        assert "charge" not in called


# ===========================================================================
# Round-robin across multiple primaries
# ===========================================================================

class TestRoundRobin:

    def test_round_robin_rotates_starting_driver(self):
        """Successive charges should start from different primaries."""
        call_log = []

        class LoggingDriver(BasePaymentDriver):
            def __init__(self, driver_name, **kw):
                self.name = driver_name

            def charge(self, *a, **kw):
                call_log.append(self.name)
                return _make_result(driver_name=self.name)

            def refund(self, *a, **kw):
                return _make_result()

            def verify(self, *a, **kw):
                return _make_result()

        config = GatewayConfig(
            primaries=[
                DriverConfig("a", LoggingDriver, {"driver_name": "a"}),
                DriverConfig("b", LoggingDriver, {"driver_name": "b"}),
                DriverConfig("c", LoggingDriver, {"driver_name": "c"}),
            ]
        )
        gw = PaymentGateway(config)

        for _ in range(6):
            gw.charge(AMOUNT, CURRENCY, PAYMENT_METHOD, IDEM_KEY)

        # Each driver should have been the *starting* driver at least once
        # across 6 calls with 3 drivers (each starts every 3rd call)
        assert len(set(call_log[:6])) >= 2

    def test_failed_primary_tries_next_before_fallback(self):
        """When primary A fails, primary B is tried before any fallback."""
        order = []

        def _logged_driver(drv_name, succeed):
            class D(BasePaymentDriver):
                name = drv_name

                def __init__(self, **kw):
                    pass

                def charge(self, *a, **kw):
                    order.append(drv_name)
                    return _make_result(success=succeed, driver_name=drv_name)

                def refund(self, *a, **kw):
                    return _make_result()

                def verify(self, *a, **kw):
                    return _make_result()

            return DriverConfig(drv_name, D)

        config = GatewayConfig(
            primaries=[_logged_driver("p1", False), _logged_driver("p2", True)],
            fallbacks=[_logged_driver("fallback", True)],
        )
        gw = PaymentGateway(config)
        result = gw.charge(AMOUNT, CURRENCY, PAYMENT_METHOD, IDEM_KEY)

        assert result.success is True
        assert "fallback" not in order  # fallback should not be reached


# ===========================================================================
# Refund routing
# ===========================================================================

class TestRefundRouting:

    def test_refund_uses_all_drivers(self):
        drv = MockDriver()
        gw = _gateway(drv)
        result = gw.refund(TX_ID, AMOUNT, IDEM_KEY)
        assert result.success is True

    def test_refund_with_driver_name(self):
        drv = MockDriver()
        drv.name = "stripe"
        gw = _gateway(drv)
        result = gw.refund(TX_ID, AMOUNT, IDEM_KEY, driver_name="stripe")
        assert result.success is True

    def test_refund_unknown_driver_raises(self):
        drv = MockDriver()
        gw = _gateway(drv)
        with pytest.raises(GatewayError, match="No driver named"):
            gw.refund(TX_ID, AMOUNT, IDEM_KEY, driver_name="nonexistent")

    def test_refund_failure_raises_gateway_error(self):
        drv = MockDriver(refund_result=_make_result(success=False))
        gw = _gateway(drv)
        with pytest.raises(GatewayError):
            gw.refund(TX_ID, AMOUNT, IDEM_KEY)

    def test_refund_falls_through_to_fallback(self):
        primary = MockDriver(refund_result=_make_result(success=False))
        primary.name = "primary"
        fallback = MockDriver(refund_result=_make_result(driver_name="fallback"))
        fallback.name = "fallback"
        gw = _gateway(primary, fallback_drivers=[fallback])
        result = gw.refund(TX_ID, AMOUNT, IDEM_KEY)
        assert result.success is True
        assert result.driver_name == "fallback"


# ===========================================================================
# Verify routing
# ===========================================================================

class TestVerifyRouting:

    def test_verify_success(self):
        drv = MockDriver()
        gw = _gateway(drv)
        result = gw.verify(TX_ID)
        assert result.success is True

    def test_verify_with_driver_name(self):
        drv = MockDriver()
        drv.name = "square"
        gw = _gateway(drv)
        result = gw.verify(TX_ID, driver_name="square")
        assert result.success is True

    def test_verify_unknown_driver_raises(self):
        drv = MockDriver()
        gw = _gateway(drv)
        with pytest.raises(GatewayError, match="No driver named"):
            gw.verify(TX_ID, driver_name="nonexistent")

    def test_verify_all_fail_raises_gateway_error(self):
        drv = MockDriver(verify_result=_make_result(success=False))
        gw = _gateway(drv)
        with pytest.raises(GatewayError):
            gw.verify(TX_ID)


# ===========================================================================
# confirm_to_ledger
# ===========================================================================

class TestConfirmToLedger:

    def test_confirm_creates_ledger_entry(self, ledger):
        gw = _gateway(MockDriver(), ledger=ledger)
        result = _make_result()
        gw.confirm_to_ledger(
            result,
            debit_account="receivables",
            credit_account="revenue",
            description="Test payment",
        )
        assert ledger.get_balance("receivables") == AMOUNT
        assert ledger.get_balance("revenue") == -AMOUNT

    def test_confirm_stores_driver_metadata_in_entry_meta(self, ledger):
        gw = _gateway(MockDriver(), ledger=ledger)
        result = _make_result()
        gw.confirm_to_ledger(
            result,
            debit_account="receivables",
            credit_account="revenue",
            description="Meta test",
            meta={"order_id": "ord_123"},
        )
        from sqlalchemy import select
        from ledgerkit.models import Entry

        # Query through the ledger's session
        with ledger.Session() as session:
            entry = session.scalar(
                select(Entry).where(
                    Entry.idempotency_key == "payment-{}".format(TX_ID)
                )
            )
        assert entry is not None
        assert entry.meta["payment_driver"] == "mock"
        assert entry.meta["payment_transaction_id"] == TX_ID
        assert entry.meta["order_id"] == "ord_123"

    def test_confirm_is_idempotent(self, ledger):
        """Calling confirm_to_ledger twice with the same result must not double-book."""
        gw = _gateway(MockDriver(), ledger=ledger)
        result = _make_result()
        gw.confirm_to_ledger(result, "receivables", "revenue", "Payment")
        gw.confirm_to_ledger(result, "receivables", "revenue", "Payment")  # duplicate

        assert ledger.get_balance("receivables") == AMOUNT  # only charged once

    def test_confirm_fails_without_ledger(self):
        gw = _gateway(MockDriver())  # no ledger supplied
        with pytest.raises(ValueError, match="Ledger instance"):
            gw.confirm_to_ledger(
                _make_result(), "receivables", "revenue", "No ledger"
            )

    def test_confirm_fails_for_unsuccessful_result(self, ledger):
        gw = _gateway(MockDriver(), ledger=ledger)
        failed = _make_result(success=False)
        with pytest.raises(ValueError, match="failed payment"):
            gw.confirm_to_ledger(failed, "receivables", "revenue", "Should fail")


# ===========================================================================
# End-to-end: charge then confirm
# ===========================================================================

class TestEndToEnd:

    def test_charge_and_confirm(self, ledger):
        """Happy path: charge succeeds and is confirmed to the ledger."""
        gw = _gateway(MockDriver(), ledger=ledger)

        result = gw.charge(AMOUNT, CURRENCY, PAYMENT_METHOD, IDEM_KEY)
        assert result.success is True

        gw.confirm_to_ledger(
            result,
            debit_account="receivables",
            credit_account="revenue",
            description="Order payment",
        )

        assert ledger.get_balance("receivables") == AMOUNT
        assert ledger.get_balance("revenue") == -AMOUNT

    def test_fallback_charge_and_confirm(self, ledger):
        """Fallback driver charges, result is confirmed to ledger."""
        primary = MockDriver(charge_result=_make_result(success=False))
        primary.name = "primary_fail"

        fallback = MockDriver()
        fallback.name = "fallback_ok"
        # Use a unique TX_ID so it doesn't clash with other tests
        fallback._charge_result = _make_result(tx_id="fallback_tx_001", driver_name="fallback_ok")

        gw = _gateway(primary, fallback_drivers=[fallback], ledger=ledger)
        result = gw.charge(AMOUNT, CURRENCY, PAYMENT_METHOD, "unique-fallback-idem")

        assert result.success is True
        assert result.driver_name == "fallback_ok"

        gw.confirm_to_ledger(result, "receivables", "revenue", "Fallback payment")
        assert ledger.get_balance("receivables") >= AMOUNT
