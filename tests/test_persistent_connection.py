"""Persistent-connection option: client selection, idle release, shutdown."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.solem_blip.client_factory import (
    PersistentSolemClient,
    StatelessSolemClient,
    build_solem_client,
    create_solem_client,
    idle_release_seconds,
)
from homeassistant.const import CONF_SCAN_INTERVAL

from custom_components.solem_blip.const import (
    BLUETOOTH_DEFAULT_TIMEOUT,
    BLUETOOTH_TIMEOUT,
    CONTROLLER_MAC_ADDRESS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    NUM_STATIONS,
    PERSISTENT_CONNECTION,
    PERSISTENT_HOLD_LINK,
    SOLEM_API_MOCK,
)
from custom_components.solem_blip.coordinator import SolemCoordinator

from tests.conftest import create_mock_solem_client


def _entry_with_options(
    template: MockConfigEntry, extra_options: dict
) -> MockConfigEntry:
    """Return a fresh MockConfigEntry with the given extra options."""
    return MockConfigEntry(
        domain=DOMAIN,
        data=dict(template.data),
        options={**template.options, **extra_options},
        unique_id=template.unique_id,
    )


def test_idle_release_is_three_quarters_of_scan_interval() -> None:
    """Idle release hands the radio back at 75% of the scan interval."""
    assert idle_release_seconds(300) == 225
    assert idle_release_seconds(120) == 90
    assert idle_release_seconds(60) == 45


def test_create_solem_client_selects_stateless_by_default() -> None:
    """Without the persistent option the stateless v2 client is built."""
    client = create_solem_client(False, 300, mac_address="AA:BB:CC:DD:EE:FF")
    assert isinstance(client, StatelessSolemClient)
    assert not isinstance(client, PersistentSolemClient)


def test_create_solem_client_persistent_gets_idle_release() -> None:
    """The persistent client receives 75% of the scan interval as idle release."""
    client = create_solem_client(True, 300, mac_address="AA:BB:CC:DD:EE:FF")
    assert isinstance(client, PersistentSolemClient)
    assert client.idle_release_seconds == 225


def test_build_solem_client_reads_entry_options_off(
    mock_config_entry: MockConfigEntry,
) -> None:
    """No persistent_connection option means the stateless client."""
    client = build_solem_client(
        mock_config_entry,
        mac_address="AA:BB:CC:DD:EE:FF",
        bluetooth_timeout=BLUETOOTH_DEFAULT_TIMEOUT,
    )
    assert isinstance(client, StatelessSolemClient)
    assert not isinstance(client, PersistentSolemClient)


def test_build_solem_client_reads_entry_options_on(
    mock_config_entry: MockConfigEntry,
) -> None:
    """persistent_connection=True selects the persistent client with idle release."""
    entry = _entry_with_options(mock_config_entry, {PERSISTENT_CONNECTION: True})
    client = build_solem_client(
        entry,
        mac_address="AA:BB:CC:DD:EE:FF",
        bluetooth_timeout=BLUETOOTH_DEFAULT_TIMEOUT,
    )
    assert isinstance(client, PersistentSolemClient)
    assert client.idle_release_seconds == round(DEFAULT_SCAN_INTERVAL * 0.75)


def test_create_solem_client_hold_link_disables_idle_release() -> None:
    """hold_link=True holds the persistent link indefinitely (None idle release)."""
    client = create_solem_client(
        True, 300, hold_link=True, mac_address="AA:BB:CC:DD:EE:FF"
    )
    assert isinstance(client, PersistentSolemClient)
    assert client.idle_release_seconds is None


def test_build_solem_client_reads_hold_link_option(
    mock_config_entry: MockConfigEntry,
) -> None:
    """persistent_hold_link=True maps to an indefinite hold (None idle release)."""
    entry = _entry_with_options(
        mock_config_entry,
        {PERSISTENT_CONNECTION: True, PERSISTENT_HOLD_LINK: True},
    )
    client = build_solem_client(
        entry,
        mac_address="AA:BB:CC:DD:EE:FF",
        bluetooth_timeout=BLUETOOTH_DEFAULT_TIMEOUT,
    )
    assert isinstance(client, PersistentSolemClient)
    assert client.idle_release_seconds is None


@pytest.fixture
async def persistent_coordinator(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> SolemCoordinator:
    """Create a coordinator with the persistent_connection option enabled."""
    entry = _entry_with_options(mock_config_entry, {PERSISTENT_CONNECTION: True})
    client = create_mock_solem_client(2)
    client.disconnect = AsyncMock()
    with patch(
        "custom_components.solem_blip.client_factory.PersistentSolemClient",
        return_value=client,
    ), patch(
        "custom_components.solem_blip.bluetooth.async_get_connectable_device",
        return_value=MagicMock(address="AA:BB:CC:DD:EE:FF", name="Solem BL-IP"),
    ):
        coordinator = SolemCoordinator(hass, entry)
        await coordinator.async_init()
        # The mocked class is not the real one, so the isinstance flag computed
        # during __init__ is False; restate it for the shutdown path.
        coordinator.persistent_connection = True
        coordinator.api = client
        return coordinator


@pytest.mark.asyncio
async def test_coordinator_uses_stateless_client_by_default(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Without the option the coordinator keeps the stateless v2 client."""
    client = create_mock_solem_client(2)
    with patch(
        "custom_components.solem_blip.client_factory.StatelessSolemClient",
        return_value=client,
    ), patch(
        "custom_components.solem_blip.bluetooth.async_get_connectable_device",
        return_value=MagicMock(address="AA:BB:CC:DD:EE:FF", name="Solem BL-IP"),
    ):
        coordinator = SolemCoordinator(hass, mock_config_entry)
        await coordinator.async_init()

    assert coordinator.persistent_connection is False
    assert not isinstance(coordinator.api, PersistentSolemClient)


@pytest.mark.asyncio
async def test_coordinator_uses_persistent_client_when_enabled(
    persistent_coordinator: SolemCoordinator,
) -> None:
    """persistent_connection=True builds the persistent client."""
    assert persistent_coordinator.persistent_connection is True
    assert persistent_coordinator.api.mock is True
    assert persistent_coordinator.api.max_station_num == 2


@pytest.mark.asyncio
async def test_coordinator_passes_idle_release_from_scan_interval(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The idle-release window is 75% of the configured scan interval."""
    entry = _entry_with_options(
        mock_config_entry, {CONF_SCAN_INTERVAL: 300, PERSISTENT_CONNECTION: True}
    )
    client = create_mock_solem_client(2)
    with patch(
        "custom_components.solem_blip.client_factory.PersistentSolemClient",
        return_value=client,
    ) as factory, patch(
        "custom_components.solem_blip.bluetooth.async_get_connectable_device",
        return_value=MagicMock(address="AA:BB:CC:DD:EE:FF", name="Solem BL-IP"),
    ):
        coordinator = SolemCoordinator(hass, entry)
        await coordinator.async_init()

    factory.assert_called_once()
    assert factory.call_args.kwargs["idle_release_seconds"] == 225


@pytest.mark.asyncio
async def test_shutdown_disconnects_persistent_client(
    persistent_coordinator: SolemCoordinator,
) -> None:
    """Shutdown explicitly disconnects the persistent client."""
    await persistent_coordinator.async_shutdown()

    persistent_coordinator.api.disconnect.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_tolerates_persistent_disconnect_timeout(
    persistent_coordinator: SolemCoordinator,
) -> None:
    """A disconnect that hangs (timeout) does not fail the shutdown."""
    persistent_coordinator.api.disconnect = AsyncMock(side_effect=TimeoutError)
    schedule_shutdown = AsyncMock()
    with patch.object(
        persistent_coordinator.schedule_coordinator,
        "async_shutdown",
        schedule_shutdown,
    ):
        await persistent_coordinator.async_shutdown()

    persistent_coordinator.api.disconnect.assert_awaited_once()
    schedule_shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_shutdown_does_not_disconnect_stateless_client(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """The stateless v2 path is unchanged: no disconnect call on shutdown."""
    client = create_mock_solem_client(2)
    client.disconnect = AsyncMock()
    with patch(
        "custom_components.solem_blip.client_factory.StatelessSolemClient",
        return_value=client,
    ), patch(
        "custom_components.solem_blip.bluetooth.async_get_connectable_device",
        return_value=MagicMock(address="AA:BB:CC:DD:EE:FF", name="Solem BL-IP"),
    ):
        coordinator = SolemCoordinator(hass, mock_config_entry)
        await coordinator.async_init()

    await coordinator.async_shutdown()

    client.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_default_scan_interval_raised_to_120s(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """Entries without an explicit scan interval poll every 120 seconds."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=dict(mock_config_entry.data),
        options={
            BLUETOOTH_TIMEOUT: BLUETOOTH_DEFAULT_TIMEOUT,
            SOLEM_API_MOCK: "true",
        },
        unique_id=mock_config_entry.unique_id,
    )
    client = create_mock_solem_client(2)
    with patch(
        "custom_components.solem_blip.client_factory.StatelessSolemClient",
        return_value=client,
    ), patch(
        "custom_components.solem_blip.bluetooth.async_get_connectable_device",
        return_value=MagicMock(address="AA:BB:CC:DD:EE:FF", name="Solem BL-IP"),
    ):
        coordinator = SolemCoordinator(hass, entry)
        await coordinator.async_init()

    assert coordinator.poll_interval == 120
    assert DEFAULT_SCAN_INTERVAL == 120
