"""Per-user startup ownership and activation, independent of EXE location."""
import hashlib
import os
import time
from pathlib import Path

from PySide6.QtCore import QObject, QLockFile, QStandardPaths, QTimer, QEventLoop
from PySide6.QtNetwork import QLocalServer, QLocalSocket


class InstanceError(RuntimeError):
    pass


class SingleInstance(QObject):
    def __init__(self, storage_dir=None):
        super().__init__()
        root = Path(storage_dir) if storage_dir is not None else Path(
            QStandardPaths.writableLocation(QStandardPaths.StandardLocation.GenericDataLocation)) / 'TKChatWave' / 'instance'
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError:
            raise InstanceError('instance_lock_unavailable') from None
        self._lock = QLockFile(str(root / 'application.lock'))
        self._lock.setStaleLockTime(0)  # Never evict a live process just because it is old.
        identity = os.path.normcase(str(root.resolve())).encode('utf-8')
        self._name = 'TKChatWave-' + hashlib.sha256(identity).hexdigest()[:32]
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self._server.newConnection.connect(self._accept)
        self._owns = False
        self._window = None
        self._pending = False
        self._clients = set()
        self.activation_sent = False

    def acquire(self):
        if self._owns:
            return True
        if not self._lock.tryLock(0):
            if self._lock.error() != QLockFile.LockError.LockFailedError:
                raise InstanceError('instance_lock_unavailable')
            deadline = time.monotonic() + 1.5
            while time.monotonic() < deadline:
                socket = QLocalSocket()
                socket.connectToServer(self._name)
                if socket.waitForConnected(100):
                    loop = QEventLoop()
                    timer = QTimer()
                    timer.setSingleShot(True)
                    timer.timeout.connect(loop.quit)
                    def received():
                        if socket.canReadLine():
                            self.activation_sent = bytes(socket.readLine(64)) == b'activated\n'
                            loop.quit()
                    socket.readyRead.connect(received)
                    socket.disconnected.connect(loop.quit)
                    socket.write(b'activate\n')
                    socket.flush()
                    timer.start(1500)
                    loop.exec()
                    received()
                    timer.stop()
                    socket.abort()
                    return False
                time.sleep(.025)
            return False  # Owner may still be starting or safely shutting down.
        self._owns = True
        # Only the lock owner may remove an orphaned endpoint after a crash.
        QLocalServer.removeServer(self._name)
        if not self._server.listen(self._name):
            self.close()
            raise InstanceError('instance_activation_unavailable')
        return True

    def set_window(self, window):
        self._window = window
        if self._pending:
            self._activate()

    def _activate(self):
        if self._window is None:
            self._pending = True
            return
        self._pending = False
        if getattr(self._window, '_closing', False):
            return
        if self._window.isMinimized():
            self._window.showNormal()
        else:
            self._window.show()
        self._window.raise_()
        self._window.activateWindow()

    def _accept(self):
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            self._clients.add(socket)
            socket.setReadBufferSize(64)
            def read(socket=socket):
                if socket.canReadLine():
                    command = bytes(socket.readLine(64))
                    if command == b'activate\n':
                        self._activate()
                        socket.write(b'activated\n')
                        socket.flush()
                    socket.disconnectFromServer()
            def release(socket=socket):
                self._clients.discard(socket)
                socket.deleteLater()
            socket.readyRead.connect(read)
            socket.disconnected.connect(release)
            timeout = QTimer(socket)
            timeout.setSingleShot(True)
            timeout.timeout.connect(socket.abort)
            timeout.start(2000)
            read()

    def close(self):
        self._window = None
        for socket in tuple(self._clients):
            socket.abort()
        self._clients.clear()
        self._server.close()
        if self._owns:
            self._lock.unlock()
            self._owns = False
