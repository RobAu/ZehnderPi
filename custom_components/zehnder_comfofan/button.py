"""Buttons that mirror the original Zehnder remote."""

from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError

from .api import ZehnderError
from .const import TIMER_MINUTES
from .entity import ZehnderEntity

# key, name, icon, speed, timer minutes (None = no timer)
BUTTONS = [
    ("low", "Low", "mdi:fan-speed-1", "low", None),
    ("medium", "Medium", "mdi:fan-speed-2", "medium", None),
    ("high", "High", "mdi:fan-speed-3", "high", None),
    ("timer", f"Timer ({TIMER_MINUTES} min high)", "mdi:fan-clock", "high", TIMER_MINUTES),
]


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    coordinator = entry.runtime_data
    async_add_entities(ZehnderButton(coordinator, *button) for button in BUTTONS)


class ZehnderButton(ZehnderEntity, ButtonEntity):
    def __init__(self, coordinator, key, name, icon, speed, minutes) -> None:
        super().__init__(coordinator, key)
        self._attr_name = name
        self._attr_icon = icon
        self._speed = speed
        self._minutes = minutes

    @property
    def available(self) -> bool:
        # A button stays usable when a status poll failed; pressing it retries.
        return True

    async def async_press(self) -> None:
        api = self.coordinator.api
        try:
            if self._minutes is None:
                settings = await api.set_speed(self._speed)
            else:
                settings = await api.set_timer(self._speed, self._minutes)
        except ZehnderError as error:
            raise HomeAssistantError(str(error)) from error
        self.coordinator.async_set_updated_data(settings)
