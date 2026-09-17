"""Optional rainfall discounts for onboard programs, with no watering timer."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import hashlib
import json
import math
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .ble.snapshot import InvalidSnapshot
from .schedule import day_matches_cycle

if TYPE_CHECKING:
    from .coordinator import SolemCoordinator

RAIN_OPTIONS = "rainfall"


def fraction(rain_mm: float, target_mm: float) -> float:
    """The input is an already accumulated rolling total, in millimetres."""
    if not math.isfinite(rain_mm) or rain_mm < 0 or not math.isfinite(target_mm) or target_mm <= 0:
        raise ValueError("Rainfall and target must be finite; target must be positive")
    return max(0.0, min(1.0, 1 - rain_mm / target_mm))


def program_fingerprint(program: dict[str, Any]) -> str:
    """Protect schedule edits, excluding the irrelevant date for date-independent cycles."""
    ignored = {"water_budget"}
    if program.get("cycle") in (0, 1, 2, 3):
        ignored.add("period_start_date")
    return hashlib.sha256(json.dumps({k: v for k, v in program.items() if k not in ignored},
                                    sort_keys=True, default=str).encode()).hexdigest()


def matches_program(program: dict[str, Any], fingerprint: str | None) -> bool:
    """Accept legacy fingerprints only when every previously protected field matches."""
    legacy = hashlib.sha256(json.dumps({k: v for k, v in program.items() if k != "water_budget"},
                                      sort_keys=True, default=str).encode()).hexdigest()
    return fingerprint in (program_fingerprint(program), legacy)


class RainfallManager:
    """Adjust saved budgets periodically; never start, stop or catch up watering."""

    def __init__(self, coordinator: SolemCoordinator) -> None:
        self.coordinator = coordinator
        assert coordinator.config_entry is not None
        self.options: dict[str, Any] = coordinator.config_entry.options.get(RAIN_OPTIONS, {})
        self.store: Store[dict[str, Any]] = Store(coordinator.hass, 1, f"solem_blip.rainfall.{coordinator.config_entry.entry_id}", private=True)
        self.started = dt_util.utcnow()
        self.next_check: datetime = self.started
        self.state: dict[str, Any] = {}
        self.reason = "disabled"
        self._lock = asyncio.Lock()
        self._automatic_lock = asyncio.Lock()

    async def load(self) -> None:
        self.state = await self.store.async_load() or {}

    @property
    def dashboard_state(self) -> dict[str, Any]:
        """Expose configured targets and saved results without doing any BLE work."""
        options = self.options
        selected = options.get("programs", [])
        programs = self.coordinator.irrigation_programs
        return {
            "sensor": options.get("sensor"),
            "target_mm": options.get("target_mm"),
            "automatic": options.get("automatic", False),
            "timing": options.get("timing", "hourly"),
            "max_age_minutes": options.get("max_age_minutes", 60),
            "whole_controller_delay": options.get("whole_controller_delay", False),
            "programs": list(selected),
            "baselines": dict(options.get("baselines", {})),
            "baseline_review_required": any(
                int(key) in programs and not matches_program(dict(programs[int(key)]),
                options.get("fingerprints", {}).get(key)) for key in selected
            ),
            "last_applied": self.state.get("last_applied"),
            "last_fraction": self.state.get("last_fraction"),
        }

    async def maybe_apply(self) -> None:
        async with self._automatic_lock:
            await self._maybe_apply_locked()

    def _due_programs(self) -> dict[str, str]:
        """One check per local run date, before the first start only."""
        now = dt_util.now()
        checked = self.state.get("prestart_checked", {})
        due = {}
        for key in self.options.get("programs", []):
            program = self.coordinator.irrigation_programs.get(int(key))
            if not program or not any(program["station_durations"]):
                continue
            starts = [value for value in program["start_times"] if value is not None]
            if not starts:
                continue
            hour, minute = divmod(min(starts), 60)
            for offset in (0, 1):
                start = (now + timedelta(days=offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)
                if not day_matches_cycle(program["cycle"], program["period_length"], program["week_days"], start,
                                         period_start_date=program.get("period_start_date"), synchro_day=program.get("synchro_day", 0)):
                    continue
                seconds = (dt_util.as_utc(start) - dt_util.as_utc(now)).total_seconds()
                day = start.date().isoformat()
                # Leave time for BLE writes; never catch up at/after a watering start.
                if 120 < seconds <= 15 * 60 and checked.get(key) != day:
                    due[key] = day
        return due

    async def _maybe_apply_locked(self) -> None:
        if not self.options.get("automatic"):
            return
        if self.options.get("timing", "hourly") == "before_program":
            due = self._due_programs()
            if not due:
                return
            try:
                await self.apply(list(due))
                self.state.setdefault("prestart_checked", {}).update(due)
                await self.store.async_save(self.state)
            except Exception as err:
                self.reason = str(err)
            return
        if dt_util.utcnow() < self.next_check:
            return
        self.next_check = dt_util.utcnow() + timedelta(hours=1)
        try:
            await self.apply()
        except Exception as err:
            self.reason = str(err)

    def read_fraction(self) -> float:
        c = self.coordinator
        state = c.hass.states.get(self.options.get("sensor", ""))
        now = dt_util.utcnow()
        if state is None or state.attributes.get("restored") or state.attributes.get("unit_of_measurement") != "mm":
            raise ValueError("Rainfall sensor must report a live accumulated value in mm")
        reported = state.last_reported
        if reported <= self.started or not 0 <= (now - reported).total_seconds() <= self.options.get("max_age_minutes", 60) * 60:
            raise ValueError("Waiting for a fresh rainfall report; last device settings retained")
        return fraction(float(state.state), float(self.options["target_mm"]))

    async def apply(self, programs: list[str] | None = None) -> None:
        async with self._lock, self.coordinator.api.transaction():
            await self._apply_locked(programs)

    async def _apply_locked(self, programs: list[str] | None = None) -> None:
        c = self.coordinator
        selected = self.options.get("programs", []) if programs is None else programs
        if not selected:
            raise ValueError("Select lawn programs and confirm their normal baseline budgets first")
        value = self.read_fraction()
        if c.program_manager.pending:
            raise InvalidSnapshot("Resolve the uncertain program write before rainfall adjustment")
        status = await c.api.get_status()
        if status.get("controller_off_mode") != "on" or status.get("is_watering") is not False:
            self.reason = "Controller OFF, delay or active watering preserved"
            return
        snapshot = await c.program_manager.refresh()
        snapshot.require_known_programs()
        expected = self.state.setdefault("expected_budgets", {})
        for key in selected:
            program = snapshot.programs[int(key)]
            baseline = self.options["baselines"][key]
            if not matches_program(dict(program), self.options["fingerprints"].get(key)):
                raise InvalidSnapshot("Program changed outside rainfall adjustment; review baseline settings")
            if program["water_budget"] not in (baseline, expected.get(key, baseline)):
                raise InvalidSnapshot("Program budget changed outside rainfall adjustment; review baseline settings")
        if value == 0:
            if not self.options.get("whole_controller_delay"):
                self.reason = "Skip needs whole-controller delay permission; budgets unchanged"
                return
            for index, program in {**snapshot.programs, **snapshot.additional_programs()}.items():
                if any(t is not None for t in program["start_times"]) and any(program["station_durations"]) and str(index) not in self.options.get("programs", []):
                    raise InvalidSnapshot("Rain delay would also pause an unselected program")
            if self.state.get("delay_episode"):
                self.reason = "Rain delay already requested for this wet episode; not extended"
                return
            # Persist before sending. An uncertain delay must never be replayed.
            self.state["delay_episode"] = True
            await self.store.async_save(self.state)
            await c.api.turn_off_x_days(1)
            actual = await c.api.get_status()
            if actual.get("controller_off_mode") != "temporary" or not actual.get("controller_off_days_remaining"):
                raise InvalidSnapshot("Rain delay outcome uncertain; inspect controller status")
            c._apply_status(actual)
            self.reason = "One-day controller rain delay saved; countdown follows device firmware"
        else:
            for key in selected:
                budget = max(1, round(self.options["baselines"][key] * value))
                if snapshot.programs[int(key)]["water_budget"] == budget:
                    continue
                expected[key] = budget
                await self.store.async_save(self.state)
                await c.set_irrigation_program(int(key), {"water_budget": budget}, snapshot.revision, require_on=True)
                assert c.program_manager.snapshot is not None
                snapshot = c.program_manager.snapshot
            self.state["delay_episode"] = False
            self.state["last_fraction"] = value
            self.state["last_applied"] = dt_util.utcnow().isoformat()
            await self.store.async_save(self.state)
            self.reason = "Baseline rainfall discount saved"
        c.publish_programs()
