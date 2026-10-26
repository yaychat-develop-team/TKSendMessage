"""A task-local send gate; it never modifies requests or delivery counters."""
import threading


class ConsecutiveUnobserved:
    def __init__(self, *, on_stop, lock=None):
        self._lock = lock if lock is not None else threading.RLock()
        self._stop = threading.Event()
        self._on_stop = on_stop
        self._outcomes = {}
        self._counted_misses = set()
        self.streak = 0

    @property
    def stopped(self):
        return self._stop.is_set()

    def record(self, index, status):
        status = status if status in ('observed', 'not_observed') else 'other'
        with self._lock:
            previous = self._outcomes.get(index)
            if self.stopped or previous == status or previous == 'observed':
                return
            self._outcomes[index] = status
            if status != 'not_observed':
                self.streak = 0
                return
            if index in self._counted_misses:
                return
            self._counted_misses.add(index)
            self.streak += 1
            if self.streak == 3:
                self._stop.set()
                self._on_stop()
