"""Identification-name reads and HA identity/override preservation."""
import asyncio

import pytest
from homeassistant.helpers import device_registry as dr

from custom_components.solem_blip.ble import client_v2, protocol
from custom_components.solem_blip.ble.client_v2 import StatelessSolemClient
from custom_components.solem_blip.const import CONTROLLER_MAC_ADDRESS, DOMAIN
from custom_components.solem_blip.controller_name import CONTROLLER_NAME, apply_controller_name
from custom_components.solem_blip.coordinator import SolemCoordinator
from custom_components.solem_blip.entity_descriptions import SENSOR_DESCRIPTIONS
from custom_components.solem_blip.sensor import BatterySensor

ADDRESS = "AA:BB:CC:DD:EE:FF"
FIRMWARE = bytes.fromhex("100f010000000000000000000501070000")
NAME = b"\x10\x10\x00" + b"Garden unit".ljust(15, b"\0")


@pytest.mark.parametrize(("frame", "expected"), [
    (NAME, "Garden unit"),
    (NAME + b"\0\0", "Garden unit"),
    (b"\x10\x10\x00" + b"ABCDEFGHIJKLMNO", "ABCDEFGHIJKLMNO"),
    (b"\x10\x11\x00" + b"A" * 16, None),
    (b"\x10\x0f\x00" + b"ABCDEFGHIJKLMN", "ABCDEFGHIJKLMN"),
    (b"\x10\x0f\x00" + "Jardin été".encode().ljust(14, b"\0"), "Jardin été"),
    (NAME[:-1], None), (NAME + b"X", None), (FIRMWARE, None),
    (b"\x10\x0f\x00" + b"\0" * 14, None),
    (b"\x10\x0f\x00" + b"Bad\nname".ljust(14, b"\0"), None),
    (b"\x10\x0f\x00" + b"\xff" * 14, None),
])
def test_name_record_decoding(frame, expected):
    assert protocol.parse_controller_name_response(frame) == expected


@pytest.mark.parametrize("order", ["name_first", "firmware_first", "missing", "malformed", "cancel"])
async def test_identification_uses_one_session_and_optional_name(monkeypatch, order):
    from tests.ble.test_client_v2 import FakeV2Client
    fake = FakeV2Client()
    monkeypatch.setattr(client_v2, "NOTIFY_SETTLE_DELAY", 0)
    monkeypatch.setattr(client_v2, "IDENTIFICATION_NAME_TIMEOUT", 0.02)
    sent_firmware = asyncio.Event()
    pending = []

    async def connect(self):
        return fake

    async def write(_uuid, payload, *, response):
        fake.writes.append(payload)
        assert payload == b"\x0f\x00"
        if order == "name_first":
            fake.handler(1, bytearray(NAME))
        fake.handler(1, bytearray(FIRMWARE))
        sent_firmware.set()
        if order in ("firmware_first", "malformed"):
            async def delayed_name():
                await asyncio.sleep(0.001)
                fake.handler(1, bytearray(NAME if order == "firmware_first" else NAME[:-1]))
            pending.append(asyncio.create_task(delayed_name()))

    monkeypatch.setattr(StatelessSolemClient, "_connect", connect)
    fake.write_gatt_char = write
    client = StatelessSolemClient(ADDRESS)
    task = asyncio.create_task(client.get_firmware_version())
    if order == "cancel":
        await sent_firmware.wait()
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result["raw_hex"] == "5.1.7"
        assert result.get("controller_name") == (
            "Garden unit" if order in ("name_first", "firmware_first") else None
        )
    await asyncio.gather(*pending)
    assert fake.writes == [b"\x0f\x00"]  # Never commit or write names.
    assert fake.disconnects == 1
    assert not fake.is_connected


@pytest.mark.parametrize("overridden", [True, False])
async def test_name_metadata_preserves_ha_identity_and_custom_labels(
    hass, coordinator, mock_config_entry, mock_solem_client, overridden,
):
    mock_config_entry.add_to_hass(hass)
    title = "My irrigation" if overridden else mock_config_entry.data[CONTROLLER_MAC_ADDRESS]
    hass.config_entries.async_update_entry(mock_config_entry, title=title)
    registry = dr.async_get(hass)
    device = registry.async_get_or_create(
        config_entry_id=mock_config_entry.entry_id,
        identifiers={(DOMAIN, ADDRESS)},
        connections={(dr.CONNECTION_BLUETOOTH, ADDRESS)}, name=ADDRESS,
    )
    if overridden:
        registry.async_update_device(device.id, name_by_user="Custom label")
    descriptor = next(d for d in coordinator.data if d["device_type"] == "BATTERY_SENSOR")
    entity = BatterySensor(coordinator, descriptor, "state", SENSOR_DESCRIPTIONS["BATTERY_SENSOR"])
    original_info, original_id = entity.device_info, entity.unique_id
    mock_solem_client.get_firmware_version.return_value = {
        "raw_hex": "5.1.7", "controller_name": "Garden unit",
    }
    await coordinator._fetch_device_metadata()
    assert coordinator.firmware_version == "5.1.7"
    for name in ("Garden unit", "Second name"):
        apply_controller_name(coordinator, name)
        apply_controller_name(coordinator, name)
        assert registry.async_get(device.id).name == name
        assert registry.async_get(device.id).name_by_user == ("Custom label" if overridden else None)
        assert mock_config_entry.title == (title if overridden else name)
        assert mock_config_entry.data[CONTROLLER_NAME] == name
        assert entity.device_info["name"] == name
        assert entity.unique_id == original_id
        assert entity.device_info["identifiers"] == original_info["identifiers"]
        assert entity.device_info["connections"] == original_info["connections"]
    restored = SolemCoordinator(hass, mock_config_entry)
    assert restored.controller_name == "Second name"


async def test_missing_registry_and_entry_are_safe(hass, coordinator, mock_config_entry):
    mock_config_entry.add_to_hass(hass)
    apply_controller_name(coordinator, "Garden unit")
    coordinator.config_entry = None
    apply_controller_name(coordinator, "Ignored")
    assert coordinator.controller_name == "Garden unit"
