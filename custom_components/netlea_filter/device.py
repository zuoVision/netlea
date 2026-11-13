"""BLE transport for Netlea filters."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from contextlib import asynccontextmanager

from bleak import BleakClient
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from .const import (
    CONTROL_MODE_UUID,
    CONTROL_UUID,
    DEFAULT_ADDRESS,
    INIT_UUID,
    STATUS_UUID,
    TRIPS_UUID,
)
from .protocol import (
    app_control_frame,
    control_frame,
    init_frame,
    parse_trip_table,
    parse_status,
    query_frame,
    trip_frame,
)

_LOGGER = logging.getLogger(__name__)


class NetleaFilter:
    def __init__(self, hass: HomeAssistant, name: str, address: str | None = None) -> None:
        self.hass = hass
        self.name = name
        self.is_on = False
        self.gear = 1
        self.mode = 1
        self.work_model = 0
        self.error_code = 0
        self.trips: list[dict[str, str | int]] = []
        self.schedule_enabled = False
        self.schedule_known = False
        self.available = False
        self.last_seen = None
        self.address: str | None = address or DEFAULT_ADDRESS
        self._feeding_snapshot = None
        self._feeding_recovery_task: asyncio.Task | None = None
        self._feeding_store = Store(hass, 1, f"netlea_filter_feeding_{name}")
        self._schedule_store = Store(hass, 1, f"netlea_filter_schedule_{name}")
        self._lock = asyncio.Lock()
        self._status_event = asyncio.Event()
        self._client: BleakClient | None = None
        self.connection_enabled = True
        self._stopped = False
        self._needs_fresh_advertisement = False
        self._transport_stage = "idle"
        self._failures = 0
        self._next_retry = 0.0
        self._last_error = None

    @property
    def signal(self) -> str:
        return f"netlea_filter_{self.name}"

    @callback
    def _notify(self, _sender, data: bytearray) -> None:
        try:
            state = parse_status(bytes(data))
        except ValueError as err:
            _LOGGER.debug("Ignoring invalid Netlea status: %s", err)
            return
        self.is_on = bool(state["is_on"])
        self.gear = int(state["gear"]) or self.gear
        self.mode = int(state["mode"])
        self.work_model = int(state["work_model"])
        self.error_code = int(state["error_code"])
        self.available = True
        self._failures = 0
        self._last_error = None
        self.last_seen = datetime.now(timezone.utc).isoformat()
        self._status_event.set()
        async_dispatcher_send(self.hass, self.signal)

    async def _find(self):
        if self._needs_fresh_advertisement and self.address:
            self._transport_stage = "waiting for fresh advertisement"
            started = asyncio.get_running_loop().time()
            info = await bluetooth.async_process_advertisements(
                self.hass,
                lambda info: info.time >= started,
                {"address": self.address, "connectable": True},
                bluetooth.BluetoothScanningMode.ACTIVE,
                timeout=15,
            )
            self._needs_fresh_advertisement = False
            return info.device
        # Always ask Home Assistant for the current BLEDevice object. BlueZ
        # device paths can disappear between scans, so retaining a BLEDevice
        # returned by a standalone BleakScanner makes later commands fail with
        # "device ... not found".
        if self.address:
            device = bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            if device is not None:
                return device

        for info in bluetooth.async_discovered_service_info(
            self.hass, connectable=True
        ):
            if info.name == self.name or info.advertisement.local_name == self.name:
                self.address = info.address
                return bluetooth.async_ble_device_from_address(
                    self.hass, info.address, connectable=True
                ) or info.device
        return None

    @callback
    def _disconnected(self, client) -> None:
        if self._client is client:
            self.available = False
            async_dispatcher_send(self.hass, self.signal)

    async def _disconnect_locked(self) -> None:
        client, self._client = self._client, None
        self.available = False
        try:
            if client is not None:
                async with asyncio.timeout(8):
                    await client.disconnect()
        finally:
            async_dispatcher_send(self.hass, self.signal)

    @asynccontextmanager
    async def _session(self):
        """Reuse one subscribed connection. Caller must hold the BLE lock."""
        if self._stopped or not self.connection_enabled:
            raise RuntimeError("HA Bluetooth connection is disabled; enable it before control")
        try:
            if self._client is None or not self._client.is_connected:
                await self._disconnect_locked()
                device = await self._find()
                if device is None:
                    raise RuntimeError(f"Bluetooth device {self.name!r} not found")
                self.address = device.address
                # Own the client before connecting so cancellation/timeouts
                # always reach the exact client that may own the BLE link.
                self._transport_stage = "connecting"
                self._client = BleakClient(
                    device, timeout=15, disconnected_callback=self._disconnected,
                )
                async with asyncio.timeout(20):
                    await self._client.connect()
                    self._transport_stage = "subscribing to status"
                    await self._client.start_notify(STATUS_UUID, self._notify)
                _LOGGER.info("Netlea persistent Bluetooth connection established")
            self._transport_stage = "communicating"
            async with asyncio.timeout(20):
                yield self._client
        except BaseException:
            self._needs_fresh_advertisement = True
            try:
                await self._disconnect_locked()
            except Exception:
                _LOGGER.debug("Netlea connection cleanup failed", exc_info=True)
            raise

    async def async_set_connection_enabled(self, enabled: bool) -> None:
        # Publish intent before waiting for a stalled I/O operation/recovery.
        self.connection_enabled = enabled
        self._next_retry = 0
        async_dispatcher_send(self.hass, self.signal)
        async with self._lock:
            self.connection_enabled = enabled
            if not enabled:
                await self._disconnect_locked()
            async_dispatcher_send(self.hass, self.signal)
        if enabled:
            await self.async_refresh()

    async def async_close(self) -> None:
        self._stopped = True
        if self._feeding_recovery_task is not None:
            self._feeding_recovery_task.cancel()
            await asyncio.gather(self._feeding_recovery_task, return_exceptions=True)
        async with self._lock:
            await self._disconnect_locked()

    async def _run(self, write_uuid: str, payload: bytes, wait_notify: bool = True) -> None:
        async with self._lock:
            async with self._session() as client:
                # Only retry read-only status requests; never replay controls.
                attempts = 2 if wait_notify and write_uuid == STATUS_UUID else 1
                for attempt in range(attempts):
                    self._status_event.clear()
                    async with asyncio.timeout(5):
                        await client.write_gatt_char(write_uuid, payload, response=False)
                    if not wait_notify:
                        break
                    try:
                        await asyncio.wait_for(self._status_event.wait(), timeout=5.0)
                        break
                    except TimeoutError:
                        if attempt + 1 == attempts or not client.is_connected:
                            raise
                        _LOGGER.debug("Netlea status reply delayed; retrying on same connection")
            self.available = True

    async def async_update(self) -> None:
        if self._stopped or not self.connection_enabled:
            return
        # Do not queue polling/advertisement work behind commands or another
        # update, and debounce repeated advertisements while offline.
        if self._lock.locked() or (not self.available and time.monotonic() < self._next_retry):
            return
        self._next_retry = time.monotonic() + 30
        try:
            await self._run(STATUS_UUID, query_frame())
        except Exception as err:
            self.available = False
            self._failures += 1
            self._last_error = f"{self._transport_stage}: {type(err).__name__}: {err}"
            _LOGGER.warning("Netlea status update failed: %s", self._last_error)
            async_dispatcher_send(self.hass, self.signal)

    @property
    def connection_diagnostics(self):
        return {
            "connection_status": "released" if not self.connection_enabled else
                "connected" if self.available else "reconnecting",
            "last_seen": self.last_seen,
            "consecutive_failures": self._failures,
            "last_error": self._last_error,
        }

    async def async_refresh(self) -> None:
        await self.async_update()
        if self.available:
            await self.async_read_schedule()

    async def async_restore_schedule(self) -> None:
        """Keep the last confirmed schedule visible through HA restarts and BLE outages."""
        saved = await self._schedule_store.async_load()
        if not isinstance(saved, dict) or not isinstance(saved.get("trips"), list):
            return
        self.trips = saved["trips"]
        self.schedule_enabled = bool(saved.get("schedule_enabled"))
        self.schedule_known = True
        async_dispatcher_send(self.hass, self.signal)

    async def _async_save_schedule(self) -> None:
        try:
            await self._schedule_store.async_save({
                "trips": self.trips,
                "schedule_enabled": self.schedule_enabled,
            })
        except Exception:
            _LOGGER.warning("Could not cache the Netlea schedule", exc_info=True)

    async def async_poll_status(self) -> None:
        """Keep HA in sync with autonomous on-device schedule transitions."""
        while not self._stopped:
            await asyncio.sleep(10 if self.available else 30)
            was_available = self.available
            await self.async_update()
            if self.available and not was_available:
                await self.async_read_schedule()

    async def async_read_schedule(self) -> None:
        if self._stopped or not self.connection_enabled:
            return
        try:
            async with self._lock:
                async with self._session() as client:
                    if self._feeding_snapshot is not None:
                        return
                    try:
                        raw_trips = await client.read_gatt_char(TRIPS_UUID)
                        trips = parse_trip_table(bytes(raw_trips))
                    except Exception as err:
                        # Status and control can remain available when this
                        # optional trip-table read times out. Keep the working
                        # BLE session and the last known schedule.
                        _LOGGER.debug("Netlea schedule read failed: %s", err)
                        return
                    if not trips and self.trips:
                        # Keep a recoverable copy if the device reports an
                        # empty table after reconnect. Only the explicit clear
                        # service should remove the saved configuration.
                        _LOGGER.warning(
                            "Netlea device schedule is empty; retaining %d cached rows",
                            len(self.trips),
                        )
                    else:
                        self.trips = trips
                    self.schedule_enabled = any(
                        bool(trip.get("enabled")) for trip in trips
                    )
                    self.schedule_known = True
                    await self._async_save_schedule()
                self.available = True
                async_dispatcher_send(self.hass, self.signal)
        except Exception as err:
            _LOGGER.debug("Netlea schedule read failed: %s", err)

    async def async_set_schedule(self, trips: list[dict]) -> None:
        if self._feeding_snapshot is not None:
            raise RuntimeError("End feeding pause before changing the schedule")
        # Validate and encode before taking the BLE lock.
        payload = trip_frame(trips)
        async with self._lock:
            await self._async_write_schedule_locked(payload)

    async def async_disable_schedule(self) -> None:
        """Disable schedule execution without deleting the stored rows."""
        if self._feeding_snapshot is not None:
            raise RuntimeError("End feeding pause before changing the schedule")
        if not self.trips or not self.schedule_enabled:
            return
        disabled = [{**trip, "enabled": False} for trip in self.trips]
        payload = trip_frame(disabled)
        async with self._lock:
            await self._async_write_schedule_locked(payload)

    async def async_enable_schedule(self) -> None:
        """Re-enable previously stored schedule rows."""
        if self._feeding_snapshot is not None:
            raise RuntimeError("End feeding pause before changing the schedule")
        if not self.trips:
            raise ValueError("No saved schedule is available")
        enabled = [{**trip, "enabled": True} for trip in self.trips]
        payload = trip_frame(enabled)
        async with self._lock:
            await self._async_write_schedule_locked(payload)

    async def _async_write_schedule_locked(self, payload: bytes) -> None:
        async with self._session() as client:
            await client.write_gatt_char(INIT_UUID, init_frame(), response=False)
            await client.write_gatt_char(TRIPS_UUID, payload, response=False)
            await asyncio.sleep(0.8)
            confirmed = parse_trip_table(
                bytes(await client.read_gatt_char(TRIPS_UUID))
            )
            # Clearing EEE2 removes the table, but this firmware remains in
            # schedule work mode until it also receives a normal EEE1
            # command. Preserve the current on/off state and selected gear
            # while explicitly handing control back to HA.
            schedule_enabled = any(bool(trip.get("enabled")) for trip in confirmed)
            if not schedule_enabled:
                restore_on = self.is_on
                restore_gear = self.gear
                await client.write_gatt_char(
                    CONTROL_MODE_UUID, app_control_frame(), response=False
                )
                await asyncio.sleep(0.25)
                self._status_event.clear()
                await client.write_gatt_char(
                    CONTROL_UUID,
                    control_frame(is_on=restore_on, gear=restore_gear, mode=1),
                    response=False,
                )
                await asyncio.wait_for(self._status_event.wait(), timeout=3.0)
            await client.write_gatt_char(STATUS_UUID, query_frame(), response=False)
            await asyncio.sleep(0.5)
        if self._feeding_snapshot is None:
            self.trips = confirmed
            self.schedule_enabled = schedule_enabled
            self.schedule_known = True
            await self._async_save_schedule()
        self.available = True
        async_dispatcher_send(self.hass, self.signal)

    async def async_pause_feeding(self) -> None:
        """Suspend execution while retaining the selected mode and schedule."""
        async with self._lock:
            if self._feeding_snapshot is not None:
                raise RuntimeError("A feeding pause is already active")
            if self._stopped or not self.connection_enabled:
                raise RuntimeError("HA Bluetooth connection is disabled")
            # Do not persist a feeding pause until the filter is reachable and
            # its current state and schedule have been read successfully.
            async with self._session() as client:
                self._status_event.clear()
                await client.write_gatt_char(STATUS_UUID, query_frame(), response=False)
                await asyncio.wait_for(self._status_event.wait(), timeout=3.0)
                self.trips = parse_trip_table(bytes(await client.read_gatt_char(TRIPS_UUID)))
                self.schedule_enabled = any(bool(trip.get("enabled")) for trip in self.trips)
            snapshot = {
                "is_on": self.is_on, "gear": self.gear, "mode": self.mode,
                "schedule_enabled": self.schedule_enabled,
                "trips": [dict(trip) for trip in self.trips],
            }
            await self._feeding_store.async_save(snapshot)
            self._feeding_snapshot = snapshot
            try:
                if snapshot["schedule_enabled"]:
                    disabled = [{**trip, "enabled": False} for trip in snapshot["trips"]]
                    await self._async_write_schedule_locked(trip_frame(disabled))
                await self._async_set_locked(False, snapshot["gear"])
            except (Exception, asyncio.CancelledError):
                try:
                    await self._async_resume_feeding_locked()
                except (Exception, asyncio.CancelledError):
                    _LOGGER.warning("Feeding pause failed; original settings retained for recovery", exc_info=True)
                    self._start_feeding_recovery()
                raise
            finally:
                # Schedule execution is temporarily suspended, not deselected.
                self.trips = snapshot["trips"]
                self.schedule_enabled = snapshot["schedule_enabled"]
                async_dispatcher_send(self.hass, self.signal)

    def _start_feeding_recovery(self) -> None:
        if self._stopped:
            return
        if self._feeding_recovery_task is None or self._feeding_recovery_task.done():
            self._feeding_recovery_task = self.hass.async_create_background_task(
                self.async_resume_feeding_with_retry(), "Netlea failed feeding recovery"
            )

    async def _async_resume_feeding_locked(self) -> None:
        snapshot = self._feeding_snapshot
        if snapshot is None:
            return
        if snapshot["schedule_enabled"]:
            await self._async_write_schedule_locked(trip_frame(snapshot["trips"]))
        else:
            await self._async_set_locked(snapshot["is_on"], snapshot["gear"], snapshot.get("mode", 1))
            self.trips = snapshot["trips"]
            self.schedule_enabled = False
        await self._feeding_store.async_save(None)
        self._feeding_snapshot = None
        async_dispatcher_send(self.hass, self.signal)

    async def async_resume_feeding(self) -> None:
        async with self._lock:
            await self._async_resume_feeding_locked()

    async def async_resume_feeding_with_retry(self) -> None:
        while not self._stopped and self._feeding_snapshot is not None:
            try:
                await self.async_resume_feeding()
            except Exception as err:
                _LOGGER.warning("Retrying feeding resume: %s", err)
                await asyncio.sleep(30)

    async def async_recover_feeding(self) -> None:
        """Restore an interrupted pause after HA restarts, retrying BLE failures."""
        self._feeding_snapshot = await self._feeding_store.async_load()
        if self._feeding_snapshot is None:
            return
        self.trips = self._feeding_snapshot["trips"]
        self.schedule_enabled = self._feeding_snapshot["schedule_enabled"]
        while not self._stopped and self._feeding_snapshot is not None:
            try:
                await self.async_resume_feeding()
            except Exception as err:
                _LOGGER.warning("Retrying interrupted feeding recovery: %s", err)
                await asyncio.sleep(30)

    async def async_set(self, is_on: bool, gear: int | None = None) -> None:
        target_gear = self.gear if gear is None else max(1, min(20, gear))
        async with self._lock:
            if self._stopped or not self.connection_enabled:
                raise RuntimeError("HA Bluetooth connection is disabled")
            if self._feeding_snapshot is not None:
                raise RuntimeError("End feeding pause before manual control")
            if self.schedule_enabled:
                disabled = [{**trip, "enabled": False} for trip in self.trips]
                await self._async_write_schedule_locked(trip_frame(disabled))
            await self._async_set_locked(is_on, target_gear)

    async def async_set_gear(self, gear: int) -> None:
        """Set or preset a gear without racing an in-flight power command."""
        target_gear = max(1, min(20, gear))
        async with self._lock:
            if self._stopped or not self.connection_enabled:
                raise RuntimeError("HA Bluetooth connection is disabled")
            if self._feeding_snapshot is not None:
                raise RuntimeError("End feeding pause before manual control")
            if self.schedule_enabled:
                disabled = [{**trip, "enabled": False} for trip in self.trips]
                await self._async_write_schedule_locked(trip_frame(disabled))
            if not self.is_on:
                self.gear = target_gear
                async_dispatcher_send(self.hass, self.signal)
                return
            await self._async_set_locked(True, target_gear)

    async def _async_set_locked(self, is_on: bool, target_gear: int, mode: int = 1) -> None:
        async with self._session() as client:
            # Reproduce the vendor app's session setup. This also safely takes
            # APP control if the filter was left in button control.
            await client.write_gatt_char(INIT_UUID, init_frame(), response=False)
            await client.write_gatt_char(STATUS_UUID, query_frame(), response=False)
            await asyncio.sleep(0.5)
            await client.write_gatt_char(
                CONTROL_MODE_UUID, app_control_frame(), response=False
            )
            await asyncio.sleep(0.25)

            self._status_event.clear()
            await client.write_gatt_char(
                CONTROL_UUID,
                control_frame(is_on=is_on, gear=target_gear, mode=mode),
                response=False,
            )
            # Status notifications from the preceding query can arrive before
            # the control acknowledgement. Wait for the requested state rather
            # than treating the first (possibly stale) notification as failure.
            try:
                async with asyncio.timeout(5.0):
                    while True:
                        await self._status_event.wait()
                        self._status_event.clear()
                        if self.is_on == is_on and (not is_on or self.gear == target_gear):
                            break
            except TimeoutError as err:
                raise RuntimeError(
                    "Filter did not confirm requested state "
                    f"(on={is_on}, gear={target_gear}); "
                    f"reported on={self.is_on}, gear={self.gear}"
                ) from err
        self.available = True
