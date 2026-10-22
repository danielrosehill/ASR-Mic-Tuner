"""Qt application: single instance, window and tray lifecycle.

A second `micscope` launch does not start a second process; it tells the
running one (over a QLocalSocket) to show its window, or — for `--tray` —
to do nothing, so autostart and a menu launch can coexist.
"""

import os
import signal
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
        from .window import MainWindow

        self.app = app
        self.session = Session()
        self.session.rescan()
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
        self.window.settings_page.save()
        if self.tray:
            self.tray.hide()
        self.session.release("window")
        self.session.release("popup")
        self.session.release("tray")
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
    app.setApplicationDisplayName("micscope")
    app.setApplicationVersion(VERSION)
    app.setOrganizationDomain("danielrosehill.com")
    # Becomes the Wayland app id: Plasma matches it to micscope.desktop for the
    # icon and task grouping, and the KWin pop-up rule matches on it too.
    QGuiApplication.setDesktopFileName("micscope")
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
