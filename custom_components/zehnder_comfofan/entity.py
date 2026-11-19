"""Shared base for the Zehnder ComfoFan entities."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import ZehnderCoordinator
from .const import DOMAIN


class ZehnderEntity(CoordinatorEntity[ZehnderCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: ZehnderCoordinator, key: str) -> None:
        super().__init__(coordinator)
        fan_id = coordinator.config_entry.unique_id
        self._attr_unique_id = f"{fan_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, fan_id)},
            name="Zehnder ComfoFan",
            manufacturer="Zehnder",
            model="ComfoFan",
        )
