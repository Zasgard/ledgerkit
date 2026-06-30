"""
Gateway configuration for LedgerKit payment drivers.

Usage example::

    from ledgerkit.config import DriverConfig, GatewayConfig
    from ledgerkit.drivers import StripeDriver, SquareDriver, CCBillDriver

    config = GatewayConfig(
        primaries=[
            DriverConfig(
                name="stripe",
                driver_class=StripeDriver,
                credentials={"api_key": "sk_live_..."},
            ),
            DriverConfig(
                name="square",
                driver_class=SquareDriver,
                credentials={"access_token": "...", "location_id": "..."},
            ),
        ],
        fallbacks=[
            DriverConfig(
                name="ccbill",
                driver_class=CCBillDriver,
                credentials={
                    "client_id": "...",
                    "client_secret": "...",
                    "merchant_id": "...",
                    "sub_account": "0000",
                },
            ),
        ],
    )

The gateway tries every *primary* driver in round-robin order.  If all
primaries return a failure result (or raise), it falls through to the
*fallback* drivers in the order they are listed.

Set ``enabled=False`` on a ``DriverConfig`` to temporarily disable that
driver without removing it from the configuration.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Type

from ledgerkit.drivers.base import BasePaymentDriver


@dataclass
class DriverConfig:
    """
    Configuration for a single payment driver instance.

    Attributes:
        name:         Human-readable identifier for this driver entry.
                      Does *not* have to match ``driver_class.name`` (useful when
                      running two instances of the same processor with different
                      credentials, e.g. a domestic and an international Stripe account).
        driver_class: The :class:`~ledgerkit.drivers.base.BasePaymentDriver` subclass.
        credentials:  Keyword arguments forwarded to the driver constructor.
        enabled:      Set to ``False`` to skip this driver without removing it.
    """

    name: str
    driver_class: Type[BasePaymentDriver]
    credentials: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True


@dataclass
class GatewayConfig:
    """
    Top-level gateway routing configuration.

    Attributes:
        primaries:  Drivers tried first in round-robin order.  At least one
                    enabled primary is recommended but not required.
        fallbacks:  Drivers tried in order when *all* primaries have failed.
                    Fallbacks are not rotated; they are tried sequentially.
    """

    primaries: List[DriverConfig] = field(default_factory=list)
    fallbacks: List[DriverConfig] = field(default_factory=list)
