"""Allow releasing the persistent BLE connection to the vendor app."""

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import STATE_OFF
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([NetleaConnectionSwitch(hass.data[DOMAIN][entry.entry_id])])


class NetleaConnectionSwitch(SwitchEntity, RestoreEntity):
    _attr_name = "滤桶 HA 蓝牙连接"
    _attr_icon = "mdi:bluetooth-connect"
    _attr_should_poll = False

    def __init__(self, device):
        self.device = device
        self._attr_unique_id = f"{device.name}_ha_bluetooth_connection"

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        state = await self.async_get_last_state()
        if state is not None:
            self.device.connection_enabled = state.state != STATE_OFF
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self.device.signal, self.async_write_ha_state
            )
        )

    @property
    def extra_state_attributes(self):
        return self.device.connection_diagnostics

    @property
    def is_on(self):
        return self.device.connection_enabled

    async def async_turn_on(self, **kwargs):
        await self.device.async_set_connection_enabled(True)

    async def async_turn_off(self, **kwargs):
        await self.device.async_set_connection_enabled(False)
