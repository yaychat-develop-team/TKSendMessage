"""Initialization producers are tested separately from the signer consumer."""
import importlib
import json
from pathlib import Path
import unittest


FIXTURE = json.loads((Path(__file__).parent / 'fixtures/wss/initialization_dom_vectors.json').read_text(encoding='utf-8'))


class FrontierInitializationTests(unittest.TestCase):
    def api(self):
        try:
            return importlib.import_module('tiktok_live.algorithms.frontier_environment')
        except ModuleNotFoundError:
            self.fail('Python SDK environment-state producer is missing')

    def test_dom_probe_matches_original_sdk_branch_vectors(self):
        api = self.api()
        for case in FIXTURE['cases']:
            snapshot = dict(FIXTURE['normal_snapshot'], **case['overrides'])
            with self.subTest(profile=case['profile']):
                self.assertEqual(api.dom_environment_is_suspicious(FIXTURE['user_agent'], snapshot), case['expected_suspicious'])
                self.assertEqual(bool(case['sdk_env_code'] & 128), case['expected_suspicious'])

    def test_absent_raw_facts_do_not_turn_into_assumed_browser_state(self):
        api = self.api()
        with self.assertRaises(ValueError):
            api.dom_environment_is_suspicious(FIXTURE['user_agent'], {})

    def test_probe_rejects_unbounded_or_invalid_raw_facts(self):
        api = self.api()
        for key, value in [('window_to_string', 'secret' * 2000), ('document_mode', 'not-a-mode'), ('navigator_type', None)]:
            with self.subTest(key=key):
                snapshot = dict(FIXTURE['normal_snapshot'], **{key: value})
                with self.assertRaises(ValueError) as caught:
                    api.dom_environment_is_suspicious(FIXTURE['user_agent'], snapshot)
                self.assertNotIn('secret' * 100, str(caught.exception))

    def test_interaction_code_is_derived_from_original_sdk_event_vectors(self):
        api = self.api()
        self.assertTrue(hasattr(api, 'interaction_code'), 'Python interaction state producer is missing')
        fixture = json.loads((Path(__file__).parent / 'fixtures/wss/initialization_interaction_vectors.json').read_text(encoding='utf-8'))
        for case in fixture['cases']:
            events = [{'kind': case['kind'], 'x': index * case['step_x'], 'y': 0,
                       'd': 1700000000000 + (index + 1) * case['step_ms'],
                       'trusted': case['trusted']} for index in range(case['count'])]
            with self.subTest(profile=case['profile']):
                self.assertEqual(api.interaction_code(events), case['expected'])

    def test_interaction_samples_are_validated_and_bounded(self):
        api = self.api()
        self.assertTrue(hasattr(api, 'interaction_code'), 'Python interaction state producer is missing')
        sample = {'kind': 'mousemove', 'x': 0, 'y': 0, 'd': 1, 'trusted': True}
        for events in ([sample] * 4097, [dict(sample, x=float('nan'))], [dict(sample, x=10**1000)], [dict(sample, kind='unknown')]):
            with self.assertRaises(ValueError):
                api.interaction_code(events)

    def test_location_probe_matches_original_sdk_variations(self):
        api = self.api()
        self.assertTrue(hasattr(api, 'location_is_suspicious'), 'Python location producer is missing')
        # Original offline outputs: normal -> RsdyuufD4MaAoe5E, local file/IP
        # -> Rsdyu8fD4MaAoe57; decoded env_code changes 5131 -> 5643 (bit9).
        for href, expected in [('https://www.tiktok.com/live', False),
                               ('file:///synthetic-fixture', True),
                               ('https://127.0.0.1/synthetic-fixture', True)]:
            with self.subTest(href=href):
                self.assertEqual(api.location_is_suspicious(FIXTURE['user_agent'], href), expected)

    def test_descriptor_probe_matches_original_sdk_variations(self):
        api = self.api()
        self.assertTrue(hasattr(api, 'undetected_driver_probe'), 'Python descriptor producer is missing')
        # Normal: env=5131, output RsdyuufD4MaAoe5E; clean own descriptors:
        # env=1035, output RsdyYufD4MaAoe5j. Own touch getter with absent rtt:
        # env=5131, same normal output. No SDK/private state was modified.
        for touch, rtt, expected in [('undefined', 'object', True),
                                     ('missing', 'undefined', False),
                                     ('function', 'undefined', True)]:
            with self.subTest(touch=touch, rtt=rtt):
                self.assertEqual(api.undetected_driver_probe(touch, '', rtt), expected)

    def test_native_hook_probe_matches_original_sdk_variations(self):
        api = self.api()
        self.assertTrue(hasattr(api, 'native_hook_is_suspicious'), 'Python native-method producer is missing')
        names = ('canvas.toDataURL', 'navigator.toString', 'document.createElement',
                 'document.getElementById', 'JSON.stringify', 'JSON.parse',
                 'window.setInterval', 'window.eval', 'window.matchMedia')
        sources = dict.fromkeys(names, 'function synthetic() { [native code] }')
        # Original native-methods profile: env=5129, RsdyuuRD4MaAoe57.
        # Only createElement source changed to non-native: env=5131,
        # RsdyuufD4MaAoe5E. SDK itself is unchanged in both runs.
        self.assertFalse(api.native_hook_is_suspicious(sources, '[object PluginArray]'))
        changed = dict(sources, **{'document.createElement': 'function createElement() { return synthetic; }'})
        self.assertTrue(api.native_hook_is_suspicious(changed, '[object PluginArray]'))
        self.assertTrue(api.native_hook_is_suspicious(sources, '[object Array]'))

    def test_pointer_rate_retains_upstream_sqrt_threshold_rounding(self):
        api = self.api()
        # Independent Node arithmetic: sqrt(x*x+y*y)>50 is true for these
        # literals. Python hypot rounds to 50 and would miss kMoveFast.
        events = [{'kind': 'mousemove', 'x': i * 49.99995647505091,
                   'y': i * 0.0659734265821121, 'd': 1000 + i,
                   'trusted': True} for i in range(3)]
        self.assertEqual(api.interaction_code(events), 28)
