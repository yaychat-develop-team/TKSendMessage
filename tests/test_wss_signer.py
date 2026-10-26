"""Original SDK fixtures guard the initialized Python frontier calculation.

These deliberately cannot certify that bootstrap supplies genuine SDK state.
"""
from concurrent.futures import ThreadPoolExecutor
import copy
import importlib
import json
from pathlib import Path
import unittest
from types import SimpleNamespace


FIXTURE = json.loads((Path(__file__).parent / 'fixtures/wss/frontier_vectors.json').read_text(encoding='utf-8'))


def rng_from_state(state):
    def next_float():
        nonlocal state
        state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
        return state / 4294967296
    return next_float


class FrontierSignerTests(unittest.TestCase):
    def implementation(self):
        # Missing implementation is the intended RED, not a collection failure.
        try:
            return importlib.import_module('tiktok_live.algorithms.frontier')
        except ModuleNotFoundError as error:
            self.fail(f'Pure Python frontier implementation missing: {error.name}')

    def inputs(self, case):
        data = dict(FIXTURE['defaults'])
        data.update({key: case[key] for key in data if key in case})
        data['metrics'] = dict(case.get('metrics', {}), frontier_state=copy.deepcopy(FIXTURE['state']))
        data['now_ms'] = case['now_ms']
        data['random_float'] = rng_from_state(case['rng_state'])
        return data

    def test_matches_reference_vectors(self):
        api = self.implementation()
        for case in FIXTURE['cases']:
            with self.subTest(expected=case['expected'], cookie=case.get('cookie', '')):
                actual = api.frontier_sign(**self.inputs(case))
                self.assertEqual(actual, case['expected'])
                self.assertEqual(len(actual), 16)

    def test_clock_random_and_initialization_state_are_not_ignored(self):
        api = self.implementation()
        case = FIXTURE['cases'][0]
        baseline = api.frontier_sign(**self.inputs(case))
        # This SDK path has no timestamp field. Independent time-only samples
        # show invariance; do not invent a timestamp contribution.
        changed_clock = self.inputs(case)
        changed_clock['now_ms'] = 1800000000000
        self.assertEqual(api.frontier_sign(**changed_clock), baseline)
        for field, value in [('initialized', False), ('env_code', 128), ('ub_code', 0), ('bogus_index', 1)]:
            changed = self.inputs(case)
            changed['metrics']['frontier_state'][field] = value
            with self.subTest(field=field):
                self.assertNotEqual(api.frontier_sign(**changed), baseline)
        changed = self.inputs(case)
        changed['random_float'] = rng_from_state(2757930144)
        self.assertNotEqual(api.frontier_sign(**changed), baseline)

    def test_concurrent_calls_do_not_share_mutable_sdk_state(self):
        api = self.implementation()
        case = FIXTURE['cases'][0]
        data = self.inputs(case)
        before = copy.deepcopy(data['metrics'])
        with ThreadPoolExecutor(max_workers=8) as pool:
            outputs = list(pool.map(lambda _: api.frontier_sign(**self.inputs(case)), range(32)))
        self.assertEqual(outputs, [case['expected']] * 32)
        api.frontier_sign(**data)
        self.assertEqual(data['metrics'], before)

    def test_invalid_input_does_not_return_fake_signature(self):
        api = self.implementation()
        for key, value in [('cookie', 'x' * 65537), ('user_agent', 'x' * 2049), ('referer', 'x' * 4097), ('metrics', {}), ('metrics', {'large': 'x' * 65537}), ('random_float', lambda: float('nan')), ('random_float', lambda: 1.0)]:
            data = self.inputs(FIXTURE['cases'][0])
            data[key] = value
            with self.subTest(key=key):
                with self.assertRaises(api.FrontierError) as caught:
                    api.frontier_sign(**data)
                self.assertNotIn('x' * 100, str(caught.exception))

    def test_missing_initialized_state_fails_instead_of_assuming_fixture_state(self):
        api = self.implementation()
        data = self.inputs(FIXTURE['cases'][0])
        data['metrics'] = {'screenWidth': 1440}
        with self.assertRaises(api.FrontierError):
            api.frontier_sign(**data)

    def test_metrics_traversal_is_bounded_before_encoding(self):
        api = self.implementation()
        data = self.inputs(FIXTURE['cases'][0])
        nested = {}
        for _ in range(32):
            nested = {'next': nested}
        data['metrics']['nested'] = nested
        with self.assertRaises(api.FrontierError):
            api.frontier_sign(**data)

    def test_invalid_input_and_cancellation_do_not_return_fake_signature(self):
        api = self.implementation()
        try:
            signer = importlib.import_module('tiktok_live.wss.signer')
        except ModuleNotFoundError as error:
            self.fail(f'Session signing adapter missing: {error.name}')
        session = SimpleNamespace(
            role='account_heartbeat', cookie_header='sessionid=synthetic_secret',
            user_agent=FIXTURE['defaults']['user_agent'],
            referer=FIXTURE['defaults']['referer'],
            metrics={'frontier_state': copy.deepcopy(FIXTURE['state'])},
        )
        for checks in ([True], [False, True]):
            cancel = iter(checks)
            with self.assertRaises(api.FrontierError) as caught:
                signer.sign_frontier(session, should_cancel=lambda: next(cancel))
            self.assertNotIn('synthetic_secret', str(caught.exception))
        session.metrics = {}
        with self.assertRaises(api.FrontierError) as caught:
            signer.sign_frontier(session)
        self.assertNotIn('synthetic_secret', str(caught.exception))
