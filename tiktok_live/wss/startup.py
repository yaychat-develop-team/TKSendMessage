"""One startup deadline with cancellation ownership of pending I/O resources."""
import socket
import threading
import time


def close_resource(resource):
    try:
        shutdown = getattr(resource, 'shutdown', None)
        if shutdown is not None:
            try:
                shutdown(socket.SHUT_RDWR)
            except TypeError:
                shutdown()
    except Exception:
        pass
    try:
        resource.close()
    except Exception:
        pass


class StartupScope:
    def __init__(self, timeout, cancel_event=None):
        self.deadline = time.monotonic() + max(0.0, timeout)
        self._external_cancel = cancel_event
        self._lock = threading.Lock()
        self._resources = []
        self._cancelled = False
        self._finished = False
        self._timer = threading.Timer(max(0.0, timeout), self.cancel)
        self._timer.daemon = True
        self._timer.start()

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if self._cancelled or remaining <= 0 or (self._external_cancel and self._external_cancel.is_set()):
            raise TimeoutError('WSS startup cancelled or timed out')
        return remaining

    def own(self, resource):
        try:
            with self._lock:
                self.remaining()
                if self._finished:
                    raise RuntimeError('Startup scope already transferred')
                self._resources.append(resource)
            return resource
        except Exception:
            close_resource(resource)
            raise

    def cancel(self):
        with self._lock:
            if self._finished:
                return
            self._cancelled = True
            resources, self._resources = self._resources, []
        for resource in resources:
            close_resource(resource)

    def finish(self):
        with self._lock:
            self.remaining()
            self._finished = True
            self._resources.clear()
        self._timer.cancel()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._timer.cancel()
        self.cancel()
