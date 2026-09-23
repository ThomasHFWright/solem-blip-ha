"""DataUpdateCoordinator for the Solem BL-IP integration."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Any

from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import Context, HomeAssistant

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .ble import IrrigationProgram

from .client_factory import (
    build_solem_client,
)

from .config_entry import MyConfigEntry
from .controller_name import CONTROLLER_NAME
from .const import (
    BLUETOOTH_DEFAULT_TIMEOUT,
    BLUETOOTH_TIMEOUT,
    CONTROLLER_MAC_ADDRESS,
    DEFAULT_CONTROLLER_OFF_DAYS,
    DEFAULT_MANUAL_DURATION,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    IRRIGATION_CONFIG_UPDATE_INTERVAL,
    NUM_STATIONS,
    PROGRAM_LABELS,
    SOLEM_API_MOCK,
)
from .coordinator_descriptors import build_all_descriptors
from .coordinator_irrigation import (
    await_irrigation_monitor_task,
    clear_irrigation_idle_state,
    clear_monitor_task_ref,
    run_irrigation_monitor,
    start_irrigation as irrigation_start,
    start_program as irrigation_start_program,
    stop_irrigation as irrigation_stop,
    turn_controller_off as irrigation_turn_off,
    turn_controller_off_for_days as irrigation_turn_off_for_days,
    turn_controller_on as irrigation_turn_on,
)
from .coordinator_polling import (
    apply_status,
    fetch_device_metadata,
    fetch_device_status,
    fetch_irrigation_config,
    remaining_minutes_for_station,
)
from .coordinator_publish import publish_descriptor_update
from .bluetooth import async_get_connectable_device

from .activity import WateringActivity
from .programs import ProgramManager
from .station_names import StationNameManager
from .rainfall import RainfallManager
from .models import IrrigationController, IrrigationStation
from .ble_health import note_cycle_outcome
from .bluetooth_issue import note_ble_recovery

_LOGGER = logging.getLogger(__name__)


class SolemCoordinator(DataUpdateCoordinator[list[dict[str, Any]]]):
    """Poll BLE status and expose manual irrigation controls."""

    def __init__(self, hass: HomeAssistant, config_entry: MyConfigEntry) -> None:
        self.controller_mac_address = config_entry.data[CONTROLLER_MAC_ADDRESS].rsplit(
            " - ", 1
        )[1]
        self.controller_name: str = config_entry.data.get(
            CONTROLLER_NAME, self.controller_mac_address
        )
        _LOGGER.info(
            "%s - Starting coordinator initialization...",
            self.controller_mac_address,
        )

        self.poll_interval = config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        self.bluetooth_timeout = config_entry.options.get(
            BLUETOOTH_TIMEOUT, BLUETOOTH_DEFAULT_TIMEOUT
        )
        self.solem_api_mock = (
            config_entry.options.get(SOLEM_API_MOCK, "false") == "true"
        )

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({config_entry.unique_id})",
            update_method=self.async_update_data,
            update_interval=timedelta(seconds=self.poll_interval),
            config_entry=config_entry,
            always_update=True,
        )

        self.num_stations = config_entry.data.get(NUM_STATIONS, 2)
        self.config_entry = config_entry
        self.entity_translations: dict[str, Any] = {}

        self.controller = IrrigationController(
            device_id=f"{self.controller_mac_address}_irrigation_controller_status",
            device_name="Controller Status",
            device_uid="",
            software_version=None,
        )
        self.station_names: dict[int, str] = {}
        self.firmware_version: str | None = None
        self._firmware_retry_after = 0.0
        self._station_names_retry_after = 0.0
        self.stations = self._build_stations()

        self.api = build_solem_client(
            config_entry,
            mac_address=self.controller_mac_address,
            bluetooth_timeout=self.bluetooth_timeout,
            mock=self.solem_api_mock,
            max_station_num=self.num_stations,
            ble_device_resolver=lambda: async_get_connectable_device(
                hass, self.controller_mac_address
            ),
        )

        self.program_manager = ProgramManager(hass, config_entry.entry_id, self.api)
        self.station_name_manager = StationNameManager(hass, config_entry.entry_id, self.api)
        self.rainfall = RainfallManager(self)
        self.irrigation_stop_event = asyncio.Event()
        self._irrigation_active = False
        self._irrigation_monitor_task: asyncio.Task[None] | None = None
        self._ready = False
        self.battery_voltage: int | None = None
        self.battery_level: int | None = None
        self.battery_low: bool | None = None
        self.time_alarm: bool | None = None
        self._has_status = False
        self.irrigation_manual_duration = DEFAULT_MANUAL_DURATION
        self.controller_off_days = DEFAULT_CONTROLLER_OFF_DAYS
        self.controller_off_mode = "unknown"
        self.controller_off_days_remaining: int | None = None
        self.remaining_seconds: int | None = None
        self.active_station_num: int | None = None
        self.active_program_num: int | None = None
        self.watering_origin: str | None = None
        self.irrigation_programs: dict[int, IrrigationProgram] = {}
        self._irrigation_config_retry_after = 0.0
        self._irrigation_config_refresh_after = 0.0
        self.schedule_coordinator = SolemScheduleCoordinator(hass, config_entry, self)
        self._last_set_time_at = 0.0
        self._last_set_time_sync: datetime | None = None
        self._set_time_pending = True
        self._ble_cycle_degraded_streak = 0
        self._ble_health_events: deque[float] = deque()
        self._ble_issue_active = False
        self._ble_first_healthy_at: float | None = None
        self._last_successful_poll_at: float | None = None
        self._is_watering = False
        self._metadata_task: asyncio.Task[None] | None = None
        self._heavy_read_lock = asyncio.Lock()
        self._first_successful_status_at: float | None = None
        self._metadata_ready_after = float("inf")
        self._schedule_ready_after = float("inf")
        self._schedule_gate = asyncio.Event()
        self.activity = WateringActivity(self)

        _LOGGER.info(
            "%s - Coordinator initialization finished.",
            self.controller_mac_address,
        )

    def _station_name(self, station_id: int) -> str:
        """Return the controller-provided station name or a stable fallback."""
        return self.station_names.get(station_id) or f"Station {station_id}"

    def _program_display_name(self, program_index: int) -> str:
        """Return the on-device program name or a stable slot fallback."""
        program = self.irrigation_programs.get(program_index)
        if program and (name := program.get("name", "").strip()):
            return name
        return f"Program {PROGRAM_LABELS[program_index]}"

    def _build_stations(self) -> list[IrrigationStation]:
        """Build station models for the configured station count."""
        return [
            IrrigationStation(
                device_id=f"{self.controller_mac_address}_irrigation_station_{station_id}_status",
                device_name=f"{self._station_name(station_id)} Status",
                device_uid="",
                station_number=station_id,
                software_version=self.firmware_version,
            )
            for station_id in range(1, self.num_stations + 1)
        ]

    async def async_shutdown(self) -> None:
        """Cancel owned tasks; each BLE operation releases its own connection."""
        self.irrigation_stop_event.set()
        task = self._irrigation_monitor_task
        if task is not None and not task.done():
            task.cancel()
        await self._await_irrigation_monitor_task()
        self._clear_irrigation_idle_state()
        await self.schedule_coordinator.async_shutdown()
        await self.activity.shutdown()

    def request_schedule_refresh(self) -> None:
        """Mark schedule data due for the next slow-coordinator refresh."""
        self._irrigation_config_refresh_after = 0.0

    async def async_init(self) -> None:
        """Build initial entity data without blocking setup on BLE availability."""
        await self.program_manager.load()
        await self.station_name_manager.load()
        await self.rainfall.load()
        await self.activity.load()
        if self.program_manager.snapshot:
            self.irrigation_programs = self.program_manager.snapshot.programs
        self._ready = True
        self.data = await self.async_update_all_sensors(fetch_status=False)
        self.last_update_success = False

    def _apply_status(self, status: dict[str, Any]) -> None:
        """Update coordinator state from a BLE status dict."""
        apply_status(self, status)

    async def _fetch_device_status(self) -> dict[str, Any]:
        """Poll device and update controller/station states from BLE status."""
        return await fetch_device_status(self)

    async def _fetch_device_metadata(self) -> None:
        """Read firmware and station names without failing status polling."""
        await fetch_device_metadata(self)

    def _clear_irrigation_idle_state(self) -> None:
        """Reset coordinator state after irrigation stops or fails to start."""
        clear_irrigation_idle_state(self)

    def _clear_monitor_task_ref(self, task: asyncio.Task[None]) -> None:
        """Clear stored monitor task when it completes."""
        clear_monitor_task_ref(self, task)

    def request_device_time_sync(self) -> None:
        """Force a device-time sync on the next successful status poll.

        Clears the 24h throttle set by ``maybe_set_device_time`` so a
        controller that lost its clock (e.g. after a power/battery blip)
        is re-synced as soon as the BLE link recovers, instead of waiting
        up to a day.
        """
        self._set_time_pending = True

    async def _await_irrigation_monitor_task(self) -> None:
        """Wait for the background irrigation monitor to finish."""
        await await_irrigation_monitor_task(self)

    def _remaining_minutes_for_station(self, station_id: int) -> int | None:
        """Return remaining sprinkle minutes for a station (0 when idle/inactive)."""
        return remaining_minutes_for_station(self, station_id)

    async def async_update_all_sensors(
        self, *, fetch_status: bool = True
    ) -> list[dict[str, Any]]:
        """Build entity descriptor list from current coordinator state."""
        if fetch_status:
            await self._fetch_device_status()
        return build_all_descriptors(self)

    async def async_update_data(self) -> list[dict[str, Any]]:
        try:
            data = await self.async_update_all_sensors()
            await self.rainfall.maybe_apply()
            data = await self.async_update_all_sensors(fetch_status=False)
            self._last_successful_poll_at = asyncio.get_running_loop().time()
            note_ble_recovery(self)
            note_cycle_outcome(self, degraded=False, reason="")
            _LOGGER.debug(
                "%s - Status poll completed",
                self.controller_mac_address,
            )
            return data
        except Exception as err:
            note_cycle_outcome(self, degraded=True, reason=f"status poll failed: {str(err) or type(err).__name__}")
            raise UpdateFailed(f"Failed to update BLE status: {err}") from err

    async def start_irrigation(
        self, station: int, minutes: int | None = None, *, context: Context | None = None
    ) -> None:
        """Send a start command, then monitor watering in the background."""
        await irrigation_start(self, station, minutes, context=context)

    async def start_program(self, program_num: int, *, context: Context | None = None) -> None:
        """Start one on-device irrigation program."""
        await irrigation_start_program(self, program_num, context=context)

    async def _run_irrigation_monitor(self, station: int, duration: int) -> None:
        """Monitor active watering until completion, stop, or safety timeout."""
        await run_irrigation_monitor(self, station, duration)

    async def stop_irrigation(self) -> None:
        """Stop active manual watering."""
        await irrigation_stop(self)

    def publish_programs(self) -> None:
        """Publish the last complete snapshot and its verification state."""
        if self.program_manager.snapshot:
            self.irrigation_programs = self.program_manager.snapshot.programs
        self.async_set_updated_data(build_all_descriptors(self))
        self.schedule_coordinator.async_set_updated_data(self.irrigation_programs)

    async def refresh_programs(self, *, accept_current: bool = False) -> None:
        """Return only after a complete fresh read, propagating read failures."""
        try:
            await self.program_manager.refresh(accept_current=accept_current)
        finally:
            self.publish_programs()

    async def refresh_station_names(self, *, accept_current: bool = False) -> None:
        """Refresh the editor from onboard names without changing HA labels."""
        await self.station_name_manager.refresh(accept_current=accept_current)
        await self._publish_station_names()

    async def rename_station(self, station: int, name: str, revision: str) -> None:
        """Save one physical station's name, leaving all other settings alone."""
        try:
            await self.station_name_manager.update(station, name, revision)
        finally:
            await self._publish_station_names()

    async def _publish_station_names(self) -> None:
        if snapshot := self.station_name_manager.snapshot:
            self.station_names.update({i: name for i, name in snapshot.names.items() if i <= self.num_stations})
            for station in self.stations:
                station.device_name = f"{self._station_name(station.station_number)} Status"
            publish_descriptor_update(self, await self.async_update_all_sensors(fetch_status=False))

    async def set_irrigation_program(
        self, program_index: int, changes: dict[str, Any], revision: str,
        *, require_on: bool = False,
    ) -> None:
        """Apply a guarded field patch; never substitute unspecified fields."""
        try:
            await self.program_manager.update(program_index, changes, revision, require_on=require_on)
        finally:
            self.publish_programs()

    async def turn_controller_on(self) -> None:
        """Turn the irrigation controller on."""
        await irrigation_turn_on(self)

    async def turn_controller_off(self) -> None:
        """Turn the irrigation controller off permanently."""
        await irrigation_turn_off(self)

    async def turn_controller_off_for_days(self) -> None:
        """Turn the irrigation controller off for the configured number of days."""
        await irrigation_turn_off_for_days(self)

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        """Return one entity descriptor from coordinator data."""
        if not self.data:
            return None
        for device in self.data:
            if device["device_id"] == device_id:
                return device
        return None

    def get_device_parameter(self, device_id: str, parameter: str) -> Any:
        """Return one field from an entity descriptor."""
        if device := self.get_device(device_id):
            return device.get(parameter)
        return None


class SolemScheduleCoordinator(DataUpdateCoordinator[dict[int, IrrigationProgram]]):
    """Refresh persisted irrigation schedules without delaying status polls."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: MyConfigEntry,
        coordinator: SolemCoordinator,
    ) -> None:
        self.solem_coordinator = coordinator
        self._first_refresh_started = False
        self._config_entry = config_entry
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} schedule ({config_entry.unique_id})",
            update_method=self.async_update_data,
            update_interval=timedelta(seconds=IRRIGATION_CONFIG_UPDATE_INTERVAL),
            config_entry=config_entry,
            always_update=False,
        )

    def async_start_first_refresh(self) -> None:
        """Start the first schedule refresh after schedule entities subscribe."""
        if self._first_refresh_started:
            return
        self._first_refresh_started = True
        self._config_entry.async_create_background_task(
            self.hass,
            self._async_deferred_first_refresh(),
            name=f"{DOMAIN} schedule first refresh",
        )

    async def _async_deferred_first_refresh(self) -> None:
        """Wait for the heavy-read gate before the first irrigation config read."""
        coordinator = self.solem_coordinator
        await coordinator._schedule_gate.wait()
        remaining = coordinator._schedule_ready_after - (
            asyncio.get_running_loop().time()
        )
        if remaining > 0:
            await asyncio.sleep(remaining)
        _LOGGER.debug(
            "%s - Schedule coordinator starting first refresh",
            coordinator.controller_mac_address,
        )
        await coordinator._fetch_device_metadata()
        await self.async_refresh()

    async def async_update_data(self) -> dict[int, IrrigationProgram]:
        """Refresh schedule state and publish updated program descriptors."""
        await fetch_irrigation_config(self.solem_coordinator)
        publish_descriptor_update(
            self.solem_coordinator,
            await self.solem_coordinator.async_update_all_sensors(fetch_status=False),
        )
        return self.solem_coordinator.irrigation_programs
