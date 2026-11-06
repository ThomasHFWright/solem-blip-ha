"""Rainfall discounts change onboard settings, never schedule watering."""

from datetime import date, timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.util import dt as dt_util
from solem_blip_ble.client_v2 import StatelessSolemClient
from solem_blip_ble.exceptions import InvalidSnapshot

from custom_components.solem_blip.rainfall import fraction, program_fingerprint


@pytest.mark.parametrize(
    ("rain", "expected"), [(0, 1), (1, 0.75), (2, 0.5), (3, 0.25), (4, 0), (8, 0)]
)
def test_discount(rain, expected):
    assert fraction(rain, 4) == expected


@pytest.mark.parametrize(
    ("rain", "target"),
    [(-1, 4), (float("nan"), 4), (float("inf"), 4), (1, 0), (1, -1)],
)
def test_invalid_totals(rain, target):
    with pytest.raises(ValueError):
        fraction(rain, target)


def test_controller_owned_date_does_not_invalidate_baseline() -> None:
    program = {
        "cycle": 0,
        "controller_date": date(2026, 11, 3),
        "period_start_date": None,
        "water_budget": 100,
        "start_times": [180, None],
    }
    previous = program_fingerprint(program)
    program["controller_date"] = date(2026, 11, 4)
    assert program_fingerprint(program) == previous


@pytest.fixture
async def rainfall(coordinator):
    api = StatelessSolemClient(
        "AA:BB:CC:DD:EE:12", mock=True, max_station_num=6
    )
    api.get_status = AsyncMock(
        return_value={
            "controller_state": "On",
            "controller_off_mode": "on",
            "is_watering": False,
        }
    )
    api.turn_off_x_days = AsyncMock()
    coordinator.api = api
    coordinator.irrigation_programs = await api.get_irrigation_config()
    manager = coordinator.rainfall
    manager.options = {
        "sensor": "sensor.rain",
        "target_mm": 4,
        "max_age_minutes": 60,
        "programs": ["0"],
        "baselines": {"0": 100},
        "fingerprints": {
            "0": program_fingerprint(dict(coordinator.irrigation_programs[0]))
        },
    }
    manager.started = dt_util.utcnow() - timedelta(seconds=1)
    coordinator.hass.states.async_set(
        "sensor.rain", "2", {"unit_of_measurement": "mm"}
    )
    return manager


async def test_discount_then_restore_from_baseline(rainfall) -> None:
    coordinator = rainfall.coordinator
    await rainfall.apply()
    assert coordinator.irrigation_programs[0]["water_budget"] == 50

    coordinator.hass.states.async_set(
        "sensor.rain", "0", {"unit_of_measurement": "mm"}
    )
    await rainfall.apply()
    assert coordinator.irrigation_programs[0]["water_budget"] == 100
    assert rainfall.state["last_applied"]


@pytest.mark.parametrize(
    ("state", "attributes"),
    [
        ("unavailable", {"unit_of_measurement": "mm"}),
        ("nan", {"unit_of_measurement": "mm"}),
        ("0", {"unit_of_measurement": "in"}),
        ("0", {"unit_of_measurement": "mm", "restored": True}),
    ],
)
async def test_invalid_rain_never_becomes_zero(rainfall, state, attributes) -> None:
    rainfall.coordinator.hass.states.async_set("sensor.rain", state, attributes)
    with pytest.raises(ValueError):
        await rainfall.apply()
    assert rainfall.coordinator.irrigation_programs[0]["water_budget"] == 100


async def test_external_program_change_blocks_adjustment(rainfall) -> None:
    snapshot = await rainfall.coordinator.api.get_program_snapshot()
    _, changed = snapshot.patch(0, {"name": "Changed elsewhere"}, 6)
    rainfall.coordinator.api._mock_snapshot = changed
    with pytest.raises(InvalidSnapshot, match="Program changed"):
        await rainfall.apply()


async def test_complete_skip_uses_delay_and_never_zero_budget(rainfall) -> None:
    coordinator = rainfall.coordinator
    coordinator.hass.states.async_set(
        "sensor.rain", "4", {"unit_of_measurement": "mm"}
    )
    await rainfall.apply()
    coordinator.api.turn_off_x_days.assert_not_awaited()
    assert coordinator.irrigation_programs[0]["water_budget"] == 100

    rainfall.options["whole_controller_delay"] = True
    rainfall.options["programs"] = ["0", "1", "2"]
    rainfall.options["baselines"] = {str(index): 100 for index in range(3)}
    rainfall.options["fingerprints"] = {
        str(index): program_fingerprint(dict(program))
        for index, program in coordinator.irrigation_programs.items()
    }
    coordinator.api.get_status.side_effect = [
        {"controller_off_mode": "on", "is_watering": False},
        {"controller_off_mode": "temporary", "controller_off_days_remaining": 1},
    ]
    await rainfall.apply()
    coordinator.api.turn_off_x_days.assert_awaited_once_with(1)
    assert coordinator.irrigation_programs[0]["water_budget"] == 100
