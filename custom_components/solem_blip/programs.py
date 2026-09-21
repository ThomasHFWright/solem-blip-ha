"""Durable, revision-checked program transactions shared by every editor."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .ble.client_v2 import StatelessSolemClient
from .ble.snapshot import InvalidSnapshot, ProgramSnapshot, StaleProgram, UncertainWrite


class ProgramManager:
    """Read from the device; never upload cached/default programs at startup."""

    def __init__(self, hass: HomeAssistant, entry_id: str, api: StatelessSolemClient) -> None:
        self.api = api
        self.store: Store[dict[str, Any]] = Store(hass, 1, f"solem_blip.programs.{entry_id}", private=True)
        self.snapshot: ProgramSnapshot | None = None
        self.pending: dict[str, Any] | None = None
        self.last_read: str | None = None
        self.last_write: str | None = None
        self.error: str | None = None

    async def load(self) -> None:
        data = await self.store.async_load() or {}
        self.pending = data.get("pending")
        self.last_read = data.get("last_read")
        self.last_write = data.get("last_write")
        if frames := data.get("frames"):
            try:
                self.snapshot = ProgramSnapshot.from_frames(tuple(bytes.fromhex(frame) for frame in frames))
            except (ValueError, InvalidSnapshot):
                self.error = "Stored snapshot is invalid; refresh the controller"
        if self.pending:
            self.error = "An earlier write is unconfirmed; refresh and reconcile"

    async def _save(self) -> None:
        await self.store.async_save({
            "frames": [frame.hex() for frame in self.snapshot.frames] if self.snapshot else [],
            "pending": self.pending, "last_read": self.last_read, "last_write": self.last_write,
        })

    @property
    def revision(self) -> str | None:
        return self.snapshot.revision if self.snapshot else None

    async def refresh(self, *, accept_current: bool = False) -> ProgramSnapshot:
        async with self.api.transaction():
            try:
                snapshot = await self.api.get_program_snapshot()
            except Exception:
                self.error = "Program refresh failed; displaying last confirmed settings"
                raise
            self.snapshot = snapshot
            self.last_read = datetime.now(timezone.utc).isoformat()
            self.error = None
            pending_before = self.pending
            if self.pending:
                known = (self.pending["before_revision"], self.pending["expected_revision"])
                if accept_current or snapshot.revision in known:
                    self.pending = None
                else:
                    self.error = "Controller differs after an interrupted write; review and accept current settings"
            try:
                await self._save()
            except (Exception, asyncio.CancelledError):
                self.pending = pending_before
                self.error = "Could not persist the latest program read; retry refresh"
                raise
            return snapshot

    async def update(
        self, index: int, changes: dict[str, Any], revision: str,
        *, require_on: bool = False,
    ) -> ProgramSnapshot:
        async with self.api.transaction():
            if self.pending:
                raise UncertainWrite("Refresh and reconcile the previous write first")
            status = await self.api.get_status()
            if status.get("is_watering") is not False or status.get("controller_state") not in ("On", "Off"):
                raise InvalidSnapshot("Controller must report idle before editing programs")
            if require_on and status.get("controller_off_mode") != "on":
                raise InvalidSnapshot("Rainfall adjustment preserves controller OFF and existing delays")
            firmware = await self.api.get_firmware_version()
            if firmware["major"] != 5:
                raise InvalidSnapshot("Only original BL-IP firmware 5.x program writes are supported")
            before = await self.api.get_program_snapshot()
            if not revision or before.revision != revision:
                self.error = "Programs changed; reopen the editor before saving"
                raise StaleProgram(self.error)
            frames, expected = before.patch(index, changes, self.api.max_station_num)
            if not frames:
                self.snapshot = before
                return before
            self.snapshot = before
            self.pending = {
                "before_revision": before.revision, "expected_revision": expected.revision,
                "before_frames": [frame.hex() for frame in before.frames],
                "expected_frames": [frame.hex() for frame in expected.frames],
            }
            # Failure/cancellation here prevents any write and leaves a recovery marker.
            await self._save()
            journal = self.pending
            try:
                actual = await self.api.write_program_frames(frames, expected)
                self.snapshot = actual
                self.last_write = self.last_read = datetime.now(timezone.utc).isoformat()
                self.pending = None
                self.error = None
                await self._save()
                return actual
            except (Exception, asyncio.CancelledError):
                self.pending = journal
                self.error = "Write outcome uncertain; refresh before further edits"
                # The pre-write journal remains on disk even after cancellation/crash.
                raise
