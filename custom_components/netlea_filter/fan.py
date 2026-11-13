"""Fan entity for a Netlea filter."""

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    # The integration entry already performs an initial BLE refresh. Avoid a
    # second connection here, which can delay setup while the filter resumes
    # advertising after the first disconnect.
    async_add_entities([NetleaFilterFan(hass.data[DOMAIN][entry.entry_id])])


class NetleaFilterFan(FanEntity):
    _attr_has_entity_name = True
    _attr_name = "Filter pump"
    # The device transport confirms every command through an EEE4 notification.
    # Avoid background polling racing a user command for the single BLE slot.
    _attr_should_poll = False
    _attr_supported_features = (
        FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )

    def __init__(self, device):
        self.device = device
        self._attr_unique_id = f"{device.name}_fan"

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self.device.signal, self.async_write_ha_state)
        )

    @property
    def available(self):
        return self.device.available

    @property
    def is_on(self):
        return self.device.is_on

    async def async_turn_on(self, percentage=None, preset_mode=None, **kwargs):
        await self.device.async_set(True, self.device.gear)

    async def async_turn_off(self, **kwargs):
        await self.device.async_set(False)

    async def async_update(self):
        await self.device.async_update()
