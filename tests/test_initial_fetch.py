"""First-party initial fetch has no credential-bearing test fixtures."""
import unittest
from urllib.parse import parse_qs, urlsplit

from tiktok_live.wss.initial_fetch import (
    build_initialized_ws_url, decode_initial_fetch,
)


def _varint(value):
    output = bytearray()
    while value > 127:
        output.append((value & 127) | 128)
        value >>= 7
    output.append(value)
    return bytes(output)


def _field(number, value):
    value = value.encode() if isinstance(value, str) else value
    return _varint(number << 3 | 2) + _varint(len(value)) + value


def _initial(*, cursor='cursor-test', push_server=None, route=True):
    if push_server is None:
        push_server = 'wss://webcast-ws.us.tiktok.com/webcast/im/ws_proxy/ws_reuse_supplement/'
    data = _field(1, b'old-comment') + _field(2, cursor)
    if route:
        data += _field(7, _field(1, 'wrss') + _field(2, 'route-secret'))
    data += _field(10, push_server)
    return data


class InitialFetchTests(unittest.TestCase):
    def test_decodes_required_fields_without_revealing_route_state_in_repr(self):
        result = decode_initial_fetch(_initial())
        self.assertEqual('cursor-test', result.cursor)
        self.assertEqual((('wrss', 'route-secret'),), result.route_params)
        self.assertEqual(1, result.historical_message_count)
        self.assertNotIn('route-secret', repr(result))
        self.assertNotIn('cursor-test', repr(result))

    def test_rejects_absent_cursor_route_or_push_server(self):
        for payload in (_initial(cursor=''), _initial(route=False),
                        _field(2, 'cursor-test') + _field(7, _field(1, 'wrss') + _field(2, 'value'))):
            with self.subTest(payload_length=len(payload)), self.assertRaises(ValueError):
                decode_initial_fetch(payload)

    def test_rejects_malformed_or_unbounded_protobuf(self):
        for payload in (b'\x12\x80', _initial(cursor=b'\xff'),
                        b'\x12' + _varint(2 * 1024 * 1024) + b'x' * (2 * 1024 * 1024)):
            with self.subTest(payload_length=len(payload)), self.assertRaises(ValueError):
                decode_initial_fetch(payload)

    def test_builds_valid_server_issued_wss_url(self):
        result = decode_initial_fetch(_initial())
        url = build_initialized_ws_url(result, room_id='7699044408762616590',
            user_agent='Mozilla/5.0 Test', additional_params=(('live_id', '12'),))
        parts = urlsplit(url)
        self.assertEqual('webcast-ws.us.tiktok.com', parts.hostname)
        self.assertEqual('7699044408762616590', parse_qs(parts.query)['room_id'][0])
        self.assertEqual('route-secret', parse_qs(parts.query)['wrss'][0])

    def test_rejects_untrusted_server_issued_wss_destinations(self):
        bad_hosts = (
            'ws://webcast-ws.us.tiktok.com/webcast/im/ws_proxy/ws_reuse_supplement/',
            'wss://webcast-ws.us.tiktok.com.evil.test/webcast/im/ws_proxy/ws_reuse_supplement/',
            'wss://webcast-ws.us.tiktok.com:8443/webcast/im/ws_proxy/ws_reuse_supplement/',
            'wss://user:pass@webcast-ws.us.tiktok.com/webcast/im/ws_proxy/ws_reuse_supplement/',
            'wss://webcast-ws.us.tiktok.com/other',
            'wss://webcast-ws.us.tiktok.com/webcast/im/ws_proxy/ws_reuse_supplement/?room_id=77',
        )
        for host in bad_hosts:
            with self.subTest(host=host.split('?')[0]), self.assertRaises(ValueError):
                build_initialized_ws_url(decode_initial_fetch(_initial(push_server=host)),
                    room_id='7699044408762616590', user_agent='Mozilla/5.0 Test',
                    additional_params=(('live_id', '12'),))


if __name__ == '__main__':
    unittest.main()
