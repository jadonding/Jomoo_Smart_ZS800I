"""Offline unit/mocked integration tests. No HA, Bleak, BLE or network needed."""
import asyncio
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

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
    pass


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
                         {"water_level": 3, "seat_level": 3, "air_level": 2, "atmosphere_on": True})
        self.assertEqual(protocol.decode_source_fields(E9), {"occupancy": False})
        self.assertEqual(protocol.decode_source_fields(EA), {"ring_open": False, "lid_open": False})

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

    async def start_notify(self, char, cb):
        self.cb = cb
        if self.fail == "subscribe":
            raise BLEError("subscribe error")

    async def stop_notify(self, char):
        self.stopped = True
        if self.fail == "stop":
            raise BLEError("stop error")

    async def write_gatt_char(self, char, data, *, response):
        self.writes.append((data, response))
        if self.gate is not None:
            await self.gate.wait()
        if self.fail == "write":
            raise BLEError("write error")
        for packet in self.packets:
            self.cb(char, bytearray(packet))


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        FakeClient.props = ["notify", "read", "write-without-response"]
        FakeClient.packets = [FA[:7], FA[7:] + E9 + EA]
        FakeClient.fail = None
        FakeClient.gate = None
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
                        patch.object(coordinator_module, "NOTIFY_TIMEOUT", 0.025)]
        for item in self.patches:
            item.start()
        self.entry = types.SimpleNamespace(data={"address": "AA:BB:CC:DD:EE:FF"}, entry_id="test")
        self.coordinator = coordinator_module.JomooCoordinator(object(), self.entry)

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
            self.assertFalse(callback(types.SimpleNamespace(time=0)))
        with patch.object(bluetooth, "async_process_advertisements", receive):
            await self.coordinator.async_load()
            self.assertEqual(calls, [])
            await self.coordinator.async_query()
        self.assertEqual(calls, [({"address": self.coordinator.address, "connectable": True},
                                 "active", const.CONNECT_TIMEOUT)])

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
        with self.assertRaises(HAError):
            await self.coordinator.async_query()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(FakeClient.instances), 1)
        self.assertTrue(FakeClient.instances[0].stopped)
        self.assertTrue(FakeClient.instances[0].disconnected)
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
