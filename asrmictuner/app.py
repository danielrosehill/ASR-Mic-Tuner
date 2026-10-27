"""Qt application: single instance, window and tray lifecycle.

A second `asr-mic-tuner` launch does not start a second process; it tells the
running one (over a QLocalSocket) to show its window, or — for `--tray` —
to do nothing, so autostart and a menu launch can coexist.
"""

import os
import shutil
import signal
import subprocess
import sys

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QGuiApplication, QIcon
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication

from . import APP, VERSION

SOCKET = f"{APP}-{os.getuid()}"


class Controller:
    def __init__(self, app, start_in_tray):
        from .session import Session
        from .tray import TrayController
        from .watch import QuietWatch
        from .window import MainWindow

        self.app = app
        self.session = Session()
        self.session.rescan()
        self.watch = QuietWatch(self.session)
        self.watch.alert.connect(self.notify)
        # Window first, so its pages are connected before the first
        # device_changed fires.
        self.window = MainWindow(self.session, self)
        dev = self.session.preferred_device()
        if dev:
            self.session.select_device(dev)
        self.window._fill_combo()
        self.tray = None
        if self.session.settings.get("tray_enabled", True) and TrayController.available():
            self.tray = TrayController(self.session, self)
            self.tray.show()
        self.session.settings_changed.connect(self._tray_setting)
        if not start_in_tray or self.tray is None:
            self.show_window("monitor")

    def _tray_setting(self):
        from .tray import TrayController
        want = self.session.settings.get("tray_enabled", True)
        if want and self.tray is None and TrayController.available():
            self.tray = TrayController(self.session, self)
            self.tray.show()
        elif not want and self.tray is not None:
            self.tray.hide()
            self.tray = None

    def notify(self, title, body):
        """Through the tray icon when there is one, else notify-send."""
        if self.tray is not None:
            from PyQt6.QtWidgets import QSystemTrayIcon
            self.tray.icon.showMessage(title, body, QSystemTrayIcon.MessageIcon.Warning, 15000)
        elif shutil.which("notify-send"):
            subprocess.Popen(["notify-send", "-a", "ASR Mic Tuner", "-i", "audio-input-microphone", title, body])

    def tray_active(self):
        return self.tray is not None

    def window_visible(self):
        return self.window.isVisible()

    def show_window(self, tab=None):
        if tab:
            self.window.show_tab(tab)
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def handle_message(self, message):
        if message == "show":
            self.show_window()

    def quit(self):
        self.watch.timer.stop()
        self.window.settings_page.save()
        if self.tray:
            self.tray.hide()
        self.session.release("window")
        self.session.release("popup")
        self.session.release("tray")
        self.session.release("watch")
        self.app.quit()


def run(start_in_tray=False):
    # Tell the running instance, if any, and stop here.
    probe = QLocalSocket()
    probe.connectToServer(SOCKET)
    if probe.waitForConnected(300):
        probe.write(b"tray" if start_in_tray else b"show")
        probe.flush()
        probe.waitForBytesWritten(300)
        probe.disconnectFromServer()
        return 0

    app = QApplication(sys.argv)
    app.setApplicationName(APP)
    app.setApplicationDisplayName("ASR Mic Tuner")
    app.setApplicationVersion(VERSION)
    app.setOrganizationDomain("danielrosehill.com")
    # Becomes the Wayland app id: Plasma matches it to asr-mic-tuner.desktop for the
    # icon and task grouping, and the KWin pop-up rule matches on it too.
    QGuiApplication.setDesktopFileName("asr-mic-tuner")
    app.setWindowIcon(QIcon.fromTheme("audio-input-microphone"))
    app.setQuitOnLastWindowClosed(False)

    QLocalServer.removeServer(SOCKET)
    server = QLocalServer()
    server.listen(SOCKET)
    controller = Controller(app, start_in_tray)

    def on_connection():
        conn = server.nextPendingConnection()
        if conn is None:
            return

        def read():
            controller.handle_message(bytes(conn.readAll()).decode(errors="ignore").strip())
            conn.disconnectFromServer()

        conn.readyRead.connect(read)

    server.newConnection.connect(on_connection)

    # Ctrl+C in a terminal: Python only sees signals between Qt events.
    signal.signal(signal.SIGINT, lambda *_: controller.quit())
    tick = QTimer()
    tick.timeout.connect(lambda: None)
    tick.start(250)
    return app.exec()
