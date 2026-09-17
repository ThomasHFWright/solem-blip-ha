"""Safety contracts for complete snapshots and durable program writes."""
import asyncio
from datetime import date
from unittest.mock import AsyncMock

import pytest

from custom_components.solem_blip.ble.client_v2 import StatelessSolemClient
from custom_components.solem_blip.ble.snapshot import ProgramSnapshot, InvalidSnapshot, StaleProgram, UncertainWrite
from custom_components.solem_blip.programs import ProgramManager


@pytest.fixture
async def api():
    client = StatelessSolemClient("AA:BB:CC:DD:EE:01", mock=True, max_station_num=6)
    await client.get_program_snapshot()
    client.get_status = AsyncMock(return_value={"is_watering": False, "controller_state": "On", "controller_off_mode": "on"})
    client.get_firmware_version = AsyncMock(return_value={"major": 5})
    return client


@pytest.fixture
async def manager(hass, api):
    m = ProgramManager(hass, "program-test", api)
    await m.load()
    await m.refresh()
    return m


async def test_patch_sixth_station_preserves_every_other_byte(api):
    original = await api.get_program_snapshot()
    writes, expected = original.patch(0, {"station_durations": {6: 901}}, 6)
    assert len(writes) == 7 and all(w != b"\x3b\x00" for w in writes)
    assert expected.blocks[1] == original.blocks[1]
    assert expected.blocks[2] == original.blocks[2]
    assert expected.programs[0]["station_durations"] == [0]*5 + [901] + [0]*6
    for i in (0, 1, 2, 3, 4, 6):
        assert original.blocks[0][i] == expected.blocks[0][i]
    assert original.blocks[0][5][7:] == expected.blocks[0][5][7:]
    assert original.patch(0, {}, 6) == ([], original)


async def test_snapshot_rejects_partial_and_conflicting_reads(api):
    original = await api.get_program_snapshot()
    with pytest.raises(InvalidSnapshot):
        ProgramSnapshot.from_frames(original.frames[:-1])
    conflicting = original.frames[0][:-1] + b"x"
    with pytest.raises(InvalidSnapshot):
        ProgramSnapshot.from_frames(original.frames + (conflicting,))
    duplicate = ProgramSnapshot.from_frames(original.frames + (original.frames[0],))
    assert duplicate.revision == original.revision
    reordered = ProgramSnapshot.from_frames(tuple(reversed(original.frames)))
    assert reordered.revision == original.revision


async def test_unknown_slots_are_captured_but_cannot_be_written(api):
    original = await api.get_program_snapshot()
    extra = original.frames[0][:3] + b"\x13" + original.frames[0][4:]
    captured = ProgramSnapshot.from_frames(original.frames + (extra,))
    assert captured.programs == original.programs
    assert extra in captured.extras
    assert captured.revision != original.revision
    with pytest.raises(InvalidSnapshot, match="Unknown program slot"):
        captured.patch(0, {"water_budget": 75}, 6)


async def test_extended_response_preserves_nine_unedited_slots(extended_program_snapshot):
    original = extended_program_snapshot
    assert len(original.frames) == 84
    assert set(original.additional_programs()) == set(range(3, 12))
    writes, changed = original.patch(0, {"water_budget": 75, "station_durations": {6: 420}}, 6)
    assert len(writes) == 7
    assert changed.extras == original.extras
    assert changed.frames[7:] == original.frames[7:]
    assert changed.programs[0]["water_budget"] == 75
    assert changed.programs[0]["station_durations"][5] == 420
    assert changed.frames[2][:4] == original.frames[2][:4]
    assert len(changed.frames) == 84


@pytest.mark.parametrize("missing", [20, 40, 83])
async def test_extended_read_requires_final_slot_and_every_fragment(extended_program_snapshot, missing):
    frames = extended_program_snapshot.frames
    with pytest.raises(InvalidSnapshot, match="Incomplete"):
        ProgramSnapshot.from_frames(frames[:missing] + frames[missing + 1:])
    with pytest.raises(InvalidSnapshot, match="Incomplete"):
        ProgramSnapshot.from_frames(frames[:21])


async def test_additional_slot_bad_shape_blocks_edits(extended_program_snapshot):
    frames = list(extended_program_snapshot.frames)
    frames[25] += b"\0"
    captured = ProgramSnapshot.from_frames(tuple(frames))
    with pytest.raises(InvalidSnapshot, match="additional program fragment"):
        captured.patch(0, {"water_budget": 75}, 6)


@pytest.mark.parametrize("changes", [
    {"name": "x" * 32}, {"name": "a\0b"}, {"name": "é" * 16},
    {"water_budget": True}, {"water_budget": -1}, {"water_budget": 65536},
    {"station_durations": {7: 1}}, {"station_durations": {6: -1}},
    {"station_durations": []}, {"start_times": [1]}, {"start_times": [1440]*8},
    {"unknown": 1}, {"cycle": 5}, {"period_length": 0}, {"period_start_date": None},
])
async def test_invalid_edits_never_form_write_frames(api, changes):
    original = await api.get_program_snapshot()
    with pytest.raises(ValueError):
        original.patch(0, changes, 6)


async def test_date_and_unused_slots_are_preserved(api):
    original = await api.get_program_snapshot()
    _, changed = original.patch(0, {"period_start_date": "2026-01-01", "cycle": 4, "period_length": 3, "synchro_day": 2,
                                    "inter_station_delay": 7, "start_times": [360]+[None]*7, "name": "Morning"}, 6)
    _, discounted = changed.patch(0, {"water_budget": 50}, 6)
    assert discounted.programs[0]["period_start_date"] == date(2026,1,1)
    assert discounted.programs[0]["synchro_day"] == 2
    with pytest.raises(InvalidSnapshot):
        discounted.patch(0, {"synchro_day": 3}, 6)
    bad = list(original.frames)
    bad[2] = bad[2][:12] + b"\0\0\0\0"
    missing_date = ProgramSnapshot.from_frames(tuple(bad))
    _, discount = missing_date.patch(0, {"water_budget": 90}, 6)
    assert discount.programs[0]["period_start_date"] is None
    with pytest.raises(InvalidSnapshot):
        missing_date.patch(0, {"cycle": 4}, 6)


async def test_stale_edit_writes_nothing(manager, api):
    revision = manager.revision
    before = manager.snapshot
    _, changed = before.patch(1, {"name": "Phone edit"}, 6)
    api._mock_snapshot = changed
    api.write_program_frames = AsyncMock()
    with pytest.raises(StaleProgram):
        await manager.update(0, {"water_budget": 50}, revision)
    api.write_program_frames.assert_not_awaited()


async def test_journal_precedes_write_and_success_survives_restart(manager, api, hass):
    before = manager.snapshot
    original_write = api.write_program_frames
    async def check_journal(frames, expected):
        persisted = await manager.store.async_load()
        assert persisted["pending"]["before_revision"] == before.revision
        assert persisted["pending"]["expected_revision"] == expected.revision
        return await original_write(frames, expected)
    api.write_program_frames = AsyncMock(side_effect=check_journal)
    actual = await manager.update(0, {"water_budget": 75}, before.revision)
    assert actual.programs[0]["water_budget"] == 75
    reloaded = ProgramManager(hass, "program-test", api)
    await reloaded.load()
    assert reloaded.pending is None and reloaded.revision == actual.revision
    assert reloaded.last_write
    assert await reloaded.update(0, {}, actual.revision) == actual


@pytest.mark.parametrize("failure", [RuntimeError("radio lost"), asyncio.CancelledError()])
async def test_interrupted_write_blocks_replay_and_recovers_by_read(manager, api, hass, failure):
    api.write_program_frames = AsyncMock(side_effect=failure)
    with pytest.raises(type(failure)):
        await manager.update(0, {"water_budget": 50}, manager.revision)
    assert manager.pending
    with pytest.raises(UncertainWrite):
        await manager.update(0, {"water_budget": 50}, manager.revision)
    reloaded = ProgramManager(hass, "program-test", api)
    await reloaded.load()
    assert reloaded.pending
    await reloaded.refresh()
    assert reloaded.pending is None
    api.write_program_frames.assert_awaited_once()


async def test_mixed_readback_requires_explicit_acceptance(manager, api):
    before = manager.snapshot
    api.write_program_frames = AsyncMock(side_effect=UncertainWrite("mismatch"))
    with pytest.raises(UncertainWrite):
        await manager.update(0, {"water_budget": 50}, before.revision)
    _, mixed = before.patch(2, {"name": "Unexpected"}, 6)
    api._mock_snapshot = mixed
    await manager.refresh()
    assert manager.pending and "interrupted" in manager.error
    await manager.refresh(accept_current=True)
    assert not manager.pending and manager.error is None


async def test_storage_failure_after_write_keeps_pending_marker(manager, api):
    original = manager.store.async_save
    count = 0
    async def save(data):
        nonlocal count
        count += 1
        if count > 1:
            raise OSError("disk full")
        await original(data)
    manager.store.async_save = AsyncMock(side_effect=save)
    with pytest.raises(OSError):
        await manager.update(0, {"water_budget": 75}, manager.revision)
    assert manager.pending


@pytest.mark.parametrize("status", [
    {"controller_state": "On", "is_watering": True},
    {"controller_state": "Unknown", "is_watering": False},
    {"controller_state": "On"},
])
async def test_fresh_idle_status_required(manager, api, status):
    api.get_status.return_value = status
    with pytest.raises(InvalidSnapshot):
        await manager.update(0, {"water_budget": 75}, manager.revision)


async def test_off_preserved_for_rainfall_and_unsupported_firmware_rejected(manager, api):
    api.get_status.return_value["controller_off_mode"] = "permanent"
    with pytest.raises(InvalidSnapshot):
        await manager.update(0, {"water_budget": 75}, manager.revision, require_on=True)
    api.get_firmware_version.return_value = {"major": 6}
    with pytest.raises(InvalidSnapshot):
        await manager.update(0, {"water_budget": 75}, manager.revision)
