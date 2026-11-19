"""Set up the integration by entering the address of the Raspberry Pi."""

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import ZehnderApi, ZehnderError
from .const import DEFAULT_HOST, DEFAULT_PORT, DOMAIN


class ZehnderConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            api = ZehnderApi(async_get_clientsession(self.hass),
                             user_input[CONF_HOST], user_input[CONF_PORT])
            try:
                info = await api.info()
            except ZehnderError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(info["id"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=info["name"], data=user_input)
        schema = vol.Schema({
            vol.Required(CONF_HOST, default=DEFAULT_HOST): str,
            vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
