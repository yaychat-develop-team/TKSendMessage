"""Windows desktop entrypoint; business operations remain in desktop workers."""

import sys
import json
from collections.abc import Sequence
from pathlib import Path

from loguru import logger
from PySide6.QtCore import QCoreApplication, QEvent, QObject, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from desktop.log_bridge import DailyLogSink, sanitize_log_text
from desktop.main_window import MainWindow
from desktop.paths import RuntimePaths
from desktop.single_instance import SingleInstance, InstanceError
from desktop.startup_errors import show_startup_error


class _QuitGate(QObject):
    """Defer application quit while the window still owns a running worker."""

    def __init__(self, application, window):
        super().__init__(application)
        self.window = window

    def eventFilter(self, watched, event):
        if event.type() == QEvent.Type.Quit:
            return not self.window.shutdown()
        return super().eventFilter(watched, event)


def _safe_log_record(record):
    record['message'] = sanitize_log_text(record['message'])
    record['exception'] = None
    return True


def _application_icon(paths):
    roots = [paths.app_dir]
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        roots.append(Path(sys._MEIPASS))
    for root in roots:
        for relative in (
            'icon.ico', 'icon.png', 'assets/icon.ico', 'assets/icon.png',
            'desktop/assets/icon.ico', 'desktop/assets/icon.png',
        ):
            candidate = root / relative
            if candidate.is_file():
                return QIcon(str(candidate))
    return QIcon()


def main(argv: Sequence[str] | None = None) -> int:
    application = QApplication.instance() or QApplication(list(sys.argv if argv is None else argv))
    application.setApplicationName('TKChatWave')
    application.setApplicationDisplayName('TKChatWave')
    application.setOrganizationName('TKChatWave')
    application.setQuitOnLastWindowClosed(True)
    instance = None
    try:
        instance = SingleInstance()
        if not instance.acquire():
            if not instance.activation_sent:
                QMessageBox.information(None, 'TKChatWave', '程序已在运行、启动或退出中，请等待已有窗口完成。')
            return 0
        return _run_desktop(application, instance, argv)
    except Exception as exc:
        code = ('startup_instance_unavailable' if isinstance(exc, InstanceError) else
                'startup_data_permission' if isinstance(exc, PermissionError) else
                'startup_storage_unavailable' if isinstance(exc, OSError) else
                'startup_initialization_failed')
        show_startup_error(code)
        return 1
    finally:
        if instance is not None:
            instance.close()


def _run_desktop(application, instance, argv):
    paths = RuntimePaths.from_application()
    icon = _application_icon(paths)
    application.setWindowIcon(icon)
    handler = logger.add(DailyLogSink(paths.log_dir), filter=_safe_log_record,
                         format='{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}',
                         backtrace=False, diagnose=False)
    original_hook = sys.excepthook

    def exception_hook(exception_type, exception, traceback):
        # Exception messages/tracebacks can contain request cookies or payloads.
        logger.error('Unhandled desktop exception ({})', exception_type.__name__)

    def shutdown_complete():
        # All owned native threads have finished. Exit this event loop directly
        # without depending on window visibility or closing unrelated windows.
        application.exit(0)

    sys.excepthook = exception_hook
    window = None
    gate = None
    try:
        window = MainWindow(paths)
        window.setWindowIcon(icon)
        instance.set_window(window)
        gate = _QuitGate(application, window)
        application.installEventFilter(gate)
        application.aboutToQuit.connect(window.shutdown)
        window.shutdown_finished.connect(shutdown_complete)
        window.show()
        if '--smoke-test' in (sys.argv if argv is None else argv):
            def startup_smoke():
                try:
                    # Imports/config loading exercise lazy packaged dependencies.
                    from DrissionPage import ChromiumOptions
                    import websocket
                    from tiktok_live.wss.direct_signer import self_test
                    from tiktok_live.wss.protocol import decode_push_frame, encode_heartbeat
                    from curl_cffi import requests
                    from tiktok_live.config import BROWSER_ENV_PATH
                    ChromiumOptions()
                    with requests.Session():
                        pass
                    json.loads(BROWSER_ENV_PATH.read_text(encoding='utf-8'))
                    smoke = {
                        'window_visible': window.isVisible(),
                        'bundled_browser': paths.browser_executable is not None,
                        'empty_accounts': not paths.account_store.exists(),
                        'dependencies_loaded': bool(self_test() and websocket.WebSocket
                            and decode_push_frame(encode_heartbeat(42)).room_id == '42'),
                    }
                    (paths.runtime_dir / 'smoke-result.json').write_text(
                        json.dumps(smoke), encoding='utf-8')
                    application.exit(0 if all(smoke.values()) else 1)
                except Exception as exc:
                    logger.error('Desktop smoke failed ({})', type(exc).__name__)
                    application.exit(1)
            QTimer.singleShot(0, startup_smoke)
        result = application.exec()
        # QApplication.exit() bypasses Quit events. Retain the GUI and re-enter
        # its event loop if an external caller used that path during work.
        while not window.shutdown():
            application.exec()
        return result
    except Exception as exc:
        logger.error('Desktop startup failed ({})', type(exc).__name__)
        raise
    finally:
        # Startup can fail after show() has already started work. Apply the same
        # ownership policy to that path before releasing the application window.
        if window is not None:
            while not window.shutdown():
                application.exec()
        sys.excepthook = original_hook
        if gate is not None:
            application.removeEventFilter(gate)
            gate.deleteLater()
        if window is not None:
            application.aboutToQuit.disconnect(window.shutdown)
            window.shutdown_finished.disconnect(shutdown_complete)
            window.close()
            window.deleteLater()
        logger.remove(handler)
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)


if __name__ == '__main__':
    raise SystemExit(main())
