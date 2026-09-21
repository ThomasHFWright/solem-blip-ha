"""One task-owned transaction lock per controller, shared by all clients."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from typing import Any
from weakref import WeakValueDictionary


class ControllerLock:
    """Reentrant for the owning task, including read/write/read transactions."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.quarantined = False
        self._owner: asyncio.Task[Any] | None = None

    @asynccontextmanager
    async def hold(self) -> AsyncIterator[None]:
        if self.quarantined:
            raise RuntimeError("Previous Bluetooth cleanup failed; reload after checking the connection")
        task = asyncio.current_task()
        if task is self._owner:
            yield
            return
        async with self._lock:
            if self.quarantined:
                raise RuntimeError("Previous Bluetooth cleanup failed; reload after checking the connection")
            self._owner = task
            try:
                yield
            finally:
                self._owner = None


_LOCKS: WeakValueDictionary[str, ControllerLock] = WeakValueDictionary()


def controller_lock(address: str) -> ControllerLock:
    """Clients retain the lock; unused controller locks are garbage collected."""
    address = address.upper()
    lock = _LOCKS.get(address)
    if lock is None:
        _LOCKS[address] = lock = ControllerLock()
    return lock
