"""Zehnder ComfoFan, controlled through zehnder.py on a Raspberry Pi."""

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import ZehnderApi, ZehnderError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.BUTTON, Platform.SENSOR]
SCAN_INTERVAL = timedelta(seconds=30)


class ZehnderCoordinator(DataUpdateCoordinator[dict]):
    """Polls the fan settings and gives the entities access to the API."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, config_entry=entry, name=DOMAIN,
                         update_interval=SCAN_INTERVAL)
        self.api = ZehnderApi(async_get_clientsession(hass),
                              entry.data[CONF_HOST], entry.data[CONF_PORT])

    async def _async_update_data(self) -> dict:
        try:
            return await self.api.status()
        except ZehnderError as error:
            raise UpdateFailed(str(error)) from error


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = ZehnderCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
