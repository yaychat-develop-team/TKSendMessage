import unittest
from loguru import logger
from tiktok_live.client import log_chat_response


class ResponseLogPreviewTests(unittest.TestCase):
    def preview(self, payload):
        lines = []
        sink = logger.add(lambda message: lines.append(message.record['message']))
        try:
            log_chat_response(payload)
        finally:
            logger.remove(sink)
        return lines[0].split('response=', 1)[1]

    def test_long_response_prints_first_150_unicode_characters_only(self):
        payload = {'data': '中' * 200}
        self.assertEqual(self.preview(payload), '{"data":"' + '中' * 141 + '…（已截断）')
        self.assertEqual(payload, {'data': '中' * 200})

    def test_response_at_150_characters_is_not_marked_truncated(self):
        self.assertEqual(self.preview({'data': 'x' * 139}), '{"data":"' + 'x' * 139 + '"}')

    def test_short_response_stays_complete(self):
        self.assertEqual(self.preview({'status_code': 0}), '{"status_code":0}')

    def test_redaction_happens_before_truncation(self):
        text = self.preview({'sessionid': 'synthetic-private-session', 'data': 'x' * 200})
        self.assertIn('<redacted>', text)
        self.assertNotIn('synthetic-private-session', text)
        self.assertTrue(text.endswith('…（已截断）'))


if __name__ == '__main__':
    unittest.main()
