"""On-demand connection and persistent, explicitly timestamped cache."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from bleak import BleakClient
from bleak.exc import BleakError
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (CHAR_UUID, CONNECT_TIMEOUT, DOMAIN, MODEL, NOTIFY_TIMEOUT,
                    QUERY, SERVICE_UUID)
from .protocol import FrameParser, decode_source_fields, status_command

_LOGGER = logging.getLogger(__name__)


class JomooCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """No update interval, startup query, background scan or held connection."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=None)
        self.address = entry.data["address"]
        self.entry = entry
        self._lock = asyncio.Lock()
        self._store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self.data = {"last_result": "never_queried", "frames": {},
                     "frame_times": {}, "fields": {}, "field_times": {}}

    async def async_load(self) -> None:
        """Restore cache without communicating with the device."""
        cached = await self._store.async_load()
        if isinstance(cached, dict):
            for key in ("frames", "frame_times", "fields", "field_times"):
                if isinstance(cached.get(key), dict):
                    self.data[key] = cached[key]
            for key in ("last_result", "last_status_time", "last_query_time"):
                if isinstance(cached.get(key), str):
                    self.data[key] = cached[key]

    @property
    def last_status_time(self) -> datetime | None:
        raw = self.data.get("last_status_time")
        return dt_util.parse_datetime(raw) if raw else None

    async def async_query(self) -> None:
        """Send only the fixed query; preserve old state on all failures."""
        if self._lock.locked():
            raise HomeAssistantError("A JOMOO status query is already in progress")
        async with self._lock:
            self.data["last_query_time"] = dt_util.utcnow().isoformat()
            self.data["last_result"] = "querying"
            self.async_set_updated_data(self.data)
            try:
                await self._async_query_device()
            except TimeoutError:
                self.data["last_result"] = "connection_timeout"
            except (BleakError, OSError, HomeAssistantError) as err:
                self.data["last_result"] = "connection_error"
                _LOGGER.debug("JOMOO status query failed: %s", err)
            except Exception:
                self.data["last_result"] = "unexpected_error"
                _LOGGER.exception("Unexpected JOMOO status query error")
            except asyncio.CancelledError:
                self.data["last_result"] = "cancelled"
                raise
            finally:
                self.async_set_updated_data(self.data)
                await self._store.async_save(self.data)

    async def _async_query_device(self) -> None:
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            self.data["last_result"] = "not_discovered"
            return
        parser = FrameParser()
        received = asyncio.Event()
        accepting = False

        @callback
        def notification(_sender, data: bytearray) -> None:
            if not accepting:
                return
            for frame in parser.feed(data):
                command = status_command(frame)
                if command is None:
                    continue
                now = dt_util.utcnow().isoformat()
                key = f"{command:02X}"
                self.data["frames"][key] = frame.hex().upper()[:42]
                self.data["frame_times"][key] = now
                self.data["last_status_time"] = now
                for name, value in decode_source_fields(frame).items():
                    self.data["fields"][name] = value
                    self.data["field_times"][name] = now
                received.set()

        # Passing BLEDevice, never an address string, avoids Bleak implicit scan.
        async with BleakClient(ble_device, timeout=CONNECT_TIMEOUT) as client:
            char = client.services.get_characteristic(CHAR_UUID)
            if char is None or char.service_uuid.lower() != SERVICE_UUID:
                self.data["last_result"] = "missing_characteristic"
                return
            props = set(char.properties)
            if "notify" not in props:
                self.data["last_result"] = "notify_not_supported"
                return
            if "write" in props:
                response = True
            elif "write-without-response" in props:
                response = False
            else:
                self.data["last_result"] = "write_not_supported"
                return
            await client.start_notify(char, notification)
            try:
                accepting = True
                # Bound write + receive phase, not eight seconds per packet.
                try:
                    async with asyncio.timeout(NOTIFY_TIMEOUT):
                        await client.write_gatt_char(char, QUERY, response=response)
                        await received.wait()
                        # Capture follow-up E9/EA packets in the same burst, while
                        # respecting the original eight-second total deadline.
                        await asyncio.sleep(0.4)
                except TimeoutError:
                    self.data["last_result"] = ("valid_status_received" if received.is_set()
                                                else "no_valid_status")
                else:
                    self.data["last_result"] = "valid_status_received"
            finally:
                accepting = False
                try:
                    await client.stop_notify(char)
                except (BleakError, OSError):
                    _LOGGER.debug("Stop notify failed; context will disconnect", exc_info=True)

    async def async_shutdown(self) -> None:
        """Do not unload while a user-requested query is using this instance."""
        async with self._lock:
            await self._store.async_save(self.data)
