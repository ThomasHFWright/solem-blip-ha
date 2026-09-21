"""Durable station renames, independent of HA entity labels and programs."""
import asyncio
from datetime import datetime, timezone
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .ble.client_v2 import StatelessSolemClient
from .ble.snapshot import InvalidSnapshot, StaleProgram, UncertainWrite
from .ble.station_names import StationNameSnapshot, pack_station_name


class StationNameManager:
    """Never replay a name write whose outcome is uncertain."""

    def __init__(self, hass: HomeAssistant, entry_id: str, api: StatelessSolemClient) -> None:
        self.api = api
        self.store: Store[dict[str, Any]] = Store(hass, 1, f"solem_blip.station_names.{entry_id}", private=True)
        self.snapshot: StationNameSnapshot | None = None
        self.pending: dict[str, Any] | None = None
        self.last_write: str | None = None

    async def load(self) -> None:
        data = await self.store.async_load() or {}
        self.pending = data.get("pending")
        self.last_write = data.get("last_write")

    async def _save(self) -> None:
        await self.store.async_save({"pending": self.pending, "last_write": self.last_write})

    async def refresh(self, *, accept_current: bool = False) -> StationNameSnapshot:
        async with self.api.transaction():
            self.snapshot = await self.api.get_station_name_snapshot()
            if self.pending and (accept_current or self.snapshot.revision in (
                self.pending["before_revision"], self.pending["expected_revision"],
            )):
                pending = self.pending
                self.pending = None
                try:
                    await self._save()
                except (Exception, asyncio.CancelledError):
                    self.pending = pending
                    raise
            return self.snapshot

    async def update(self, station: int, name: str, revision: str) -> StationNameSnapshot:
        pack_station_name(station, name, self.api.max_station_num)
        async with self.api.transaction():
            if self.pending:
                raise UncertainWrite("Review the current station names before saving again")
            status = await self.api.get_status()
            if status.get("is_watering") is not False or status.get("controller_state") not in ("On", "Off"):
                raise InvalidSnapshot("Wait until the controller reports idle")
            if (await self.api.get_firmware_version())["major"] != 5:
                raise InvalidSnapshot("Onboard station renaming requires BL-IP firmware 5.x")
            before = await self.api.get_station_name_snapshot()
            self.snapshot = before
            if before.revision != revision:
                raise StaleProgram("Station names changed; reopen the editor")
            if before.names[station] == name:
                return before
            expected = before.renamed(station, name, self.api.max_station_num)
            self.pending = {
                "before_revision": before.revision, "expected_revision": expected.revision,
                "before_names": {str(k): v.hex() for k, v in before.raw_names.items()},
                "expected_names": {str(k): v.hex() for k, v in expected.raw_names.items()},
            }
            await self._save()
            journal = self.pending
            try:
                self.snapshot = await self.api.write_station_name(station, name, expected, before=before)
                self.last_write = datetime.now(timezone.utc).isoformat()
                self.pending = None
                await self._save()
                return self.snapshot
            except (Exception, asyncio.CancelledError):
                self.pending = journal
                raise
