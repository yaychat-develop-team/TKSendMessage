import tempfile
import unittest
from pathlib import Path

from desktop.settings_store import DesktopSettings, load_desktop_settings, save_desktop_settings


class DesktopSettingsTests(unittest.TestCase):
    def test_room_mode_round_trip_and_legacy_invalid_settings_default_to_serial(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'desktop-settings.json'
            for content in ('{}', '{"room_execution_mode":"invalid"}', '{"room_execution_mode":null}'):
                path.write_text(content, encoding='utf-8')
                self.assertEqual(load_desktop_settings(path).room_execution_mode, 'serial')
            values = DesktopSettings(room_execution_mode='parallel', message_attempts=4)
            save_desktop_settings(path, values)
            loaded = load_desktop_settings(path)
            self.assertEqual(loaded.room_execution_mode, 'parallel')
            self.assertEqual(loaded.message_attempts, 4)
            self.assertNotIn('room_execution_mode', loaded.runner_kwargs())

    def test_defaults_match_script_settings_and_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'desktop-settings.json'
            defaults = load_desktop_settings(path)
            self.assertEqual(defaults.login_timeout_seconds, 300)
            self.assertEqual(defaults.account_interval_seconds, 0)
            self.assertTrue(defaults.observation_enabled)
            self.assertEqual(defaults.message_attempts, 1)
            changed = DesktopSettings(**{
                **defaults.to_dict(),
                'login_timeout_seconds': 90,
                'show_live_comments': True,
                'room_id_cookie': 'fixture-cookie',
            })
            save_desktop_settings(path, changed)
            self.assertEqual(load_desktop_settings(path), changed)

    def test_invalid_file_falls_back_without_exposing_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'desktop-settings.json'
            path.write_text('{broken', encoding='utf-8')
            loaded = load_desktop_settings(path)
            self.assertEqual(loaded.login_timeout_seconds, 300)
            self.assertEqual(loaded.room_id_cookie, '')


if __name__ == '__main__':
    unittest.main()
