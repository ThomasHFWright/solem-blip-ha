"""Custom services for Solem BL-IP schedule management."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any, cast

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers import device_registry as dr

from .config_entry import MyConfigEntry
from .const import DOMAIN, PROGRAM_LABELS

if TYPE_CHECKING:
    from .coordinator import SolemCoordinator

SERVICE_REFRESH_PROGRAMS = "refresh_programs"
SERVICE_SET_PROGRAM = "set_program"

ATTR_CYCLE = "cycle"
ATTR_DEVICE_ID = "device_id"
ATTR_INTER_STATION_DELAY = "inter_station_delay"
ATTR_NAME = "name"
ATTR_PERIOD_LENGTH = "period_length"
ATTR_PERIOD_START_DATE = "period_start_date"
ATTR_PROGRAM = "program"
ATTR_START_TIMES = "start_times"
ATTR_STATION_DURATIONS = "station_durations"
ATTR_SYNCHRO_DAY = "synchro_day"
ATTR_WATER_BUDGET = "water_budget"
ATTR_WEEK_DAYS = "week_days"

_CYCLES = {
    "custom": 0,
    "even": 1,
    "odd": 2,
    "odd_31": 3,
    "periodic": 4,
}
_WEEKDAYS = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}

_COMMON_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.string,
    }
)
_SET_PROGRAM_SCHEMA = _COMMON_SERVICE_SCHEMA.extend({
    vol.Required(ATTR_PROGRAM): vol.All(vol.Coerce(int), vol.Range(min=1, max=3)),
    vol.Required("revision"): cv.string,
    vol.Optional(ATTR_NAME): cv.string,
    vol.Optional(ATTR_START_TIMES): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional(ATTR_STATION_DURATIONS): dict,
    vol.Optional(ATTR_CYCLE): vol.In(tuple(_CYCLES)),
    vol.Optional(ATTR_WEEK_DAYS): vol.All(cv.ensure_list, list),
    vol.Optional(ATTR_PERIOD_LENGTH): vol.All(vol.Coerce(int), vol.Range(min=1, max=255)),
    vol.Optional(ATTR_SYNCHRO_DAY): vol.All(vol.Coerce(int), vol.Range(min=0, max=255)),
    vol.Optional(ATTR_PERIOD_START_DATE): cv.date,
    vol.Optional(ATTR_INTER_STATION_DELAY): vol.All(vol.Coerce(int), vol.Range(min=0, max=65535)),
    vol.Optional(ATTR_WATER_BUDGET): vol.All(vol.Coerce(int), vol.Range(min=0, max=65535)),
})


async def async_setup_services(hass: HomeAssistant) -> None:
    """Register Solem BL-IP services."""
    if hass.services.has_service(DOMAIN, SERVICE_SET_PROGRAM):
        return

    async def handle_set_program(call: ServiceCall) -> None:
        coordinator = _coordinator_from_device(hass, call.data[ATTR_DEVICE_ID])
        if coordinator._irrigation_active or coordinator._is_watering:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_program_while_watering",
            )

        program_index = int(call.data[ATTR_PROGRAM]) - 1
        program = _program_from_service_data(
            call.data,
            num_stations=coordinator.num_stations,
        )
        try:
            await coordinator.set_irrigation_program(program_index, program, call.data["revision"])
        except Exception as err:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_program_failed",
                translation_placeholders={
                    "program_name": f"Program {PROGRAM_LABELS[program_index]}"
                },
            ) from err

    async def handle_refresh_programs(call: ServiceCall) -> None:
        coordinator = _coordinator_from_device(hass, call.data[ATTR_DEVICE_ID])
        await coordinator.refresh_programs()

    async def handle_accept_current(call: ServiceCall) -> None:
        coordinator = _coordinator_from_device(hass, call.data[ATTR_DEVICE_ID])
        await coordinator.refresh_programs(accept_current=True)

    async def handle_rainfall(call: ServiceCall) -> None:
        coordinator = _coordinator_from_device(hass, call.data[ATTR_DEVICE_ID])
        await coordinator.rainfall.apply()

    hass.services.async_register(DOMAIN, "apply_rainfall", handle_rainfall, schema=_COMMON_SERVICE_SCHEMA)
    hass.services.async_register(DOMAIN, "accept_current_programs", handle_accept_current, schema=_COMMON_SERVICE_SCHEMA)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_PROGRAM,
        handle_set_program,
        schema=_SET_PROGRAM_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REFRESH_PROGRAMS,
        handle_refresh_programs,
        schema=_COMMON_SERVICE_SCHEMA,
    )


def async_unload_services(hass: HomeAssistant) -> None:
    """Remove Solem BL-IP services."""
    for service in (SERVICE_SET_PROGRAM, SERVICE_REFRESH_PROGRAMS, "accept_current_programs", "apply_rainfall"):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)


def _coordinator_from_device(hass: HomeAssistant, device_id: str) -> SolemCoordinator:
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="service_device_not_found",
        )

    macs = {
        identifier[1]
        for identifier in device.identifiers
        if len(identifier) == 2 and identifier[0] == DOMAIN
    }
    for entry in hass.config_entries.async_entries(DOMAIN):
        config_entry = cast(MyConfigEntry, entry)
        runtime_data = config_entry.runtime_data
        if runtime_data and runtime_data.coordinator.controller_mac_address in macs:
            return runtime_data.coordinator

    raise HomeAssistantError(
        translation_domain=DOMAIN,
        translation_key="service_device_not_found",
    )


def _parse_start_time(value: str) -> int:
    try:
        hours_text, minutes_text = value.split(":", 1)
        hours = int(hours_text)
        minutes = int(minutes_text)
    except ValueError as exc:
        raise vol.Invalid("start_times must use HH:MM") from exc
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise vol.Invalid("start_times must use HH:MM between 00:00 and 23:59")
    return hours * 60 + minutes


def _week_days_mask(values: list[Any]) -> int:
    mask = 0
    for value in values:
        if isinstance(value, int):
            day = value
        else:
            key = str(value).lower()
            if key not in _WEEKDAYS:
                raise vol.Invalid(f"invalid weekday: {value}")
            day = _WEEKDAYS[key]
        if not 0 <= day <= 6:
            raise vol.Invalid("weekday integers must be between 0 and 6")
        mask |= 1 << day
    return mask


def _station_durations(value: dict[Any, Any], *, num_stations: int) -> list[int]:
    durations = [0] * num_stations
    for station_raw, seconds_raw in value.items():
        station = int(station_raw)
        seconds = int(seconds_raw)
        if not 1 <= station <= num_stations:
            raise vol.Invalid(f"station must be between 1 and {num_stations}")
        if not 0 <= seconds <= 0xFFFFFF:
            raise vol.Invalid("station duration must be between 0 and 16777215")
        durations[station - 1] = seconds
    return durations


def _program_from_service_data(data: dict[str, Any], *, num_stations: int) -> dict[str, Any]:
    """Translate only supplied fields; omitted stations/settings are preserved."""
    result = {key: data[key] for key in (
        ATTR_NAME, ATTR_INTER_STATION_DELAY, ATTR_WATER_BUDGET,
        ATTR_PERIOD_LENGTH, ATTR_SYNCHRO_DAY, ATTR_PERIOD_START_DATE,
    ) if key in data}
    if ATTR_CYCLE in data:
        result[ATTR_CYCLE] = _CYCLES[data[ATTR_CYCLE]]
    if ATTR_WEEK_DAYS in data:
        result[ATTR_WEEK_DAYS] = _week_days_mask(data[ATTR_WEEK_DAYS])
    if ATTR_START_TIMES in data:
        starts: list[int | None] = [_parse_start_time(value) for value in data[ATTR_START_TIMES]]
        if len(starts) > 8:
            raise vol.Invalid("start_times must contain at most 8 entries")
        result[ATTR_START_TIMES] = starts + [None] * (8 - len(starts))
    if ATTR_STATION_DURATIONS in data:
        _station_durations(data[ATTR_STATION_DURATIONS], num_stations=num_stations)
        result[ATTR_STATION_DURATIONS] = {int(key): int(value) for key, value in data[ATTR_STATION_DURATIONS].items()}
    return result
