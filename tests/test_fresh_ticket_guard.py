"""Fresh Ticket Guard signing at the real HTTP boundary, using synthetic keys."""
import base64
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

import send_live_message as facade
from tiktok_live import client
from tiktok_live.algorithms import ticket_guard


class FreshTicketGuardTests(unittest.TestCase):
    def setUp(self):
        self.private = ec.generate_private_key(ec.SECP256R1())
        pem = self.private.private_bytes(serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=16,
            salt=b"secure-salt", iterations=1000).derive(b"tt-ticket-guard-iv")
        nonce = b"0123456789ab"
        self.state = dict(private_key_pem=pem, ts_sign="ts.1.synthetic",
            encrypt_ticket=base64.b64encode(nonce + AESGCM(key).encrypt(
                nonce, b"synthetic-ticket", None)).decode())
        stale = base64.b64encode(
            b'{"ts_sign":"old","req_sign":"old","req_content":"ticket,path,timestamp","timestamp":1}'
        ).decode()
        self.cached = {"tt-ticket-guard-client-data": stale,
                       "tt-ticket-guard-public-key": "old-synthetic-key"}
        self.posts = []
        self.session = SimpleNamespace(post=lambda url, **kw: self.posts.append(kw))

    def send(self, state):
        with patch.object(client, "sign_url", side_effect=lambda url, *a, **kw: url):
            facade._ORIGINAL_POST_CHAT_REQUEST(self.session, client.CHAT_URL, b"{}",
                "sessionid=synthetic", "fresh-ms", "synthetic-csrf",
                "https://www.tiktok.com/@host/live",
                ticket_guard_state=state, ticket_guard_headers=self.cached)

    def test_each_post_signs_current_timestamp_despite_saved_headers(self):
        # Reusing saved headers or the previous POST signature must fail this test.
        for stamp in (1791871957, 1791871967):
            with patch.object(ticket_guard.time, "time", return_value=stamp):
                self.send(self.state)
        self.assertEqual(len(self.posts), 2)
        for stamp, post in zip((1791871957, 1791871967), self.posts):
            data = ticket_guard.decode_client_data(
                post["headers"]["tt-ticket-guard-client-data"])
            self.assertEqual(data["timestamp"], stamp)
            signed = ("ticket=synthetic-ticket&path=/webcast/room/chat/"
                      f"&timestamp={stamp}").encode()
            self.private.public_key().verify(base64.b64decode(data["req_sign"]),
                signed, ec.ECDSA(hashes.SHA256()))
        self.assertNotEqual(self.posts[0]["headers"]["tt-ticket-guard-client-data"],
                            self.posts[1]["headers"]["tt-ticket-guard-client-data"])

    def test_invalid_state_never_falls_back_to_cached_signature(self):
        with self.assertRaises(client.SafeOperationError) as caught:
            self.send({"ts_sign": "synthetic"})
        self.assertEqual(caught.exception.classification, "ticket_guard_unavailable")
        self.assertEqual(self.posts, [])

    def test_cached_headers_without_state_never_post(self):
        with self.assertRaises(client.SafeOperationError) as caught:
            self.send(None)
        self.assertEqual(caught.exception.classification, "ticket_guard_unavailable")
        self.assertEqual(self.posts, [])
