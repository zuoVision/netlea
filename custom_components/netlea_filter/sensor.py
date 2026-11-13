"""Diagnostic sensor for a Netlea filter."""

from homeassistant.components.sensor import SensorEntity
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN


async def async_setup_entry(hass, entry, async_add_entities):
    device = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([NetleaGearSensor(device), NetleaScheduleSensor(device)])


class NetleaGearSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_name = "Gear"
    _attr_native_unit_of_measurement = "档"
    _attr_icon = "mdi:fan-speed-2"
    _attr_should_poll = False

    def __init__(self, device):
        self.device = device
        self._attr_unique_id = f"{device.name}_gear"

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self.device.signal, self.async_write_ha_state)
        )

    @property
    def native_value(self):
        return self.device.gear if self.device.is_on else 0

    @property
    def available(self):
        return self.device.available

    @property
    def extra_state_attributes(self):
        return {
            "last_seen": self.device.last_seen,
            "mode": self.device.mode,
            "work_model": self.device.work_model,
            "error_code": self.device.error_code,
            "schedule_enabled": self.device.schedule_enabled,
            "trips": self.device.trips,
        }


class NetleaScheduleSensor(SensorEntity):
    """Expose the last confirmed schedule even while the BLE device is offline."""

    _attr_has_entity_name = True
    _attr_name = "Schedule cache"
    _attr_suggested_object_id = "netlea_filter_schedule"
    _attr_icon = "mdi:calendar-clock"
    _attr_should_poll = False

    def __init__(self, device):
        self.device = device
        self._attr_unique_id = f"{device.name}_schedule_cache"
        self.entity_id = "sensor.netlea_filter_schedule"

    async def async_added_to_hass(self):
        self.async_on_remove(
            async_dispatcher_connect(self.hass, self.device.signal, self.async_write_ha_state)
        )

    @property
    def native_value(self):
        if not self.device.schedule_known:
            return "unknown"
        return "enabled" if self.device.schedule_enabled else "disabled"

    @property
    def extra_state_attributes(self):
        return {
            "trips": self.device.trips,
            "schedule_enabled": self.device.schedule_enabled,
            "device_online": self.device.available,
            "schedule_known": self.device.schedule_known,
        }
