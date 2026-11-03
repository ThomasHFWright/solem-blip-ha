"""Pre-start rainfall checks run once before the first daily slot."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

from homeassistant.util import dt as dt_util

from test_rainfall import rainfall  # noqa: F401 -- shared controller fixture


async def test_first_start_window_only(rainfall, freezer) -> None:
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(ZoneInfo("Europe/Lisbon"))
    try:
        freezer.move_to("2026-09-20T01:44:00Z")
        manager = rainfall
        program = manager.coordinator.irrigation_programs[0]
        program["cycle"] = 0
        program["week_days"] = 127
        program["start_times"] = [180, 600] + [None] * 6
        manager.options.update(automatic=True, timing="before_program")
        manager.apply = AsyncMock()

        await manager.maybe_apply()
        manager.apply.assert_not_awaited()
        freezer.tick(timedelta(minutes=1))
        await manager.maybe_apply()
        manager.apply.assert_awaited_once_with(["0"])

        restarted = type(manager)(manager.coordinator)
        restarted.options = deepcopy(manager.options)
        await restarted.load()
        restarted.apply = AsyncMock()
        freezer.move_to("2026-09-20T08:45:00Z")
        await restarted.maybe_apply()
        restarted.apply.assert_not_awaited()
    finally:
        dt_util.set_default_time_zone(previous)
