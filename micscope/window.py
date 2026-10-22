"""The main window: device and location header, five tabs, status bar."""


from PyQt6.QtGui import QIcon, QKeySequence, QShortcut
from PyQt6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QMainWindow, QPushButton, QScrollArea,
    QTabWidget, QVBoxLayout, QWidget,
)

from . import VERSION
from .measure import MeasurePage
from .monitor import MonitorPage
from .pages import HistoryPage, ProfilesPage, SettingsPage


def scrolled(widget):
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.setWidget(widget)
    return area


class MainWindow(QMainWindow):
    def __init__(self, session, app_controller):
        super().__init__()
        self.s = session
        self.ctl = app_controller
        self.setWindowTitle("micscope")
        self.setWindowIcon(QIcon.fromTheme("audio-input-microphone"))
        self.resize(940, 780)
        self._filling = False

        central = QWidget()
        root = QVBoxLayout(central)
        header = QHBoxLayout()
        header.addWidget(QLabel("Microphone"))
        self.devices = QComboBox()
        self.devices.currentIndexChanged.connect(self._on_device_combo)
        header.addWidget(self.devices, 3)
        rescan = QPushButton("Rescan")
        rescan.clicked.connect(self.rescan)
        header.addWidget(rescan)
        header.addSpacing(12)
        header.addWidget(QLabel("Location"))
        self.location = QComboBox()
        self.location.setEditable(True)
        self.location.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.location.addItems(self.s.settings.get("locations", []))
        self.location.setCurrentText(self.s.location())
        self.location.lineEdit().setPlaceholderText("e.g. living room")
        self.location.setToolTip("Tagged onto every measurement, so readings from the same room can be compared")
        self.location.lineEdit().editingFinished.connect(self._on_location)
        self.location.activated.connect(lambda _i: self._on_location())
        header.addWidget(self.location, 2)
        root.addLayout(header)

        self.tabs = QTabWidget()
        self.monitor = MonitorPage(session)
        self.measure = MeasurePage(session)
        self.history = HistoryPage(session)
        self.profiles = ProfilesPage(session)
        self.settings_page = SettingsPage(session)
        self.tabs.addTab(scrolled(self.monitor), QIcon.fromTheme("audio-input-microphone"), "Monitor")
        self.tabs.addTab(scrolled(self.measure), QIcon.fromTheme("media-record"), "Measure")
        self.tabs.addTab(self.history, QIcon.fromTheme("view-history"), "History")
        self.tabs.addTab(scrolled(self.profiles), QIcon.fromTheme("bookmarks"), "Profiles")
        self.tabs.addTab(scrolled(self.settings_page), QIcon.fromTheme("configure"), "Settings")
        root.addWidget(self.tabs, 1)
        self.setCentralWidget(central)
        self.statusBar().showMessage(f"micscope {VERSION}")

        self.s.status.connect(lambda msg: self.statusBar().showMessage(msg, 8000))
        self.s.device_changed.connect(self._sync_combo)
        QShortcut(QKeySequence("Ctrl+Q"), self, activated=self.ctl.quit)
        QShortcut(QKeySequence("Ctrl+W"), self, activated=self.close)

    # -- devices --
    def rescan(self):
        self.s.rescan()
        self._fill_combo()
        if self.s.device is None or self.s.device.name not in [d.name for d in self.s.devices]:
            dev = self.s.preferred_device()
            if dev:
                self.s.select_device(dev)
        else:
            current = next(d for d in self.s.devices if d.name == self.s.device.name)
            self.s.select_device(current)

    def _fill_combo(self):
        self._filling = True
        self.devices.clear()
        for d in self.s.devices:
            self.devices.addItem(QIcon.fromTheme("audio-card" if d.is_hardware else "audio-input-microphone"),
                                 d.label, d.name)
        self._filling = False
        self._sync_combo()

    def _sync_combo(self):
        if self.s.device is None:
            return
        idx = self.devices.findData(self.s.device.name)
        if idx >= 0 and idx != self.devices.currentIndex():
            self._filling = True
            self.devices.setCurrentIndex(idx)
            self._filling = False

    def _on_device_combo(self, idx):
        if self._filling or idx < 0 or idx >= len(self.s.devices):
            return
        name = self.devices.itemData(idx)
        dev = next((d for d in self.s.devices if d.name == name), None)
        if dev and (self.s.device is None or dev.name != self.s.device.name):
            self.s.select_device(dev)

    def _on_location(self):
        text = self.location.currentText().strip()
        if text != self.s.location():
            self.s.set_location(text)
            if text and self.location.findText(text) < 0:
                self.location.insertItem(0, text)

    # -- visibility owns the capture stream --
    def showEvent(self, event):
        super().showEvent(event)
        if not self.s.devices:
            self.rescan()
        self.s.acquire("window")

    def hideEvent(self, event):
        super().hideEvent(event)
        self.settings_page.save()
        self.s.release("window")

    def closeEvent(self, event):
        if self.monitor.calibration:
            self.monitor._end_calibration("Cancelled.")
        if self.measure.recording:
            self.measure._cancel("Cancelled.")
        if self.ctl.tray_active() and self.s.settings.get("close_to_tray", True):
            event.ignore()
            self.hide()
            return
        event.accept()
        self.ctl.quit()

    def show_tab(self, name):
        index = {"monitor": 0, "measure": 1, "history": 2, "profiles": 3, "settings": 4}.get(name, 0)
        self.tabs.setCurrentIndex(index)
