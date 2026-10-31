"""On-demand connection and persistent, explicitly timestamped cache."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import establish_connection
from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (CHAR_UUID, CONNECT_TIMEOUT, DOMAIN, MODEL, NOTIFY_TIMEOUT,
                    QUERY, SERVICE_UUID)
from .protocol import FrameParser, decode_source_fields, status_command
from .control import ACTIONS, LEVELS, SHORT_SWITCHES, build_control, expected_after

CONTROL_SETTLE = 1.0
CONTROL_READBACK_TIMEOUT = 3.0

_LOGGER = logging.getLogger(__name__)


class JomooCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """No update interval, startup query, background scan or held connection."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=None)
        self.address = entry.data["address"]
        self.entry = entry
        self._lock = asyncio.Lock()
        self._store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self.data = {"last_result": "never_queried", "last_control_result": "never_sent", "frames": {},
                     "frame_times": {}, "fields": {}, "field_times": {}}

    async def async_load(self) -> None:
        """Restore cache without communicating with the device."""
        cached = await self._store.async_load()
        if isinstance(cached, dict):
            for key in ("frames", "frame_times", "fields", "field_times"):
                if isinstance(cached.get(key), dict):
                    self.data[key] = cached[key]
            for key in ("last_result", "last_status_time", "last_query_time",
                        "last_control_result", "last_control_name", "last_control_time",
                        "last_control_frame"):
                if isinstance(cached.get(key), str):
                    self.data[key] = cached[key]
            if isinstance(cached.get("control_write_completed"), bool):
                self.data["control_write_completed"] = cached["control_write_completed"]

    @property
    def last_status_time(self) -> datetime | None:
        raw = self.data.get("last_status_time")
        return dt_util.parse_datetime(raw) if raw else None

    async def async_query(self) -> None:
        """Send only the fixed query; preserve old state on all failures."""
        if self._lock.locked():
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="query_in_progress")
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
                _LOGGER.debug("Status query finished: %s", self.data["last_result"])
                self.async_set_updated_data(self.data)
                await self._store.async_save(self.data)

    async def async_control(self, name: str, value=None) -> None:
        """One serialized command, with fresh baseline and non-optimistic readback."""
        if name not in ACTIONS and name not in LEVELS and name not in SHORT_SWITCHES:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="control_unsupported")
        if self._lock.locked():
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="query_in_progress")
        async with self._lock:
            self.data.update(last_control_result="sending", last_control_name=name,
                             last_control_time=dt_util.utcnow().isoformat(), last_result="querying",
                             last_query_time=dt_util.utcnow().isoformat(),
                             last_control_frame=None, control_write_completed=False)
            self.async_set_updated_data(self.data)
            try:
                await self._async_query_device(control=(name, value))
                if self.data["last_control_result"] == "sending":
                    raise HomeAssistantError(translation_domain=DOMAIN, translation_key="control_state_missing")
            except asyncio.CancelledError:
                self.data["last_control_result"] = ("sent_unconfirmed" if self.data.get("control_write_completed") else "failed")
                raise
            except HomeAssistantError:
                self.data["last_control_result"] = ("sent_unconfirmed" if self.data.get("control_write_completed") else "failed")
                if self.data["last_result"] == "querying":
                    self.data["last_result"] = "connection_error"
                raise
            except Exception as err:
                self.data["last_control_result"] = ("sent_unconfirmed" if self.data.get("control_write_completed") else "failed")
                self.data["last_result"] = "connection_error"
                _LOGGER.warning("Control %s failed without automatic command replay: %s", name, err)
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="control_failed",
                                         translation_placeholders={"error": str(err)}) from err
            finally:
                self.async_set_updated_data(self.data)
                await self._store.async_save(self.data)
                _LOGGER.debug("Control %s finished: %s", name, self.data["last_control_result"])

    @staticmethod
    async def _wait_observation(predicate, event, timeout):
        async with asyncio.timeout(timeout):
            while not predicate():
                event.clear()
                await event.wait()

    async def _async_control_on_client(self, client, char, response, request, frames, fields, event, parser):
        name, value = request
        required = {"E9"} if name in SHORT_SWITCHES else {"FA", "E9"}
        if name in {"lid_open", "lid_close", "ring_open", "ring_close"}:
            required.add("EA")
        for attempt in range(3):
            await client.write_gatt_char(char, QUERY, response=response)
            try:
                await self._wait_observation(lambda: required <= frames.keys(), event, NOTIFY_TIMEOUT / 3)
            except TimeoutError:
                pass  # firmware may require a second E2 query after reconnect
            if required <= frames.keys():
                break
        else:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="control_state_missing")
        if ("occupancy" not in fields or "working" not in fields or "auto_work" not in fields
                or (name not in SHORT_SWITCHES and len(frames["FA"][6:-2]) < 8)):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="control_state_missing")
        self.data["last_result"] = "valid_status_received"
        await asyncio.sleep(CONTROL_SETTLE)
        if name in LEVELS and fields["working"]:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="control_requires_idle")
        if name in {"lid_open", "lid_close", "ring_open", "ring_close"} and (
                fields["occupancy"] or fields["working"] or fields.get("lid_moving", True) or fields.get("ring_moving", True)):
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="control_requires_unoccupied")
        if name in {"wash_hip", "wash_woman", "wash_auto_hip", "wash_auto_woman", "dry"} and not fields["occupancy"]:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="control_requires_occupied")
        packet = build_control(name, value, frames.get("FA"), fields)
        expected = expected_after(name, value, fields)
        if name == "stop":
            expected = {"working": False}
        elif name == "flush_large":
            expected = {"flush_large_running": True}
        elif name == "flush_small":
            expected = {"flush_small_running": True}
        elif name == "dry":
            expected = {"dry_running": True}
        elif name in {"wash_hip", "wash_auto_hip", "wash_woman", "wash_auto_woman"}:
            expected = {"woman_wash_running" if "woman" in name else "hip_wash_running": True,
                        "auto_work": "auto" in name}
        if name in {"lid_open", "lid_close", "ring_open", "ring_close"}:
            expected.update(lid_moving=False, ring_moving=False)
        # These dictionaries are new for this connection, then reset for the
        # post-write phase: stale persisted state can never confirm a command.
        parser.reset()
        frames.clear()
        fields.clear()
        event.clear()
        self.data["last_control_frame"] = packet.hex().upper()
        await client.write_gatt_char(char, packet, response=response)  # exactly once
        self.data["control_write_completed"] = True
        self.data["last_control_result"] = "sent_unconfirmed"
        confirmed = lambda: bool(expected) and all(fields.get(k) == v for k, v in expected.items())
        for _ in range(3):
            if confirmed():
                break
            await client.write_gatt_char(char, QUERY, response=response)  # reads may retry, actions never do
            try:
                await self._wait_observation(confirmed, event, CONTROL_READBACK_TIMEOUT)
            except TimeoutError:
                pass
        self.data["last_result"] = "valid_status_received" if frames else "no_valid_status"
        self.data["last_control_result"] = ("confirmed" if confirmed() else
            "readback_mismatch" if expected and all(k in fields for k in expected) else "sent_unconfirmed")

    @asynccontextmanager
    async def _connected(self, ble_device):
        """Use HA's recommended connector, including bounded connection retries."""
        client = await establish_connection(
            BleakClient, ble_device, MODEL, max_attempts=3, timeout=CONNECT_TIMEOUT
        )
        try:
            yield client
        finally:
            try:
                await client.disconnect()
            except (BleakError, OSError):
                _LOGGER.debug("Disconnect failed", exc_info=True)

    async def _async_wait_for_advertisement(self):
        """Refresh HA's connection source, including unchanged advertisements."""
        register_every = getattr(bluetooth, "async_register_advertisement_callback", None)
        request_sweep = getattr(bluetooth, "async_request_active_scan", None)
        if register_every is None or request_sweep is None:
            # Older HA APIs replay cached service data but do not dispatch every
            # identical packet, so never demand a timestamp change there.
            return await bluetooth.async_process_advertisements(
                self.hass, lambda info: True,
                {"address": self.address, "connectable": True},
                bluetooth.BluetoothScanningMode.ACTIVE, CONNECT_TIMEOUT,
            )
        fresh = asyncio.get_running_loop().create_future()

        def received(info):
            if info.connectable and not fresh.done():
                fresh.set_result(info)

        cancel = register_every(self.hass, received, self.address)
        sweep = self.hass.async_create_task(request_sweep(self.hass, CONNECT_TIMEOUT))
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                return await fresh
        finally:
            cancel()
            sweep.cancel()
            await asyncio.gather(sweep, return_exceptions=True)

    async def _async_query_device(self, control=None) -> None:
        # Use HA's shared scanner on demand, never an independent scanner.
        current_sources = getattr(bluetooth, "async_scanner_devices_by_address", None)
        reachable = current_sources(self.hass, self.address, connectable=True) if current_sources else []
        _LOGGER.debug("Current connection sources for target: %d", len(reachable))
        # A scanner's CURRENT device table is sufficient for routing. Requiring
        # another advertisement immediately after disconnect can stall devices
        # which briefly stop advertising. Status is still read afresh over GATT.
        if not reachable:
            try:
                await self._async_wait_for_advertisement()
            except TimeoutError:
                self.data["last_result"] = "not_discovered"
                return
        ble_device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        if ble_device is None:
            self.data["last_result"] = "not_discovered"
            return
        parser = FrameParser()
        received = asyncio.Event()
        observed_frames = {}
        observed_fields = {}
        accepting = False

        @callback
        def notification(_sender, data: bytearray) -> None:
            if not accepting:
                return
            for frame in parser.feed(data):
                command = status_command(frame)
                if command is None or frame[3] != 0x21:
                    continue
                now = dt_util.utcnow().isoformat()
                key = f"{command:02X}"
                _LOGGER.debug("Received %s status frame (%d bytes)", key, len(frame))
                observed_frames[key] = frame
                observed_fields.update(decode_source_fields(frame))
                self.data["frames"][key] = frame.hex().upper()[:42]
                self.data["frame_times"][key] = now
                self.data["last_status_time"] = now
                for name, value in decode_source_fields(frame).items():
                    self.data["fields"][name] = value
                    self.data["field_times"][name] = now
                received.set()

        # Passing BLEDevice, never an address string, avoids Bleak implicit scan.
        async with self._connected(ble_device) as client:
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
            # Some firmwares announce FA inside start_notify, before its await
            # returns. Arm the receiver first or that baseline is lost forever.
            accepting = True
            try:
                await client.start_notify(char, notification)
                if control is not None:
                    async with asyncio.timeout(35):
                        await self._async_control_on_client(
                            client, char, response, control, observed_frames, observed_fields, received, parser
                        )
                    return
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
