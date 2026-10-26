import gzip
import importlib.util
import json
from pathlib import Path
import unittest


class DirectProtocolTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec('tiktok_live.wss.protocol'))
        from tiktok_live.wss import protocol, direct_signer
        self.p, self.s = protocol, direct_signer

    def test_direct_signature_matches_source_vectors(self):
        cases = json.loads((Path(__file__).parent / 'fixtures/wss/direct_vectors.json').read_text(encoding='utf-8'))
        for case in cases:
            with self.subTest(payload=case['payload'], context=case['context']):
                self.assertEqual(self.s.frontier_sign(case['payload'], context=case['context']), case['signature'])

    def test_duplicate_query_keys_and_raw_alphabet_are_preserved(self):
        from urllib.parse import urlsplit
        url = self.s.build_direct_url('42', user_agent='Mozilla/5.0 test', random_values=(.1, .2, .3))
        query = urlsplit(url).query
        self.assertEqual([x for x in query.split('&') if x.startswith('version_code=')],
                         ['version_code=180800', 'version_code=270000'])
        signature = self.s.frontier_sign('', context=dict(sdk_version='1.0.0.417', envcode=129, ubcode=0, index=1, random_values=(.1, .2, .3)))
        self.assertTrue(query.endswith('X-Bogus=' + signature))
        self.assertIn('browser_version=5.0%20test', query)

    def test_protocol_large_ids_gzip_and_ack_roundtrip(self):
        # Literal protobuf pieces: construct an independent server-shaped chat.
        p = self.p
        user = p._wire(1, 18446744073709551613) + p._wire(38, 'sender') + p._wire(3, '昵称')
        body = p._wire(2, user) + p._wire(3, '你好')
        envelope = p._wire(1, 'WebcastChatMessage') + p._wire(2, body) + p._wire(3, 18446744073709551614)
        response = p._wire(1, envelope) + p._wire(5, 'current-ext') + p._wire(9, 1)
        packet = p._encode('msg', gzip.compress(response), 18446744073709551612)
        frame = p.decode_push_frame(packet)
        self.assertEqual(frame.events[0].message_id, '18446744073709551614')
        self.assertEqual(frame.events[0].user_id, '18446744073709551613')
        self.assertEqual(frame.events[0].text, '你好')
        self.assertTrue(frame.need_ack)
        ack = p._fields(p.encode_ack(frame.log_id, frame.internal_ext))
        self.assertEqual(ack[2], [18446744073709551612])
        self.assertEqual(ack[8], [b'current-ext'])
        self.assertEqual(p.decode_push_frame(p.encode_heartbeat(42)).room_id, '42')

    def test_server_heartbeat_payload_does_not_need_outbound_room_format(self):
        p = self.p
        # The server's hb response is opaque; it need not reuse our outbound
        # protobuf payload, and must not tear down an otherwise healthy WSS.
        frame = p.decode_push_frame(p._encode('hb', b'{"status":0}'))
        self.assertEqual(frame.payload_type, 'hb')
        self.assertIsNone(frame.room_id)
        self.assertEqual(p.decode_push_frame(p.encode_enter_room(42)).room_id, '42')

    def test_invalid_frames_are_bounded(self):
        p = self.p
        for raw in (b'\x80', b'\x08' + b'\xff'*10, b'\x0a\x05x', b'\x00', b'\x0b', b'\x08\x01'*20001,
                    b'x'*(2*1024*1024+1), p._wire(7, b'\xff')):
            with self.subTest(size=len(raw)), self.assertRaises((ValueError, UnicodeError)):
                p.decode_push_frame(raw)
        for payload in (gzip.compress(b'x'*(4*1024*1024+1)), b'\x1f\x8bbad'):
            with self.assertRaises(ValueError):
                p.decode_push_frame(p._encode('msg', payload))

    def test_signer_and_ids_fail_closed(self):
        for room in ('0', '-1', '18446744073709551616', 'x', '42\r\nInjected'):
            with self.assertRaises(ValueError):
                self.s.build_direct_url(room, user_agent='UA')
        with self.assertRaises(ValueError):
            self.s.frontier_sign('', context={})
