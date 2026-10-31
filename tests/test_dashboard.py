"""Layout regression tests; JSON supplies the YAML roundtrip without extra deps."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

BUILDER = Path(__file__).resolve().parents[1] / 'build_dashboard.py'
KEYS = {
    'button': ('stop','refresh_status','wash_hip','wash_woman','dry','flush_large',
               'wash_auto_hip','wash_auto_woman','flush_small','lid_open','lid_close',
               'ring_open','ring_close','disconnect_bluetooth'),
    'number': ('set_water_level','set_seat_level','set_air_level'),
    'switch': ('control_atmosphere_on','control_auto_temp','control_foot_sensor','control_prewet',
               'control_auto_bubble','control_uv_water','control_close_flush','control_auto_small_flush'),
    'sensor': ('last_query_result','last_control_result','last_status_time'),
    'binary_sensor': ('occupancy','lid_open','ring_open'),
}

class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / '.storage').mkdir()
        self.output = self.root / 'staged.yaml'
        self.entities = self.make_entities('entry-a', 'a')

    def make_entities(self, entry, suffix):
        return [{'entity_id': f'{domain}.toilet_{suffix}_{key}',
                 'unique_id': f'00:11:22:33:44:{suffix}_{key}',
                 'config_entry_id': entry, 'platform': 'jomoo_zs800i', 'disabled_by': None}
                for domain, keys in KEYS.items() for key in keys]

    def build(self, *extra):
        (self.root / '.storage/core.entity_registry').write_text(json.dumps({'data': {'entities': self.entities}}))
        yaml = types.ModuleType('yaml')
        yaml.safe_dump = lambda value, **kwargs: json.dumps(value, ensure_ascii=False)
        yaml.safe_load = json.loads
        argv = [str(BUILDER), '--config', str(self.root), '--output', str(self.output), *extra]
        with patch.object(sys, 'argv', argv), patch.dict(sys.modules, {'yaml': yaml}), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            runpy.run_path(str(BUILDER), run_name='__main__')
        return json.loads(self.output.read_text(encoding='utf-8'))

    def test_beautified_sections_controls_and_safe_taps_are_preserved(self):
        data = self.build()
        self.assertEqual(len(data['views']), 2)
        home = data['views'][0]
        self.assertEqual(home['type'], 'sections')
        self.assertEqual(home['max_columns'], 2)
        self.assertEqual([s['cards'][0]['heading'] for s in home['sections']],
                         ['日常使用','舒适温度','盖板与座圈','自动功能'])
        cards = [c for s in home['sections'] for c in s['cards']]
        buttons = [c for c in cards if c.get('entity','').startswith('button.')]
        self.assertEqual(len(buttons), 14)
        self.assertTrue(any(c['entity'].endswith('_disconnect_bluetooth') for c in buttons))
        for card in buttons:
            self.assertEqual(card['tap_action'], card['icon_tap_action'])
            self.assertEqual(card['double_tap_action'], {'action':'none'})
            self.assertEqual(card['hold_action'], {'action':'none'})
        features = [f['type'] for c in cards for f in c.get('features', [])]
        self.assertEqual(features.count('numeric-input'), 3)
        self.assertEqual(features.count('toggle'), 8)

    def test_staging_does_not_overwrite_existing_live_panel(self):
        live = self.root / 'dashboard-jomoo.yaml'
        live.write_text('existing user layout')
        self.build()
        self.assertEqual(live.read_text(), 'existing user layout')

    def test_multiple_devices_require_explicit_entry(self):
        self.entities += self.make_entities('entry-b','b')
        with self.assertRaises(SystemExit):
            self.build()
        self.assertFalse(self.output.exists())
        data = self.build('--entry-id', 'entry-b')
        self.assertIn('toilet_b_', json.dumps(data))
        self.assertNotIn('toilet_a_', json.dumps(data))

    def test_disabled_control_is_not_silently_published(self):
        self.entities[0]['disabled_by'] = 'user'
        with self.assertRaises(AssertionError):
            self.build()
        self.assertFalse(self.output.exists())
