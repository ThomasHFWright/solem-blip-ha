"""Rainfall discounts never become an HA watering schedule."""
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.util import dt as dt_util

from custom_components.solem_blip.ble.client_v2 import StatelessSolemClient
from custom_components.solem_blip.ble.snapshot import InvalidSnapshot
from custom_components.solem_blip.rainfall import fraction, program_fingerprint


@pytest.mark.parametrize("rain,expected", [(0,1),(1,.75),(2,.5),(3,.25),(4,0),(8,0)])
def test_discount(rain, expected):
    assert fraction(rain,4) == expected


@pytest.mark.parametrize("rain,target", [(-1,4),(float('nan'),4),(float('inf'),4),(1,0),(1,-1),(1,float('inf'))])
def test_invalid_totals(rain,target):
    with pytest.raises(ValueError):
        fraction(rain,target)


@pytest.fixture
async def rainfall(coordinator):
    api = StatelessSolemClient("AA:BB:CC:DD:EE:02", mock=True, max_station_num=6)
    api.get_status = AsyncMock(return_value={"controller_state":"On", "controller_off_mode":"on", "is_watering":False})
    api.get_firmware_version = AsyncMock(return_value={"major":5})
    api.turn_off_x_days = AsyncMock()
    coordinator.api = coordinator.program_manager.api = api
    await coordinator.refresh_programs()
    r = coordinator.rainfall
    r.options = {"sensor":"sensor.rain", "target_mm":4, "max_age_minutes":60, "programs":["0"],
                 "baselines":{"0":100}, "fingerprints":{"0":program_fingerprint(dict(coordinator.irrigation_programs[0]))}}
    r.started = dt_util.utcnow() - timedelta(seconds=1)
    coordinator.hass.states.async_set("sensor.rain", "2", {"unit_of_measurement":"mm"})
    return r


async def test_discount_and_restore_from_normal_not_reduced(rainfall):
    c = rainfall.coordinator
    await rainfall.apply()
    assert c.irrigation_programs[0]["water_budget"] == 50
    await rainfall.apply()
    assert c.irrigation_programs[0]["water_budget"] == 50
    assert c.irrigation_programs[1]["water_budget"] == 100
    c.hass.states.async_set("sensor.rain", "1", {"unit_of_measurement":"mm"})
    await rainfall.apply()
    assert c.irrigation_programs[0]["water_budget"] == 75
    c.hass.states.async_set("sensor.rain", "0", {"unit_of_measurement":"mm"})
    await rainfall.apply()
    assert c.irrigation_programs[0]["water_budget"] == 100
    assert rainfall.state["last_applied"]


@pytest.mark.parametrize("state,attrs", [("unavailable",{"unit_of_measurement":"mm"}),
    ("unknown",{"unit_of_measurement":"mm"}), ("nan",{"unit_of_measurement":"mm"}),
    ("0",{"unit_of_measurement":"in"}), ("0",{"unit_of_measurement":"mm","restored":True})])
async def test_invalid_rain_never_zero(rainfall,state,attrs):
    rainfall.coordinator.hass.states.async_set("sensor.rain",state,attrs)
    with pytest.raises(ValueError):
        await rainfall.apply()
    assert rainfall.coordinator.irrigation_programs[0]["water_budget"] == 100


async def test_missing_startup_and_stale_rain(rainfall, freezer):
    c = rainfall.coordinator
    c.hass.states.async_remove("sensor.rain")
    with pytest.raises(ValueError): await rainfall.apply()
    c.hass.states.async_set("sensor.rain", "0", {"unit_of_measurement":"mm"})
    rainfall.started = dt_util.utcnow() + timedelta(seconds=1)
    with pytest.raises(ValueError): await rainfall.apply()
    rainfall.started -= timedelta(seconds=2)
    freezer.tick(timedelta(hours=2))
    with pytest.raises(ValueError): await rainfall.apply()


@pytest.mark.parametrize("mode", ["permanent","temporary","unknown"])
async def test_preserve_off_and_manual_delay(rainfall,mode):
    rainfall.coordinator.api.get_status.return_value["controller_off_mode"] = mode
    await rainfall.apply()
    assert rainfall.coordinator.irrigation_programs[0]["water_budget"] == 100


async def test_complete_skip_requires_global_permission_and_never_zero(rainfall):
    c = rainfall.coordinator
    c.hass.states.async_set("sensor.rain","4",{"unit_of_measurement":"mm"})
    await rainfall.apply()
    c.api.turn_off_x_days.assert_not_awaited()
    assert c.irrigation_programs[0]["water_budget"] == 100
    rainfall.options["whole_controller_delay"] = True
    c.api.get_status.side_effect = [
        {"controller_off_mode":"on", "is_watering":False},
        {"controller_off_mode":"temporary", "controller_off_days_remaining":1},
    ]
    await rainfall.apply()
    c.api.turn_off_x_days.assert_awaited_once_with(1)
    c.api.get_status.side_effect = None
    await rainfall.apply()
    c.api.turn_off_x_days.assert_awaited_once()
    assert "not extended" in rainfall.reason


async def test_delay_refused_if_unselected_program_would_pause(rainfall):
    c=rainfall.coordinator
    s=c.program_manager.snapshot
    _, enabled=s.patch(1,{"start_times":[360]+[None]*7,"station_durations":{1:60}},6)
    c.api._mock_snapshot=enabled
    c.hass.states.async_set("sensor.rain","4",{"unit_of_measurement":"mm"})
    rainfall.options["whole_controller_delay"]=True
    with pytest.raises(InvalidSnapshot): await rainfall.apply()
    c.api.turn_off_x_days.assert_not_awaited()


async def test_phone_change_stops_rainfall(rainfall):
    c=rainfall.coordinator
    _, changed=c.program_manager.snapshot.patch(0,{"name":"Changed on phone"},6)
    c.api._mock_snapshot=changed
    with pytest.raises(InvalidSnapshot): await rainfall.apply()
    _, changed=c.program_manager.snapshot.patch(0,{"name":"Program A","water_budget":40},6)
    c.api._mock_snapshot=changed
    with pytest.raises(InvalidSnapshot): await rainfall.apply()


async def test_pending_write_and_no_selection_block(rainfall):
    rainfall.coordinator.program_manager.pending={"uncertain":True}
    with pytest.raises(InvalidSnapshot): await rainfall.apply()
    rainfall.options["programs"]=[]
    with pytest.raises(ValueError): await rainfall.apply()


async def test_automatic_updates_are_opt_in_hourly_and_errors_reported(rainfall,freezer):
    rainfall.apply=AsyncMock(side_effect=ValueError("Rain unavailable"))
    await rainfall.maybe_apply()
    rainfall.apply.assert_not_awaited()
    rainfall.options["automatic"]=True
    await rainfall.maybe_apply()
    assert rainfall.reason == "Rain unavailable"
    await rainfall.maybe_apply()
    rainfall.apply.assert_awaited_once()
    freezer.tick(timedelta(hours=1))
    await rainfall.maybe_apply()
    assert rainfall.apply.await_count==2


async def test_delay_checks_additional_storage_slots(rainfall, extended_program_snapshot):
    from custom_components.solem_blip.ble.snapshot import ProgramSnapshot
    c = rainfall.coordinator
    frames = list(extended_program_snapshot.frames)
    # Slot four has a start and watering duration; A/B/C remain empty.
    starts = bytearray(frames[24]); starts[4:6] = (360).to_bytes(2, 'big'); frames[24] = bytes(starts)
    duration = bytearray(frames[25]); duration[4:7] = (120).to_bytes(3, 'big'); frames[25] = bytes(duration)
    snapshot = ProgramSnapshot.from_frames(tuple(frames))
    c.api._mock_snapshot = snapshot
    rainfall.options['fingerprints']['0'] = program_fingerprint(dict(snapshot.programs[0]))
    rainfall.options['whole_controller_delay'] = True
    c.hass.states.async_set('sensor.rain', '4', {'unit_of_measurement': 'mm'})
    with pytest.raises(InvalidSnapshot, match='unselected program'):
        await rainfall.apply()
    c.api.turn_off_x_days.assert_not_awaited()


async def test_dashboard_summary_preserves_config_and_flags_changed_baseline(rainfall):
    """Display reads neither touch BLE nor mistake edited programs for the baseline."""
    c = rainfall.coordinator
    c.api.get_status.reset_mock()
    summary = rainfall.dashboard_state
    assert summary['target_mm'] == 4
    assert summary['sensor'] == 'sensor.rain'
    assert summary['baselines'] == {'0': 100}
    assert summary['last_applied'] is None
    assert not summary['baseline_review_required']
    # A water-budget discount does not invalidate the normal baseline.
    c.irrigation_programs[0]['water_budget'] = 75
    assert not rainfall.dashboard_state['baseline_review_required']
    c.irrigation_programs[0]['name'] = 'Edited program'
    assert rainfall.dashboard_state['baseline_review_required']
    summary['baselines']['0'] = 25
    summary['programs'].clear()
    assert rainfall.options['baselines']['0'] == 100
    assert rainfall.options['programs'] == ['0']
    c.api.get_status.assert_not_awaited()
