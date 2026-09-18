"""Observe watering runs without scheduling or issuing controller commands."""
from __future__ import annotations

from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, AsyncIterator

from homeassistant.components.logbook import async_log_entry
from homeassistant.core import Context
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .schedule import next_start_datetime
from .util import format_entity_unique_id

if TYPE_CHECKING:
    from .coordinator import SolemCoordinator

HA_SOURCE = "Manual Home Assistant"
SCHEDULE_SOURCE = "Scheduled"
BLUETOOTH_SOURCE = "Manual Bluetooth"
UNKNOWN_SOURCE = "Unknown"
HISTORY_LIMIT = 30


class WateringActivity:
    """Bounded persistent observations; inferred sources never identify a person."""

    def __init__(self, coordinator: SolemCoordinator) -> None:
        self.c = coordinator
        assert coordinator.config_entry is not None
        self.store: Store[dict[str, Any]] = Store(
            coordinator.hass, 1, f"solem_blip.activity.{coordinator.config_entry.entry_id}", private=True
        )
        self.current: dict[str, Any] | None = None
        self.history: list[dict[str, Any]] = []
        self.pending: dict[str, Any] | None = None
        self.last_seen: datetime | None = None
        self.last_revision: str | None = None

    @property
    def state(self) -> dict[str, Any]:
        return deepcopy({"current": self.current, "history": self.history})

    def _stored(self) -> dict[str, Any]:
        return {**self.state, "pending": deepcopy(self.pending)}

    def _save(self) -> None:
        self.store.async_delay_save(self._stored, 1)

    async def load(self) -> None:
        data = await self.store.async_load() or {}
        self.history = data.get("history", [])[:HISTORY_LIMIT]
        self.current = data.get("current")
        self.pending = data.get("pending")
        if self.pending:
            self.pending["confirmed"] = False
        # A restart cannot establish whether a run continued or stopped offline.
        if self.current:
            self._finish("Observation interrupted", None)

    async def shutdown(self) -> None:
        if self.current:
            self._finish("Observation interrupted", None)
        await self.store.async_save(self._stored())

    @asynccontextmanager
    async def command(self, *, station: int | None = None, program: int | None = None,
                      context: Context | None = None) -> AsyncIterator[None]:
        """Journal HA intent before sending; acknowledge only successful commands."""
        async with self.c.api.transaction():
            actor = None
            if context and context.user_id:
                user = await self.c.hass.auth.async_get_user(context.user_id)
                actor = user.name if user else None
            self.pending = {
                "at": dt_util.utcnow().isoformat(), "station": station, "program": program,
                "confirmed": False, "user_id": context.user_id if context else None,
                "actor": actor, "context_id": context.id if context else None,
                "parent_id": context.parent_id if context else None,
            }
            await self.store.async_save(self._stored())
            # Failed/cancelled commands stay uncertain, never attributed externally.
            yield
            self.pending["confirmed"] = True
            self._save()

    def _log(self, message: str, run: dict[str, Any]) -> None:
        entity_id = er.async_get(self.c.hass).async_get_entity_id(
            "sensor", DOMAIN,
            format_entity_unique_id(self.c.controller_mac_address, self.c.controller.device_id),
        )
        async_log_entry(self.c.hass, "Watering activity", message, DOMAIN, entity_id,
                        Context(user_id=run.get("user_id"), parent_id=run.get("context_id")))

    def _finish(self, outcome: str, now: datetime | None) -> None:
        assert self.current is not None
        self.current.update(outcome=outcome, finished_detected=now.isoformat() if now else None)
        self.history.insert(0, self.current)
        del self.history[HISTORY_LIMIT:]
        self._log(f"{outcome}: {self.current['source']}", self.current)
        self.current = None
        self._save()

    def _classify(self, status: dict[str, Any], now: datetime, continuous: bool,
                  intent: dict[str, Any] | None) -> str:
        if intent:
            if intent["confirmed"]:
                return HA_SOURCE
            return UNKNOWN_SOURCE
        if not continuous:
            return UNKNOWN_SOURCE
        program_num = status.get("active_program")
        if not program_num:
            if status.get("watering_origin") == "manual":
                return BLUETOOTH_SOURCE
            return UNKNOWN_SOURCE
        program = self.c.irrigation_programs.get(program_num - 1)
        read_at = dt_util.parse_datetime(self.c.program_manager.last_read or "")
        if (not program or program["cycle"] == 4 or status.get("time_alarm")
            or self.c.program_manager.pending or self.last_revision != self.c.program_manager.revision
            or read_at is None or not 0 <= (now - read_at).total_seconds() <= 7200):
            return UNKNOWN_SOURCE
        assert self.last_seen is not None
        # Compare start slots in HA's local timezone across the observed transition.
        # A small clock tolerance is included; matching is evidence, not proof.
        candidate = next_start_datetime(program, dt_util.as_local(self.last_seen - timedelta(seconds=30)))
        if candidate and candidate <= dt_util.as_local(now + timedelta(seconds=30)):
            return SCHEDULE_SOURCE
        return BLUETOOTH_SOURCE

    def observe(self, status: dict[str, Any]) -> None:
        """Process actual status only, including inter-station program delays."""
        now = dt_util.utcnow()
        continuous = self.last_seen is not None and 0 <= (now - self.last_seen).total_seconds() <= max(60, self.c.poll_interval * 2 + 30)
        active = bool(status.get("is_watering") or status.get("active_program"))
        intent = None
        if self.pending:
            at = dt_util.parse_datetime(self.pending["at"])
            if not active or at is None or not 0 <= (now - at).total_seconds() <= 300:
                self.pending = None
                self._save()
            elif active and (self.pending["program"] == status.get("active_program") if self.pending["program"]
                             else not status.get("active_program") and self.pending["station"] == status.get("station_num")):
                intent = self.pending
                self.pending = None
        if self.current and not continuous:
            self._finish("Observation interrupted", None)
        if self.current and active and (intent or self.current["program"] != status.get("active_program")):
            self._finish("Run replaced", now)
        if active and self.current is None:
            source = self._classify(status, now, continuous, intent)
            # An unmatched recent HA attempt could have affected this run too.
            if self.pending:
                source = UNKNOWN_SOURCE
            self.current = {
                "source": source,
                "first_detected": now.isoformat(), "last_seen": now.isoformat(),
                "finished_detected": None, "outcome": "Running", "program": status.get("active_program"),
                "stations": [], "actor": intent.get("actor") if intent else None,
                "user_id": intent.get("user_id") if intent else None,
                "context_id": intent.get("context_id") if intent else None,
            }
            target = f"Program {chr(64 + status['active_program'])}" if status.get("active_program") else f"Station {status.get('station_num', 'unknown')}"
            self._log(f"Started: {source} — {target}", self.current)
        if self.current:
            if not active:
                self._finish("Finished", now)
            else:
                station = status.get("station_num")
                if station and station not in self.current["stations"]:
                    self.current["stations"].append(station)
                self.current["last_seen"] = now.isoformat()
                self._save()
        self.last_seen = now
        self.last_revision = self.c.program_manager.revision
