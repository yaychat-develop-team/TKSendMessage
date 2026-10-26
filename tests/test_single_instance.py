import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication, QWidget


class SingleInstanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def test_duplicate_activates_existing_window_and_does_not_own_lock(self):
        from desktop.single_instance import SingleInstance
        with tempfile.TemporaryDirectory() as directory:
            owner = SingleInstance(Path(directory))
            other = SingleInstance(Path(directory))
            window = QWidget()
            try:
                self.assertTrue(owner.acquire())
                window.showMinimized()
                owner.set_window(window)
                acquired = []
                def duplicate():
                    client = SingleInstance(Path(directory))
                    acquired.append(client.acquire())
                    acquired.append(client.activation_sent)
                    client.close()
                thread = threading.Thread(target=duplicate)
                thread.start()
                while thread.is_alive():
                    self.qt.processEvents()
                    time.sleep(.005)
                thread.join(2)
                self.assertEqual(acquired, [False, True])
                self.assertFalse(other.acquire())
                deadline = time.monotonic() + 2
                while window.isMinimized() and time.monotonic() < deadline:
                    self.qt.processEvents()
                    time.sleep(.005)
                self.assertFalse(window.isMinimized())
                other.close()
                third = SingleInstance(Path(directory))
                try:
                    self.assertFalse(third.acquire(), 'secondary released primary lock')
                finally:
                    third.close()
            finally:
                owner.close()
                other.close()
                window.close()
            again = SingleInstance(Path(directory))
            try:
                self.assertTrue(again.acquire())
            finally:
                again.close()

    def test_lock_is_independent_of_working_directory_and_recovers_after_process_crash(self):
        script = r'''
import sys, time
from pathlib import Path
from PySide6.QtWidgets import QApplication
from desktop.single_instance import SingleInstance
application = QApplication([])
guard = SingleInstance(Path(sys.argv[1]))
try:
    print('primary' if guard.acquire() else 'duplicate', flush=True)
    if sys.argv[2] == 'hold':
        time.sleep(30)
finally:
    guard.close()
'''
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            env = {**os.environ, 'PYTHONPATH': str(project)}
            first = subprocess.Popen([sys.executable, '-u', '-c', script, directory, 'hold'],
                cwd=project, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(first.stdout.readline().strip(), 'primary')
                other = subprocess.run([sys.executable, '-u', '-c', script, directory, 'once'],
                    cwd=directory, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(other.returncode, 0, other.stderr)
                self.assertEqual(other.stdout.strip(), 'duplicate')
            finally:
                first.terminate()
                first.communicate(timeout=10)
            after = subprocess.run([sys.executable, '-u', '-c', script, directory, 'once'],
                env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(after.returncode, 0, after.stderr)
            self.assertEqual(after.stdout.strip(), 'primary')

    def test_second_process_receives_activation_ack_and_primary_window_is_shown(self):
        script = r'''
import sys
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget
from desktop.single_instance import SingleInstance
application = QApplication([])
guard = SingleInstance(Path(sys.argv[1]))
class Window(QWidget):
    def show(self):
        super().show()
        print('activated', flush=True)
        QTimer.singleShot(50, application.quit)
try:
    if guard.acquire():
        window = Window()
        guard.set_window(window)
        print('primary', flush=True)
        QTimer.singleShot(8000, application.quit)
        application.exec()
    else:
        print('ack' if guard.activation_sent else 'missing', flush=True)
finally:
    guard.close()
'''
        with tempfile.TemporaryDirectory() as directory:
            primary = subprocess.Popen([sys.executable, '-u', '-c', script, directory],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                self.assertEqual(primary.stdout.readline().strip(), 'primary')
                second = subprocess.run([sys.executable, '-u', '-c', script, directory],
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(second.returncode, 0, second.stderr)
                self.assertEqual(second.stdout.strip(), 'ack')
                out, err = primary.communicate(timeout=10)
                self.assertEqual(primary.returncode, 0, err)
                self.assertIn('activated', out)
            finally:
                if primary.poll() is None:
                    primary.terminate()
                    primary.communicate(timeout=5)
