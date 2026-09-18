"""Onboard naming safety: complete reads, byte limits, conflicts and recovery."""
import asyncio
from unittest.mock import AsyncMock, PropertyMock, patch

import pytest

from custom_components.solem_blip.ble.client_v2 import StatelessSolemClient
from custom_components.solem_blip.ble.snapshot import InvalidSnapshot, StaleProgram, UncertainWrite
from custom_components.solem_blip.ble.station_names import StationNameSnapshot, pack_station_name
from custom_components.solem_blip.station_names import StationNameManager
from custom_components.solem_blip.config_flow import SolemOptionsFlowHandler
from custom_components.solem_blip.config_entry import RuntimeData


def name_frames(snapshot):
    result = []
    for station, raw in sorted(snapshot.raw_names.items()):
        seq = (len(snapshot.raw_names) - station) * 2
        result += [bytes([0x36, 0x12, seq + 1, station - 1]) + raw[:16],
                   bytes([0x36, 0x12, seq, station - 1]) + raw[16:]]
    return result


@pytest.fixture
async def names(hass):
    api = StatelessSolemClient('AA:BB:CC:DD:EE:19', mock=True, max_station_num=6)
    api.get_status = AsyncMock(return_value={'is_watering': False, 'controller_state': 'Off'})
    api.get_firmware_version = AsyncMock(return_value={'major': 5})
    manager = StationNameManager(hass, 'names-test', api)
    await manager.load()
    await manager.refresh()
    return manager


@pytest.mark.parametrize('name', ['a'*32, 'a'*15+'é'+'b'*15, '🌱'*8])
def test_utf8_exact_limit_split_and_station_six(name):
    frames = pack_station_name(6, name, 6)
    assert [f[:4] for f in frames] == [b'\x33\x12\x00\x05', b'\x33\x12\x01\x05']
    assert all(len(f) == 20 for f in frames)
    assert frames[0][4:] + frames[1][4:] == name.encode()


@pytest.mark.parametrize('station,name', [(0,'ok'), (7,'ok'), (True,'ok'), (1.5,'ok'), (1,''), (1,'  '), (1,'a\0b'), (1,'a'*33), (1,'🌱'*9), (1,None)])
def test_invalid_names_never_encode(station, name):
    with pytest.raises(ValueError): pack_station_name(station, name, 6)


async def test_complete_names_preserve_unused_slots_unicode_and_order(names):
    before = names.snapshot
    after = before.renamed(6, 'a'*15+'é'+'b'*15, 6)
    frames = name_frames(after)
    actual = StationNameSnapshot.from_frames(list(reversed(frames)) + [frames[0]], 6)
    assert actual.revision == after.revision
    assert actual.names[6] == 'a'*15+'é'+'b'*15
    assert all(actual.raw_names[i] == before.raw_names[i] for i in range(1,13) if i != 6)
    for missing in (0, 5, 22, 23):
        with pytest.raises(InvalidSnapshot):
            StationNameSnapshot.from_frames(frames[:missing]+frames[missing+1:], 6)
    for bad in (b'', b'\x36\x12\x00\x0c'+bytes(16), b'\x00'*20, frames[0][:-1]+b'X'):
        with pytest.raises(InvalidSnapshot): StationNameSnapshot.from_frames(frames+[bad], 6)
    with pytest.raises(InvalidSnapshot): StationNameSnapshot.from_frames([], 6)
    with pytest.raises(InvalidSnapshot): StationNameSnapshot.from_frames(frames[12:], 6)


async def test_only_selected_name_changes_off_and_programs_preserved(names, hass):
    api = names.api
    programs = await api.get_program_snapshot()
    before = names.snapshot
    api.write_station_name = AsyncMock(wraps=api.write_station_name)
    assert await names.update(1, before.names[1], before.revision) == before
    api.write_station_name.assert_not_awaited()
    after = await names.update(6, '06 - Kitchen garden', before.revision)
    assert after.names[6] == '06 - Kitchen garden'
    assert await api.get_station_name(6) == '06 - Kitchen garden'
    assert (await api.get_program_snapshot()).revision == programs.revision
    assert (await api.get_status())['controller_state'] == 'Off'
    assert names.pending is None and names.last_write
    assert {k:v for k,v in after.raw_names.items() if k != 6} == {k:v for k,v in before.raw_names.items() if k != 6}
    restored = StationNameManager(hass, 'names-test', api)
    await restored.load()
    assert restored.last_write == names.last_write and restored.snapshot is None


async def test_stale_foreign_station_change_and_invalid_input_do_not_write(names):
    before = names.snapshot
    names.api._mock_station_names = before.renamed(2, 'Changed in phone', 6)
    names.api.write_station_name = AsyncMock()
    with pytest.raises(StaleProgram): await names.update(6, 'New name', before.revision)
    names.api.write_station_name.assert_not_awaited()
    names.api.get_status.reset_mock()
    with pytest.raises(ValueError): await names.update(6, '🌱'*9, before.revision)
    names.api.get_status.assert_not_awaited()


@pytest.mark.parametrize('status,firmware', [({'is_watering':True,'controller_state':'On'},5), ({'controller_state':'Off'},5), ({'is_watering':False,'controller_state':'Unknown'},5), ({'is_watering':False,'controller_state':'Off'},6)])
async def test_busy_unknown_and_unsupported_fail_closed(names, status, firmware):
    names.api.get_status.return_value = status
    names.api.get_firmware_version.return_value = {'major':firmware}
    names.api.write_station_name = AsyncMock()
    with pytest.raises(InvalidSnapshot): await names.update(1,'New',names.snapshot.revision)
    names.api.write_station_name.assert_not_awaited()


@pytest.mark.parametrize('error', [UncertainWrite('lost'), asyncio.CancelledError()])
async def test_interruption_persists_journal_and_does_not_replay(names, hass, error):
    before = names.snapshot
    names.api.write_station_name = AsyncMock(side_effect=error)
    with pytest.raises(type(error)): await names.update(6,'New',before.revision)
    assert names.pending
    restored = StationNameManager(hass, 'names-test', names.api)
    await restored.load()
    assert restored.pending == names.pending
    with pytest.raises(UncertainWrite): await restored.update(6,'New',before.revision)
    assert names.api.write_station_name.await_count == 1
    await restored.refresh()
    assert restored.pending is None  # Device still has exact old names.


async def test_partial_write_requires_explicit_review(names):
    before = names.snapshot
    names.api.write_station_name = AsyncMock(side_effect=UncertainWrite('lost'))
    with pytest.raises(UncertainWrite): await names.update(6, 'New long name', before.revision)
    names.api._mock_station_names = before.renamed(6, 'Partial name', 6)
    await names.refresh()
    assert names.pending
    await names.refresh(accept_current=True)
    assert names.pending is None and names.snapshot.names[6] == 'Partial name'
    assert names.api.write_station_name.await_count == 1


async def test_storage_failure_never_allows_unjournalled_write(names):
    names.store.async_save = AsyncMock(side_effect=OSError('disk full'))
    names.api.write_station_name = AsyncMock()
    with pytest.raises(OSError): await names.update(1, 'New', names.snapshot.revision)
    names.api.write_station_name.assert_not_awaited()
    assert names.pending
    with pytest.raises(OSError): await names.refresh()
    assert names.pending


async def test_final_storage_failure_keeps_recoverable_journal(names):
    names.store.async_save = AsyncMock(side_effect=[None, OSError('disk full'), None])
    with pytest.raises(OSError): await names.update(6, 'New', names.snapshot.revision)
    assert names.pending and names.snapshot.names[6] == 'New'
    await names.refresh()
    assert names.pending is None


@pytest.fixture
async def editor(hass, mock_config_entry, coordinator, names):
    mock_config_entry.add_to_hass(hass)
    coordinator.station_name_manager = names
    coordinator.api = names.api
    coordinator.num_stations = 6
    coordinator.async_update_all_sensors = AsyncMock(return_value={})
    mock_config_entry.runtime_data = RuntimeData(coordinator)
    handler = SolemOptionsFlowHandler()
    with patch.object(SolemOptionsFlowHandler, 'config_entry', new_callable=PropertyMock, return_value=mock_config_entry):
        yield handler, coordinator, mock_config_entry


async def test_editor_reads_and_saves_only_after_submit(editor):
    flow, co, entry = editor
    names = co.station_name_manager
    co.api.write_station_name = AsyncMock(wraps=co.api.write_station_name)
    result = await flow.async_step_station_select()
    assert result['step_id'] == 'station_select'
    result = await flow.async_step_station_select({'station':'6'})
    assert result['step_id'] == 'station_name'
    assert result['data_schema']({}) == {'name':'Station 6'}
    co.api.write_station_name.assert_not_awaited()
    result = await flow.async_step_station_name({'name':'06 - Garden'})
    assert result['type'] == 'create_entry' and result['data'] == dict(entry.options)
    assert names.snapshot.names[6] == co.station_names[6] == '06 - Garden'
    assert len(co.station_names) == 6


@pytest.mark.parametrize('error,pending,key', [(StaleProgram('changed'),False,'stale_station_name'), (ValueError('bad'),False,'invalid_station_name'), (OSError('offline'),False,'station_name_failed'), (UncertainWrite('lost'),True,'station_name_uncertain')])
async def test_editor_write_errors_keep_draft(editor, error, pending, key):
    flow, co, _ = editor
    await flow.async_step_station_select({'station':'1'})
    co.rename_station = AsyncMock(side_effect=error)
    co.station_name_manager.pending = {'pending':True} if pending else None
    result = await flow.async_step_station_name({'name':'Typed name'})
    assert key in result['errors'].values()
    assert result['data_schema']({})['name'] == 'Typed name'


async def test_editor_recovery_requires_explicit_accept(editor):
    flow, co, _ = editor
    before = co.station_name_manager.snapshot
    co.station_name_manager.pending = {'before_revision':'old','expected_revision':'expected'}
    result = await flow.async_step_station_select({'station':'6'})
    assert result['errors']['base'] == 'station_name_uncertain'
    result = await flow.async_step_station_select({'station':'6','accept_current':True})
    assert result['step_id'] == 'station_name' and co.station_name_manager.pending is None
    assert co.station_name_manager.snapshot == before


async def test_editor_read_failure_and_unloaded_abort(editor):
    flow, co, entry = editor
    co.refresh_station_names = AsyncMock(side_effect=OSError('offline'))
    assert (await flow.async_step_station_select())['reason'] == 'station_names_read_failed'
    assert (await flow.async_step_station_name())['reason'] == 'station_names_read_failed'
    entry.runtime_data = None
    assert (await flow.async_step_station_select())['type'] == 'abort'


async def test_editor_recovery_read_failure_and_invalid_station(editor):
    flow, co, _ = editor
    await flow.async_step_station_select()
    assert (await flow.async_step_station_select({'station':'7'}))['type'] == 'abort'
    co.station_name_manager.pending = {'before_revision':'old','expected_revision':'new'}
    co.refresh_station_names = AsyncMock(side_effect=OSError('offline'))
    assert (await flow.async_step_station_select({'station':'1','accept_current':True}))['type'] == 'abort'
