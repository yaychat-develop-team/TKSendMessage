"""Saved account edits preserve structure and refuse stale writes."""

import tempfile
import unittest
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from desktop.account_edit import AccountEditError, save_edited_account
from tiktok_live.account_store import load_account_store, save_account_store_atomic, upsert_account


class AccountEditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'accounts.json'
        store = load_account_store(self.path)
        self.account = upsert_account(store, [
            {'name': 'uid_tt', 'value': 'synthetic-session', 'domain': '.tiktok.com'}
        ], datetime.now(timezone.utc))
        self.account['username'] = 'before'
        save_account_store_atomic(self.path, store)
        self.account = load_account_store(self.path)['accounts'][0]

    def test_updates_only_selected_record_and_preserves_id(self):
        updated = deepcopy(self.account)
        updated['username'] = 'after'
        save_edited_account(self.path, self.account, updated)
        saved = load_account_store(self.path)['accounts'][0]
        self.assertEqual(saved['id'], self.account['id'])
        self.assertEqual(saved['username'], 'after')

    def test_rejects_id_change_and_malformed_cookie_without_writing(self):
        original = self.path.read_bytes()
        for field, value in [
                ('id', 'other-id'),
                ('cookies', [{'name': 'uid_tt', 'value': 12}]),
                ('cookies', [{'name': 'uid_tt', 'value': 'other', 'domain': 'example.com'}]),
        ]:
            with self.subTest(field=field):
                edited = deepcopy(self.account)
                edited[field] = value
                with self.assertRaises(AccountEditError):
                    save_edited_account(self.path, self.account, edited)
                self.assertEqual(self.path.read_bytes(), original)

    def test_rejects_stale_dialog_instead_of_overwriting_newer_login(self):
        current = load_account_store(self.path)
        current['accounts'][0]['username'] = 'newer-login'
        save_account_store_atomic(self.path, current)
        edited = deepcopy(self.account)
        edited['username'] = 'stale-edit'
        with self.assertRaises(AccountEditError):
            save_edited_account(self.path, self.account, edited)
        self.assertEqual(load_account_store(self.path)['accounts'][0]['username'], 'newer-login')
