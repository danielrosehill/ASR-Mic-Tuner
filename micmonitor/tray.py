"""System tray: a status icon, a quick pop-up, and a context menu.

Without the opt-in live meter, the tray never opens the microphone. It polls
ALSA (cheap, silent) for the gain zone and for drift — wireplumber moving
the analog gain behind your back — and says so in the icon and a
notification. Left-click opens the pop-up, which listens only while shown.
"""

from PyQt6.QtCore import QObject, Qt, QTimer
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QMenu, QPushButton, QSystemTrayIcon,
    QVBoxLayout, QWidget,
)

from .core import METER_MIN_DB, fmt_gain, zone_for_db
from . import kwin
from .kwin import POPUP_TITLE
from .monitor import live_verdict
from .qtutil import wrap_label
from .widgets import ZONE_HEX, GainSlider, LevelMeter, tray_icon

POLL_MS = 4000
POPUP_W, POPUP_H = 400, 360


class TrayPopup(QWidget):
    def __init__(self, session, controller):
        super().__init__(None, Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
                         | Qt.WindowType.WindowStaysOnTopHint)
        self.s = session
        self.ctl = controller
        self.setWindowTitle(POPUP_TITLE)
        self.setFixedSize(POPUP_W, POPUP_H)
        self.setAutoFillBackground(True)
        self._setting = False
        self._was_active = False
        self._fix = None

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 12, 14, 12)
        top = QHBoxLayout()
        title = QLabel("<b>Mic Monitor</b>")
        top.addWidget(title)
        top.addStretch(1)
        close = QPushButton("✕")
        close.setFlat(True)
        close.setFixedWidth(28)
        close.clicked.connect(self.hide)
        top.addWidget(close)
        root.addLayout(top)

        self.devices = QComboBox()
        self.devices.currentIndexChanged.connect(self._on_device_combo)
        root.addWidget(self.devices)
        self.gain = wrap_label()
        root.addWidget(self.gain)
        row = QHBoxLayout()
        minus, plus = QPushButton("−"), QPushButton("+")
        for b, d in ((minus, -1), (plus, 1)):
            b.setFixedWidth(32)
            b.setAutoRepeat(True)
            b.clicked.connect(lambda _c=False, d=d: self.s.step(d))
        self.slider = GainSlider()
        self.slider.valueChanged.connect(self._on_slider)
        row.addWidget(minus)
        row.addWidget(self.slider, 1)
        row.addWidget(plus)
        root.addLayout(row)
        self.meter = LevelMeter(compact=True)
        root.addWidget(self.meter)
        vrow = QHBoxLayout()
        self.verdict = wrap_label()
        vrow.addWidget(self.verdict, 1)
        self.fix = QPushButton("Fix")
        self.fix.clicked.connect(self._on_fix)
        self.fix.hide()
        vrow.addWidget(self.fix)
        root.addLayout(vrow)
        self.clip = wrap_label()
        root.addWidget(self.clip)

        prow = QHBoxLayout()
        self.presets = QComboBox()
        prow.addWidget(self.presets, 1)
        apply = QPushButton("Apply preset")
        apply.clicked.connect(lambda: self.presets.currentData() and self.s.apply_preset(self.presets.currentData()))
        prow.addWidget(apply)
        root.addLayout(prow)
        brow = QHBoxLayout()
        for label, tab in (("Open Mic Monitor", "monitor"), ("Measure…", "measure")):
            b = QPushButton(label)
            b.clicked.connect(lambda _c=False, tab=tab: (self.hide(), self.ctl.show_window(tab)))
            brow.addWidget(b)
        root.addLayout(brow)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        for signal in (self.s.device_changed, self.s.gain_changed, self.s.presets_changed):
            signal.connect(self.refresh)

    # -- show / hide own the capture stream --
    def showEvent(self, event):
        super().showEvent(event)
        self._was_active = False
        self.refresh()
        self.s.acquire("popup")
        self.timer.start(50)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.timer.stop()
        self.s.release("popup")

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == event.Type.ActivationChange:
            if self.isActiveWindow():
                self._was_active = True
            elif self._was_active:
                # Clicked elsewhere: behave like a Plasma applet pop-up.
                self.hide()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)

    def refresh(self):
        s = self.s
        self._setting = True
        try:
            self.devices.blockSignals(True)
            self.devices.clear()
            for d in s.devices:
                self.devices.addItem(d.description, d.name)
            if s.device:
                self.devices.setCurrentIndex(max(0, self.devices.findData(s.device.name)))
            self.devices.blockSignals(False)
            if s.device:
                self.meter.configure(s.device.channel_labels())
            if s.gain is not None and s.curve is not None:
                db = s.gain_db()
                zone = zone_for_db(db)
                self.slider.setRange(s.gain.imin, s.gain.imax)
                self.slider.set_curve(s.curve)
                self.slider.setValue(s.gain.index)
                self.slider.setEnabled(True)
                drift = s.drift()
                self.gain.setText(
                    f"<span style='font-size:17pt;color:{ZONE_HEX[zone]}'><b>{fmt_gain(db)}</b></span>"
                    f" <small>analog · index {s.gain.index}/{s.gain.imax} · {zone}</small>"
                    + (f"<br><span style='color:#d9a340'>Drifted from remembered index {drift[0]}</span>"
                       if drift else ""))
            elif s.device is not None and s.device.pw_percent is not None:
                self.slider.set_curve(None)
                self.slider.setRange(0, 150)
                self.slider.setValue(s.device.pw_percent)
                self.slider.setEnabled(True)
                self.gain.setText(f"<span style='font-size:17pt'><b>{s.device.pw_percent}%</b></span>"
                                  " <small>software volume — no analog gain</small>")
            else:
                self.slider.setEnabled(False)
                self.gain.setText("No adjustable gain")
            self.presets.clear()
            default = s.default_preset_label()
            for label in sorted(s.presets()):
                self.presets.addItem(("★ " if label == default else "") + label, label)
            if not s.presets():
                self.presets.addItem("No presets — save one in Profiles", None)
        finally:
            self._setting = False

    def _on_device_combo(self, idx):
        name = self.devices.itemData(idx)
        dev = next((d for d in self.s.devices if d.name == name), None)
        if dev and (self.s.device is None or dev.name != self.s.device.name):
            self.s.select_device(dev)

    def _on_slider(self, value):
        if self._setting:
            return
        if self.s.gain is not None:
            if value != self.s.gain.index:
                self.s.set_index(value)
        else:
            self.s.set_soft(value)

    def _on_fix(self):
        if self._fix:
            (self.s.set_index if self.s.gain is not None else self.s.set_soft)(self._fix[0])

    def _tick(self):
        a = self.s.analyser
        if a is None:
            self.verdict.setText("<small>Not listening</small>")
            return
        data, duration = a.snapshot()
        self.meter.set_data(data)
        headline, colour, detail, fix = live_verdict(self.s, data)
        style = f" style='color:{colour}'" if colour else ""
        self.verdict.setText(f"<b{style}>{headline}</b><br><small>{detail}</small>")
        self._fix = fix
        self.fix.setVisible(bool(fix))
        runs = sum(s["runs"] for s in data)
        self.clip.setText(
            f"<span style='color:#d43333'><b>Clipping:</b> {runs} runs in {duration:.0f} s</span>"
            if runs else f"<small><span style='color:#2ea44f'>No clipping</span> · {duration:.0f} s</small>")


class TrayController(QObject):
    def __init__(self, session, app_controller):
        super().__init__()
        self.s = session
        self.ctl = app_controller
        self.icon = QSystemTrayIcon(tray_icon("idle"))
        self.icon.setToolTip("Mic Monitor")
        self.icon.activated.connect(self._activated)
        self.popup = TrayPopup(session, app_controller)
        self.menu = QMenu()
        self._build_menu()
        self.icon.setContextMenu(self.menu)
        self._state = None
        self._notified_drift = None
        self._live = False

        self.poll = QTimer(self)
        self.poll.timeout.connect(self.update_state)
        self.poll.start(POLL_MS)
        self.live_timer = QTimer(self)
        self.live_timer.timeout.connect(self._live_tick)
        for signal in (self.s.device_changed, self.s.gain_changed):
            signal.connect(self.update_state)
        self.s.presets_changed.connect(self._build_menu)
        self.s.device_changed.connect(self._build_menu)
        self.s.settings_changed.connect(self._apply_settings)

    @staticmethod
    def available():
        return QSystemTrayIcon.isSystemTrayAvailable()

    def show(self):
        if self.s.settings.get("pin_popup", True) and kwin.available() and not kwin.rule_installed():
            kwin.install_rule(POPUP_W, POPUP_H)
        self.icon.show()
        self._apply_settings()
        self.update_state()

    def hide(self):
        self.icon.hide()
        self.popup.hide()
        self._set_live(False)

    def _build_menu(self):
        self.menu.clear()
        self.menu.addAction(QIcon.fromTheme("audio-input-microphone"), "Open Mic Monitor",
                            lambda: self.ctl.show_window("monitor"))
        self.menu.addAction(QIcon.fromTheme("media-record"), "Measure room or voice…",
                            lambda: self.ctl.show_window("measure"))
        presets = self.s.presets() if self.s.device else {}
        sub = self.menu.addMenu(QIcon.fromTheme("bookmarks"), "Gain presets")
        default = self.s.default_preset_label() if self.s.device else None
        if presets:
            for label in sorted(presets):
                act = sub.addAction(("★ " if label == default else "") + label)
                act.triggered.connect(lambda _c=False, label=label: self.s.apply_preset(label))
        else:
            sub.addAction("None saved").setEnabled(False)
        self.restore_action = self.menu.addAction(QIcon.fromTheme("edit-undo"), "Restore remembered gain",
                                                  self.s.restore_remembered)
        self.restore_action.setEnabled(bool(self.s.drift()) if self.s.device else False)
        self.menu.addSeparator()
        self.menu.addAction(QIcon.fromTheme("application-exit"), "Quit", self.ctl.quit)

    def _activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            if self.popup.isVisible():
                self.popup.hide()
            else:
                self.popup.show()
                self.popup.raise_()
                self.popup.activateWindow()
        elif reason == QSystemTrayIcon.ActivationReason.MiddleClick:
            self.ctl.show_window("monitor")

    def _apply_settings(self):
        self._set_live(bool(self.s.settings.get("tray_live_meter")))

    def _set_live(self, on):
        if on == self._live:
            return
        self._live = on
        if on:
            self.s.acquire("tray")
            self.live_timer.start(250)
        else:
            self.live_timer.stop()
            self.s.release("tray")
            self.update_state()

    # -- state --
    def update_state(self):
        s = self.s
        if not s.device:
            self._set_icon("idle", "Mic Monitor — no capture device")
            return
        if not (self.ctl.window_visible() or self.popup.isVisible()):
            s.refresh_controls()            # cheap: amixer only, no capture
        drift = s.drift()
        if drift and drift != self._notified_drift:
            self._notified_drift = drift
            if s.settings.get("auto_restore"):
                s.restore_remembered()
                self.icon.showMessage("Mic Monitor", f"Something moved the mic gain to index {drift[1]}; "
                                      f"put it back to {drift[0]}.", QSystemTrayIcon.MessageIcon.Information, 6000)
            else:
                self.icon.showMessage("Mic Monitor", f"Mic gain changed behind your back: index {drift[1]}, "
                                      f"remembered {drift[0]}. Right-click the tray icon to restore.",
                                      QSystemTrayIcon.MessageIcon.Warning, 8000)
        elif not drift:
            self._notified_drift = None
        if hasattr(self, "restore_action"):
            self.restore_action.setEnabled(bool(s.drift()))
        if self._live:
            return                          # the live tick owns the icon
        db = s.gain_db()
        if db is not None:
            zone = zone_for_db(db)
            state = "drift" if s.drift() else ("safe" if zone in ("safe", "low") else zone)
            tip = f"Mic Monitor — {s.device.description}\nAnalog gain {fmt_gain(db)} ({zone})"
            if s.drift():
                tip += "\nGain has drifted from the remembered value"
        elif s.device.pw_percent is not None:
            state = "idle"
            tip = f"Mic Monitor — {s.device.description}\nSoftware volume {s.device.pw_percent}%"
        else:
            state, tip = "idle", f"Mic Monitor — {s.device.description}"
        self._set_icon(state, tip)

    def _set_icon(self, state, tip, level=None):
        key = (state, None if level is None else round(level * 8))
        if key != self._state:
            self._state = key
            self.icon.setIcon(tray_icon(state, None if level is None else round(level * 8) / 8))
        self.icon.setToolTip(tip)

    def _live_tick(self):
        a = self.s.analyser
        if a is None:
            return
        data, _ = a.snapshot()
        hold = max((d["hold"] for d in data), default=METER_MIN_DB)
        level = max(0.0, min(1.0, (hold - METER_MIN_DB) / -METER_MIN_DB))
        runs = sum(d["runs"] for d in data)
        headline, _c, detail, _fix = live_verdict(self.s, data)
        if runs:
            state = "clip"
        elif headline.startswith("Good"):
            state = "safe"
        elif headline.startswith("Too loud"):
            state = "caution"
        elif headline.startswith("Too quiet"):
            state = "low"
        else:
            state = "idle"
        self._set_icon(state, f"Mic Monitor — {headline}\n{detail}", level)
