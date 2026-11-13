"""Config flow for Netlea Bluetooth filter."""

import voluptuous as vol
from homeassistant import config_entries

from .const import CONF_BLE_NAME, DEFAULT_NAME, DOMAIN


class NetleaFilterConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            name = user_input[CONF_BLE_NAME].strip()
            await self.async_set_unique_id(name)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=name, data={CONF_BLE_NAME: name})
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_BLE_NAME, default=DEFAULT_NAME): str}),
        )

