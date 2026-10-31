"""Offline pure-protocol tests; imports neither HA nor BLE, opens no network."""
import importlib.util
from pathlib import Path
import sys
import types
import unittest

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "jomoo_zs800i"
PACKAGE = "_jomoo_control_tests"
package = types.ModuleType(PACKAGE)
package.__path__ = [str(COMPONENT)]
sys.modules[PACKAGE] = package


def load(name):
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", COMPONENT / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


protocol = load("protocol")
control = load("control")
FA = bytes.fromhex("F3 F4 0C 21 00 FA 3E 53 B2 35 33 3B 29 23 59 FC")


def changed(**offsets):
    frame = bytearray(FA)
    for key, value in offsets.items():
        frame[int(key[1:])] = value
    frame[-2] = sum(frame[2:-2]) & 255
    return bytes(frame)


class ControlTests(unittest.TestCase):
    def build(self, name, value=None, fa=FA, fields=None):
        return control.build_control(name, value, fa, fields)

    def test_exact_sample_and_checksum(self):
        actual = self.build("water_level", 3)
        self.assertEqual(actual, bytes.fromhex("F3 F4 0F 92 21 19 3E 53 B2 30 00 30 00 00 00 00 00 7E FC"))
        self.assertTrue(protocol.valid_frame(actual))

    def test_reserved_bytes_are_source_zero_not_fa_copy(self):
        frame = self.build("stop", fa=changed(b6=0xBE, b7=0x5B, b8=0xBA))
        self.assertEqual(frame[6:9], bytes.fromhex("3E 53 B2"))
        self.assertEqual(frame[10], 0)
        self.assertEqual(frame[12:17], b"\0" * 5)

    def test_female_mapping_differs_from_hip(self):
        fa = changed(b9=0x42)
        woman = self.build("wash_woman", fa=fa)
        hip = self.build("wash_hip", fa=fa)
        self.assertEqual(woman[7:9], bytes.fromhex("23 C2"))
        self.assertEqual(hip[7:9], bytes.fromhex("53 B2"))
        self.assertEqual(woman[5], 11)

    def test_auto_female_mapping(self):
        frame = self.build("wash_auto_woman", fa=changed(b9=0x42))
        self.assertEqual(frame[5:9], bytes.fromhex("07 3E 23 C2"))

    def test_setting_uses_latest_female_mode(self):
        frame = self.build("seat_level", 4, changed(b9=0x42), {"is_woman": True})
        self.assertEqual(frame[7:9], bytes.fromhex("24 C2"))

    def test_hip_action_overrides_previous_female(self):
        frame = self.build("wash_hip", fields={"is_woman": True})
        self.assertEqual(frame[7:9], bytes.fromhex("53 B2"))

    def test_lid_open_preserves_ring_choice(self):
        self.assertEqual(self.build("lid_open", fields={"ring_open": False})[5], 17)
        self.assertEqual(self.build("lid_open", fields={"ring_open": True})[5], 19)

    def test_lid_open_requires_ea_ring(self):
        with self.assertRaises(ValueError):
            self.build("lid_open")

    def test_ring_close_opens_lid(self):
        self.assertEqual(self.build("ring_close")[5], 17)
        self.assertEqual(control.expected_after("ring_close"), {"lid_open": True, "ring_open": False})

    def test_temperature_masks(self):
        water = self.build("water_level", 5)
        seat = self.build("seat_level", 0)
        air = self.build("air_level", 3)
        self.assertEqual(water[6], 0x5E)
        self.assertEqual(seat[7], 0x50)
        self.assertEqual(air[8], 0xB3)

    def test_invalid_temperatures(self):
        for name, value in (("water_level", 6), ("seat_level", -1), ("air_level", 4),
                            ("water_level", True), ("seat_level", 1.0)):
            with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                self.build(name, value)

    def test_missing_fa(self):
        with self.assertRaises(ValueError):
            self.build("stop", fa=None)

    def test_bad_checksum(self):
        with self.assertRaises(ValueError):
            self.build("stop", fa=FA[:-2] + b"\x00\xfc")

    def test_wrong_source_or_command(self):
        for fa in (changed(b3=0x92), changed(b5=0xE9), changed(b5=0xEA)):
            with self.subTest(fa=fa), self.assertRaises(ValueError):
                self.build("stop", fa=fa)

    def test_short_valid_fa_rejected(self):
        frame = bytearray.fromhex("F3 F4 07 21 00 FA 3E 53 B2")
        frame.extend((sum(frame[2:]) & 255, 0xFC))
        self.assertTrue(protocol.valid_frame(bytes(frame)))
        with self.assertRaises(ValueError):
            self.build("stop", fa=bytes(frame))

    def test_invalid_baseline_ranges(self):
        for fa in (changed(b6=0x6E), changed(b7=0x56), changed(b8=0xB4),
                   changed(b7=0x03), changed(b8=0xE2), changed(b13=0x20)):
            with self.subTest(fa=fa), self.assertRaises(ValueError):
                self.build("stop", fa=fa)

    def test_invalid_female_values(self):
        for val in (0x36, 0x63, 0x30, 0x03, 0xF3):
            with self.subTest(val=val), self.assertRaises(ValueError):
                self.build("wash_woman", fa=changed(b9=val))

    def test_short_frame_exact_no_fa(self):
        frame = self.build("atmosphere_on", False, fa=None)
        self.assertEqual(frame, bytes.fromhex("F3 F4 06 92 21 1F 33 10 1B FC"))
        self.assertTrue(protocol.valid_frame(frame))

    def test_every_short_switch_round_trip_expectation(self):
        for name, (sub, on, off) in control.SHORT_SWITCHES.items():
            for value, encoded in ((True, on), (False, off)):
                with self.subTest(name=name, value=value):
                    frame = self.build(name, value, fa=None)
                    self.assertTrue(protocol.valid_frame(frame))
                    self.assertEqual(frame[5:8], bytes((0x1F, sub, encoded)))
                    self.assertEqual(control.expected_after(name, value), {name: value})

    def test_no_arbitrary_raw_or_unsupported_model_control(self):
        for name in ("raw", "25", "open_loop_bubble", "windspeed", "brightness", "sitz", "hibernate"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.build(name)

    def test_actions_are_not_value_oriented(self):
        with self.assertRaises(ValueError):
            self.build("stop", True)
        with self.assertRaises(ValueError):
            self.build("atmosphere_on", 1)

    def test_every_action_frames_valid(self):
        for name, code in control.ACTIONS.items():
            with self.subTest(name=name):
                frame = self.build(name, fields={"ring_open": False})
                self.assertEqual(frame[5], code)
                self.assertEqual(len(frame), 19)
                self.assertTrue(protocol.valid_frame(frame))

    def test_expected_completion_is_not_transport_success(self):
        self.assertEqual(control.expected_after("wash_hip"), {})
        self.assertEqual(control.expected_after("stop"), {})
        self.assertEqual(control.expected_after("air_level", 2), {"air_level": 2})
        self.assertEqual(control.expected_after("lid_close"), {"lid_open": False, "ring_open": False})
        self.assertEqual(control.expected_after("lid_open", fields={"ring_open": True}),
                         {"lid_open": True, "ring_open": True})


if __name__ == "__main__":
    unittest.main()
