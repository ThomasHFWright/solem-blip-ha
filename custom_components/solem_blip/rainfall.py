"""Optional rainfall discounts for onboard programs, with no watering timer."""
from __future__ import annotations

import asyncio
from datetime import datetime
import hashlib
import json
import math
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .ble.snapshot import InvalidSnapshot

if TYPE_CHECKING:
    from .coordinator import SolemCoordinator

RAIN_OPTIONS = "rainfall"


def fraction(rain_mm: float, target_mm: float) -> float:
    """The input is an already accumulated rolling total, in millimetres."""
    if not math.isfinite(rain_mm) or rain_mm < 0 or not math.isfinite(target_mm) or target_mm <= 0:
        raise ValueError("Rainfall and target must be finite; target must be positive")
    return max(0.0, min(1.0, 1 - rain_mm / target_mm))


def program_fingerprint(program: dict[str, Any]) -> str:
    """Detect changed cadence, durations or names without compounding budgets."""
    return hashlib.sha256(json.dumps({k: v for k, v in program.items() if k != "water_budget"},
                                    sort_keys=True, default=str).encode()).hexdigest()


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
            "max_age_minutes": options.get("max_age_minutes", 60),
            "whole_controller_delay": options.get("whole_controller_delay", False),
            "programs": list(selected),
            "baselines": dict(options.get("baselines", {})),
            "baseline_review_required": any(
                int(key) in programs and program_fingerprint(dict(programs[int(key)]))
                != options.get("fingerprints", {}).get(key) for key in selected
            ),
            "last_applied": self.state.get("last_applied"),
            "last_fraction": self.state.get("last_fraction"),
        }

    async def maybe_apply(self) -> None:
        if not self.options.get("automatic") or dt_util.utcnow() < self.next_check:
            return
        from datetime import timedelta
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

    async def apply(self) -> None:
        async with self._lock, self.coordinator.api.transaction():
            await self._apply_locked()

    async def _apply_locked(self) -> None:
        c = self.coordinator
        selected = self.options.get("programs", [])
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
            if program_fingerprint(dict(program)) != self.options["fingerprints"].get(key):
                raise InvalidSnapshot("Program changed outside rainfall adjustment; review baseline settings")
            if program["water_budget"] not in (baseline, expected.get(key, baseline)):
                raise InvalidSnapshot("Program budget changed outside rainfall adjustment; review baseline settings")
        if value == 0:
            if not self.options.get("whole_controller_delay"):
                self.reason = "Skip needs whole-controller delay permission; budgets unchanged"
                return
            for index, program in {**snapshot.programs, **snapshot.additional_programs()}.items():
                if any(t is not None for t in program["start_times"]) and any(program["station_durations"]) and str(index) not in selected:
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
