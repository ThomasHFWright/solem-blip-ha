"""One task-owned transaction lock per controller, shared by all clients."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from weakref import WeakValueDictionary

from .exceptions import SolemConnectionError


class ControllerLock:
    """Serialize a controller and retry unresolved cleanup before reuse."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.recover: Callable[[], Awaitable[bool]] | None = None
        self._owner: asyncio.Task[Any] | None = None

    def defer_cleanup(self, recover: Callable[[], Awaitable[bool]]) -> None:
        """Retain unresolved cleanup across client replacement and reloads."""
        self.recover = recover
        _RECOVERING.add(self)

    async def check_cleanup(self) -> None:
        """Retry deferred cleanup before allowing another BLE operation."""
        if self.recover is None:
            return
        if not await self.recover():
            raise SolemConnectionError(
                "Previous Bluetooth cleanup still pending; will retry automatically"
            )
        self.recover = None
        _RECOVERING.discard(self)

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        """Hold the controller lock, re-entering from the owning task safely."""
        task = asyncio.current_task()
        if task is self._owner:
            await self.check_cleanup()
            yield
            return
        async with self._lock:
            await self.check_cleanup()
            self._owner = task
            try:
                yield
            finally:
                self._owner = None


_LOCKS: WeakValueDictionary[str, ControllerLock] = WeakValueDictionary()
_RECOVERING: set[ControllerLock] = set()


def controller_lock(address: str) -> ControllerLock:
    """Return the process-wide lock for one controller address."""
    address = address.upper()
    lock = _LOCKS.get(address)
    if lock is None:
        _LOCKS[address] = lock = ControllerLock()
    return lock
