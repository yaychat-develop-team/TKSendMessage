import threading
import time
import unittest
from unittest.mock import patch
import listen_msg
from tests.support.direct_fakes import visitor, WaitingConnection


class ListenerShutdownTests(unittest.TestCase):
    def test_close_retains_owner_until_pending_preparation_exits(self):
        release = threading.Event()
        entered = threading.Event()
        def prepare():
            entered.set()
            release.wait(10)
            return visitor()
        listener = listen_msg.LiveMessageListener('host', session_factory=prepare, connection_factory=WaitingConnection)
        # Cancel only once preparation is actually pending. A 10ms startup
        # deadline races the OS scheduler and does not establish that condition.
        self.addCleanup(listener.close)
        self.addCleanup(release.set)
        self.assertFalse(listener.start(5, should_cancel=entered.is_set))
        self.assertTrue(entered.is_set())
        original_join = listener._thread.join
        def join(*args, **kw):
            # Simulate a timed join expiring immediately instead of waiting 16s.
            if args or 'timeout' in kw:
                return
            return original_join()
        timer = threading.Timer(.05, release.set)
        timer.start()
        try:
            with patch.object(listener._thread, 'join', side_effect=join):
                listener.close()
            self.assertFalse(listener._thread.is_alive())
        finally:
            release.set()
            original_join(2)
            timer.join()

    def test_close_owns_connection_and_thread(self):
        listener = listen_msg.LiveMessageListener('host', session_factory=visitor, connection_factory=WaitingConnection)
        self.assertTrue(listener.start(1))
        listener.close()
        listener.close()
        self.assertFalse(listener._thread.is_alive())
        self.assertTrue(listener.connection.closed)
        self.assertFalse(listener.available)

    def test_failed_connect_cleans_once_and_exposes_safe_error(self):
        class Failed(WaitingConnection):
            def run(self, stop):
                raise ConnectionError('sessionid=DO_NOT_LOG')
        lines = []
        sink = listen_msg.logger.add(lambda message: lines.append(str(message)))
        try:
            listener = listen_msg.LiveMessageListener('host', session_factory=visitor, connection_factory=Failed)
            self.assertFalse(listener.start(1))
            listener.close()
        finally:
            listen_msg.logger.remove(sink)
        self.assertTrue(listener.connection.closed)
        self.assertIsInstance(listener.error, ConnectionError)
        self.assertNotIn('DO_NOT_LOG', ''.join(lines))
        self.assertNotIn('DO_NOT_LOG', str(listener.error))

    def test_timeout_during_preparation_never_publishes_late_connection(self):
        release = threading.Event()
        def prepare():
            release.wait(1)
            return visitor()
        listener = listen_msg.LiveMessageListener('host', session_factory=prepare, connection_factory=WaitingConnection)
        self.assertFalse(listener.start(.02))
        release.set()
        listener.close()
        self.assertFalse(listener.available)
        self.assertIsNone(listener.connection)
        self.assertFalse(listener._thread.is_alive())

    def test_remaining_connect_budget_is_forwarded(self):
        seen = []
        class Record(WaitingConnection):
            def __init__(self, *a, **kw):
                seen.append(kw['connect_timeout'])
                super().__init__(*a, **kw)
        def prepare():
            time.sleep(.02)
            return visitor()
        listener = listen_msg.LiveMessageListener('host', session_factory=prepare, connection_factory=Record)
        try:
            self.assertTrue(listener.start(.5))
            self.assertLess(seen[0], .5)
            self.assertGreater(seen[0], 0)
        finally:
            listener.close()
