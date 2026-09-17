"""Rainfall writes precede onboard starts; HA never triggers watering."""
from copy import deepcopy
import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.util import dt as dt_util

from custom_components.solem_blip.rainfall import RainfallManager, program_fingerprint
from test_rainfall import rainfall  # noqa: F401 -- shared real mock-controller fixture


@pytest.fixture
async def timed_rainfall(rainfall, freezer):
    previous = dt_util.DEFAULT_TIME_ZONE
    dt_util.set_default_time_zone(ZoneInfo("Europe/Lisbon"))
    freezer.move_to("2026-09-20T01:44:00Z")  # 02:44 local
    c = rainfall.coordinator
    snapshot = c.program_manager.snapshot
    for index, start in ((0, 180), (1, 240)):
        _, snapshot = snapshot.patch(index, {"cycle": 0, "week_days": 127,
            "start_times": [start, 600] + [None] * 6, "station_durations": {1: 60}}, 6)
    c.api._mock_snapshot = snapshot
    await c.refresh_programs()
    rainfall.options.update(automatic=True, timing="before_program", programs=["0", "1"],
        baselines={"0": 100, "1": 100},
        fingerprints={str(i): program_fingerprint(dict(snapshot.programs[i])) for i in (0, 1)})
    rainfall.started = dt_util.utcnow() - timedelta(seconds=1)
    c.hass.states.async_set("sensor.rain", "2", {"unit_of_measurement": "mm"})
    try:
        yield rainfall
    finally:
        dt_util.set_default_time_zone(previous)


async def test_once_per_program_day_only_due_budget_changes(timed_rainfall, freezer):
    r = timed_rainfall
    c = r.coordinator
    await r.maybe_apply()
    assert c.irrigation_programs[0]["water_budget"] == 100
    freezer.tick(timedelta(minutes=1))  # 02:45 local
    await r.maybe_apply()
    assert c.irrigation_programs[0]["water_budget"] == 50
    assert c.irrigation_programs[1]["water_budget"] == 100
    assert r.state["prestart_checked"] == {"0": "2026-09-20"}
    # A reload and another start slot do not cause a second daily adjustment.
    restarted = RainfallManager(c)
    restarted.options = deepcopy(r.options)
    await restarted.load()
    restarted.apply = AsyncMock()
    await restarted.maybe_apply()
    restarted.apply.assert_not_awaited()
    freezer.move_to("2026-09-20T02:45:00Z")
    await restarted.maybe_apply()
    restarted.apply.assert_awaited_once_with(["1"])
    freezer.move_to("2026-09-20T08:45:00Z")
    await restarted.maybe_apply()
    assert restarted.apply.await_count == 1
    freezer.move_to("2026-09-21T01:45:00Z")
    await restarted.maybe_apply()
    assert restarted.apply.await_count == 2
    assert restarted.apply.call_args.args == (["0"],)


async def test_missing_rain_retries_in_window_without_late_catchup(timed_rainfall, freezer):
    r = timed_rainfall
    r.apply = AsyncMock(side_effect=ValueError("Waiting for fresh rain"))
    freezer.move_to("2026-09-20T01:45:00Z")
    await r.maybe_apply()
    assert "prestart_checked" not in r.state
    assert r.reason == "Waiting for fresh rain"
    freezer.tick(timedelta(minutes=2))
    await r.maybe_apply()
    assert r.apply.await_count == 2
    freezer.move_to("2026-09-20T01:58:00Z")
    await r.maybe_apply()
    freezer.move_to("2026-09-20T02:01:00Z")
    await r.maybe_apply()
    assert r.apply.await_count == 2


async def test_no_later_slot_catchup_after_missed_first_start(timed_rainfall, freezer):
    r = timed_rainfall
    r.apply = AsyncMock()
    freezer.move_to("2026-09-20T08:45:00Z")
    await r.maybe_apply()
    r.apply.assert_not_awaited()


async def test_concurrent_polls_adjust_only_once(timed_rainfall, freezer):
    r = timed_rainfall
    r.apply = AsyncMock()
    freezer.move_to("2026-09-20T01:45:00Z")
    await asyncio.gather(r.maybe_apply(), r.maybe_apply())
    r.apply.assert_awaited_once_with(["0"])


async def test_midnight_run_checked_previous_day_and_respects_weekdays(timed_rainfall, freezer):
    r = timed_rainfall
    r.apply = AsyncMock()
    r.options["programs"] = ["0"]
    p = r.coordinator.irrigation_programs[0]
    p["start_times"] = [5] + [None] * 7
    p["week_days"] = 1  # Monday only
    freezer.move_to("2026-09-20T22:50:00Z")  # Sunday 23:50 for Monday 00:05
    await r.maybe_apply()
    r.apply.assert_awaited_once_with(["0"])
    assert r.state["prestart_checked"] == {"0": "2026-09-21"}
    freezer.move_to("2026-09-21T22:50:00Z")
    await r.maybe_apply()
    assert r.apply.await_count == 1


@pytest.mark.parametrize("disabled", ["automatic", "selection", "starts", "durations", "missing"])
async def test_no_check_without_selected_active_schedule(timed_rainfall, freezer, disabled):
    r = timed_rainfall
    r.options["programs"] = ["0"]
    r.apply = AsyncMock()
    if disabled == "automatic":
        r.options["automatic"] = False
    elif disabled == "selection":
        r.options["programs"] = []
    elif disabled == "missing":
        r.coordinator.irrigation_programs.clear()
    else:
        p = r.coordinator.irrigation_programs[0]
        p["start_times" if disabled == "starts" else "station_durations"] = [None] * 8 if disabled == "starts" else [0] * 6
    freezer.move_to("2026-09-20T01:45:00Z")
    await r.maybe_apply()
    r.apply.assert_not_awaited()
