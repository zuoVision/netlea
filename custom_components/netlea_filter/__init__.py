"""Netlea Bluetooth filter integration."""

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth.match import ADDRESS, BluetoothCallbackMatcher
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .device import NetleaFilter

PLATFORMS = ["fan", "number", "sensor", "switch"]

TRIP_SCHEMA = vol.Schema(
    {
        vol.Required("start"): cv.string,
        vol.Required("end"): cv.string,
        vol.Required("gear"): vol.All(vol.Coerce(int), vol.Range(min=1, max=20)),
        vol.Optional("mode", default=1): vol.All(
            vol.Coerce(int), vol.In([1, 2, 3])
        ),
    }
)


async def async_setup(hass: HomeAssistant, _config: dict) -> bool:
    async def _device():
        devices = list(hass.data.get(DOMAIN, {}).values())
        if not devices:
            raise RuntimeError("No Netlea filter is configured")
        return devices[0]

    async def set_schedule(call) -> None:
        device = await _device()
        await device.async_set_schedule(call.data["trips"])

    async def clear_schedule(_call) -> None:
        device = await _device()
        await device.async_set_schedule([])

    async def disable_schedule(_call) -> None:
        device = await _device()
        await device.async_disable_schedule()

    async def enable_schedule(_call) -> None:
        device = await _device()
        await device.async_enable_schedule()

    async def pause_feeding(_call) -> None:
        device = await _device()
        await device.async_pause_feeding()

    async def resume_feeding(_call) -> None:
        device = await _device()
        await device.async_resume_feeding_with_retry()

    async def turn_on(_call) -> None:
        device = await _device()
        await device.async_set(True, device.gear)

    async def turn_off(_call) -> None:
        device = await _device()
        await device.async_set(False)

    hass.services.async_register(DOMAIN, "pause_feeding", pause_feeding)
    hass.services.async_register(DOMAIN, "resume_feeding", resume_feeding)
    hass.services.async_register(DOMAIN, "turn_on", turn_on)
    hass.services.async_register(DOMAIN, "turn_off", turn_off)
    hass.services.async_register(
        DOMAIN,
        "set_schedule",
        set_schedule,
        schema=vol.Schema(
            {
                vol.Required("trips"): vol.All(
                    cv.ensure_list, [TRIP_SCHEMA], vol.Length(max=10)
                )
            }
        ),
    )
    hass.services.async_register(DOMAIN, "clear_schedule", clear_schedule)
    hass.services.async_register(DOMAIN, "disable_schedule", disable_schedule)
    hass.services.async_register(DOMAIN, "enable_schedule", enable_schedule)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    device = NetleaFilter(
        hass, entry.data["ble_name"], entry.data.get("ble_address")
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = device
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    advertisement_reconnect_task = None

    @callback
    def reconnect_on_advertisement(service_info, _change) -> None:
        """Reconnect immediately when a previously unavailable filter advertises."""
        nonlocal advertisement_reconnect_task
        if (
            not device.connection_enabled
            or device.available
            or device._lock.locked()
            or (
                advertisement_reconnect_task is not None
                and not advertisement_reconnect_task.done()
            )
        ):
            return
        device.address = service_info.address
        advertisement_reconnect_task = entry.async_create_background_task(
            hass,
            device.async_refresh(),
            "Netlea filter advertisement reconnect",
        )

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            reconnect_on_advertisement,
            BluetoothCallbackMatcher({ADDRESS: device.address}),
            bluetooth.BluetoothScanningMode.ACTIVE,
        )
    )

    async def close_connection(_event):
        await device.async_close()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, close_connection)
    )
    # Do not hold up integration/entity setup when the BLE peripheral is out of
    # range or temporarily connected to another central. Refresh in the
    # background; the control entity remains present and can retry on demand.
    async def initialize():
        await device.async_restore_schedule()
        await device.async_recover_feeding()
        await device.async_refresh()

    entry.async_create_background_task(
        hass, initialize(), "Netlea filter initial refresh"
    )
    entry.async_create_background_task(
        hass, device.async_poll_status(), "Netlea filter status polling"
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        device = hass.data[DOMAIN].pop(entry.entry_id)
        await device.async_close()
    return ok
