"""Measure tab: record room tone or a voice sample, analyse it, keep it.

Every recording lands in History automatically, with its sample on disk.
"Save as profile" promotes one to a named room or voice profile; the active
room profile becomes the noise reference for voice SNR and the overlay on
the live spectrum.
"""

import time

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import (
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
    QPushButton, QSpinBox, QTextBrowser, QVBoxLayout, QWidget,
)

from . import analysis, store
from .core import fmt_gain
from .qtutil import (
    ask_ai, ask_text, ai_html, esc, findings_html, metrics_html, run_async, wrap_label,
)
from .widgets import BandChart, ScoreBadge

PASSAGE = (
    "Please read this aloud at your normal dictation pace and distance. "
    "<i>“The quick check starts now. I usually dictate emails, notes and long documents, "
    "sometimes six or seven thousand words a day. Thursday's schedule has three meetings: "
    "a budget review at nine fifteen, a call with the Zurich office, and a design "
    "session that will probably run late. Should we ship the fix today, or wait for "
    "the second opinion? Either way, please send me the figures — forty-two, "
    "eighty-nine, and one hundred and sixteen — before lunch.”</i>"
)


class MeasurePage(QWidget):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.s = session
        self.recording = None
        self.current = None

        root = QVBoxLayout(self)
        top = QHBoxLayout()

        room = QGroupBox("Room tone")
        rl = QVBoxLayout(room)
        rl.addWidget(wrap_label(
            "Stay silent and let the room sound as it normally does while you dictate — "
            "fans, traffic, the fridge. Saves the noise floor, its spectrum, hum and hiss, "
            "and a sample for later noise reduction."))
        rl.addStretch(1)
        row = QHBoxLayout()
        row.addWidget(QLabel("Seconds"))
        self.room_seconds = QSpinBox()
        self.room_seconds.setRange(5, 120)
        self.room_seconds.setValue(int(self.s.settings.get("room_seconds", 15)))
        row.addWidget(self.room_seconds)
        row.addStretch(1)
        self.room_button = QPushButton("Record room tone")
        self.room_button.clicked.connect(lambda: self.start("room"))
        row.addWidget(self.room_button)
        rl.addLayout(row)
        top.addWidget(room, 1)

        voice = QGroupBox("Voice sample")
        vl = QVBoxLayout(voice)
        vl.addWidget(wrap_label(PASSAGE))
        row = QHBoxLayout()
        row.addWidget(QLabel("Seconds"))
        self.voice_seconds = QSpinBox()
        self.voice_seconds.setRange(10, 120)
        self.voice_seconds.setValue(int(self.s.settings.get("voice_seconds", 20)))
        row.addWidget(self.voice_seconds)
        row.addStretch(1)
        self.voice_button = QPushButton("Record voice")
        self.voice_button.clicked.connect(lambda: self.start("voice"))
        row.addWidget(self.voice_button)
        vl.addLayout(row)
        top.addWidget(voice, 1)
        root.addLayout(top)

        prow = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.hide()
        prow.addWidget(self.progress, 1)
        self.state = wrap_label()
        prow.addWidget(self.state, 2)
        root.addLayout(prow)

        # result
        self.result_box = QGroupBox("Result")
        grid = QGridLayout(self.result_box)
        self.header = wrap_label()
        grid.addWidget(self.header, 0, 0, 1, 2)
        self.badge = ScoreBadge()
        grid.addWidget(self.badge, 1, 0)
        self.findings = wrap_label()
        grid.addWidget(self.findings, 1, 1)
        self.metrics = wrap_label()
        grid.addWidget(self.metrics, 2, 0, 1, 2)
        self.bands = BandChart()
        grid.addWidget(self.bands, 3, 0, 1, 2)
        self.bands_note = wrap_label("<small>Green bars: speech band (300–3400 Hz). "
                                     "Amber ticks: active room profile.</small>")
        grid.addWidget(self.bands_note, 4, 0, 1, 2)

        nrow = QHBoxLayout()
        nrow.addWidget(QLabel("Notes"))
        self.notes = QLineEdit()
        self.notes.setPlaceholderText("e.g. laptop on the sofa, window open")
        self.notes.editingFinished.connect(self._save_notes)
        nrow.addWidget(self.notes, 1)
        grid.addLayout(nrow, 5, 0, 1, 2)

        brow = QHBoxLayout()
        self.profile_button = QPushButton("Save as profile…")
        self.profile_button.clicked.connect(self._save_profile)
        self.apply_button = QPushButton("Apply suggested gain")
        self.apply_button.clicked.connect(self._apply_suggestion)
        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(lambda: self.current and store.play(self.current.get("sample_path")))
        self.ai_button = QPushButton("Ask AI")
        self.ai_button.clicked.connect(self._ask_ai)
        for b in (self.profile_button, self.apply_button, self.play_button, self.ai_button):
            brow.addWidget(b)
        brow.addStretch(1)
        grid.addLayout(brow, 6, 0, 1, 2)
        self.ai_view = QTextBrowser()
        self.ai_view.setOpenExternalLinks(True)
        self.ai_view.setMinimumHeight(110)
        self.ai_view.hide()
        grid.addWidget(self.ai_view, 7, 0, 1, 2)
        root.addWidget(self.result_box)
        root.addStretch(1)
        self.result_box.hide()

        self.s.device_changed.connect(self._on_device)

    # -- recording --
    def _on_device(self):
        if self.recording:
            self._cancel("Cancelled — device changed.")

    def start(self, kind):
        if self.recording:
            self._cancel("Cancelled.")
            return
        a = self.s.analyser
        if not a or not a.alive:
            self.state.setText("<span style='color:#d43333'>No capture stream.</span>")
            return
        seconds = self.room_seconds.value() if kind == "room" else self.voice_seconds.value()
        self.s.settings[f"{kind}_seconds"] = seconds
        self.s.save_settings()
        self.recording = {"kind": kind, "until": time.monotonic() + seconds, "length": seconds}
        (self.room_button if kind == "room" else self.voice_button).setText("Cancel")
        (self.voice_button if kind == "room" else self.room_button).setEnabled(False)
        self.progress.show()
        self.state.setText("<b>Stay silent…</b>" if kind == "room" else "<b>Read the passage…</b>")
        a.begin_record()
        QTimer.singleShot(100, self._tick)

    def _tick(self):
        if not self.recording:
            return
        remaining = self.recording["until"] - time.monotonic()
        if remaining > 0:
            self.progress.setValue(int(100 * (1 - remaining / self.recording["length"])))
            self.progress.setFormat(f"{remaining:.1f} s")
            QTimer.singleShot(100, self._tick)
            return
        kind = self.recording["kind"]
        a = self.s.analyser
        audio = a.end_record() if a else None
        rate = a.rate if a else 48000
        self._reset_buttons()
        if audio is None or len(audio) < rate:
            self.state.setText("<span style='color:#d43333'>Too little audio captured.</span>")
            return
        self.state.setText("Analysing…")
        room = self.s.active_room_profile()
        room_metrics = room.get("metrics") if room else None
        device_name = self.s.device.name

        def work():
            metrics = (analysis.analyse_room(audio, rate) if kind == "room"
                       else analysis.analyse_voice(audio, rate, room=room_metrics))
            path = store.save_sample(audio, rate, kind, device_name)
            prof = store.make_noise_profile(path) if kind == "room" else None
            return metrics, path, prof, len(audio) / rate

        run_async(work, lambda r: self._done(kind, *r),
                  lambda msg: self.state.setText(f"<span style='color:#d43333'>{esc(msg)}</span>"))

    def _done(self, kind, metrics, path, prof, duration):
        mid = self.s.log(kind, metrics, sample_path=str(path),
                         noiseprof=str(prof) if prof else None, duration_s=duration)
        self.state.setText(f"Saved to History · <small>{esc(path)}</small>")
        self.show_measurement(self.s.store.measurement(mid))

    def _cancel(self, message):
        if self.s.analyser:
            self.s.analyser.end_record()
        self._reset_buttons()
        self.state.setText(message)

    def _reset_buttons(self):
        self.recording = None
        self.progress.hide()
        self.room_button.setText("Record room tone")
        self.voice_button.setText("Record voice")
        self.room_button.setEnabled(True)
        self.voice_button.setEnabled(True)

    # -- result --
    def show_measurement(self, m):
        self.current = m
        metrics = m.get("metrics") or {}
        kind = m["kind"]
        self.result_box.show()
        self.result_box.setTitle("Room tone" if kind == "room" else "Voice sample")
        self.header.setText(
            f"<b>{esc(m.get('device_label') or m['device'])}</b> · gain {fmt_gain(m.get('gain_db'))}"
            + (f" (index {m['gain_index']})" if m.get("gain_index") is not None else "")
            + (f" · {esc(m['location'])}" if m.get("location") else "")
            + f" · <small>{esc(m['created'])}</small>")
        if kind == "voice":
            r = metrics.get("readiness") or {}
            self.badge.set_score(r.get("score"), f"ASR {r.get('grade', '')}")
            self.badge.show()
        else:
            self.badge.hide()
        text = findings_html(metrics.get("findings"))
        if kind == "room" and metrics.get("character"):
            text = f"<p><b>{esc(metrics['character'].capitalize())}.</b></p>" + text
        self.findings.setText(text)
        self.metrics.setText(metrics_html(metrics))
        room = self.s.active_room_profile()
        reference = (room or {}).get("metrics", {}).get("bands") if kind == "voice" and room else None
        self.bands.set_bands(metrics.get("bands"), reference)
        self.notes.setText(m.get("notes") or "")

        suggestion = None
        if kind == "voice" and self.s.device and m["device"] == self.s.device.name:
            suggestion = analysis.suggest_index(self.s.curve, self.s.gain.index if self.s.gain else 0,
                                                metrics.get("speech_peak_p95_dbfs")) if self.s.gain else None
        self._suggestion = suggestion
        if suggestion is not None and suggestion != self.s.gain.index:
            self.apply_button.setText(f"Apply suggested gain ({fmt_gain(self.s.curve.db(suggestion))})")
            self.apply_button.show()
        else:
            self.apply_button.hide()
        self.profile_button.setText("Save as room profile…" if kind == "room" else "Save as voice profile…")
        if m.get("ai"):
            self.ai_view.setHtml(ai_html(m["ai"]))
            self.ai_view.show()
        else:
            self.ai_view.hide()

    def _save_notes(self):
        if self.current and (self.notes.text() or "") != (self.current.get("notes") or ""):
            self.s.store.update_measurement(self.current["id"], notes=self.notes.text())
            self.current["notes"] = self.notes.text()
            self.s.history_changed.emit()

    def _save_profile(self):
        if not self.current:
            return
        kind = self.current["kind"]
        default = self.current.get("location") or ("Room" if kind == "room" else "My voice")
        name = ask_text(self, "Save profile", f"Name for this {kind} profile:", default)
        if not name:
            return
        pid = self.s.store.add_profile(kind, name, self.current["device"], self.current["id"],
                                       self.notes.text())
        if kind == "room" and self.s.device and self.current["device"] == self.s.device.name:
            self.s.set_active_room_profile(pid)
            self.state.setText(f"Room profile “{esc(name)}” saved and set active for this device.")
        else:
            self.s.profiles_changed.emit()
            self.state.setText(f"{kind.capitalize()} profile “{esc(name)}” saved.")

    def _apply_suggestion(self):
        if self._suggestion is not None and self.s.set_index(self._suggestion):
            self.apply_button.hide()
            self.state.setText("Gain applied. Record another voice sample to confirm.")

    def _ask_ai(self):
        if self.current:
            self.ai_view.show()
            ask_ai(self.s, self.current, self.ai_view, self.ai_button)
