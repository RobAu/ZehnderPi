"""Sensor showing the fan's current speed."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity

from .const import SPEEDS
from .entity import ZehnderEntity


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    async_add_entities([ZehnderSpeedSensor(entry.runtime_data)])


class ZehnderSpeedSensor(ZehnderEntity, SensorEntity):
    _attr_name = "Speed"
    _attr_icon = "mdi:fan"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = SPEEDS

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "speed")

    @property
    def native_value(self) -> str | None:
        speed = self.coordinator.data.get("speed")
        return speed if speed in SPEEDS else None

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data
        return {"power_percent": data.get("power"), "timer": data.get("timer")}
