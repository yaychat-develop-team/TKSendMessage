import subprocess
import unittest
import tempfile
from pathlib import Path

import settings


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_NAMES = {
    "tiktok_accounts.json",
    "failed_accounts.json",
    "silenced_accounts.json",
    "port",
    ".browser-profile",
    "__pycache__",
    ".pytest_cache",
    "runtime",
    "logs",
    "reports",
    "profiles",
    "cache",
}


def tracked_distribution_paths():
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [Path(item.decode()) for item in result.stdout.split(b"\0") if item]


class DistributionSafetyTests(unittest.TestCase):
    def test_source_copy_contains_no_runtime_credentials(self):
        tracked = tracked_distribution_paths()
        tracked_forbidden = sorted(
            path.as_posix()
            for path in tracked
            if any(part in FORBIDDEN_NAMES for part in path.parts)
            or path.suffix.lower() == ".log"
            or path.suffix.lower() == ".pyc"
        )
        self.assertEqual(tracked_forbidden, [], f"forbidden tracked inputs found: {tracked_forbidden}")
        self.assertEqual(settings.ROOM_ID_COOKIE, "")
        # A developer checkout may contain ignored runtime data. The artifact
        # boundary is the actual staging script, not a clean developer machine.
        # Inspect a fresh staged copy, including untracked shipping sources.
        from build.verify_release import scan_sensitive_files
        (ROOT / 'build/work').mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='distribution-test-', dir=ROOT / 'build/work') as directory:
            stage = Path(directory) / 'app'
            subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                '-File', str(ROOT / 'build/stage_direct_release.ps1'),
                '-Destination', str(stage)], cwd=ROOT, check=True, capture_output=True)
            self.assertTrue((stage / 'desktop/main_window.py').is_file())
            self.assertTrue((stage / 'tiktok_live/client.py').is_file())
            self.assertEqual(scan_sensitive_files(stage), [])


if __name__ == "__main__":
    unittest.main()
