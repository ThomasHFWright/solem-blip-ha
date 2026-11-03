"""Focused checks for the integration's local BLE resilience changes."""

import asyncio
from unittest.mock import AsyncMock

from bleak.backends.device import BLEDevice
from bleak.exc import BleakError
import pytest

from custom_components.solem_blip.ble import client_v2 as module
from custom_components.solem_blip.ble.client_v2 import StatelessSolemClient
from custom_components.solem_blip.ble.exceptions import SolemConnectionError
from custom_components.solem_blip.ble.exceptions import StaleProgram


class Radio:
    """Small connected-client stand-in."""

    def __init__(self, *_args, **_kwargs):
        self.is_connected = True

    async def disconnect(self) -> None:
        self.is_connected = False


async def test_ha_resolver_is_never_hidden_by_cached_route() -> None:
    old = BLEDevice("AA:BB:CC:DD:EE:01", "old", {})
    new = BLEDevice(old.address, "new", {"source": "replacement-proxy"})
    route = new
    client = StatelessSolemClient(old.address, ble_device_resolver=lambda: route)
    client._ble_device = old
    client._ble_device_cached_at = 1e20

    assert await client._resolve_ble_device() is new


async def test_failed_cleanup_recovers_before_next_connection(monkeypatch) -> None:
    monkeypatch.setattr(module, "DISCONNECT_CLEANUP_TIMEOUT", 0.01)
    address = "AA:BB:CC:DD:EE:02"
    stuck = Radio()
    stuck.disconnect = AsyncMock(side_effect=BleakError("busy"))
    first = StatelessSolemClient(address)
    first._connect = AsyncMock(return_value=stuck)

    await first._run_operation(AsyncMock())
    assert first.cleanup_pending

    replacement = StatelessSolemClient(address)
    replacement._connect = AsyncMock(return_value=Radio())
    with pytest.raises(SolemConnectionError, match="cleanup still pending"):
        await replacement._run_operation(AsyncMock())
    replacement._connect.assert_not_awaited()

    stuck.is_connected = False
    await replacement._run_operation(AsyncMock())
    replacement._connect.assert_awaited_once()
    assert not replacement.cleanup_pending


async def test_cancelled_connect_releases_partial_client(monkeypatch) -> None:
    made: list[Radio] = []
    connected = asyncio.Event()

    class PartialRadio(Radio):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            made.append(self)

    async def establish(client_class, device, **_kwargs):
        client_class(device)
        connected.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "BleakClientWithServiceCache", PartialRadio)
    monkeypatch.setattr(module, "establish_connection", establish)
    client = StatelessSolemClient(
        "AA:BB:CC:DD:EE:03", ble_device_resolver=lambda: object()
    )
    task = asyncio.create_task(client.get_status())
    await connected.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert made and not made[0].is_connected
    assert client._active_client is None and client._connecting_client is None


async def test_clients_for_one_controller_are_serialized() -> None:
    address = "AA:BB:CC:DD:EE:04"
    entered = asyncio.Event()
    release = asyncio.Event()
    first = StatelessSolemClient(address)
    second = StatelessSolemClient(address)
    first._connect = AsyncMock(return_value=Radio())
    second._connect = AsyncMock(return_value=Radio())

    async def hold(_client):
        entered.set()
        await release.wait()

    first_task = asyncio.create_task(first._run_operation(hold))
    await entered.wait()
    second_task = asyncio.create_task(second._run_operation(AsyncMock()))
    await asyncio.sleep(0)
    second._connect.assert_not_awaited()
    release.set()
    await asyncio.gather(first_task, second_task)


async def test_program_edit_is_revision_guarded_and_preserves_hidden_stations() -> None:
    client = StatelessSolemClient(
        "AA:BB:CC:DD:EE:05", mock=True, max_station_num=6
    )
    before = await client.get_program_snapshot()
    _, with_hidden = before.patch(0, {"station_durations": {7: 777}}, 12)
    client._mock_snapshot = with_hidden
    program = dict(with_hidden.programs[0])
    program["water_budget"] = 75
    program["station_durations"] = program["station_durations"][:6]

    programs = await client.set_irrigation_program(
        0, program, before_revision=with_hidden.revision
    )

    assert programs[0]["water_budget"] == 75
    assert programs[0]["station_durations"][6] == 777
    with pytest.raises(StaleProgram):
        await client.set_irrigation_program(
            0, program, before_revision="stale editor revision"
        )

