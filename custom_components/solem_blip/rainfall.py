"""Optional rainfall discounts for onboard programs, with no HA watering timer."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
import math
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util
from solem_blip_ble.exceptions import InvalidSnapshot

from .schedule import next_start_datetime

if TYPE_CHECKING:
    from .coordinator import SolemCoordinator

RAIN_OPTIONS = "rainfall"


def fraction(rain_mm: float, target_mm: float) -> float:
    """Discount an already accumulated rolling rainfall total."""
    if (
        not math.isfinite(rain_mm)
        or rain_mm < 0
        or not math.isfinite(target_mm)
        or target_mm <= 0
    ):
        raise ValueError("Rainfall and target must be finite; target must be positive")
    return max(0.0, min(1.0, 1 - rain_mm / target_mm))


def program_fingerprint(program: dict[str, Any]) -> str:
    """Protect schedule edits while ignoring managed and controller-owned data."""
    ignored = {"controller_date", "water_budget"}
    if program.get("cycle") in (0, 1, 2, 3):
        ignored.add("period_start_date")
    protected = {key: value for key, value in program.items() if key not in ignored}
    return hashlib.sha256(
        json.dumps(protected, sort_keys=True, default=str).encode()
    ).hexdigest()


def matches_program(program: dict[str, Any], fingerprint: str | None) -> bool:
    """Accept current and legacy fingerprints from the previous release."""
    legacy = hashlib.sha256(
        json.dumps(
            {key: value for key, value in program.items() if key != "water_budget"},
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()
    return fingerprint in (program_fingerprint(program), legacy)


class RainfallManager:
    """Adjust saved device budgets; never start, stop, or catch up watering."""

    def __init__(self, coordinator: SolemCoordinator) -> None:
        self.coordinator = coordinator
        entry = coordinator.config_entry
        assert entry is not None
        self.options: dict[str, Any] = entry.options.get(RAIN_OPTIONS, {})
        self.store: Store[dict[str, Any]] = Store(
            coordinator.hass,
            1,
            f"solem_blip.rainfall.{entry.entry_id}",
            private=True,
        )
        self.started = dt_util.utcnow()
        self.next_check: datetime = self.started
        self.state: dict[str, Any] = {}
        self.reason = (
            "Waiting for next selected program"
            if self.options.get("automatic")
            else "disabled"
        )
        self._lock = asyncio.Lock()
        self._automatic_lock = asyncio.Lock()

    async def load(self) -> None:
        """Restore only rainfall bookkeeping, never controller settings."""
        self.state = await self.store.async_load() or {}

    @property
    def dashboard_state(self) -> dict[str, Any]:
        """Expose configuration and results without doing BLE work."""
        selected = self.options.get("programs", [])
        programs = self.coordinator.irrigation_programs
        return {
            "sensor": self.options.get("sensor"),
            "target_mm": self.options.get("target_mm"),
            "automatic": self.options.get("automatic", False),
            "timing": self.options.get("timing", "hourly"),
            "max_age_minutes": self.options.get("max_age_minutes", 60),
            "whole_controller_delay": self.options.get(
                "whole_controller_delay", False
            ),
            "programs": list(selected),
            "baselines": dict(self.options.get("baselines", {})),
            "baseline_review_required": any(
                int(key) in programs
                and not matches_program(
                    dict(programs[int(key)]),
                    self.options.get("fingerprints", {}).get(key),
                )
                for key in selected
            ),
            "last_applied": self.state.get("last_applied"),
            "last_fraction": self.state.get("last_fraction"),
        }

    def _due_programs(self) -> dict[str, str]:
        """Return selected programs within 15 minutes of their first daily start."""
        now = dt_util.now()
        checked = self.state.get("prestart_checked", {})
        due: dict[str, str] = {}
        for key in self.options.get("programs", []):
            program = self.coordinator.irrigation_programs.get(int(key))
            if not program or not any(program["station_durations"]):
                continue
            starts = [value for value in program["start_times"] if value is not None]
            if not starts:
                continue
            first_only = deepcopy(program)
            first_only["start_times"] = [min(starts)] + [None] * 7
            start = next_start_datetime(first_only, now)
            if start is None:
                continue
            seconds = (dt_util.as_utc(start) - dt_util.as_utc(now)).total_seconds()
            day = start.date().isoformat()
            if 120 < seconds <= 15 * 60 and checked.get(key) != day:
                due[key] = day
        return due

    async def maybe_apply(self) -> None:
        """Run an opted-in automatic check when its configured time is due."""
        async with self._automatic_lock:
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
        """Read one fresh, live millimetre total or refuse to make a change."""
        state = self.coordinator.hass.states.get(self.options.get("sensor", ""))
        now = dt_util.utcnow()
        if (
            state is None
            or state.attributes.get("restored")
            or state.attributes.get("unit_of_measurement") != "mm"
        ):
            raise ValueError("Rainfall sensor must report a live accumulated value in mm")
        reported = state.last_reported
        max_age = self.options.get("max_age_minutes", 60) * 60
        if reported <= self.started or not 0 <= (now - reported).total_seconds() <= max_age:
            raise ValueError(
                "Waiting for a fresh rainfall report; last device settings retained"
            )
        return fraction(float(state.state), float(self.options["target_mm"]))

    async def apply(self, programs: list[str] | None = None) -> None:
        """Apply one guarded rainfall adjustment to fresh controller state."""
        async with self._lock, self.coordinator._heavy_read_lock:
            async with self.coordinator.api.transaction():
                await self._apply_locked(programs)

    async def _apply_locked(self, programs: list[str] | None = None) -> None:
        coordinator = self.coordinator
        selected = self.options.get("programs", []) if programs is None else programs
        if not selected:
            raise ValueError(
                "Select lawn programs and confirm their normal baseline budgets first"
            )
        value = self.read_fraction()
        if coordinator.program_backup.pending:
            raise InvalidSnapshot(
                "Resolve the uncertain program restore before rainfall adjustment"
            )
        status = await coordinator.api.get_status()
        if status.get("controller_off_mode") != "on" or status.get("is_watering") is not False:
            self.reason = "Controller OFF, delay or active watering preserved"
            return

        snapshot = await coordinator.api.get_program_snapshot()
        snapshot.require_known_programs()
        expected = self.state.setdefault("expected_budgets", {})
        for key in selected:
            program = snapshot.programs[int(key)]
            baseline = self.options["baselines"][key]
            if not matches_program(
                dict(program), self.options["fingerprints"].get(key)
            ):
                raise InvalidSnapshot(
                    "Program changed outside rainfall adjustment; review baseline settings"
                )
            if program["water_budget"] not in (
                baseline,
                expected.get(key, baseline),
            ):
                raise InvalidSnapshot(
                    "Program budget changed outside rainfall adjustment; review baseline settings"
                )

        if value == 0:
            await self._apply_skip(snapshot)
            return

        for key in selected:
            budget = max(1, round(self.options["baselines"][key] * value))
            program = snapshot.programs[int(key)]
            if program["water_budget"] == budget:
                continue
            updated = deepcopy(program)
            updated["water_budget"] = budget
            expected[key] = budget
            await self.store.async_save(self.state)
            await coordinator.set_irrigation_program(
                int(key), updated, expected_revision=snapshot.revision
            )
            snapshot = await coordinator.api.get_program_snapshot()
        self.state["delay_episode"] = False
        self.state["last_fraction"] = value
        self.state["last_applied"] = dt_util.utcnow().isoformat()
        await self.store.async_save(self.state)
        self.reason = "Baseline rainfall discount saved"

    async def _apply_skip(self, snapshot: Any) -> None:
        """Use the controller's expiring global delay for a complete skip."""
        if not self.options.get("whole_controller_delay"):
            self.reason = "Skip needs whole-controller delay permission; budgets unchanged"
            return
        for index, program in {**snapshot.programs, **snapshot.additional_programs()}.items():
            if (
                any(value is not None for value in program["start_times"])
                and any(program["station_durations"])
                and str(index) not in self.options.get("programs", [])
            ):
                raise InvalidSnapshot("Rain delay would also pause an unselected program")
        if self.state.get("delay_episode"):
            self.reason = "Rain delay already requested for this wet episode; not extended"
            return
        self.state["delay_episode"] = True
        await self.store.async_save(self.state)
        await self.coordinator.api.turn_off_x_days(1)
        actual = await self.coordinator.api.get_status()
        if (
            actual.get("controller_off_mode") != "temporary"
            or not actual.get("controller_off_days_remaining")
        ):
            raise InvalidSnapshot("Rain delay outcome uncertain; inspect controller status")
        self.coordinator._apply_status(actual)
        self.reason = "One-day controller rain delay saved; countdown follows device firmware"
