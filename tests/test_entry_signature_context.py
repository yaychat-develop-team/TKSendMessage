import unittest
from urllib.parse import parse_qs, urlsplit
from tiktok_live.algorithms import x_dynosaur as d, x_gnarly as g
from tiktok_live.signing import sign_url


class EntrySignatureContextTests(unittest.TestCase):
    def test_initial_fetch_uses_working_example_session_context(self):
        url = 'https://webcast.us.tiktok.com/webcast/im/fetch/?room_id=42'
        dyn = d._select_x_dynosaur_context(url)
        gnarly = g._select_x_gnarly_context(url)
        self.assertEqual((dyn['field37'], dyn['field47']), (12, 72))
        self.assertEqual((gnarly['field13'], gnarly['field12']), (12, 72))

    def test_initial_fetch_signed_url_embeds_working_session_context(self):
        signed = sign_url(
            'https://webcast.us.tiktok.com/webcast/im/fetch/?room_id=42&client_enter=1',
            'synthetic-ms-token', user_agent='Synthetic test agent',
        )
        params = parse_qs(urlsplit(signed).query)
        dyn = d._decode_x_dynosaur_context(params['X-Dynosaur'][0])
        gnarly = g._decode_x_gnarly_context(params['X-Gnarly'][0])
        self.assertEqual((dyn['field37'], dyn['field47']), (12, 72))
        self.assertEqual((gnarly['field13'], gnarly['field12']), (12, 72))

    def test_dynosaur_signs_current_page_and_session_marker(self):
        fields = d._parse_x_dynosaur_payload(d.build_x_dynosaur_payload(
            'room_id=1', 'test-agent', ts=1700000000, body=b'room_id=1',
            context={'field44': 12345, 'field53': 'www.tiktok.com/@host/live'}))
        self.assertEqual(d._dynosaur_string_decode(fields[44]), '12345')
        self.assertEqual(d._dynosaur_string_decode(fields[53]), 'www.tiktok.com/@host/live')

    def test_gnarly_preserves_sdk_and_shared_session_marker(self):
        parsed = g._parse_x_gnarly_payload(g.build_x_gnarly_payload(
            'room_id=1', 'test-agent', ts=1700000000, body=b'room_id=1',
            field8_seed=10, cfg={'perf_data': 12345, 'sdk_version': '5.3.2',
                                 'scm_version': '1.0.0.417'}))['fields']
        self.assertEqual(parsed[7], 12345)
        self.assertEqual(parsed[9], '5.3.2')
        self.assertEqual(parsed[10], '1.0.0.417')

    def test_dynosaur_roundtrip_keeps_entry_state(self):
        payload = d.build_x_dynosaur_payload('a=1', 'test-agent', ts=1700000000,
            context={'field44': 12345, 'field53': 'www.tiktok.com/@host/live'})
        context = d._decode_x_dynosaur_context(d.generate_x_dynosaur(payload))
        self.assertEqual(context.get('field44'), 12345)
        self.assertEqual(context.get('field53'), 'www.tiktok.com/@host/live')

    def test_gnarly_roundtrip_keeps_entry_state(self):
        value = g.generate_x_gnarly('https://example.com/', 'a=1', 'test-agent',
            'synthetic', '1', ts=1700000000, cfg={
                'perf_data': 12345, 'sdk_version': '5.3.2', 'scm_version': '1.0.0.417'})
        context = g._decode_x_gnarly_context(value)
        self.assertEqual(context.get('perf_data'), 12345)
        self.assertEqual(context.get('sdk_version'), '5.3.2')
        self.assertEqual(context.get('scm_version'), '1.0.0.417')
