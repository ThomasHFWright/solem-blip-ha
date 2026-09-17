"""Build the bundled connect-per-operation client; never retain the radio."""

from __future__ import annotations
from typing import Any
from .ble.client_v2 import StatelessSolemClient


def create_solem_client(
    persistent: bool = False, scan_interval: int = 120, hold_link: bool = False,
    **client_kwargs: Any,
) -> StatelessSolemClient:
    """Legacy connection options are ignored; all sessions release promptly."""
    return StatelessSolemClient(**client_kwargs)


def build_solem_client(config_entry: Any, **client_kwargs: Any) -> StatelessSolemClient:
    return create_solem_client(**client_kwargs)
