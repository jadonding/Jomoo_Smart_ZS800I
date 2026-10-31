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

async def establish_connection(client_class, device, name, *, max_attempts, timeout):
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
                          "uv_water": True, "auto_bubble": True, "prewet": True,
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
                        patch.object(coordinator_module, "CONTROL_READBACK_TIMEOUT", 0.005)]
        for item in self.patches:
            item.start()
        self.entry = types.SimpleNamespace(data={"address": "AA:BB:CC:DD:EE:FF"}, entry_id="test")
        self.hass = types.SimpleNamespace(async_create_task=asyncio.create_task)
        self.coordinator = coordinator_module.JomooCoordinator(self.hass, self.entry)

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()

    async def test_startup_never_connects(self):
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

    async def test_current_scanner_route_does_not_wait_for_duplicate_advertisement(self):
        with patch.object(bluetooth, "async_scanner_devices_by_address", lambda *a, **kw: [object()], create=True), \
             patch.object(self.coordinator, "_async_wait_for_advertisement", AsyncMock(side_effect=AssertionError("unnecessary scan"))):
            await self.coordinator.async_control("atmosphere_on", True)
        self.assertEqual(self.coordinator.data["last_control_result"], "confirmed")
        self.assertTrue(FakeClient.instances[0].disconnected)

    async def test_initial_fa_inside_subscription_is_not_dropped(self):
        FakeClient.initial_packets = [FA]
        FakeClient.packets = [E9, EA]
        await self.coordinator.async_control("air_level", 1)
        controls = [p for p, _ in FakeClient.instances[0].writes if p != const.QUERY]
        self.assertEqual(len(controls), 1)
        self.assertEqual(controls[0][5], 25)
        self.assertEqual(self.coordinator.data["last_control_result"], "sent_unconfirmed")

    async def test_short_control_does_not_require_fa_baseline(self):
        FakeClient.packets = [E9, EA]
        await self.coordinator.async_control("atmosphere_on", False)
        controls = [p for p, _ in FakeClient.instances[0].writes if p != const.QUERY]
        self.assertEqual(controls, [bytes.fromhex("f3f40692211f33101bfc")])
        self.assertEqual(self.coordinator.data["last_control_result"], "sent_unconfirmed")

    async def test_second_readonly_query_recovers_missing_fa_before_control(self):
        payload = bytearray(FA[6:-2])
        payload[2] = (payload[2] & ~7) | 1
        changed = frame(0xFA, payload)
        FakeClient.packet_script = [[E9, EA], [FA, E9, EA], [changed, E9, EA]]
        await self.coordinator.async_control("air_level", 1)
        writes = FakeClient.instances[0].writes
        self.assertEqual([p for p, _ in writes[:2]], [const.QUERY, const.QUERY])
        self.assertEqual(len([p for p, _ in writes if p != const.QUERY]), 1)
        self.assertEqual(self.coordinator.data["last_control_result"], "confirmed")

    async def test_readback_error_after_write_preserves_unknown_outcome(self):
        original = FakeClient.write_gatt_char
        async def broken_readback(client, char, data, *, response):
            if data == const.QUERY and len(client.writes) >= 2:
                raise BLEError("readback transport broke")
            await original(client, char, data, response=response)
        with patch.object(FakeClient, "write_gatt_char", broken_readback), \
             patch.object(coordinator_module._LOGGER, "warning"):
            with self.assertRaises(HAError):
                await self.coordinator.async_control("atmosphere_on", False)
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
                await self.coordinator.async_control("lid_close")
        self.assertEqual(error.exception.translation_key, "control_requires_unoccupied")
        self.assertEqual(FakeClient.instances[0].writes, [(const.QUERY, False)])

    async def test_control_light_confirms_only_reported_state(self):
        await self.coordinator.async_control("atmosphere_on", True)
        client = FakeClient.instances[0]
        controls = [packet for packet, response in client.writes if packet != const.QUERY]
        self.assertEqual(controls, [bytes.fromhex("f3f40692211f33202bfc")])
        self.assertEqual(self.coordinator.data["last_control_result"], "confirmed")
        self.assertTrue(client.stopped and client.disconnected)

    async def test_mismatching_control_is_never_replayed_or_optimistic(self):
        await self.coordinator.async_control("atmosphere_on", False)
        writes = FakeClient.instances[0].writes
        controls = [packet for packet, _ in writes if packet != const.QUERY]
        self.assertEqual(controls, [bytes.fromhex("f3f40692211f33101bfc")])
        self.assertEqual(self.coordinator.data["last_control_result"], "readback_mismatch")
        self.assertTrue(self.coordinator.data["fields"]["atmosphere_on"])
        self.assertEqual(sum(packet == const.QUERY for packet, _ in writes), 4)

    async def test_temperature_control_uses_fresh_baseline_and_real_readback(self):
        payload = bytearray(FA[6:-2])
        payload[1] = (payload[1] & ~7) | 2
        changed = frame(0xFA, payload)
        FakeClient.packet_script = [[FA, E9, EA], [changed, E9, EA]]
        await self.coordinator.async_control("seat_level", 2)
        controls = [p for p, _ in FakeClient.instances[0].writes if p != const.QUERY]
        self.assertEqual(len(controls), 1)
        self.assertEqual((len(controls[0]), controls[0][5], controls[0][7] & 7), (19, 25, 2))
        self.assertTrue(protocol.valid_frame(controls[0]))
        self.assertEqual(self.coordinator.data["fields"]["seat_level"], 2)
        self.assertEqual(self.coordinator.data["fields"]["water_level"], 3)
        self.assertEqual(self.coordinator.data["last_control_result"], "confirmed")

    async def test_wash_requires_fresh_occupied_status(self):
        with self.assertRaises(HAError) as error:
            await self.coordinator.async_control("wash_hip")
        self.assertEqual(error.exception.translation_key, "control_requires_occupied")
        self.assertEqual(FakeClient.instances[0].writes, [(const.QUERY, False)])
        self.assertEqual(self.coordinator.data["last_control_result"], "failed")
        self.assertTrue(FakeClient.instances[0].disconnected)

    async def test_control_missing_baseline_never_uses_saved_frames(self):
        self.coordinator.data["frames"] = {"FA": FA.hex(), "E9": E9.hex(), "EA": EA.hex()}
        self.coordinator.data["fields"] = {"occupancy": False, "working": False}
        FakeClient.packets = []
        with self.assertRaises(HAError) as error:
            await self.coordinator.async_control("seat_level", 2)
        self.assertEqual(error.exception.translation_key, "control_state_missing")
        self.assertEqual(FakeClient.instances[0].writes, [(const.QUERY, False)] * 3)

    async def test_lid_control_rejects_motion(self):
        payload = bytearray(EA[6:-2])
        payload[0] |= 0x40
        FakeClient.packets = [FA, E9, frame(0xEA, payload)]
        with self.assertRaises(HAError) as error:
            await self.coordinator.async_control("lid_close")
        self.assertEqual(error.exception.translation_key, "control_requires_unoccupied")
        self.assertEqual(FakeClient.instances[0].writes, [(const.QUERY, False)])

    async def test_level_setting_rejects_active_mode(self):
        payload = bytearray(E9[6:-2])
        payload[3] = 0x20
        FakeClient.packets = [FA, frame(0xE9, payload), EA]
        with self.assertRaises(HAError) as error:
            await self.coordinator.async_control("water_level", 2)
        self.assertEqual(error.exception.translation_key, "control_requires_idle")
        self.assertEqual(FakeClient.instances[0].writes, [(const.QUERY, False)])

    async def test_unknown_control_never_connects(self):
        with self.assertRaises(HAError):
            await self.coordinator.async_control("raw", b"arbitrary")
        self.assertEqual(FakeClient.instances, [])

    async def test_cancelled_control_cleans_up_and_shares_query_lock(self):
        FakeClient.gate = asyncio.Event()
        task = asyncio.create_task(self.coordinator.async_control("stop"))
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
