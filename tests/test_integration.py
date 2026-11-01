"""Offline unit/mocked integration tests. No HA, Bleak, BLE or network needed."""
import asyncio
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "jomoo_zs800i"
PKG = "custom_components.jomoo_zs800i"


def module(name, **attrs):
    obj = types.ModuleType(name)
    obj.__dict__.update(attrs)
    sys.modules[name] = obj
    return obj


module("custom_components", __path__=[str(ROOT / "custom_components")])
module(PKG, __path__=[str(COMPONENT)])
module("homeassistant", __path__=[])
module("homeassistant.components", __path__=[])
async def process_advertisements(*args, **kwargs):
    return None

bluetooth = module("homeassistant.components.bluetooth",
                   async_ble_device_from_address=lambda *a, **kw: None,
                   async_process_advertisements=process_advertisements,
                   BluetoothScanningMode=types.SimpleNamespace(ACTIVE="active"))
module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)


class HAError(Exception):
    def __init__(self, *args, **kwargs):
        super().__init__(*args)
        self.translation_domain = kwargs.get("translation_domain")
        self.translation_key = kwargs.get("translation_key")


class BLEError(Exception):
    pass


class Store:
    persisted = None

    def __init__(self, *args):
        self.saved = None

    async def async_load(self):
        return self.persisted

    async def async_save(self, value):
        self.saved = json.loads(json.dumps(value))


class Coordinator:
    @classmethod
    def __class_getitem__(cls, item):
        return cls

    def __init__(self, hass, logger, **kwargs):
        self.hass = hass
        self.update_interval = kwargs.get("update_interval")

    def async_set_updated_data(self, value):
        self.data = value


module("homeassistant.exceptions", HomeAssistantError=HAError)
module("homeassistant.helpers", __path__=[])
module("homeassistant.helpers.storage", Store=Store)
module("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=Coordinator)
module("homeassistant.util", __path__=[])
module("homeassistant.util.dt", utcnow=lambda: datetime.now(timezone.utc),
       parse_datetime=datetime.fromisoformat)
module("bleak", BleakClient=None)
module("bleak.exc", BleakError=BLEError)

async def establish_connection(client_class, device, name, *, max_attempts, timeout, disconnected_callback=None):
    assert max_attempts == 3 and timeout == const.CONNECT_TIMEOUT
    client = client_class(device, timeout=timeout)
    await client.__aenter__()
    return client

module("bleak_retry_connector", establish_connection=establish_connection)
protocol = importlib.import_module(f"{PKG}.protocol")
coordinator_module = importlib.import_module(f"{PKG}.coordinator")
const = importlib.import_module(f"{PKG}.const")

FA = bytes.fromhex("f3f40c2100fa3e53b235333b292359fc")
E9 = bytes.fromhex("f3f40a2122e95700000100008efc")
EA = bytes.fromhex("f3f4112122ea05008500804c13005f0000000006fc")


def frame(command, payload=b""):
    body = bytes([len(payload) + 4, 0x21, 0, command]) + payload
    return b"\xf3\xf4" + body + bytes([sum(body) & 255, 0xFC])


class ProtocolTests(unittest.TestCase):
    def test_real_frames_and_query(self):
        for value in (FA, E9, EA, const.QUERY):
            self.assertTrue(protocol.valid_frame(value))
        self.assertIsNone(protocol.status_command(const.QUERY))
        self.assertEqual(protocol.status_command(FA), 0xFA)

    def test_strict_checks(self):
        for value in (b"", FA[:-1], FA + b"\x00", b"xx" + FA[2:], FA[:-1] + b"\x00"):
            self.assertFalse(protocol.valid_frame(value))
        bad = bytearray(FA)
        bad[-2] ^= 1
        self.assertFalse(protocol.valid_frame(bad))

    def test_every_split(self):
        for n in range(1, len(FA)):
            parser = protocol.FrameParser()
            self.assertEqual(parser.feed(FA[:n]), [])
            self.assertEqual(parser.feed(FA[n:]), [FA])

    def test_glued_noise_and_bad_checksum_resync(self):
        parser = protocol.FrameParser()
        bad = bytearray(FA)
        bad[-2] ^= 1
        self.assertEqual(parser.feed(b"noise\xf3" + bad + FA + E9 + EA), [FA, E9, EA])

    def test_oversized_input_and_buffer_bound(self):
        parser = protocol.FrameParser()
        parser.feed(FA[:4])
        self.assertEqual(parser.feed(b"x" * (protocol.MAX_INPUT + 1)), [])
        self.assertEqual(parser.buffered_bytes, 0)
        for _ in range(50):
            parser.feed(b"\xf3\xf4\xff" + b"x" * 100)
            self.assertLessEqual(parser.buffered_bytes, 258)
        self.assertEqual(parser.feed(FA), [FA])

    def test_bad_short_length_resync(self):
        self.assertEqual(protocol.FrameParser().feed(b"\xf3\xf4\x00" + EA), [EA])

    def test_source_fields(self):
        self.assertEqual(protocol.decode_source_fields(FA),
                         {"water_level": 3, "seat_level": 3, "air_level": 2, "atmosphere_on": True,
                          "close_flush": True, "flush_small_running": False, "flush_large_running": False,
                          "uv_water": True, "auto_bubble": True, "prewet": True, "auto_temp": True,
                          "foot_sensor": True, "auto_small_flush": False})
        self.assertEqual(protocol.decode_source_fields(E9), {"occupancy": False, "work_code": 1,
                         "working": False, "is_woman": False, "dry_running": False,
                         "hip_wash_running": False, "woman_wash_running": False, "auto_work": False})
        self.assertEqual(protocol.decode_source_fields(EA), {"ring_open": False, "lid_open": False,
                         "ring_moving": False, "lid_moving": False})

    def test_parser_phase_reset_drops_partial_pre_write_frame(self):
        parser = protocol.FrameParser()
        self.assertEqual(parser.feed(FA[:5]), [])
        parser.reset()
        self.assertEqual(parser.buffered_bytes, 0)
        self.assertEqual(parser.feed(FA), [FA])

    def test_special_work_modes_are_not_misconfirmed_as_wash(self):
        payload = bytearray(E9[6:-2])
        payload[3], payload[4] = 0x20, 0x20
        fields = protocol.decode_source_fields(frame(0xE9, payload))
        self.assertFalse(fields["hip_wash_running"])
        self.assertTrue(fields["working"])
        payload[3], payload[4] = 0x60, 0
        fields = protocol.decode_source_fields(frame(0xE9, payload))
        self.assertTrue(fields["hip_wash_running"])
        self.assertFalse(fields["woman_wash_running"])

    def test_atmosphere_payload_bit_and_minimum_length(self):
        self.assertFalse(protocol.decode_source_fields(frame(0xFA, bytes([0, 0, 0, 0, 0, 0x2A])))["atmosphere_on"])
        self.assertTrue(protocol.decode_source_fields(frame(0xFA, bytes([0, 0, 0, 0, 0, 0x3A])))["atmosphere_on"])
        self.assertNotIn("atmosphere_on", protocol.decode_source_fields(frame(0xFA, b"\x00" * 5)))

    def test_short_payload_does_not_index_checksum_or_trailer(self):
        self.assertEqual(protocol.decode_source_fields(frame(0xFA)), {})
        self.assertEqual(protocol.decode_source_fields(frame(0xFA, b"\x10\x20")), {})
        self.assertEqual(protocol.decode_source_fields(frame(0xEA)), {})
        self.assertEqual(protocol.decode_source_fields(frame(0xE9)), {})


class FakeClient:
    props = ["notify", "read", "write-without-response"]
    packets = [FA[:7], FA[7:] + E9 + EA]
    fail = None
    gate = None
    instances = []
    missing = False
    service = const.SERVICE_UUID

    def __init__(self, device, **kwargs):
        assert not isinstance(device, str), "Never pass an address to BleakClient"
        self.device = device
        self.disconnected = False
        self.stopped = False
        self.writes = []
        self.char = types.SimpleNamespace(properties=self.props, service_uuid=self.service)
        self.services = types.SimpleNamespace(get_characteristic=lambda uuid: None if self.missing else self.char)
        self.instances.append(self)

    async def __aenter__(self):
        if self.fail == "connect":
            raise BLEError("connect error")
        if self.fail == "connect_timeout":
            raise TimeoutError
        return self

    async def __aexit__(self, *args):
        self.disconnected = True

    @property
    def is_connected(self):
        return not self.disconnected

    async def disconnect(self):
        self.disconnected = True

    initial_packets = []

    async def start_notify(self, char, cb):
        self.cb = cb
        for packet in self.initial_packets:
            cb(char, bytearray(packet))
        if self.fail == "subscribe":
            raise BLEError("subscribe error")

    async def stop_notify(self, char):
        self.stopped = True
        if self.fail == "stop":
            raise BLEError("stop error")

    packet_script = None

    async def write_gatt_char(self, char, data, *, response):
        self.writes.append((data, response))
        if self.gate is not None:
            await self.gate.wait()
        if self.fail == "write":
            raise BLEError("write error")
        packets = (self.packet_script[min(len(self.writes) - 1, len(self.packet_script) - 1)]
                   if self.packet_script else self.packets)
        for packet in packets:
            self.cb(char, bytearray(packet))


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeClient.props = ["notify", "read", "write-without-response"]
        FakeClient.packets = [FA[:7], FA[7:] + E9 + EA]
        FakeClient.fail = None
        FakeClient.gate = None
        FakeClient.packet_script = None
        FakeClient.initial_packets = []
        FakeClient.instances = []
        FakeClient.missing = False
        FakeClient.service = const.SERVICE_UUID
        Store.persisted = None
        self.device = object()
        self.lookups = []

        def lookup(hass, address, *, connectable):
            self.lookups.append((hass, address, connectable))
            return self.device

        self.patches = [patch.object(coordinator_module, "BleakClient", FakeClient),
                        patch.object(bluetooth, "async_ble_device_from_address", lookup),
                        patch.object(coordinator_module, "NOTIFY_TIMEOUT", 0.025),
                        patch.object(coordinator_module, "CONTROL_SETTLE", 0),
                        patch.object(coordinator_module, "MIN_WRITE_GAP", 0),
                        patch.object(coordinator_module, "POST_COMMAND_PAUSE", 0),
                        patch.object(coordinator_module, "SESSION_IDLE_TIMEOUT", 0),
                        patch.object(coordinator_module, "CONTROL_READBACK_TIMEOUT", 0.005)]
        for item in self.patches:
            item.start()
        self.entry = types.SimpleNamespace(data={"address": "AA:BB:CC:DD:EE:FF"}, entry_id="test")
        self.hass = types.SimpleNamespace(async_create_task=asyncio.create_task)
        self.coordinator = coordinator_module.JomooCoordinator(self.hass, self.entry)

    async def control_and_observe(self, name, value=None):
        """Legacy protocol assertions await observation explicitly, not via the API.

        New nonblocking tests below invoke coordinator.async_control directly.
        Keep the mock transport alive while checking readback behavior.
        """
        ephemeral = self.coordinator.connection_timeout() == 0
        try:
            with patch.object(coordinator_module, "SESSION_IDLE_TIMEOUT", 60 if ephemeral else coordinator_module.SESSION_IDLE_TIMEOUT):
                await self.coordinator.async_control(name, value)
                task = self.coordinator._confirmation_task
                if task is not None:
                    await task
        finally:
            if ephemeral:
                await self.coordinator._close_session()

    async def asyncTearDown(self):
        await self.coordinator.async_shutdown()
        for item in reversed(self.patches):
            item.stop()

    async def test_control_returns_before_missing_readback(self):
        self.entry.options = {"connection_mode": "persistent"}
        FakeClient.packets = []
        with patch.object(coordinator_module, "POST_COMMAND_PAUSE", 1):
            await asyncio.wait_for(self.coordinator.async_control("atmosphere_on", False), 0.1)
        self.assertEqual(self.coordinator.data["last_control_result"], "sent_unconfirmed")
        self.assertIsNotNone(self.coordinator._confirmation_task)
        self.assertFalse(self.coordinator._lock.locked())
        self.assertFalse(FakeClient.instances[0].disconnected)

    async def test_next_control_supersedes_background_without_disconnect(self):
        self.entry.options = {"connection_mode": "persistent"}
        FakeClient.packets = []
        with patch.object(coordinator_module, "POST_COMMAND_PAUSE", 1):
            await self.coordinator.async_control("atmosphere_on", False)
            old = self.coordinator._confirmation_task
            await asyncio.wait_for(self.coordinator.async_control("atmosphere_on", True), 0.1)
        self.assertTrue(old.cancelled())
        client = FakeClient.instances[0]
        self.assertEqual(len(FakeClient.instances), 1)
        self.assertFalse(client.disconnected)
        self.assertEqual(len([p for p, _ in client.writes if p != const.QUERY]), 2)
        self.assertTrue(self.coordinator.data["last_control_frame"].endswith("33202BFC"))

    async def test_background_wait_never_holds_transport_lock(self):
        self.entry.options = {"connection_mode": "persistent"}
        FakeClient.packets = []
        with patch.object(coordinator_module, "CONTROL_READBACK_TIMEOUT", 1):
            await self.coordinator.async_control("atmosphere_on", False)
            old = self.coordinator._confirmation_task
            await asyncio.sleep(0.01)
            self.assertFalse(self.coordinator._lock.locked())
            self.assertTrue(any(p == const.QUERY for p, _ in FakeClient.instances[0].writes))
            await asyncio.wait_for(self.coordinator.async_control("atmosphere_on", True), 0.1)
            self.assertTrue(old.cancelled())
            self.assertFalse(FakeClient.instances[0].disconnected)

    async def test_inflight_background_query_finishes_before_next_write(self):
        self.entry.options = {"connection_mode": "persistent"}
        FakeClient.packets = []
        started, release = asyncio.Event(), asyncio.Event()
        original = FakeClient.write_gatt_char
        async def gated(client, char, data, *, response):
            if data == const.QUERY:
                started.set()
                await release.wait()
            await original(client, char, data, response=response)
        with patch.object(FakeClient, "write_gatt_char", gated), \
             patch.object(coordinator_module, "CONTROL_READBACK_TIMEOUT", 1):
            await self.coordinator.async_control("atmosphere_on", False)
            await started.wait()
            second = asyncio.create_task(self.coordinator.async_control("atmosphere_on", True))
            await asyncio.sleep(0.005)
            self.assertFalse(second.done())
            self.assertFalse(FakeClient.instances[0].disconnected)
            release.set()
            await asyncio.wait_for(second, 0.1)
        self.assertEqual(len([p for p, _ in FakeClient.instances[0].writes if p != const.QUERY]), 2)

    async def test_old_observation_cannot_overwrite_new_control(self):
        self.entry.options = {"connection_mode": "persistent"}
        FakeClient.packets = []
        with patch.object(coordinator_module, "POST_COMMAND_PAUSE", 1):
            await self.coordinator.async_control("atmosphere_on", False)
            old = self.coordinator._confirmation
            await self.coordinator.async_control("atmosphere_on", True)
            current = self.coordinator._confirmation
            old["fields"]["atmosphere_on"] = False
            await self.coordinator._confirm_in_background(old)
            self.assertIs(self.coordinator._confirmation, current)
            self.assertEqual(self.coordinator.data["last_control_result"], "sent_unconfirmed")
            FakeClient.instances[0].cb(None, bytearray(FA))
            await self.coordinator._confirmation_task
        self.assertEqual(self.coordinator.data["last_control_result"], "confirmed")

    async def test_two_foreground_requests_queue_writes_not_confirmations(self):
        self.entry.options = {"connection_mode": "persistent"}
        FakeClient.packets = []
        FakeClient.gate = asyncio.Event()
        with patch.object(coordinator_module, "POST_COMMAND_PAUSE", 1):
            first = asyncio.create_task(self.coordinator.async_control("atmosphere_on", False))
            await asyncio.sleep(0)
            second = asyncio.create_task(self.coordinator.async_control("atmosphere_on", True))
            await asyncio.sleep(0)
            FakeClient.gate.set()
            await asyncio.wait_for(asyncio.gather(first, second), 0.2)
        self.assertEqual(self.coordinator._control_waiters, 0)
        self.assertEqual(len(FakeClient.instances), 1)
        self.assertEqual(len([p for p, _ in FakeClient.instances[0].writes if p != const.QUERY]), 2)

    async def test_refresh_cancels_readback_but_keeps_session(self):
        self.entry.options = {"connection_mode": "persistent"}
        FakeClient.packets = []
        with patch.object(coordinator_module, "POST_COMMAND_PAUSE", 1):
            await self.coordinator.async_control("atmosphere_on", False)
            old = self.coordinator._confirmation_task
            await self.coordinator.async_query()
        self.assertTrue(old.cancelled())
        self.assertIsNone(self.coordinator._confirmation_task)
        self.assertFalse(FakeClient.instances[0].disconnected)

    async def test_release_and_shutdown_remove_pending_observers(self):
        self.entry.options = {"connection_mode": "persistent"}
        FakeClient.packets = []
        with patch.object(coordinator_module, "POST_COMMAND_PAUSE", 1):
            await self.coordinator.async_control("atmosphere_on", False)
            old = self.coordinator._confirmation_task
            session = self.coordinator._session
            await self.coordinator.async_disconnect()
        self.assertTrue(old.cancelled())
        self.assertEqual(session._observers, [])
        self.assertTrue(FakeClient.instances[0].disconnected)
        await self.coordinator.async_shutdown()
        with self.assertRaises(HAError):
            await self.coordinator.async_control("atmosphere_on", True)

    async def test_cancelled_caller_does_not_cancel_inflight_background_write(self):
        self.entry.options = {"connection_mode": "persistent"}
        FakeClient.packets = []
        started, release, interrupted = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original = FakeClient.write_gatt_char
        async def gated(client, char, data, *, response):
            if data == const.QUERY:
                started.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    interrupted.set()
                    raise
            await original(client, char, data, response=response)
        with patch.object(FakeClient, "write_gatt_char", gated), \
             patch.object(coordinator_module, "CONTROL_READBACK_TIMEOUT", 1):
            await self.coordinator.async_control("atmosphere_on", False)
            await started.wait()
            caller = asyncio.create_task(self.coordinator.async_control("atmosphere_on", True))
            await asyncio.sleep(0.005)
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller
            self.assertFalse(interrupted.is_set())
            self.assertTrue(self.coordinator._lock.locked())
            release.set()
            await self.coordinator._cancel_confirmation()
        self.assertFalse(self.coordinator._lock.locked())
        self.assertFalse(FakeClient.instances[0].disconnected)

    async def test_shutdown_blocks_refresh_and_option_warmup(self):
        await self.coordinator.async_shutdown()
        with self.assertRaises(HAError):
            await self.coordinator.async_query()
        self.entry.options = {"connection_mode": "persistent"}
        await self.coordinator.async_apply_options()
        self.assertEqual(FakeClient.instances, [])

    async def test_local_guard_rejection_keeps_the_warm_link(self):
        """A rejected command must not cost the next tap a cold connect."""
        self.entry.options = {"connection_mode": "persistent"}
        await self.coordinator.async_query()
        client = FakeClient.instances[0]
        self.assertTrue(self.coordinator.data["fields"]["auto_temp"])
        for name, value in (("water_level", 1), ("seat_level", 5)):
            with self.subTest(name=name):
                with self.assertRaises(HAError) as error:
                    await self.coordinator.async_control(name, value)
                self.assertEqual(error.exception.translation_key,
                                 "control_requires_manual_temp")
                self.assertFalse(client.disconnected)
                self.assertTrue(self.coordinator._session.live)
        self.assertEqual([p for p, _ in client.writes if p != const.QUERY], [])

    async def test_guard_rejection_in_ephemeral_mode_still_releases(self):
        """Explicit release policies keep their contract even after a refusal."""
        self.entry.options = {"connection_mode": "disconnect"}
        with self.assertRaises(HAError):
            await self.coordinator.async_control("water_level", 1)
        self.assertTrue(FakeClient.instances[0].disconnected)

    async def test_unexpected_disconnect_rewarms_persistent_link_once(self):
        self.entry.options = {"connection_mode": "persistent"}
        await self.coordinator.async_query()
        first = FakeClient.instances[0]
        self.coordinator._on_disconnected(first)
        self.assertFalse(self.coordinator.data["bluetooth_connected"])
        self.assertIsNotNone(self.coordinator._rewarm_handle)
        again = self.coordinator._rewarm_handle
        self.coordinator._schedule_rewarm()
        self.assertIs(again, self.coordinator._rewarm_handle)
        self.coordinator._cancel_rewarm()
        self.assertIsNone(self.coordinator._rewarm_handle)

    async def test_manual_release_is_never_undone_by_rewarm(self):
        self.entry.options = {"connection_mode": "persistent"}
        await self.coordinator.async_query()
        self.coordinator._releasing = True
        try:
            self.coordinator._on_disconnected(FakeClient.instances[0])
            self.coordinator._schedule_rewarm()
        finally:
            self.coordinator._releasing = False
        self.assertIsNone(self.coordinator._rewarm_handle)
        self.coordinator._start_rewarm()
        self.assertEqual(len(FakeClient.instances), 1)

    async def test_rewarm_skipped_for_non_persistent_policies(self):
        for mode in ("idle", "disconnect"):
            with self.subTest(mode=mode):
                self.entry.options = {"connection_mode": mode}
                self.coordinator._schedule_rewarm()
                self.assertIsNone(self.coordinator._rewarm_handle)

    async def test_slow_connect_fails_fast_and_schedules_retry(self):
        self.entry.options = {"connection_mode": "persistent"}

        async def stalled(*args, **kwargs):
            await asyncio.sleep(30)
        started = asyncio.get_running_loop().time()
        with patch.object(coordinator_module, "establish_connection", stalled), \
             patch.object(coordinator_module, "CONNECT_BUDGET", 0.02):
            await self.coordinator.async_query()
        elapsed = asyncio.get_running_loop().time() - started
        self.assertLess(elapsed, 5, "a stalled connect must not hold the request")
        self.assertEqual(self.coordinator.data["last_result"], "connection_timeout")
        self.assertIsNotNone(self.coordinator._rewarm_handle)
        self.coordinator._cancel_rewarm()

    async def test_shutdown_cancels_pending_rewarm(self):
        self.entry.options = {"connection_mode": "persistent"}
        await self.coordinator.async_query()
        self.coordinator._schedule_rewarm()
        self.assertIsNotNone(self.coordinator._rewarm_handle)
        await self.coordinator.async_shutdown()
        self.assertIsNone(self.coordinator._rewarm_handle)

    async def test_cache_restore_never_connects(self):
        await self.coordinator.async_load()
        self.assertEqual(FakeClient.instances, [])
        self.assertEqual(self.lookups, [])
        self.assertIsNone(self.coordinator.update_interval)
        self.assertEqual(self.coordinator.data["last_result"], "never_queried")
        self.assertIsNone(self.coordinator.last_status_time)

    async def test_success_write_without_response_cache_disconnect(self):
        await self.coordinator.async_query()
        client = FakeClient.instances[0]
        self.assertEqual(client.writes, [(const.QUERY, False)])
        self.assertTrue(client.disconnected)
        self.assertTrue(client.stopped)
        self.assertEqual(self.lookups[0][1:], (self.entry.data["address"], True))
        self.assertEqual(self.coordinator.data["last_result"], "valid_status_received")
        self.assertEqual(self.coordinator.data["frames"], {"FA": FA.hex().upper(), "E9": E9.hex().upper(), "EA": EA.hex().upper()})
        self.assertEqual(self.coordinator.data["fields"]["water_level"], 3)
        self.assertIsNotNone(self.coordinator.last_status_time)
        self.assertIsNotNone(self.coordinator._store.saved)

    async def test_fresh_advertisement_is_requested_only_on_refresh(self):
        calls = []
        async def receive(hass, callback, matcher, mode, timeout):
            import time
            calls.append((matcher, mode, timeout))
            self.assertTrue(callback(types.SimpleNamespace(time=time.monotonic())))
            self.assertTrue(callback(types.SimpleNamespace(time=0)))
        with patch.object(bluetooth, "async_process_advertisements", receive):
            await self.coordinator.async_load()
            self.assertEqual(calls, [])
            await self.coordinator.async_query()
        self.assertEqual(calls, [({"address": self.coordinator.address, "connectable": True},
                                 "active", const.CONNECT_TIMEOUT)])

    async def test_unchanged_advertisements_refresh_modern_ha_source(self):
        events = []
        callbacks = []
        def register(hass, callback, address):
            self.assertEqual(address, self.coordinator.address)
            callbacks.append(callback)
            events.append("register")
            return lambda: events.append("unregister")
        async def sweep(hass, duration):
            events.append("sweep")
            # Identical packets need not have changed service data.
            callbacks[0](types.SimpleNamespace(connectable=False))
            callbacks[0](types.SimpleNamespace(connectable=True))
        with patch.object(bluetooth, "async_register_advertisement_callback", register, create=True), \
             patch.object(bluetooth, "async_request_active_scan", sweep, create=True):
            await self.coordinator.async_load()
            self.assertEqual(events, [])
            await self.coordinator.async_query()
        self.assertEqual(events, ["register", "sweep", "unregister"])
        self.assertEqual(self.coordinator.data["last_result"], "valid_status_received")

    async def test_modern_sweep_timeout_cleans_up(self):
        events = []
        def register(*args):
            return lambda: events.append("unregister")
        async def sweep(*args):
            try:
                await asyncio.Future()
            finally:
                events.append("cancel_sweep")
        with patch.object(bluetooth, "async_register_advertisement_callback", register, create=True), \
             patch.object(bluetooth, "async_request_active_scan", sweep, create=True), \
             patch.object(coordinator_module, "CONNECT_TIMEOUT", 0.005):
            await self.coordinator.async_query()
        self.assertEqual(self.coordinator.data["last_result"], "not_discovered")
        self.assertEqual(FakeClient.instances, [])
        self.assertEqual(events, ["unregister", "cancel_sweep"])

    async def test_fresh_advertisement_timeout_never_connects(self):
        async def missing(*args):
            raise TimeoutError
        with patch.object(bluetooth, "async_process_advertisements", missing):
            await self.coordinator.async_query()
        self.assertEqual(self.coordinator.data["last_result"], "not_discovered")
        self.assertEqual(FakeClient.instances, [])

    async def test_write_response_property(self):
        FakeClient.props = ["notify", "write", "write-without-response"]
        await self.coordinator.async_query()
        self.assertEqual(FakeClient.instances[0].writes, [(const.QUERY, True)])

    async def test_only_fa_is_enough(self):
        FakeClient.packets = [FA]
        await self.coordinator.async_query()
        self.assertEqual(self.coordinator.data["last_result"], "valid_status_received")
        self.assertNotIn("occupancy", self.coordinator.data["fields"])

    async def test_timeout_keeps_old_cache_not_old_success(self):
        await self.coordinator.async_query()
        old_time = self.coordinator.data["last_status_time"]
        old_frames = self.coordinator.data["frames"].copy()
        FakeClient.packets = [const.QUERY, b"junk"]
        await self.coordinator.async_query()
        self.assertEqual(self.coordinator.data["last_result"], "no_valid_status")
        self.assertEqual(self.coordinator.data["last_status_time"], old_time)
        self.assertEqual(self.coordinator.data["frames"], old_frames)
        self.assertTrue(FakeClient.instances[-1].disconnected)

    async def test_bad_checksum_not_success(self):
        FakeClient.packets = [FA[:-2] + b"\x00\xfc"]
        await self.coordinator.async_query()
        self.assertEqual(self.coordinator.data["last_result"], "no_valid_status")
        self.assertIsNone(self.coordinator.last_status_time)

    async def test_missing_cached_ble_device_does_not_connect(self):
        self.device = None
        await self.coordinator.async_query()
        self.assertEqual(self.coordinator.data["last_result"], "not_discovered")
        self.assertEqual(FakeClient.instances, [])

    async def test_unsupported_properties_and_wrong_service(self):
        cases = [(True, const.SERVICE_UUID, ["notify", "write"], "missing_characteristic"),
                 (False, "wrong", ["notify", "write"], "missing_characteristic"),
                 (False, const.SERVICE_UUID, ["write"], "notify_not_supported"),
                 (False, const.SERVICE_UUID, ["notify", "read"], "write_not_supported")]
        for missing, service, props, result in cases:
            FakeClient.missing, FakeClient.service, FakeClient.props = missing, service, props
            await self.coordinator.async_query()
            self.assertEqual(self.coordinator.data["last_result"], result)
            self.assertEqual(FakeClient.instances[-1].writes, [])
            self.assertTrue(FakeClient.instances[-1].disconnected)

    async def test_connection_and_write_errors_cleanup(self):
        for fail, result in [("connect", "connection_error"), ("connect_timeout", "connection_timeout"),
                             ("subscribe", "connection_error"), ("write", "connection_error")]:
            FakeClient.fail = fail
            await self.coordinator.async_query()
            self.assertEqual(self.coordinator.data["last_result"], result)
            if fail in ("subscribe", "write"):
                self.assertTrue(FakeClient.instances[-1].disconnected)
            if fail == "write":
                self.assertTrue(FakeClient.instances[-1].stopped)

    async def test_stop_failure_still_disconnects(self):
        FakeClient.fail = "stop"
        await self.coordinator.async_query()
        self.assertTrue(FakeClient.instances[-1].disconnected)
        self.assertEqual(self.coordinator.data["last_result"], "valid_status_received")

    async def test_busy_and_cancellation_disconnect(self):
        FakeClient.gate = asyncio.Event()
        task = asyncio.create_task(self.coordinator.async_query())
        await asyncio.sleep(0)
        with self.assertRaises(HAError) as busy:
            await self.coordinator.async_query()
        self.assertEqual(busy.exception.translation_domain, const.DOMAIN)
        self.assertEqual(busy.exception.translation_key, "query_in_progress")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(FakeClient.instances), 1)
        self.assertTrue(FakeClient.instances[0].stopped)
        self.assertTrue(FakeClient.instances[0].disconnected)
        self.assertFalse(self.coordinator._lock.locked())

    async def test_warm_controls_reuse_one_connection_and_subscription(self):
        with patch.object(coordinator_module, "SESSION_IDLE_TIMEOUT", 60):
            await self.control_and_observe("atmosphere_on", True)
            client = FakeClient.instances[0]
            callback = client.cb
            self.assertFalse(client.disconnected)
            await self.control_and_observe("atmosphere_on", True)
            self.assertEqual(len(FakeClient.instances), 1)
            self.assertIs(client.cb, callback)
            self.assertTrue(self.coordinator.data["last_connection_reused"])
            self.assertTrue(self.coordinator.data["bluetooth_connected"])
            self.assertEqual(len(client.writes), 2)

    async def test_warm_write_waits_for_receive_quiet_window(self):
        with patch.object(coordinator_module, "SESSION_IDLE_TIMEOUT", 60), \
             patch.object(coordinator_module, "CONTROL_SETTLE", 0.02):
            await self.control_and_observe("atmosphere_on", True)
            await self.control_and_observe("atmosphere_on", True)
            self.assertGreaterEqual(self.coordinator.data["last_control_write_ms"], 15)
            self.assertEqual(len(FakeClient.instances), 1)

    async def test_persistent_mode_has_no_idle_disconnect_timer(self):
        self.entry.options = {"connection_mode": "persistent"}
        await self.control_and_observe("atmosphere_on", True)
        self.assertIsNone(self.coordinator._idle_handle)
        self.assertFalse(FakeClient.instances[0].disconnected)

    async def test_persistent_startup_warmup_only_sends_queries(self):
        self.entry.options = {"connection_mode": "persistent"}
        self.coordinator.async_start_warmup()
        await self.coordinator._warmup_task
        self.assertTrue(self.coordinator._session.live)
        self.assertTrue(all(p == const.QUERY for p, _ in FakeClient.instances[0].writes))
        await self.coordinator.async_disconnect()
        await asyncio.sleep(0.03)
        self.assertEqual(len(FakeClient.instances), 1)
        self.assertIsNone(self.coordinator._session)

    async def test_warmup_is_cancelled_and_cleaned_on_shutdown(self):
        self.entry.options = {"connection_mode": "persistent"}
        FakeClient.gate = asyncio.Event()
        self.coordinator.async_start_warmup()
        await asyncio.sleep(0)
        await self.coordinator.async_shutdown()
        self.assertIsNone(self.coordinator._warmup_task)
        self.assertIsNone(self.coordinator._session)
        self.assertTrue(FakeClient.instances[0].disconnected)

    async def test_option_change_can_release_persistent_connection(self):
        self.entry.options = {"connection_mode": "persistent"}
        await self.control_and_observe("atmosphere_on", True)
        self.entry.options = {"connection_mode": "disconnect"}
        await self.coordinator.async_apply_options()
        self.assertTrue(FakeClient.instances[0].disconnected)

    async def test_idle_mode_does_not_autowarm(self):
        self.entry.options = {"connection_mode": "idle"}
        self.coordinator.async_start_warmup()
        self.assertIsNone(self.coordinator._warmup_task)
        self.assertEqual(FakeClient.instances, [])

    async def test_idle_timeout_releases_session(self):
        with patch.object(coordinator_module, "SESSION_IDLE_TIMEOUT", 0.01):
            await self.control_and_observe("atmosphere_on", True)
            await asyncio.sleep(0.04)
        self.assertTrue(FakeClient.instances[0].disconnected)
        self.assertIsNone(self.coordinator._session)

    async def test_old_idle_generation_never_disconnects_new_operation(self):
        with patch.object(coordinator_module, "SESSION_IDLE_TIMEOUT", 60):
            await self.control_and_observe("atmosphere_on", True)
            old = self.coordinator._idle_generation
            await self.control_and_observe("atmosphere_on", True)
            await self.coordinator._idle_expired(old)
            self.assertFalse(FakeClient.instances[0].disconnected)

    async def test_release_button_path_disconnects_without_device_command(self):
        with patch.object(coordinator_module, "SESSION_IDLE_TIMEOUT", 60):
            await self.control_and_observe("atmosphere_on", True)
            client = FakeClient.instances[0]
            count = len(client.writes)
            await self.coordinator.async_disconnect()
            self.assertEqual(len(client.writes), count)
            self.assertTrue(client.disconnected)
            self.assertIsNone(self.coordinator._idle_handle)

    async def test_warm_ordinary_control_still_requires_new_fa(self):
        with patch.object(coordinator_module, "SESSION_IDLE_TIMEOUT", 60):
            await self.control_and_observe("atmosphere_on", True)
            client = FakeClient.instances[0]
            count = len([p for p, _ in client.writes if p != const.QUERY])
            FakeClient.packets = [E9, EA]
            with self.assertRaises(HAError):
                await self.control_and_observe("air_level", 1)
            self.assertEqual(len([p for p, _ in client.writes if p != const.QUERY]), count)

    async def test_live_notifications_update_cache_without_renewing_idle(self):
        with patch.object(coordinator_module, "SESSION_IDLE_TIMEOUT", 60):
            await self.control_and_observe("atmosphere_on", True)
            generation = self.coordinator._idle_generation
            payload = bytearray(FA[6:-2])
            payload[0] = (payload[0] & ~0x70) | (4 << 4)
            FakeClient.instances[0].cb(None, bytearray(frame(0xFA, payload)))
            self.assertEqual(self.coordinator.data["fields"]["water_level"], 4)
            self.assertEqual(self.coordinator._idle_generation, generation)

    async def test_explicit_disconnect_callback_rejects_late_packets(self):
        with patch.object(coordinator_module, "SESSION_IDLE_TIMEOUT", 60):
            await self.control_and_observe("atmosphere_on", True)
            client = FakeClient.instances[0]
            self.coordinator._on_disconnected(client)
            self.assertFalse(self.coordinator._session.active)
            self.assertEqual(self.coordinator._session.frames, {})
            before = dict(self.coordinator.data["fields"])
            client.cb(None, bytearray(FA))
            self.assertEqual(self.coordinator.data["fields"], before)

    async def test_failed_disconnect_prevents_second_connection(self):
        with patch.object(coordinator_module, "SESSION_IDLE_TIMEOUT", 60):
            await self.control_and_observe("atmosphere_on", True)
            client = FakeClient.instances[0]
            async def stuck(_client):
                raise BLEError("disconnect stuck")
            with patch.object(FakeClient, "disconnect", stuck):
                with self.assertRaises(BLEError):
                    await self.coordinator.async_disconnect()
                self.assertIs(self.coordinator._retired_client, client)
                with patch.object(coordinator_module._LOGGER, "warning"):
                    with self.assertRaises(HAError):
                        await self.control_and_observe("atmosphere_on", True)
                self.assertEqual(len(FakeClient.instances), 1)
            await self.coordinator.async_disconnect()

    async def test_ambiguous_actuator_write_is_not_reported_unsent(self):
        FakeClient.fail = "write"
        with patch.object(coordinator_module._LOGGER, "warning"):
            with self.assertRaises(HAError):
                await self.control_and_observe("atmosphere_on", False)
        self.assertTrue(self.coordinator.data["control_write_attempted"])
        self.assertFalse(self.coordinator.data["control_write_completed"])
        self.assertEqual(self.coordinator.data["last_control_result"], "sent_unconfirmed")
        self.assertEqual(len(FakeClient.instances[0].writes), 1)

    async def test_current_scanner_route_does_not_wait_for_duplicate_advertisement(self):
        with patch.object(bluetooth, "async_scanner_devices_by_address", lambda *a, **kw: [object()], create=True), \
             patch.object(self.coordinator, "_async_wait_for_advertisement", AsyncMock(side_effect=AssertionError("unnecessary scan"))):
            await self.control_and_observe("atmosphere_on", True)
        self.assertEqual(self.coordinator.data["last_control_result"], "confirmed")
        self.assertTrue(FakeClient.instances[0].disconnected)

    async def test_initial_fa_inside_subscription_is_not_dropped(self):
        FakeClient.initial_packets = [FA]
        FakeClient.packets = [E9, EA]
        await self.control_and_observe("air_level", 1)
        controls = [p for p, _ in FakeClient.instances[0].writes if p != const.QUERY]
        self.assertEqual(len(controls), 1)
        self.assertEqual(controls[0][5], 25)
        self.assertEqual(self.coordinator.data["last_control_result"], "sent_unconfirmed")

    async def test_short_control_does_not_require_fa_baseline(self):
        FakeClient.packets = [E9, EA]
        await self.control_and_observe("atmosphere_on", False)
        controls = [p for p, _ in FakeClient.instances[0].writes if p != const.QUERY]
        self.assertEqual(controls, [bytes.fromhex("f3f40692211f33101bfc")])
        self.assertEqual(self.coordinator.data["last_control_result"], "sent_unconfirmed")

    async def test_second_readonly_query_recovers_missing_fa_before_control(self):
        payload = bytearray(FA[6:-2])
        payload[2] = (payload[2] & ~7) | 1
        changed = frame(0xFA, payload)
        FakeClient.packet_script = [[E9, EA], [FA, E9, EA], [changed, E9, EA]]
        await self.control_and_observe("air_level", 1)
        writes = FakeClient.instances[0].writes
        self.assertEqual([p for p, _ in writes[:2]], [const.QUERY, const.QUERY])
        self.assertEqual(len([p for p, _ in writes if p != const.QUERY]), 1)
        self.assertEqual(self.coordinator.data["last_control_result"], "confirmed")

    async def test_readback_error_after_write_preserves_unknown_outcome(self):
        original = FakeClient.write_gatt_char
        async def broken_readback(client, char, data, *, response):
            if data == const.QUERY and len(client.writes) >= 1:
                raise BLEError("readback transport broke")
            await original(client, char, data, response=response)
        with patch.object(FakeClient, "write_gatt_char", broken_readback), \
             patch.object(coordinator_module._LOGGER, "warning"):
            await self.control_and_observe("atmosphere_on", False)
        self.assertTrue(self.coordinator.data["control_write_completed"])
        self.assertEqual(self.coordinator.data["last_control_result"], "sent_unconfirmed")
        self.assertEqual(len([p for p, _ in FakeClient.instances[0].writes if p != const.QUERY]), 1)

    async def test_notify_source_must_be_toilet(self):
        def spoof(packet):
            raw = bytearray(packet)
            raw[3] = 0x92
            raw[-2] = sum(raw[2:-2]) & 255
            return bytes(raw)
        FakeClient.packets = [spoof(FA), spoof(E9), spoof(EA)]
        await self.coordinator.async_query()
        self.assertEqual(self.coordinator.data["frames"], {})
        self.assertEqual(self.coordinator.data["last_result"], "no_valid_status")

    async def test_guard_is_rechecked_after_settle_before_actuation(self):
        payload = bytearray(E9[6:-2])
        payload[0] |= 0x80
        occupied = frame(0xE9, payload)
        original = FakeClient.write_gatt_char
        async def changing_occupancy(client, char, data, *, response):
            await original(client, char, data, response=response)
            if len(client.writes) == 1:
                asyncio.get_running_loop().call_soon(client.cb, char, bytearray(occupied))
        with patch.object(FakeClient, "write_gatt_char", changing_occupancy):
            with self.assertRaises(HAError) as error:
                await self.control_and_observe("lid_close")
        self.assertEqual(error.exception.translation_key, "control_requires_unoccupied")
        self.assertEqual(FakeClient.instances[0].writes, [(const.QUERY, False)])

    async def test_control_light_confirms_only_reported_state(self):
        await self.control_and_observe("atmosphere_on", True)
        client = FakeClient.instances[0]
        controls = [packet for packet, response in client.writes if packet != const.QUERY]
        self.assertEqual(controls, [bytes.fromhex("f3f40692211f33202bfc")])
        self.assertEqual(self.coordinator.data["last_control_result"], "confirmed")
        self.assertTrue(client.stopped and client.disconnected)

    async def test_mismatching_control_is_never_replayed_or_optimistic(self):
        await self.control_and_observe("atmosphere_on", False)
        writes = FakeClient.instances[0].writes
        controls = [packet for packet, _ in writes if packet != const.QUERY]
        self.assertEqual(controls, [bytes.fromhex("f3f40692211f33101bfc")])
        self.assertEqual(self.coordinator.data["last_control_result"], "readback_mismatch")
        self.assertTrue(self.coordinator.data["fields"]["atmosphere_on"])
        self.assertLessEqual(sum(packet == const.QUERY for packet, _ in writes), 3)
        self.assertGreaterEqual(sum(packet == const.QUERY for packet, _ in writes), 1)

    async def test_temperature_control_uses_fresh_baseline_and_real_readback(self):
        # Manual temperature is only accepted with seasonal auto temp off
        # (FA[11].bit3); the shipped fixture has it on, so clear that bit.
        base = bytearray(FA[6:-2])
        base[5] &= ~8
        manual_fa = frame(0xFA, base)
        payload = bytearray(base)
        payload[1] = (payload[1] & ~7) | 2
        changed = frame(0xFA, payload)
        FakeClient.packet_script = [[manual_fa, E9, EA], [changed, E9, EA]]
        await self.control_and_observe("seat_level", 2)
        controls = [p for p, _ in FakeClient.instances[0].writes if p != const.QUERY]
        self.assertEqual(len(controls), 1)
        self.assertEqual((len(controls[0]), controls[0][5], controls[0][7] & 7), (19, 25, 2))
        self.assertTrue(protocol.valid_frame(controls[0]))
        self.assertEqual(self.coordinator.data["fields"]["seat_level"], 2)
        self.assertEqual(self.coordinator.data["fields"]["water_level"], 3)
        self.assertEqual(self.coordinator.data["last_control_result"], "confirmed")

    async def test_manual_temperature_blocked_while_seasonal_auto_is_on(self):
        self.assertTrue(self.coordinator.data.get("fields", {}).get("auto_temp") in (None, True))
        for name, value in (("water_level", 1), ("seat_level", 5)):
            with self.subTest(name=name):
                with self.assertRaises(HAError) as error:
                    await self.coordinator.async_control(name, value)
                self.assertEqual(error.exception.translation_key,
                                 "control_requires_manual_temp")
        client = FakeClient.instances[0]
        self.assertEqual([p for p, _ in client.writes if p != const.QUERY], [])
        self.assertEqual(self.coordinator.data["last_control_result"], "failed")

    async def test_auto_temp_toggle_uses_command_two_with_readback(self):
        base = bytearray(FA[6:-2])
        base[5] &= ~8
        auto_off = frame(0xFA, base)
        payload = bytearray(base)
        payload[5] |= 8
        auto_on = frame(0xFA, payload)
        FakeClient.packet_script = [[auto_off, E9, EA], [auto_on, E9, EA]]
        await self.control_and_observe("auto_temp", True)
        controls = [p for p, _ in FakeClient.instances[0].writes if p != const.QUERY]
        self.assertEqual(len(controls), 1)
        self.assertEqual((len(controls[0]), controls[0][5]), (19, 2))
        self.assertTrue(protocol.valid_frame(controls[0]))
        self.assertEqual(self.coordinator.data["last_control_result"], "confirmed")
        self.assertTrue(self.coordinator.data["fields"]["auto_temp"])

    async def test_wash_requires_fresh_occupied_status(self):
        with self.assertRaises(HAError) as error:
            await self.control_and_observe("wash_hip")
        self.assertEqual(error.exception.translation_key, "control_requires_occupied")
        self.assertEqual(FakeClient.instances[0].writes, [(const.QUERY, False)])
        self.assertEqual(self.coordinator.data["last_control_result"], "failed")
        self.assertTrue(FakeClient.instances[0].disconnected)

    async def test_control_missing_baseline_never_uses_saved_frames(self):
        self.coordinator.data["frames"] = {"FA": FA.hex(), "E9": E9.hex(), "EA": EA.hex()}
        self.coordinator.data["fields"] = {"occupancy": False, "working": False}
        FakeClient.packets = []
        with self.assertRaises(HAError) as error:
            await self.control_and_observe("seat_level", 2)
        self.assertEqual(error.exception.translation_key, "control_state_missing")
        self.assertEqual(FakeClient.instances[0].writes, [(const.QUERY, False)] * 3)

    async def test_lid_control_rejects_motion(self):
        payload = bytearray(EA[6:-2])
        payload[0] |= 0x40
        FakeClient.packets = [FA, E9, frame(0xEA, payload)]
        with self.assertRaises(HAError) as error:
            await self.control_and_observe("lid_close")
        self.assertEqual(error.exception.translation_key, "control_requires_unoccupied")
        self.assertEqual(FakeClient.instances[0].writes, [(const.QUERY, False)])

    async def test_level_setting_rejects_active_mode(self):
        payload = bytearray(E9[6:-2])
        payload[3] = 0x20
        FakeClient.packets = [FA, frame(0xE9, payload), EA]
        with self.assertRaises(HAError) as error:
            await self.control_and_observe("water_level", 2)
        self.assertEqual(error.exception.translation_key, "control_requires_idle")
        self.assertEqual(FakeClient.instances[0].writes, [(const.QUERY, False)])

    async def test_unknown_control_never_connects(self):
        with self.assertRaises(HAError):
            await self.control_and_observe("raw", b"arbitrary")
        self.assertEqual(FakeClient.instances, [])

    async def test_cancelled_control_cleans_up_and_shares_query_lock(self):
        FakeClient.gate = asyncio.Event()
        task = asyncio.create_task(self.control_and_observe("stop"))
        await asyncio.sleep(0)
        with self.assertRaises(HAError):
            await self.coordinator.async_query()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(FakeClient.instances[0].stopped and FakeClient.instances[0].disconnected)
        self.assertEqual(self.coordinator.data["last_control_result"], "failed")
        self.assertFalse(self.coordinator._lock.locked())

    async def test_restore_cache_without_connection(self):
        Store.persisted = {"frames": {"FA": FA.hex().upper()}, "last_result": "no_valid_status",
                           "last_status_time": "2026-01-01T00:00:00+00:00"}
        await self.coordinator.async_load()
        self.assertEqual(self.coordinator.data["frames"]["FA"], FA.hex().upper())
        self.assertIsNotNone(self.coordinator.last_status_time)
        self.assertEqual(FakeClient.instances, [])

    async def test_long_raw_frame_is_capped(self):
        FakeClient.packets = [frame(0xFA, b"\x00" * 40)]
        await self.coordinator.async_query()
        self.assertEqual(len(self.coordinator.data["frames"]["FA"]), 42)


class PackagingTests(unittest.TestCase):
    def test_manifest_and_translation_keys(self):
        manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["requirements"], [])
        self.assertIn(manifest["iot_class"], {"assumed_state", "cloud_polling", "cloud_push", "local_polling", "local_push", "calculated"})
        self.assertIn("bluetooth", manifest["dependencies"])
        self.assertEqual(manifest["bluetooth"], [{"local_name": const.LOCAL_NAME, "connectable": True}])
        english = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
        def keys(obj, prefix=""):
            return {prefix + key for key in obj} | set().union(*(keys(value, prefix + key + ".") for key, value in obj.items() if isinstance(value, dict)))
        for language in ("en", "zh-Hans"):
            translated = json.loads((COMPONENT / "translations" / f"{language}.json").read_text(encoding="utf-8"))
            self.assertEqual(keys(english), keys(translated))

    def test_query_result_translations_cover_all_machine_states(self):
        for name in ("en", "zh-Hans"):
            data = json.loads((COMPONENT / "translations" / f"{name}.json").read_text(encoding="utf-8"))
            states = data["entity"]["sensor"]["last_query_result"]["state"]
            self.assertEqual(set(states), set(const.QUERY_RESULTS))
            if name == "zh-Hans":
                self.assertEqual(states["valid_status_received"], "查询成功")
                self.assertNotIn("源码推导", data["entity"]["sensor"]["seat_level"]["name"])

    def test_no_scanner_or_periodic_interval(self):
        for source in COMPONENT.glob("*.py"):
            text = source.read_text(encoding="utf-8")
            self.assertNotIn("BleakScanner", text)
            self.assertNotIn("update_interval=timedelta", text)


if __name__ == "__main__":
    unittest.main()
