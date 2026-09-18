"""Exercise real protocol exchanges through fake BLE connections."""
import asyncio
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from bleak.exc import BleakError

from custom_components.solem_blip.ble import client_v2 as module, protocol
from custom_components.solem_blip.ble.client_v2 import StatelessSolemClient
from custom_components.solem_blip.ble.exceptions import SolemConnectionError, SolemDeadlineExceeded
from custom_components.solem_blip.ble.snapshot import UncertainWrite


class Radio:
    def __init__(self, responses):
        self.is_connected = True
        self.responses = responses
        self.handler = None
        self.writes = []
        self.services = [SimpleNamespace(characteristics=[SimpleNamespace(uuid=module.WRITE_CHAR_UUID, properties=['write-without-response'])])]
        self.disconnects = 0
    async def start_notify(self, uuid, handler): self.handler = handler
    async def stop_notify(self, uuid): self.handler = None
    async def write_gatt_char(self, uuid, payload, response=False):
        self.writes.append(payload)
        replies = self.responses.get(payload, [])
        for frame in (replies() if callable(replies) else replies):
            self.handler(1, bytearray(frame))
    async def disconnect(self):
        self.disconnects += 1
        self.is_connected = False


@pytest.fixture
async def wire(monkeypatch, request):
    for name in ('NOTIFY_SETTLE_DELAY','NOTIFY_PARTIAL_RETRY_DELAY','REQUEST_RETRY_DELAY','IRRIGATION_CONFIG_IDLE_TIMEOUT','STATION_NAMES_IDLE_TIMEOUT'):
        monkeypatch.setattr(module, name, 0)
    monkeypatch.setattr(module, 'STATUS_NOTIFY_TIMEOUT', .02)
    monkeypatch.setattr(module, 'REQUEST_MAX_ATTEMPTS', 1)
    import hashlib
    suffix=hashlib.sha256(request.node.name.encode()).hexdigest()[:6]
    address='AA:BB:CC:'+':'.join(suffix[i:i+2] for i in range(0,6,2))
    api=StatelessSolemClient(address, max_station_num=6)
    responses={}
    radios=[]
    async def connect():
        radio=Radio(responses)
        radios.append(radio)
        return radio
    api._connect=connect
    return api, responses, radios


async def test_metadata_all_six_names_and_reachability(wire):
    api,responses,radios=wire
    responses[protocol.pack_get_firmware_version()]=[b'invalid',bytes.fromhex('0f00010000000000000000000501070000')]
    responses[protocol.pack_get_station_names()]=[b'invalid']
    for station in range(7):
        responses[protocol.pack_get_station_names()] += [bytes([0x36,0x12,seq,station])+name.ljust(16,b'\0') for seq,name in [(1,f'Zone {station+1}'.encode()),(0,b'')]]
    await api.connect()
    assert (await api.get_firmware_version())['raw_hex']=='5.1.7'
    assert await api.get_station_names()=={i:f'Zone {i}' for i in range(1,7)}
    assert await api.get_station_name(6)=='Zone 6'
    with pytest.raises(ValueError): await api.get_station_name(7)
    assert all(not r.is_connected and r.disconnects==1 for r in radios)


async def program_exchange(wire, *, compact=False):
    api, responses, radios = wire
    seed = StatelessSolemClient('AA:BB:CC:DD:EE:04', mock=True, max_station_num=6)
    before = await seed.get_program_snapshot()
    api.last_snapshot = before
    frames, expected = before.patch(0, {'water_budget': 75}, 6)
    reads = iter([before.frames, expected.frames])
    responses[b'\x39\x00'] = lambda: list(next(reads))
    for frame in frames:
        reply = bytes([frame[0] + 1]) + (b'\x00' if compact else frame[1:])
        responses[frame] = [b'ignored', reply]
    return before, frames, expected


@pytest.mark.parametrize('compact', [False, True])
async def test_program_read_write_verify_and_no_manual_commit(wire, compact):
    api, responses, radios = wire
    before, frames, expected = await program_exchange(wire, compact=compact)
    result = await api.write_program_frames(frames, expected)
    assert result.revision == expected.revision
    assert len(radios) == 1
    assert radios[0].writes == [b'\x39\x00', *frames, b'\x39\x00']
    assert radios[0].disconnects == 1
    assert api.program_write_diagnostics['phase'] == 'verified'
    assert api.program_write_diagnostics['acknowledged_blocks'] == 7
    # Full readback still detects a controller that acknowledged but did not save.
    api.last_snapshot = before
    responses[b'\x39\x00'] = list(before.frames)
    with pytest.raises(UncertainWrite):
        await api.write_program_frames(frames, expected)
    assert api.program_write_diagnostics['phase'] == 'readback_mismatch'
    assert api.program_write_diagnostics['mismatched_blocks'] == {'0': [2]}
    assert not api.program_write_diagnostics['extra_frames_changed']


@pytest.mark.parametrize('reply', [[], [b'\x30\x12\x01\x10' + bytes(16)],
                                  [b'\x30\x12\x00\x11' + bytes(16)], [b'\x30\x00\xf0']])
async def test_program_missing_wrong_or_rejected_ack_stops_without_replay(wire, monkeypatch, reply):
    api, responses, radios = wire
    _, frames, expected = await program_exchange(wire)
    monkeypatch.setattr(module, 'REQUEST_MAX_ATTEMPTS', 3)
    responses[frames[0]] = reply
    with pytest.raises(UncertainWrite):
        await api.write_program_frames(frames, expected)
    assert len(radios) == 1
    assert radios[0].writes == [b'\x39\x00', frames[0]]
    assert radios[0].disconnects == 1
    assert api.program_write_diagnostics['acknowledged_blocks'] == 0


async def test_program_session_preflight_blocks_changed_or_missing_snapshot(wire):
    api, responses, radios = wire
    _, frames, expected = await program_exchange(wire)
    responses[b'\x39\x00'] = list(expected.frames)
    with pytest.raises(UncertainWrite):
        await api.write_program_frames(frames, expected)
    assert radios[0].writes == [b'\x39\x00']
    api.last_snapshot = None
    with pytest.raises(module.InvalidSnapshot):
        await api.write_program_frames(frames, expected)
    assert len(radios) == 1


async def test_program_cancel_after_first_block_disconnects_without_replay(wire):
    api, responses, radios = wire
    _, frames, expected = await program_exchange(wire)
    reached = asyncio.Event()
    def received():
        reached.set()
        return []
    responses[frames[0]] = received
    task = asyncio.create_task(api.write_program_frames(frames, expected))
    await reached.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(radios) == 1
    assert radios[0].writes == [b'\x39\x00', frames[0]]
    assert not radios[0].is_connected


@pytest.mark.parametrize('method',['get_status','get_firmware_version','get_station_names','get_irrigation_config'])
async def test_missing_notifications_fail_and_disconnect(wire,method):
    api,_,radios=wire
    with pytest.raises(SolemDeadlineExceeded): await getattr(api,method)()
    assert all(not r.is_connected for r in radios)


async def test_manual_commands_acknowledge_seq2_without_final_seq0(wire):
    api,responses,radios=wire
    responses[protocol.pack_commit()]=[b'invalid',bytes.fromhex('3210024200aaaaaa00014f0c10003c100000')]
    methods=[('turn_on',()),('turn_off_permanent',()),('turn_off_x_days',(1,)),
             ('sprinkle_station_x_for_y_minutes',(1,1)),('sprinkle_all_stations_for_y_minutes',(1,)),
             ('run_program_x',(1,)),('stop_manual_sprinkle',())]
    for name,args in methods:
        reply=bytearray(18)
        reply[0:4]=bytes([0x32,0x10,2,0x40])
        if name in ('turn_off_permanent','turn_off_x_days'):
            reply[3]=0
            reply[4]=1 if name=='turn_off_x_days' else 0
        if name in ('sprinkle_station_x_for_y_minutes','sprinkle_all_stations_for_y_minutes','run_program_x'):
            reply[3]=0x44 if name=='run_program_x' else 0x42
            reply[9]=1
            reply[8]=1 if name=='run_program_x' else 0
        responses[protocol.pack_commit()]=[b'invalid',bytes(reply)]
        await getattr(api,name)(*args)
        assert len(radios[-1].writes)==2
        assert radios[-1].writes[-1]==b'\x3b\x00'
    status=await api.get_status(include_raw=True)
    assert status['raw_notification_hex'].startswith('321002')
    await api.set_time(datetime(2026,6,1,12))
    assert radios[-1].writes==[protocol.pack_set_time(datetime(2026,6,1,12))]
    mock=StatelessSolemClient('AA:BB:CC:DD:EE:05',mock=True)
    for name,args in methods: await getattr(mock,name)(*args)
    await mock.set_time()
    assert await mock.get_station_name(1)=='Station 1'
    assert len(await mock.get_station_names())==mock.max_station_num
    await mock._execute_command(b'')
    with pytest.raises(SolemConnectionError): await mock._run_operation(AsyncMock())


async def test_mutation_timeout_never_replayed(wire,monkeypatch):
    api,_,radios=wire
    monkeypatch.setattr(module,'REQUEST_MAX_ATTEMPTS',3)
    with pytest.raises(UncertainWrite): await api.turn_on()
    assert len(radios)==1
    assert radios[0].writes==[protocol.pack_turn_on(),protocol.pack_commit()]


async def test_notify_failures_retry_subscription_and_disconnect(wire):
    api,_,radios=wire
    radio=Radio({})
    radio.start_notify=AsyncMock(side_effect=BleakError('subscribe failed'))
    radio.stop_notify=AsyncMock(side_effect=BleakError('not subscribed'))
    api._connect=AsyncMock(return_value=radio)
    with pytest.raises(SolemDeadlineExceeded): await api.get_status()
    assert radio.start_notify.await_count==3 and not radio.is_connected


async def test_write_failure_is_uncertain_and_disconnects(wire):
    api,_,_=wire
    radio=Radio({})
    radio.write_gatt_char=AsyncMock(side_effect=BleakError('lost'))
    api._connect=AsyncMock(return_value=radio)
    with pytest.raises(UncertainWrite): await api.set_time()
    assert radio.write_gatt_char.await_count==1 and not radio.is_connected
    with pytest.raises(SolemConnectionError): await api._write(radio,b'')
    with pytest.raises(SolemConnectionError): api._ensure_client(radio,'test')


async def test_repeated_cancellation_waits_for_disconnect_and_serializes(wire):
    api,_,_=wire
    started=asyncio.Event(); release=asyncio.Event()
    radio=Radio({})
    async def disconnect():
        started.set()
        await release.wait()
        radio.is_connected=False
    radio.disconnect=disconnect
    api._connect=AsyncMock(return_value=radio)
    task=asyncio.create_task(api._run_operation(lambda c: asyncio.sleep(99)))
    await asyncio.sleep(0.01)
    task.cancel()
    await started.wait()
    task.cancel()
    assert not task.done()
    second=StatelessSolemClient(api.mac_address)
    acquired=asyncio.Event()
    async def claim():
        async with second.transaction(): acquired.set()
    waiter=asyncio.create_task(claim())
    await asyncio.sleep(0)
    assert not acquired.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError): await task
    await waiter
    assert acquired.is_set() and not radio.is_connected


async def test_failed_disconnect_blocks_further_controller_operations(wire):
    api,_,_=wire
    radio=Radio({})
    radio.disconnect=AsyncMock(side_effect=BleakError('disconnect failed'))
    api._connect=AsyncMock(return_value=radio)
    await api.set_time()
    assert radio.disconnect.await_count==2
    with pytest.raises(RuntimeError,match='cleanup failed'):
        await api.set_time()


async def test_cancel_during_connection_releases_partially_connected_client(monkeypatch):
    connected=asyncio.Event()
    made=[]
    class PartialRadio(Radio):
        def __init__(self,*args,**kwargs):
            super().__init__({})
            made.append(self)
    async def establish(cls,device,**kwargs):
        client=cls(device)
        connected.set()
        await asyncio.Event().wait()
        return client
    monkeypatch.setattr(module,'BleakClientWithServiceCache',PartialRadio)
    monkeypatch.setattr(module,'establish_connection',establish)
    api=StatelessSolemClient('AA:BB:CC:DD:EE:06',ble_device_resolver=lambda:object())
    task=asyncio.create_task(api.get_status())
    await connected.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert made and not made[0].is_connected
    assert api._active_client is None and api._connecting_client is None


async def test_resolver_paths_and_cached_callback(monkeypatch):
    from bleak.backends.device import BLEDevice
    found=BLEDevice('AA:BB:CC:DD:EE:07','Test',{})
    api=StatelessSolemClient(found.address,ble_device_resolver=lambda:found)
    assert api._ble_device_callback() is found
    assert await api._resolve_ble_device() is found
    api._ble_device_resolver=None
    assert api._ble_device_callback() is found
    api._ble_device=None
    with pytest.raises(SolemConnectionError): api._ble_device_callback()
    monkeypatch.setattr(module.BleakScanner,'find_device_by_address',AsyncMock(return_value=None))
    monkeypatch.setattr(module.BleakScanner,'discover',AsyncMock(return_value=[found]))
    assert await api._resolve_ble_device() is found
    api._ble_device=None
    monkeypatch.setattr(module.BleakScanner,'discover',AsyncMock(return_value=[]))
    with pytest.raises(SolemConnectionError): await api._resolve_ble_device()
    monkeypatch.setattr(module.BleakScanner,'find_device_by_address',AsyncMock(return_value=found))
    assert await api._resolve_ble_device() is found


@pytest.mark.parametrize('error',[BleakError('failed'),OSError('failed'),RuntimeError('unexpected')])
async def test_connect_failures_are_normalized(monkeypatch,error):
    api=StatelessSolemClient('AA:BB:CC:DD:EE:08',ble_device_resolver=lambda:object())
    monkeypatch.setattr(module,'establish_connection',AsyncMock(side_effect=error))
    with pytest.raises(SolemConnectionError): await api._connect()


async def test_subscription_cancellation_and_wait_event(wire):
    api,_,_=wire
    radio=Radio({})
    radio.start_notify=AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError): await api._start_notify(radio,lambda *_:None)
    event=asyncio.Event()
    async def notify():
        await asyncio.sleep(.001)
        event.set()
    notifier=asyncio.create_task(notify())
    await api._wait_for_event(event,1,'test')
    await notifier
    assert event.is_set()


async def test_seq0_command_ack_without_status(wire):
    api,responses,_=wire
    responses[protocol.pack_commit()]=[bytes.fromhex('321000')]
    api.get_status=AsyncMock(return_value={'controller_state':'On'})
    await api.turn_on()
    api.get_status.assert_awaited_once()


@pytest.mark.parametrize('services',[None,[]])
async def test_unsuitable_device_disconnects(wire,services):
    api,_,_=wire
    radio=Radio({});radio.services=services
    api._connect=AsyncMock(return_value=radio)
    with pytest.raises(SolemDeadlineExceeded): await api.connect()
    assert not radio.is_connected


async def test_wrong_station_acknowledgement_is_uncertain_not_success(wire):
    api,responses,radios=wire
    responses[protocol.pack_commit()]=[bytes.fromhex('3210024200aaaaaa00014f0c10003c100000')]
    with pytest.raises(UncertainWrite,match='does not confirm'):
        await api.sprinkle_station_x_for_y_minutes(6,1)
    assert len(radios)==2
    assert radios[0].writes==[protocol.pack_sprinkle_station(6,1),protocol.pack_commit()]
    assert radios[1].writes==[protocol.pack_commit()]


async def test_final_ack_followed_by_unavailable_status_is_uncertain(wire):
    api,responses,_=wire
    responses[protocol.pack_commit()]=[bytes.fromhex('321000')]
    with pytest.raises(UncertainWrite,match='verification failed'):
        await api.turn_on()


@pytest.mark.parametrize('short_ack', [True, False])
async def test_station_name_write_frames_and_complete_readback(wire, short_ack):
    from tests.test_station_names import name_frames
    from custom_components.solem_blip.ble.station_names import pack_station_name
    api, responses, radios = wire
    seed = StatelessSolemClient('AA:BB:CC:DD:EE:20', mock=True, max_station_num=6)
    before = await seed.get_station_name_snapshot()
    responses[b'\x35\x00'] = [b'ignored'] + name_frames(before)
    assert (await api.get_station_name_snapshot()).revision == before.revision
    expected = before.renamed(6, '06 - Garden', 6)
    readbacks = iter([name_frames(before), name_frames(expected)])
    responses[b'\x35\x00'] = lambda: next(readbacks)
    for frame in pack_station_name(6, '06 - Garden', 6):
        responses[frame] = [b'ignored', b'\x34\x00' if short_ack else b'\x34' + frame[1:]]
    assert await api.write_station_name(6, '06 - Garden', expected, before=before) == expected
    assert radios[-1].writes == [b'\x35\x00', *pack_station_name(6, '06 - Garden', 6), b'\x35\x00']
    assert api.station_name_write_diagnostics['phase'] == 'verified'
    assert api.station_name_write_diagnostics['acknowledged_parts'] == 2
    assert all(not r.is_connected and r.disconnects == 1 for r in radios)
    assert all(b'\x3b\x00' not in r.writes for r in radios)
    responses[b'\x35\x00'] = name_frames(before)
    with pytest.raises(UncertainWrite): await api.write_station_name(6, '06 - Garden', expected, before=before)


async def test_station_name_partial_read_and_partial_write_disconnect_without_retry(wire, monkeypatch):
    from tests.test_station_names import name_frames
    from custom_components.solem_blip.ble.snapshot import InvalidSnapshot
    api, responses, radios = wire
    seed = StatelessSolemClient('AA:BB:CC:DD:EE:21', mock=True, max_station_num=6)
    before = await seed.get_station_name_snapshot()
    responses[b'\x35\x00'] = name_frames(before)[:-1]
    with pytest.raises(SolemDeadlineExceeded): await api.get_station_name_snapshot()
    assert all(not r.is_connected for r in radios)
    radio = Radio({})
    async def write_half(uuid, payload, response=False):
        if payload == b'\x35\x00':
            for frame in name_frames(before): radio.handler(1, bytearray(frame))
            return
        radio.writes.append(payload)
        if len(radio.writes) == 1:
            radio.handler(1, bytearray(b'\x34' + payload[1:]))
        else:
            raise BleakError('dropped after first half')
    radio.write_gatt_char = AsyncMock(side_effect=write_half)
    api._connect = AsyncMock(return_value=radio)
    monkeypatch.setattr(module, 'REQUEST_MAX_ATTEMPTS', 3)
    with pytest.raises(UncertainWrite): await api.write_station_name(6, 'New', before.renamed(6,'New',6), before=before)
    assert radio.write_gatt_char.await_count == 3 and not radio.is_connected
    api._connect.assert_awaited_once()


@pytest.mark.parametrize('reply', [b'', b'\x34', b'\x34\x12', b'\x36\x12\x00', b'\x34\x12\x01', b'\x34\x12\xf0', b'\x34\x12\x00\xf0'])
async def test_station_name_requires_ack_before_second_half(wire, monkeypatch, reply):
    api, responses, radios = wire
    seed = StatelessSolemClient('AA:BB:CC:DD:EE:23', mock=True, max_station_num=6)
    before = await seed.get_station_name_snapshot()
    from custom_components.solem_blip.ble.station_names import pack_station_name
    frames = pack_station_name(4, 'Garden left', 6)
    from tests.test_station_names import name_frames
    responses[b'\x35\x00'] = name_frames(before)
    responses[frames[0]] = [reply]
    monkeypatch.setattr(module, 'REQUEST_MAX_ATTEMPTS', 3)
    with pytest.raises(UncertainWrite):
        await api.write_station_name(4, 'Garden left', before.renamed(4,'Garden left',6), before=before)
    assert len(radios) == 1 and radios[0].writes == [b'\x35\x00', *frames[:1]]
    assert not radios[0].is_connected


async def test_station_name_cancel_while_waiting_for_ack_does_not_send_next_frame(wire):
    api, responses, radios = wire
    seed = StatelessSolemClient('AA:BB:CC:DD:EE:24', mock=True, max_station_num=6)
    before = await seed.get_station_name_snapshot()
    from custom_components.solem_blip.ble.station_names import pack_station_name
    frames = pack_station_name(4, 'Garden left', 6)
    sent = asyncio.Event()
    radio = Radio({})
    async def send(uuid, payload, response=False):
        if payload == b'\x35\x00':
            from tests.test_station_names import name_frames
            for frame in name_frames(before): radio.handler(1, bytearray(frame))
            return
        radio.writes.append(payload)
        sent.set()
    radio.write_gatt_char = send
    api._connect = AsyncMock(return_value=radio)
    task = asyncio.create_task(api.write_station_name(4, 'Garden left', before.renamed(4,'Garden left',6), before=before))
    await sent.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError): await task
    assert radio.writes == frames[:1] and not radio.is_connected


@pytest.mark.parametrize('wrong_reply', [
    bytes.fromhex('34120005') + bytes(16),  # Late duplicate of part 0.
    bytes.fromhex('34120104') + bytes(16),  # Correct part, wrong output.
    bytes.fromhex('34120105'),             # Truncated full acknowledgement.
    bytes.fromhex('34110105') + bytes(16),  # Wrong payload length declaration.
])
async def test_second_name_ack_must_match_part_output_and_layout(wire, wrong_reply):
    from custom_components.solem_blip.ble.station_names import pack_station_name
    api, responses, radios = wire
    seed = StatelessSolemClient('AA:BB:CC:DD:EE:25', mock=True, max_station_num=6)
    before = await seed.get_station_name_snapshot()
    frames = pack_station_name(6, 'Garden right', 6)
    responses[frames[0]] = [b'\x34' + frames[0][1:]]
    responses[frames[1]] = [wrong_reply]
    from tests.test_station_names import name_frames
    responses[b'\x35\x00'] = name_frames(before)
    with pytest.raises(UncertainWrite):
        await api.write_station_name(6, 'Garden right', before.renamed(6,'Garden right',6), before=before)
    assert len(radios) == 1 and radios[0].writes == [b'\x35\x00', *frames]
    assert api.station_name_write_diagnostics['acknowledged_parts'] == 1
    assert not radios[0].is_connected


async def test_second_name_part_one_ack_advances_to_verified_readback(wire):
    """The second write reply uses part 1, never a remaining-frame count of 0."""
    from custom_components.solem_blip.ble.station_names import pack_station_name
    from tests.test_station_names import name_frames
    api, responses, radios = wire
    seed = StatelessSolemClient('AA:BB:CC:DD:EE:26', mock=True, max_station_num=6)
    before = await seed.get_station_name_snapshot()
    expected = before.renamed(6, 'Garden right', 6)
    frames = pack_station_name(6, 'Garden right', 6)
    responses[frames[0]] = [b'\x34' + frames[0][1:]]
    # A delayed duplicate first reply cannot complete the second part.
    responses[frames[1]] = [b'\x34' + frames[0][1:], bytes.fromhex('34120105') + frames[1][4:]]
    readbacks = iter([name_frames(before), name_frames(expected)])
    responses[b'\x35\x00'] = lambda: next(readbacks)
    assert await api.write_station_name(6, 'Garden right', expected, before=before) == expected
    assert api.station_name_write_diagnostics == {
        'phase':'verified', 'acknowledged_parts':2, 'part':1,
        'last_reply_header':'34120105', 'last_reply_length':20,
    }
    assert len(radios) == 1 and radios[0].writes == [b'\x35\x00', *frames, b'\x35\x00']


@pytest.mark.parametrize('changed', [False, True])
async def test_name_session_preflight_requires_live_unchanged_names(wire, changed):
    """A dead notification path or stale names must not receive name writes."""
    from tests.test_station_names import name_frames
    api, responses, radios = wire
    seed = StatelessSolemClient('AA:BB:CC:DD:EE:27', mock=True, max_station_num=6)
    before = await seed.get_station_name_snapshot()
    if changed:
        responses[b'\x35\x00'] = name_frames(before.renamed(2, 'Phone edit', 6))
    with pytest.raises(UncertainWrite):
        await api.write_station_name(6, 'Garden right', before.renamed(6,'Garden right',6), before=before)
    assert len(radios) == 1 and radios[0].writes == [b'\x35\x00']
    assert not radios[0].is_connected
    assert api.station_name_write_diagnostics['acknowledged_parts'] == 0
