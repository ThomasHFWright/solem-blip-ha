"""Shared API exceptions for the Solem BL-IP integration."""

from .ble import APIConnectionError, SolemConnectionError

__all__ = ["APIConnectionError", "SolemConnectionError"]
