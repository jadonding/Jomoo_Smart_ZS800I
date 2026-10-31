"""Offline session tests: no HA imports, BLE access or network operations."""
import asyncio
import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest.mock import patch

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "jomoo_zs800i"
PACKAGE = "_jomoo_session_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(COMPONENT)]
sys.modules[PACKAGE] = package
spec = importlib.util.spec_from_file_location(f"{PACKAGE}.session", COMPONENT / "session.py")
session_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = session_module
spec.loader.exec_module(session_module)
BLESession = session_module.BLESession

FA = bytes.fromhex("f3f40c2100fa3e53b235333b292359fc")
E9 = bytes.fromhex("f3f40a2122e95700000100008efc")
EA = bytes.fromhex("f3f4112122ea05008500804c13005f0000000006fc")


class FakeClient:
    def __init__(self):
        self.is_connected = True
        self.initial = []
        self.start_count = 0
        self.stop_count = 0
        self.disconnect_count = 0
        self.stop_error = None
        self.disconnect_error = None
        self.stop_gate = None

    async def start_notify(self, char, callback):
        self.start_count += 1
        self.callback = callback
        for frame in self.initial:
            self.emit(frame)

    def emit(self, data):
        self.callback("char", bytearray(data))

    async def stop_notify(self, char):
        self.stop_count += 1
        if self.stop_gate is not None:
            await self.stop_gate.wait()
        if self.stop_error:
            raise self.stop_error

    async def disconnect(self):
        self.disconnect_count += 1
        self.is_connected = False
        if self.disconnect_error:
            raise self.disconnect_error


class SessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = FakeClient()
        self.seen = []
        self.session = BLESession(self.client, "char", self.seen.append)

    async def test_initial_fa_and_pre_start_observer(self):
        observed = []
        self.client.initial = [FA]
        self.session.add_observer(observed.append)
        await self.session.start()
        self.assertEqual(self.seen, [FA])
        self.assertEqual(observed, [FA])
        self.assertEqual(self.session.frames, {"FA": FA})
        self.assertGreater(self.session.frame_times["FA"], 0)
        self.assertTrue(self.session.live)

    async def test_unsubscribe_is_idempotent(self):
        observed = []
        unsubscribe = self.session.add_observer(observed.append)
        await self.session.start()
        unsubscribe()
        unsubscribe()
        self.client.emit(FA)
        self.assertEqual(observed, [])
        self.assertEqual(self.seen, [FA])

    async def test_wrong_source_and_bad_checksum_are_ignored(self):
        await self.session.start()
        wrong_source = bytearray(FA)
        wrong_source[3] = 0x92
        wrong_source[-2] = sum(wrong_source[2:-2]) & 255
        broken = bytearray(FA)
        broken[-2] ^= 1
        self.client.emit(wrong_source)
        self.client.emit(broken)
        self.assertEqual(self.session.frames, {})
        self.assertEqual(self.seen, [])

    async def test_split_and_coalesced_frames(self):
        await self.session.start()
        self.client.emit(FA[:5])
        self.assertEqual(self.seen, [])
        self.client.emit(FA[5:] + E9 + EA)
        self.assertEqual(self.seen, [FA, E9, EA])
        self.assertEqual(set(self.session.frames), {"FA", "E9", "EA"})

    async def test_close_clears_cache_observers_and_is_idempotent(self):
        self.session.add_observer(self.seen.append)
        await self.session.start()
        self.client.emit(FA)
        await self.session.close()
        await self.session.close()
        self.assertFalse(self.session.active)
        self.assertEqual(self.session.frames, {})
        self.assertEqual(self.session.frame_times, {})
        self.assertEqual(self.session._observers, [])
        self.assertEqual(self.client.stop_count, 1)
        self.assertEqual(self.client.disconnect_count, 1)

    async def test_stop_error_still_disconnects(self):
        await self.session.start()
        self.client.stop_error = RuntimeError("stop failure")
        await self.session.close()
        self.assertEqual(self.client.disconnect_count, 1)
        self.assertFalse(self.session.live)

    async def test_unexpected_disconnect_invalidates_live_state(self):
        await self.session.start()
        self.client.emit(FA)
        self.client.is_connected = False
        self.assertFalse(self.session.live)
        self.assertEqual(self.session.frames, {})
        self.assertEqual(self.session.frame_times, {})

    async def test_late_notifications_are_dropped(self):
        await self.session.start()
        await self.session.close()
        self.client.emit(FA)
        self.assertEqual(self.seen, [])
        self.assertEqual(self.session.frames, {})

    async def test_cancelled_stop_still_disconnects_and_propagates(self):
        await self.session.start()
        self.client.stop_gate = asyncio.Event()
        task = asyncio.create_task(self.session.close())
        await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.client.disconnect_count, 1)
        self.assertFalse(self.session.active)

    async def test_stop_timeout_still_disconnects(self):
        await self.session.start()
        self.client.stop_gate = asyncio.Event()
        with patch.object(session_module, "CLOSE_TIMEOUT", 0.01):
            await self.session.close()
        self.assertEqual(self.client.disconnect_count, 1)

    async def test_start_once_and_closed_session_cannot_restart(self):
        await self.session.start()
        await self.session.start()
        self.assertEqual(self.client.start_count, 1)
        await self.session.close()
        with self.assertRaises(RuntimeError):
            await self.session.start()
        with self.assertRaises(RuntimeError):
            self.session.add_observer(self.seen.append)

    async def test_callback_failure_does_not_starve_observers(self):
        def broken(_frame):
            raise ValueError("consumer failed")
        self.session.on_frame = broken
        self.session.add_observer(self.seen.append)
        await self.session.start()
        with self.assertLogs(session_module.__name__, level="ERROR"):
            self.client.emit(FA)
        self.assertEqual(self.seen, [FA])

    async def test_primary_callback_precedes_observers(self):
        calls = []
        self.session.on_frame = lambda frame: calls.append("primary")
        self.session.add_observer(lambda frame: calls.append("observer"))
        await self.session.start()
        self.client.emit(FA)
        self.assertEqual(calls, ["primary", "observer"])


if __name__ == "__main__":
    unittest.main()
