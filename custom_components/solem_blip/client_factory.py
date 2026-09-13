"""Solem client construction shared by the coordinator and the config flow."""

from __future__ import annotations

from typing import Any

from solem_blip_ble.client_persistent import PersistentSolemClient
from solem_blip_ble.client_v2 import StatelessSolemClient

from homeassistant.const import CONF_SCAN_INTERVAL

from .const import (
    DEFAULT_SCAN_INTERVAL,
    PERSISTENT_CONNECTION,
    PERSISTENT_HOLD_LINK,
    PERSISTENT_IDLE_RELEASE_FRACTION,
)

__all__ = [
    "PersistentSolemClient",
    "StatelessSolemClient",
    "build_solem_client",
    "create_solem_client",
    "idle_release_seconds",
]


def idle_release_seconds(scan_interval: int) -> int:
    """Return the idle-release window for a persistent connection.

    Hands the BLE radio back at 75% of the scan interval so an overlapping
    poll never stalls, while keeping the link up between consecutive polls.
    """
    return round(scan_interval * PERSISTENT_IDLE_RELEASE_FRACTION)


def create_solem_client(
    persistent: bool,
    scan_interval: int,
    hold_link: bool = False,
    **client_kwargs: Any,
) -> StatelessSolemClient:
    """Select and build the BLE client for the requested connection mode.

    With ``persistent`` enabled, a :class:`PersistentSolemClient` is returned
    (one BLE connection held across operations); with ``hold_link`` it is
    held indefinitely (no idle release), otherwise it is released after 75%
    of the scan interval when idle. Without ``persistent`` the stateless v2
    client that connects per operation is returned.
    """
    if not persistent:
        return StatelessSolemClient(**client_kwargs)
    idle_release = (
        None if hold_link else idle_release_seconds(scan_interval)
    )
    return PersistentSolemClient(
        idle_release_seconds=idle_release,
        **client_kwargs,
    )


def build_solem_client(
    config_entry: Any,
    **client_kwargs: Any,
) -> StatelessSolemClient:
    """Build the BLE client for a config entry from its options."""
    options = config_entry.options
    return create_solem_client(
        bool(options.get(PERSISTENT_CONNECTION, False)),
        int(options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
        bool(options.get(PERSISTENT_HOLD_LINK, False)),
        **client_kwargs,
    )
