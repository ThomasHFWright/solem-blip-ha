"""Bundled Solem BLE protocol and client (see NOTICE.md)."""

from .exceptions import SolemConnectionError
from .protocol import IrrigationProgram, parse_status_notification

APIConnectionError = SolemConnectionError
