"""Integer gear control for a Netlea filter."""

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([NetleaGearNumber(hass.data[DOMAIN][entry.entry_id])])


class NetleaGearNumber(NumberEntity):
    _attr_has_entity_name = True
    _attr_name = "Gear setting"
    _attr_icon = "mdi:fan-speed-2"
    _attr_native_min_value = 1
    _attr_native_max_value = 20
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "档"
    _attr_mode = NumberMode.SLIDER
    _attr_should_poll = False

    def __init__(self, device):
        self.device = device
        self._attr_unique_id = f"{device.name}_gear_setting"

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, self.device.signal, self.async_write_ha_state
            )
        )

    @property
    def native_value(self):
        return self.device.gear

    @property
    def available(self):
        return self.device.available

    async def async_set_native_value(self, value: float) -> None:
        target_gear = max(1, min(20, round(value)))
        await self.device.async_set_gear(target_gear)
