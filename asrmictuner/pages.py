"""History, Profiles and Settings tabs."""

import json
import shlex
from pathlib import Path

from PyQt6.QtCore import QUrl, Qt
from PyQt6.QtGui import QDesktopServices, QGuiApplication
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

from . import ai, autostart, kwin, store
from .core import TARGET_LO_DB, fmt_gain, fmt_level
from .widgets import TRAY_STYLE_DEFAULT, TRAY_STYLES, tray_icon
from .qtutil import (
    ask_ai, ask_text, ai_html, esc, findings_html, key_result, metrics_html, run_async,
    wrap_label,
)

KINDS = [("All kinds", None), ("Room tone", "room"), ("Voice", "voice"),
         ("Calibration", "calibration"), ("Logged reading", "snapshot")]


def _item(text, data=None):
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    if data is not None:
        item.setData(Qt.ItemDataRole.UserRole, data)
    return item


def _table(headers):
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.verticalHeader().setVisible(False)
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    t.setAlternatingRowColors(True)
    return t


def _when(iso):
    return (iso or "")[:16].replace("T", " ")


# ---------------------------------------------------------------------------

class HistoryPage(QWidget):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.s = session
        self.rows = []
        root = QVBoxLayout(self)
        frow = QHBoxLayout()
        self.scope = QComboBox()
        self.scope.addItems(["This device", "All devices"])
        self.kind = QComboBox()
        for label, kind in KINDS:
            self.kind.addItem(label, kind)
        for w in (self.scope, self.kind):
            w.currentIndexChanged.connect(self.reload)
            frow.addWidget(w)
        frow.addStretch(1)
        export = QPushButton("Export JSON…")
        export.clicked.connect(self._export)
        frow.addWidget(export)
        root.addLayout(frow)

        split = QSplitter(Qt.Orientation.Vertical)
        self.table = _table(["When", "Kind", "Device", "Location", "Gain", "Result", "Notes"])
        self.table.itemSelectionChanged.connect(self._selected)
        split.addWidget(self.table)

        detail = QWidget()
        dl = QVBoxLayout(detail)
        self.summary = QTextBrowser()
        dl.addWidget(self.summary, 2)
        dl.addWidget(QLabel("Notes"))
        self.notes = QPlainTextEdit()
        self.notes.setMaximumHeight(70)
        dl.addWidget(self.notes)
        brow = QHBoxLayout()
        self.buttons = {}
        for key, label in (("save", "Save notes"), ("play", "Play sample"), ("ai", "Ask AI"),
                           ("profile", "Make profile…"), ("folder", "Show file"),
                           ("delete", "Delete")):
            b = QPushButton(label)
            b.clicked.connect(getattr(self, f"_on_{key}"))
            brow.addWidget(b)
            self.buttons[key] = b
        brow.addStretch(1)
        dl.addLayout(brow)
        split.addWidget(detail)
        split.setSizes([300, 320])
        root.addWidget(split)

        self.s.history_changed.connect(self.reload)
        self.s.device_changed.connect(self.reload)
        self.reload()

    def current(self):
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        if not rows:
            return None
        mid = self.table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole)
        return self.s.store.measurement(mid)

    def reload(self):
        keep = self.current()
        device = self.s.device.name if (self.scope.currentIndex() == 0 and self.s.device) else None
        self.rows = self.s.store.measurements(device=device, kind=self.kind.currentData())
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.rows))
        select = None
        for r, m in enumerate(self.rows):
            values = [_when(m["created"]), m["kind"], m.get("device_label") or m["device"],
                      m.get("location") or "", fmt_gain(m.get("gain_db")) if m.get("gain_db") is not None
                      else (f"{m['soft_percent']}% sw" if m.get("soft_percent") is not None else "—"),
                      key_result(m), (m.get("notes") or "").replace("\n", " ")[:60]]
            for c, v in enumerate(values):
                self.table.setItem(r, c, _item(v, m["id"] if c == 0 else None))
            if keep and m["id"] == keep["id"]:
                select = r
        if select is None and self.rows:
            select = 0
        if select is not None:
            self.table.selectRow(select)
        else:
            self.summary.setHtml("<p><i>No measurements yet. Record room tone or a voice sample "
                                 "on the Measure tab, calibrate, or log a reading.</i></p>")
        self._selected()

    def _selected(self):
        m = self.current()
        for b in self.buttons.values():
            b.setEnabled(m is not None)
        if not m:
            return
        self.buttons["play"].setEnabled(bool(m.get("sample_path")))
        self.buttons["folder"].setEnabled(bool(m.get("sample_path")))
        self.buttons["profile"].setEnabled(m["kind"] in ("room", "voice"))
        metrics = m.get("metrics") or {}
        head = (f"<h3>{esc(m['kind'].capitalize())} — {esc(_when(m['created']))}</h3>"
                f"<p><b>{esc(m.get('device_label') or m['device'])}</b> · gain "
                f"{fmt_gain(m.get('gain_db'))}"
                + (f" (index {m['gain_index']})" if m.get("gain_index") is not None else "")
                + (f" · {esc(m['location'])}" if m.get("location") else "") + "</p>")
        score = (metrics.get("readiness") or {}).get("score")
        if score is not None:
            head += f"<p>ASR readiness <b>{score}</b> ({esc(metrics['readiness'].get('grade'))})</p>"
        if metrics.get("character"):
            head += f"<p><b>{esc(metrics['character'].capitalize())}.</b></p>"
        body = findings_html(metrics.get("findings")) + metrics_html(metrics)
        if m.get("noiseprof"):
            head += (f"<p><small>Noise profile: {esc(m['noiseprof'])}</small></p>")
        if m.get("ai"):
            body += "<hr><h4>AI review</h4>" + ai_html(m["ai"])
        self.summary.setHtml(head + body)
        self.notes.setPlainText(m.get("notes") or "")

    def _on_save(self):
        m = self.current()
        if m:
            self.s.store.update_measurement(m["id"], notes=self.notes.toPlainText())
            self.reload()

    def _on_play(self):
        m = self.current()
        if m:
            store.play(m.get("sample_path"))

    def _on_ai(self):
        m = self.current()
        if m:
            ask_ai(self.s, m, self.summary, self.buttons["ai"])

    def _on_profile(self):
        m = self.current()
        if not m:
            return
        name = ask_text(self, "Save profile", f"Name for this {m['kind']} profile:",
                        m.get("location") or "")
        if name:
            pid = self.s.store.add_profile(m["kind"], name, m["device"], m["id"], m.get("notes") or "")
            if m["kind"] == "room" and self.s.device and m["device"] == self.s.device.name:
                self.s.set_active_room_profile(pid)
            else:
                self.s.profiles_changed.emit()

    def _on_folder(self):
        m = self.current()
        if m and m.get("sample_path"):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(m["sample_path"]).parent)))

    def _on_delete(self):
        m = self.current()
        if not m:
            return
        answer = QMessageBox.question(
            self, "Delete measurement",
            f"Delete this {m['kind']} measurement from {_when(m['created'])}"
            + (" and its sample file" if m.get("sample_path") else "") + "?")
        if answer == QMessageBox.StandardButton.Yes:
            self.s.store.delete_measurement(m["id"])
            self.s.profiles_changed.emit()
            self.reload()

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export history", str(Path.home() / "asr-mic-tuner-history.json"),
                                              "JSON (*.json)")
        if path:
            Path(path).write_text(json.dumps(self.s.store.export(), indent=2, default=str))
            self.s.status.emit(f"Exported to {path}")


# ---------------------------------------------------------------------------

class ProfilesPage(QWidget):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.s = session
        root = QVBoxLayout(self)

        presets = QGroupBox("Gain presets for this microphone")
        pl = QVBoxLayout(presets)
        pl.addWidget(wrap_label(
            "<small>Named gain settings for this device — one per room or use. The ★ preset is "
            "the remembered gain: it is what <i>Restore</i>, the tray and the login unit reapply.</small>"))
        self.preset_list = QListWidget()
        self.preset_list.setMaximumHeight(150)
        pl.addWidget(self.preset_list)
        row = QHBoxLayout()
        for label, fn in (("Save current gain as preset…", self._save_preset), ("Apply", self._apply_preset),
                          ("Make default ★", self._default_preset), ("Delete", self._delete_preset)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        pl.addLayout(row)
        self.remembered = wrap_label()
        pl.addWidget(self.remembered)
        root.addWidget(presets)

        rooms = QGroupBox("Room noise profiles")
        rl = QVBoxLayout(rooms)
        rl.addWidget(wrap_label(
            "<small>The active room profile for a device is the noise reference for voice SNR and "
            "is overlaid on the live spectrum. Each keeps its room-tone sample and a sox noise "
            "profile for cleanup.</small>"))
        self.room_table = _table(["Name", "Device", "Created", "Noise floor", "Hum", "Character", "Active"])
        rl.addWidget(self.room_table)
        row = QHBoxLayout()
        for label, fn in (("Set active for this device", self._set_active), ("Clear active", self._clear_active),
                          ("Play", lambda: self._play(self.room_table)),
                          ("Copy cleanup command", self._copy_cleanup),
                          ("Delete", lambda: self._delete(self.room_table))):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        rl.addLayout(row)
        root.addWidget(rooms)

        voices = QGroupBox("Speaker profiles (a mic test of one person on one microphone)")
        vl = QVBoxLayout(voices)
        self.voice_table = _table(["Name", "Device", "Created", "Gain", "Speech peak", "SNR", "F0", "Score"])
        vl.addWidget(self.voice_table)
        row = QHBoxLayout()
        for label, fn in (("Play", lambda: self._play(self.voice_table)),
                          ("Build personal chain", self._personal_chain),
                          ("Delete", lambda: self._delete(self.voice_table))):
            b = QPushButton(label)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        vl.addLayout(row)
        root.addWidget(voices)

        for signal in (self.s.presets_changed, self.s.device_changed, self.s.profiles_changed,
                       self.s.gain_changed):
            signal.connect(self.reload)
        self.s.history_changed.connect(self._reload_profiles)
        self.reload()

    def reload(self):
        self._reload_presets()
        self._reload_profiles()

    def _reload_presets(self):
        self.preset_list.clear()
        default = self.s.default_preset_label()
        for label, entry in sorted(self.s.presets().items()):
            if "index" in entry:
                value = f"{fmt_gain(entry.get('db'))} (index {entry['index']})"
            else:
                value = f"{entry.get('soft_percent')}% software volume"
            star = "★ " if label == default else "    "
            notes = f" — {entry['notes']}" if entry.get("notes") else ""
            item = QListWidgetItem(f"{star}{label}: {value}{notes}")
            item.setData(Qt.ItemDataRole.UserRole, label)
            self.preset_list.addItem(item)
        saved = self.s.remembered()
        self.remembered.setText(
            f"<small>Remembered gain: index {saved['index']} ({fmt_gain(saved.get('db'))})</small>"
            if saved else "<small>No remembered gain for this device.</small>")

    def _reload_profiles(self):
        active = self.s.active_room_profile()
        active_id = active["id"] if active else None
        rooms = self.s.store.profiles(kind="room")
        self.room_table.setRowCount(len(rooms))
        for r, p in enumerate(rooms):
            m = p.get("metrics") or {}
            hum = m.get("hum") or {}
            values = [p["name"], p["device"].replace("alsa_input.", ""), _when(p["created"]),
                      fmt_level(m.get("noise_floor_p10_dbfs")),
                      f"{hum.get('strongest_hz', 0):.0f} Hz" if hum.get("present") else "—",
                      m.get("character", ""), "●" if p["id"] == active_id else ""]
            for c, v in enumerate(values):
                self.room_table.setItem(r, c, _item(v, p["id"] if c == 0 else None))
        voices = self.s.store.profiles(kind="voice")
        self.voice_table.setRowCount(len(voices))
        for r, p in enumerate(voices):
            m = p.get("metrics") or {}
            values = [p["name"], p["device"].replace("alsa_input.", ""), _when(p["created"]),
                      fmt_gain(p.get("gain_db")), fmt_level(m.get("speech_peak_p95_dbfs")),
                      f"{m['snr_db']:.0f} dB" if m.get("snr_db") is not None else "—",
                      f"{m['f0_median_hz']:.0f} Hz" if m.get("f0_median_hz") else "—",
                      str((m.get("readiness") or {}).get("score", "—"))]
            for c, v in enumerate(values):
                self.voice_table.setItem(r, c, _item(v, p["id"] if c == 0 else None))

    def _selected_preset(self):
        item = self.preset_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _save_preset(self):
        name = ask_text(self, "Save preset", "Preset name (e.g. the room):", self.s.location())
        if name:
            self.s.save_preset(name)

    def _apply_preset(self):
        label = self._selected_preset()
        if label:
            self.s.apply_preset(label)

    def _default_preset(self):
        label = self._selected_preset()
        if label:
            self.s.make_preset_default(label)

    def _delete_preset(self):
        label = self._selected_preset()
        if label:
            self.s.delete_preset(label)

    def _selected_profile(self, table):
        rows = table.selectionModel().selectedRows()
        if not rows:
            return None
        return self.s.store.profile(table.item(rows[0].row(), 0).data(Qt.ItemDataRole.UserRole))

    def _personal_chain(self):
        p = self._selected_profile(self.voice_table)
        if not p:
            return
        from .engine import personal_chain
        room = None
        if self.s.device and p["device"] == self.s.device.name:
            room = (self.s.active_room_profile() or {}).get("metrics")
        chain, notes = personal_chain(p.get("metrics") or {}, room)
        self.s.open_chain.emit(chain, f"ASR Personal ({p['name']})", notes)

    def _set_active(self):
        p = self._selected_profile(self.room_table)
        if p:
            self.s.set_active_room_profile(p["id"])

    def _clear_active(self):
        self.s.set_active_room_profile(None)

    def _play(self, table):
        p = self._selected_profile(table)
        if p:
            store.play(p.get("sample_path"))

    def _copy_cleanup(self):
        p = self._selected_profile(self.room_table)
        if not p or not p.get("noiseprof"):
            self.s.status.emit("This profile has no sox noise profile (is sox installed?).")
            return
        cmd = f"sox input.wav output.wav noisered {shlex.quote(p['noiseprof'])} 0.21"
        QGuiApplication.clipboard().setText(cmd)
        self.s.status.emit(f"Copied: {cmd}")

    def _delete(self, table):
        p = self._selected_profile(table)
        if p and QMessageBox.question(self, "Delete profile",
                                      f"Delete profile “{p['name']}”? The measurement stays in History."
                                      ) == QMessageBox.StandardButton.Yes:
            self.s.store.delete_profile(p["id"])
            self.s.profiles_changed.emit()


# ---------------------------------------------------------------------------

MODEL_SUGGESTIONS = [
    "google/gemini-3.8-flash",
    "google/gemini-3.5-flash-lite",
    "google/gemini-3.1-pro-preview",
    "anthropic/claude-sonnet-5.5",
    "anthropic/claude-haiku-5.5",
]


class SettingsPage(QWidget):
    def __init__(self, session, watch=None, parent=None):
        super().__init__(parent)
        self.s = session
        self.watch = watch
        st = self.s.settings
        root = QVBoxLayout(self)

        aibox = QGroupBox("AI review (OpenRouter)")
        form = QFormLayout(aibox)
        self.key = QLineEdit(st.get("openrouter_api_key", ""))
        self.key.setEchoMode(QLineEdit.EchoMode.Password)
        self.key.setPlaceholderText("sk-or-v1-…  (or set OPENROUTER_API_KEY)")
        form.addRow("API key", self.key)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.addItems(MODEL_SUGGESTIONS)
        self.model.setCurrentText(st.get("openrouter_model", MODEL_SUGGESTIONS[0]))
        form.addRow("Model", self.model)
        self.audio = QCheckBox("Send the recording as well as the numbers (needs an audio model, e.g. Gemini)")
        self.audio.setChecked(bool(st.get("ai_include_audio")))
        form.addRow("", self.audio)
        row = QHBoxLayout()
        test = QPushButton("Save and test key")
        test.clicked.connect(self._test)
        row.addWidget(test)
        self.key_state = wrap_label()
        row.addWidget(self.key_state, 1)
        form.addRow("", row)
        root.addWidget(aibox)

        traybox = QGroupBox("Tray and session")
        tl = QVBoxLayout(traybox)
        self.tray_enabled = QCheckBox("Show ASR Mic Tuner in the system tray")
        self.close_to_tray = QCheckBox("Closing the window keeps ASR Mic Tuner in the tray")
        self.live = QCheckBox("Live level in the tray icon (keeps the microphone open — "
                              "Plasma will show it as in use)")
        self.pin = QCheckBox("Open the tray pop-up at the panel corner (adds a KWin window rule)")
        self.autostart = QCheckBox("Start in the tray at login")
        self.auto_restore = QCheckBox("Put the remembered gain back automatically when something moves it")
        for box, key in ((self.tray_enabled, "tray_enabled"), (self.close_to_tray, "close_to_tray"),
                         (self.live, "tray_live_meter"), (self.auto_restore, "auto_restore")):
            box.setChecked(bool(st.get(key)))
            box.toggled.connect(lambda on, key=key: self._set(key, on))
            tl.addWidget(box)
        row = QHBoxLayout()
        row.addWidget(QLabel("Tray icon"))
        self.icon_style = QComboBox()
        for key, label in TRAY_STYLES:
            self.icon_style.addItem(tray_icon("safe", style=key), label, key)
        self.icon_style.setCurrentIndex(max(0, self.icon_style.findData(
            st.get("tray_icon_style", TRAY_STYLE_DEFAULT))))
        self.icon_style.currentIndexChanged.connect(
            lambda _i: self._set("tray_icon_style", self.icon_style.currentData()))
        row.addWidget(self.icon_style)
        row.addStretch(1)
        tl.addLayout(row)
        self.pin.setChecked(kwin.rule_installed())
        self.pin.toggled.connect(self._pin)
        tl.addWidget(self.pin)
        self.autostart.setChecked(autostart.enabled())
        self.autostart.toggled.connect(lambda on: autostart.set_enabled(on))
        tl.addWidget(self.autostart)
        root.addWidget(traybox)

        quietbox = QGroupBox("While you dictate")
        ql = QFormLayout(quietbox)
        self.conservative = QCheckBox("Conservative gain: leave room for sudden loud sounds")
        self.conservative.setToolTip(
            f"Recommendations, calibration and auto gain aim speech peaks at {TARGET_LO_DB:.0f} dBFS instead of "
            "−12, so a shout or a crying baby has 18 dB before clipping instead of 12. Auto gain also raises "
            "in 1.5 dB steps instead of 3. Lowering is unchanged.")
        self.conservative.setChecked(bool(st.get("conservative_gain", False)))
        self.conservative.toggled.connect(lambda on: self._set("conservative_gain", on))
        ql.addRow("", self.conservative)
        self.autogain = QCheckBox("ASR auto gain: adjust the analog gain slowly towards the target")
        self.autogain.setToolTip("Judges 30 s of recording at a time, moves at most 3 dB, only in pauses, "
                                 "and never above the ceiling. Steps down at once on clipping.")
        self.autogain.setChecked(bool(st.get("asr_autogain", True)))
        self.autogain.toggled.connect(lambda on: self._set("asr_autogain", on))
        ql.addRow("", self.autogain)
        self.ceiling = QSpinBox()
        self.ceiling.setRange(0, 40)
        self.ceiling.setSuffix(" dB analog gain")
        self.ceiling.setValue(int(st.get("asr_autogain_max_db", 15)))
        self.ceiling.valueChanged.connect(lambda v: self._set("asr_autogain_max_db", v))
        ql.addRow("Auto gain ceiling", self.ceiling)
        self.quiet = QCheckBox("Notify me when dictation runs too quiet")
        self.quiet.setChecked(bool(st.get("quiet_alert", True)))
        self.quiet.toggled.connect(lambda on: self._set("quiet_alert", on))
        ql.addRow("", self.quiet)
        self.quiet_window = QSpinBox()
        self.quiet_window.setRange(1, 15)
        self.quiet_window.setSuffix(" min of recording")
        self.quiet_window.setValue(int(st.get("quiet_window_min", 3)))
        self.quiet_window.valueChanged.connect(lambda v: self._set("quiet_window_min", v))
        ql.addRow("Judge over", self.quiet_window)
        self.quiet_margin = QSpinBox()
        self.quiet_margin.setRange(3, 20)
        self.quiet_margin.setSuffix(f" dB under the {TARGET_LO_DB:.0f} dBFS target")
        self.quiet_margin.setValue(int(st.get("quiet_margin_db", 6)))
        self.quiet_margin.valueChanged.connect(lambda v: self._set("quiet_margin_db", v))
        ql.addRow("Alert at", self.quiet_margin)
        ql.addRow(wrap_label(
            "<small>Both listen only while another application is recording from this microphone, and count "
            "only speech, so pauses and silence never trigger them. With auto gain on, the alert fires only "
            "once auto gain has reached its ceiling. At most one alert per half hour.</small>"))
        self.quiet_state = wrap_label()
        ql.addRow("Now", self.quiet_state)
        if self.watch is not None:
            self.watch.state_changed.connect(self._quiet_status)
            self.s.settings_changed.connect(self._quiet_status)
            self._quiet_status()
        self.s.settings_changed.connect(self._sync_switches)
        root.addWidget(quietbox)

        databox = QGroupBox("Data")
        dl = QVBoxLayout(databox)
        dl.addWidget(wrap_label(
            f"<small>History and samples: <code>{esc(store.DATA_DIR)}</code><br>"
            f"Settings, presets, remembered gain: <code>{esc(store.CONFIG_DIR)}</code></small>"))
        row = QHBoxLayout()
        b = QPushButton("Open data folder")
        b.clicked.connect(lambda: (store.DATA_DIR.mkdir(parents=True, exist_ok=True),
                                   QDesktopServices.openUrl(QUrl.fromLocalFile(str(store.DATA_DIR)))))
        row.addWidget(b)
        row.addStretch(1)
        dl.addLayout(row)
        root.addWidget(databox)
        root.addStretch(1)

    def _set(self, key, value):
        self.s.settings[key] = value
        self.s.save_settings()

    def _sync_switches(self):
        """The tray menu has the same switches; show the current state."""
        for box, key, default in ((self.autogain, "asr_autogain", True),
                                  (self.conservative, "conservative_gain", False)):
            box.blockSignals(True)
            box.setChecked(bool(self.s.settings.get(key, default)))
            box.blockSignals(False)
        self.icon_style.blockSignals(True)
        self.icon_style.setCurrentIndex(max(0, self.icon_style.findData(
            self.s.settings.get("tray_icon_style", TRAY_STYLE_DEFAULT))))
        self.icon_style.blockSignals(False)

    def _quiet_status(self):
        text = esc(self.watch.status())
        if self.watch.adjustments:
            text += "<br>" + "<br>".join(f"{esc(t)} {esc(a)}" for t, a in self.watch.adjustments)
        self.quiet_state.setText(f"<small>{text}</small>")

    def showEvent(self, event):
        super().showEvent(event)
        self.autostart.blockSignals(True)       # the tray menu can toggle it too
        self.autostart.setChecked(autostart.enabled())
        self.autostart.blockSignals(False)

    def _pin(self, on):
        ok, message = kwin.install_rule() if on else kwin.remove_rule()
        self.s.status.emit(message)
        if ok:
            self._set("pin_popup", on)
        if not ok:
            self.pin.blockSignals(True)
            self.pin.setChecked(not on)
            self.pin.blockSignals(False)

    def _test(self):
        self.s.settings["openrouter_api_key"] = self.key.text().strip()
        self.s.settings["openrouter_model"] = self.model.currentText().strip()
        self.s.settings["ai_include_audio"] = self.audio.isChecked()
        self.s.save_settings()
        self.key_state.setText("Checking…")
        settings = dict(self.s.settings)
        run_async(lambda: ai.check_key(settings),
                  lambda msg: self.key_state.setText(f"<span style='color:#2ea44f'>{esc(msg)}</span>"),
                  lambda msg: self.key_state.setText(f"<span style='color:#d43333'>{esc(msg)}</span>"))

    def save(self):
        """Persist the AI fields even if the user never pressed Test."""
        self.s.settings["openrouter_api_key"] = self.key.text().strip()
        self.s.settings["openrouter_model"] = self.model.currentText().strip()
        self.s.settings["ai_include_audio"] = self.audio.isChecked()
        self.s.save_settings()
