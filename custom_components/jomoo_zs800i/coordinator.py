"""Serialized controls, persistent BLE sessions and timestamped observations."""
from __future__ import annotations

import asyncio
import logging
from time import monotonic
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
from .session import BLESession
from .control import ACTIONS, LEVELS, SHORT_SWITCHES, TOGGLES, build_control, expected_after

CONTROL_SETTLE = 0.25  # quiet receive window, not a fixed sleep
MIN_WRITE_GAP = 0.25
POST_COMMAND_PAUSE = 0.25
BASELINE_QUERY_TIMEOUT = 1.0
SESSION_IDLE_TIMEOUT = -1.0  # persistent by default; UI options can select 60 or 0
CONTROL_READBACK_TIMEOUT = 3.0
# A single cold BLE connect has been measured at 4.5-9 s. Bound the whole
# establishment (the library retries internally, which alone once burned 56 s
# and exhausted the adapter's connection slots) so the UI fails fast instead.
CONNECT_BUDGET = 25.0
REWARM_DELAY = 1.0
REWARM_RETRY = 15.0

_LOGGER = logging.getLogger(__name__)


class ControlRejected(HomeAssistantError):
    """A local guard refused a command while the BLE link itself is healthy.

    Such a refusal must never tear down the session: the previous behaviour did,
    so the user's next tap paid a full cold connect (measured 4.5-9 s) and could
    even exhaust the adapter's connection slots.
    """


class JomooCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """On-demand BLE sessions, released after a bounded idle period."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=None)
        self.address = entry.data["address"]
        self.entry = entry
        self._lock = asyncio.Lock()
        self._session = None
        self._retired_client = None
        self._idle_handle = None
        self._idle_generation = 0
        self._publish_handle = None
        self._operation_start = 0.0
        self._warmup_task = None
        self._confirmation = None
        self._confirmation_task = None
        self._confirmation_draining = set()
        self._control_waiters = 0
        self._closing = False
        self._releasing = False
        self._rewarm_handle = None
        self._store = Store(hass, 1, f"{DOMAIN}.{entry.entry_id}")
        self.data = {"bluetooth_connected": False, "last_result": "never_queried", "last_control_result": "never_sent", "frames": {},
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
            for key in ("last_control_write_ms", "last_control_write_complete_ms", "last_control_total_ms", "last_control_confirmation_ms"):
                if type(cached.get(key)) in (int, float) and cached[key] >= 0:
                    self.data[key] = cached[key]
            if isinstance(cached.get("last_connection_reused"), bool):
                self.data["last_connection_reused"] = cached["last_connection_reused"]

    @property
    def last_status_time(self) -> datetime | None:
        raw = self.data.get("last_status_time")
        return dt_util.parse_datetime(raw) if raw else None

    async def async_query(self) -> None:
        """Send only the fixed query; preserve old state on all failures."""
        if self._closing:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="query_in_progress")
        await self._cancel_confirmation()
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
                # Keep the specific reason a bounded connect already recorded
                # instead of flattening it to a generic connection error.
                if self.data["last_result"] not in ("connection_timeout", "not_discovered"):
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
        if name not in ACTIONS and name not in LEVELS and name not in SHORT_SWITCHES \
                and name not in TOGGLES:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="control_unsupported")
        requested = monotonic()
        async with self._control_slot():
            self.data.update(last_control_result="sending", last_control_name=name,
                             last_control_time=dt_util.utcnow().isoformat(), last_result="querying",
                             last_query_time=dt_util.utcnow().isoformat(),
                             last_control_frame=None, control_write_completed=False,
                             control_write_attempted=False, last_control_confirmation_ms=None, last_control_write_ms=None,
                             last_control_write_complete_ms=None, last_control_total_ms=None)
            self._operation_start = requested
            self.async_set_updated_data(self.data)
            try:
                await self._async_query_device(control=(name, value))
                if self.data["last_control_result"] == "sending":
                    raise HomeAssistantError(translation_domain=DOMAIN, translation_key="control_state_missing")
            except asyncio.CancelledError:
                self.data["last_control_result"] = ("confirmed" if self.data["last_control_result"] == "confirmed" else
                    "sent_unconfirmed" if self.data.get("control_write_attempted") else "failed")
                raise
            except HomeAssistantError:
                self.data["last_control_result"] = ("confirmed" if self.data["last_control_result"] == "confirmed" else
                    "sent_unconfirmed" if self.data.get("control_write_attempted") else "failed")
                if self.data["last_result"] == "querying":
                    self.data["last_result"] = "connection_error"
                raise
            except Exception as err:
                self.data["last_control_result"] = ("confirmed" if self.data["last_control_result"] == "confirmed" else
                    "sent_unconfirmed" if self.data.get("control_write_attempted") else "failed")
                self.data["last_result"] = "connection_error"
                _LOGGER.warning("Control %s failed without automatic command replay: %s", name, err)
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="control_failed",
                                         translation_placeholders={"error": str(err)}) from err
            finally:
                self.data["last_control_total_ms"] = round((monotonic() - self._operation_start) * 1000)
                self.async_set_updated_data(self.data)
                if getattr(self._store, "async_delay_save", None):
                    self._save_later()
                else:
                    await self._store.async_save(self.data)
                _LOGGER.debug("Control %s finished: %s; tx=%sms write_done=%sms total=%sms reused=%s",
                              name, self.data["last_control_result"], self.data.get("last_control_write_ms"),
                              self.data.get("last_control_write_complete_ms"), self.data["last_control_total_ms"],
                              self.data.get("last_connection_reused"))

    @staticmethod
    async def _wait_observation(predicate, event, timeout):
        async with asyncio.timeout(timeout):
            while not predicate():
                event.clear()
                await event.wait()

    async def _wait_command_window(self, session):
        # Firmware can ignore a command that overlaps the previous status burst.
        # Wait for a short quiet gap, with a hard cap so status traffic cannot
        # postpone stop/configuration commands indefinitely.
        await asyncio.sleep(0)
        deadline = monotonic() + 1.0
        while True:
            now = monotonic()
            wait = max(CONTROL_SETTLE - (now - session.last_rx_at),
                       MIN_WRITE_GAP - (now - session.last_write_at))
            if wait <= 0 or now >= deadline:
                return
            await asyncio.sleep(min(wait, deadline - now))

    async def _async_control_on_client(self, client, char, response, request, frames, fields, event, session):
        name, value = request
        if name not in SHORT_SWITCHES:
            required = {"FA", "E9"}
            if name in {"lid_open", "lid_close", "ring_open", "ring_close"}:
                required.add("EA")
            for attempt in range(3):
                await session.write(QUERY, response=response)
                try:
                    await self._wait_observation(lambda: required <= frames.keys(), event,
                                                 min(NOTIFY_TIMEOUT / 3, BASELINE_QUERY_TIMEOUT))
                except TimeoutError:
                    pass
                if required <= frames.keys():
                    break
            else:
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="control_state_missing")
            if ("occupancy" not in fields or "working" not in fields or "auto_work" not in fields
                    or len(frames["FA"][6:-2]) < 8):
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="control_state_missing")
            self.data["last_result"] = "valid_status_received"
        await self._wait_command_window(session)
        if name in LEVELS and fields["working"]:
            raise ControlRejected(translation_domain=DOMAIN, translation_key="control_requires_idle")
        if name in ("water_level", "seat_level") and fields.get("auto_temp"):
            # Verified on 237371: with seasonal-auto temperature on, the
            # firmware accepts command 25 and applies air level but keeps
            # FA[6]/FA[7] unchanged. Do not write a command that cannot apply.
            raise ControlRejected(translation_domain=DOMAIN, translation_key="control_requires_manual_temp")
        if name in {"lid_open", "lid_close", "ring_open", "ring_close"} and (
                fields["occupancy"] or fields["working"] or fields.get("lid_moving", True) or fields.get("ring_moving", True)):
            raise ControlRejected(translation_domain=DOMAIN, translation_key="control_requires_unoccupied")
        if name in {"wash_hip", "wash_woman", "wash_auto_hip", "wash_auto_woman", "dry"} and not fields["occupancy"]:
            raise ControlRejected(translation_domain=DOMAIN, translation_key="control_requires_occupied")
        if self._closing:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="query_in_progress")
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
        session.reset()
        frames.clear()
        fields.clear()
        event.clear()
        self.data["last_control_frame"] = packet.hex().upper()
        self.data["control_write_attempted"] = True
        self.data["last_control_write_ms"] = round((monotonic() - self._operation_start) * 1000)
        _LOGGER.debug("Control %s single write starts after %sms", name, self.data["last_control_write_ms"])
        await session.write(packet, response=response)  # exactly once
        self.data["control_write_completed"] = True
        self.data["last_control_write_complete_ms"] = round((monotonic() - self._operation_start) * 1000)
        self.data["last_control_result"] = "sent_unconfirmed"
        self.async_set_updated_data(self.data)
        self._start_confirmation(session, expected, frames, fields, event, response,
                                 self._operation_start)

    @asynccontextmanager
    async def _control_slot(self):
        # Bound explicit user requests rather than dropping a second click just
        # because the preceding write is still completing. Never queue minutes.
        if self._closing or self._control_waiters >= 4:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="query_in_progress")
        self._control_waiters += 1
        acquired = False
        try:
            await self._cancel_confirmation()
            try:
                async with asyncio.timeout(10):
                    await self._lock.acquire()
            except TimeoutError as err:
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="query_in_progress") from err
            acquired = True
            # A preceding foreground request may have created its confirmation
            # while this request was queued. Cancel that observer too.
            await self._cancel_confirmation()
            if self._closing:
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="query_in_progress")
            yield
        finally:
            if acquired:
                self._lock.release()
            self._control_waiters -= 1

    async def _cancel_confirmation(self):
        record, task = self._confirmation, self._confirmation_task
        self._confirmation = None
        self._confirmation_task = None
        if record is not None:
            record["unsubscribe"]()
        if task is not None and not task.done():
            if record is None or not record.get("writing"):
                task.cancel()
            # Retain and shield the owning task, not a detached write. Even if
            # this caller is cancelled, its bounded QUERY still owns the lock.
            self._confirmation_draining.add(task)
            task.add_done_callback(self._confirmation_draining.discard)
        draining = tuple(self._confirmation_draining)
        if draining:
            await asyncio.shield(asyncio.gather(*draining, return_exceptions=True))

    def _start_confirmation(self, session, expected, frames, fields, event, response, started):
        # Local dictionaries contain only post-write observations. They never
        # borrow stored/global snapshots, even when the transport is reused.
        if self._closing:
            return
        def observe(frame):
            frames[f"{frame[5]:02X}"] = frame
            fields.update(decode_source_fields(frame))
            event.set()
        record = {"session": session, "expected": dict(expected), "frames": frames,
                  "fields": fields, "event": event, "response": response,
                  "started": started, "unsubscribe": session.add_observer(observe)}
        self._confirmation = record
        create = getattr(self.hass, "async_create_background_task", None)
        self._confirmation_task = (create(self._confirm_in_background(record), "JOMOO background observation")
            if create else self.hass.async_create_task(self._confirm_in_background(record)))

    async def _confirm_in_background(self, record):
        session = record["session"]
        expected, fields, frames = record["expected"], record["fields"], record["frames"]
        matched = lambda: bool(expected) and all(fields.get(k) == v for k, v in expected.items())
        deadline = monotonic() + CONTROL_READBACK_TIMEOUT * 3
        try:
            if not matched():
                await asyncio.sleep(POST_COMMAND_PAUSE)
            while self._confirmation is record and not matched() and monotonic() < deadline:
                if self._session is not session or not session.live:
                    break
                if self._control_waiters or self._lock.locked():
                    await asyncio.sleep(min(0.05, max(0, deadline - monotonic())))
                    continue
                # Hold the transport lock for a query WRITE only, never while
                # waiting for a reply. Foreground requests cancel this task.
                async with self._lock:
                    if self._confirmation is not record or self._control_waiters:
                        continue
                    record["writing"] = True
                    try:
                        async with asyncio.timeout(1):
                            await session.write(QUERY, response=record["response"])
                    finally:
                        record["writing"] = False
                if self._confirmation is not record:
                    return
                try:
                    await self._wait_observation(matched, record["event"],
                        min(CONTROL_READBACK_TIMEOUT, 1.0, max(0.001, deadline-monotonic())))
                except TimeoutError:
                    pass
            if self._confirmation is record:
                self.data["last_control_result"] = ("confirmed" if matched() else
                    "readback_mismatch" if expected and all(k in fields for k in expected) else "sent_unconfirmed")
                self.data["last_result"] = "valid_status_received" if frames else "no_valid_status"
                self.data["last_control_confirmation_ms"] = round((monotonic()-record["started"])*1000)
                self.async_set_updated_data(self.data)
                self._save_later()
                _LOGGER.debug("Background control observation: %s in %sms",
                              self.data["last_control_result"], self.data["last_control_confirmation_ms"])
        except asyncio.CancelledError:
            raise  # a newer request superseded observation, not the BLE session
        except Exception as err:
            if self._confirmation is record:
                self.data["last_control_result"] = "sent_unconfirmed"
                self.async_set_updated_data(self.data)
                self._save_later()
                _LOGGER.debug("Background readback ended without command replay: %s", err)
        finally:
            record["unsubscribe"]()
            if self._confirmation is record:
                self._confirmation = None
                self._confirmation_task = None

    def _save_later(self):
        save = getattr(self._store, "async_delay_save", None)
        if save:
            save(lambda: self.data, 0.5)

    def _cancel_idle(self):
        self._idle_generation += 1
        if self._idle_handle:
            self._idle_handle.cancel()
            self._idle_handle = None

    def _publish_live(self):
        self._publish_handle = None
        self.async_set_updated_data(self.data)

    def _on_session_frame(self, session, frame):
        if self._session is not session or not session.live:
            return
        key = f"{frame[5]:02X}"
        now = dt_util.utcnow().isoformat()
        self.data["frames"][key] = frame.hex().upper()[:42]
        self.data["frame_times"][key] = now
        self.data["last_status_time"] = now
        self.data["bluetooth_connected"] = True
        for name, value in decode_source_fields(frame).items():
            self.data["fields"][name] = value
            self.data["field_times"][name] = now
        if not self._lock.locked() and self._publish_handle is None:
            self._publish_handle = asyncio.get_running_loop().call_later(0.1, self._publish_live)
            delay_save = getattr(self._store, "async_delay_save", None)
            if delay_save:
                delay_save(lambda: self.data, 10)

    def _on_disconnected(self, client):
        session = self._session
        if session is not None and session.client is client:
            session.invalidate()
            self.data["bluetooth_connected"] = False
            self.async_set_updated_data(self.data)
            # An unplanned link loss would otherwise bill the next tap for a
            # 5-9 s cold connect. Re-warm with one read-only query when the
            # user's policy is persistent; releases never re-acquire.
            if not self._closing and not self._releasing and self.connection_timeout() < 0:
                self._schedule_rewarm()

    async def _drain_retired(self):
        client = self._retired_client
        if client is None:
            return
        try:
            if client.is_connected:
                async with asyncio.timeout(6):
                    await client.disconnect()
            if client.is_connected:
                raise ConnectionError("Previous BLE link is still connected")
        finally:
            if not client.is_connected and self._retired_client is client:
                self._retired_client = None

    async def _close_session(self):
        await self._cancel_confirmation()
        self._cancel_idle()
        session, self._session = self._session, None
        if session is not None:
            self._retired_client = session.client
            try:
                await session.close()
            finally:
                if not session.client.is_connected and self._retired_client is session.client:
                    self._retired_client = None
                self.data["bluetooth_connected"] = bool(self._retired_client and self._retired_client.is_connected)
                self.async_set_updated_data(self.data)
        await self._drain_retired()
        self.data["bluetooth_connected"] = False
        self.async_set_updated_data(self.data)

    async def _idle_expired(self, generation):
        async with self._lock:
            if generation == self._idle_generation:
                try:
                    await self._close_session()
                except (BleakError, OSError, TimeoutError) as err:
                    _LOGGER.warning("Idle BLE release failed; retaining previous link: %s", err)

    def connection_timeout(self):
        mode = getattr(self.entry, "options", {}).get("connection_mode")
        return {"persistent": -1, "idle": 60, "disconnect": 0}.get(mode, SESSION_IDLE_TIMEOUT)

    def async_start_warmup(self):
        if self._closing or self.connection_timeout() >= 0:
            return
        create = getattr(self.hass, "async_create_background_task", None)
        if self._warmup_task is not None and not self._warmup_task.done():
            return
        if create:
            self._warmup_task = create(self._async_warmup(), "JOMOO read-only BLE warmup")
        else:
            self._warmup_task = self.hass.async_create_task(self._async_warmup())

    def _schedule_rewarm(self, delay=REWARM_DELAY):
        """Re-establish a persistent link once, never replaying an action."""
        if self._closing or self._releasing or self.connection_timeout() >= 0:
            return
        if self._rewarm_handle is not None:
            return
        self._cancel_rewarm()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # The disconnected callback may arrive off the event loop.
            loop = self.hass.loop
        self._rewarm_handle = loop.call_later(delay, self._start_rewarm)

    def _cancel_rewarm(self):
        if self._rewarm_handle is not None:
            self._rewarm_handle.cancel()
            self._rewarm_handle = None

    def _start_rewarm(self):
        self._rewarm_handle = None
        if self._closing or self._releasing:
            return
        self.async_start_warmup()

    async def _async_warmup(self):
        if self._lock.locked() or (self._session is not None and self._session.live):
            return
        try:
            await self.async_query()
        except HomeAssistantError as err:
            _LOGGER.debug("BLE warmup deferred: %s", err)

    async def async_apply_options(self):
        if self._closing:
            return
        await self._cancel_confirmation()
        self._cancel_rewarm()
        async with self._lock:
            if self._closing:
                return
            await self._release_or_keep()
            needs_warmup = self.connection_timeout() < 0 and self._session is None
        if needs_warmup:
            self.async_start_warmup()

    async def _release_or_keep(self):
        self._cancel_idle()
        if self._session is None:
            return
        timeout = self.connection_timeout()
        if timeout == 0 or not self._session.live:
            await self._close_session()
            return
        if timeout < 0:
            return
        generation = self._idle_generation
        def expire():
            self.hass.async_create_task(self._idle_expired(generation))
        self._idle_handle = asyncio.get_running_loop().call_later(timeout, expire)

    async def async_disconnect(self):
        """Release the BLE session for the manufacturer's app; never send an action."""
        await self._cancel_confirmation()
        self._cancel_rewarm()
        if self._lock.locked():
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="query_in_progress")
        self._releasing = True
        try:
            async with self._lock:
                await self._close_session()
                await self._store.async_save(self.data)
        finally:
            self._releasing = False

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

    async def _get_session(self, observer):
        if self._closing:
            raise HomeAssistantError(translation_domain=DOMAIN, translation_key="query_in_progress")
        self._cancel_idle()
        await self._drain_retired()
        if self._session is not None and self._session.live:
            self.data["last_connection_reused"] = True
            return self._session, self._session.add_observer(observer)
        if self._session is not None:
            await self._close_session()
        self.data["last_connection_reused"] = False
        current_sources = getattr(bluetooth, "async_scanner_devices_by_address", None)
        reachable = current_sources(self.hass, self.address, connectable=True) if current_sources else []
        _LOGGER.debug("Current connection sources for target: %d", len(reachable))
        if not reachable:
            try:
                await self._async_wait_for_advertisement()
            except TimeoutError:
                self.data["last_result"] = "not_discovered"
                return None, lambda: None
        ble_device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
        if ble_device is None:
            self.data["last_result"] = "not_discovered"
            return None, lambda: None
        try:
            async with asyncio.timeout(CONNECT_BUDGET):
                client = await establish_connection(
                    BleakClient, ble_device, MODEL, max_attempts=3, timeout=CONNECT_TIMEOUT,
                    disconnected_callback=self._on_disconnected,
                )
        except (TimeoutError, BleakError, OSError) as err:
            # The library's internal retries can hold the adapter for a minute
            # or more; a bounded failure plus a later silent re-warm keeps the
            # next tap from inheriting that wait.
            self.data["last_result"] = ("connection_timeout" if isinstance(err, TimeoutError)
                                        else "connection_error")
            _LOGGER.debug("BLE connect failed within %.0fs: %s", CONNECT_BUDGET, err)
            self._schedule_rewarm(REWARM_RETRY)
            raise HomeAssistantError(translation_domain=DOMAIN,
                                     translation_key="connection_failed") from err
        self._retired_client = client
        try:
            char = client.services.get_characteristic(CHAR_UUID)
            failure = None
            if char is None or char.service_uuid.lower() != SERVICE_UUID:
                failure = "missing_characteristic"
            elif "notify" not in char.properties:
                failure = "notify_not_supported"
            elif not ({"write", "write-without-response"} & set(char.properties)):
                failure = "write_not_supported"
        except BaseException:
            await self._drain_retired()
            raise
        if failure:
            self.data["last_result"] = failure
            await self._drain_retired()
            return None, lambda: None
        session = BLESession(client, char, lambda frame: self._on_session_frame(session, frame))
        self._session = session
        self._retired_client = None
        unsubscribe = session.add_observer(observer)
        try:
            await session.start()
        except BaseException:
            await self._close_session()
            raise
        self.data["bluetooth_connected"] = True
        return session, unsubscribe

    async def _async_query_device(self, control=None) -> None:
        received = asyncio.Event()
        observed_frames, observed_fields = {}, {}

        def observe(frame):
            observed_frames[f"{frame[5]:02X}"] = frame
            observed_fields.update(decode_source_fields(frame))
            received.set()

        session, unsubscribe = await self._get_session(observe)
        if session is None:
            return
        # Reuse the transport, never old guard/settings observations. Ordinary
        # 19-byte controls still obtain FA/E9/EA from this transaction's query.
        client, char = session.client, session.char
        response = "write" in char.properties
        try:
            if control is not None:
                async with asyncio.timeout(35):
                    await self._async_control_on_client(
                        client, char, response, control, observed_frames, observed_fields, received, session
                    )
            else:
                try:
                    async with asyncio.timeout(NOTIFY_TIMEOUT):
                        await session.write(QUERY, response=response)
                        await received.wait()
                        await asyncio.sleep(0.4)
                except TimeoutError:
                    self.data["last_result"] = "valid_status_received" if received.is_set() else "no_valid_status"
                else:
                    self.data["last_result"] = "valid_status_received"
        except ControlRejected:
            # A guard said "not now" while the link is fine. Keep the session:
            # tearing it down made every retry pay a cold connect.
            raise
        except BaseException:
            await self._close_session()
            raise
        finally:
            unsubscribe()
            await self._release_or_keep()

    async def async_shutdown(self) -> None:
        """Release timers and subscription without replaying any command."""
        self._closing = True
        await self._cancel_confirmation()
        if self._warmup_task is not None and not self._warmup_task.done():
            self._warmup_task.cancel()
            await asyncio.gather(self._warmup_task, return_exceptions=True)
        self._warmup_task = None
        self._cancel_idle()
        self._cancel_rewarm()
        if self._publish_handle:
            self._publish_handle.cancel()
            self._publish_handle = None
        async with self._lock:
            await self._close_session()
            await self._store.async_save(self.data)
