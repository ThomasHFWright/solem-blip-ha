"""Use a confirmed identification name without changing HA identities/overrides."""
from typing import TYPE_CHECKING

from homeassistant.helpers import device_registry as dr

from .const import CONTROLLER_MAC_ADDRESS, DOMAIN

if TYPE_CHECKING:
    from .coordinator import SolemCoordinator

CONTROLLER_NAME = "controller_name"


def apply_controller_name(coordinator: "SolemCoordinator", name: str) -> None:
    """Persist an onboard name and update only HA's integration-owned labels."""
    entry = coordinator.config_entry
    if entry is None:
        return
    previous = coordinator.controller_name
    coordinator.controller_name = name
    if entry.data.get(CONTROLLER_NAME) != name:
        auto_titles = {
            previous, coordinator.controller_mac_address,
            "Solem BL-IP", entry.data[CONTROLLER_MAC_ADDRESS],
        }
        coordinator.hass.config_entries.async_update_entry(
            entry,
            data={**entry.data, CONTROLLER_NAME: name},
            title=name if entry.title in auto_titles else entry.title,
        )
    registry = dr.async_get(coordinator.hass)
    device = registry.async_get_device_by_identifier(
        (DOMAIN, coordinator.controller_mac_address), config_entry_id=entry.entry_id,
    )
    if device is not None and device.name != name:
        registry.async_update_device(device.id, name=name)
