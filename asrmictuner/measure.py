"""Measure tab: the mic test (read a passage aloud), and room tone.

The mic test is the main event: pick a text of the length you want (30 s to
3 min, passages.py), read it at your normal dictation distance, and stop when
you reach the end. The result scores the sample for ASR, names what would
hurt recognition, and offers the gain that puts speech in the target band,
applied and remembered for this microphone in one click.

Every recording lands in History with its sample on disk, tagged with the
device, so each microphone keeps its own results and recommendations.
"""

import time

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QComboBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QProgressBar,
    QPushButton, QSpinBox, QTextBrowser, QVBoxLayout, QWidget,
)

from . import analysis, passages, store
from .core import fmt_gain, fmt_level, target_peak_db
from .qtutil import (
    ask_ai, ask_text, ai_html, esc, findings_html, metrics_html, run_async, wrap_label,
)
from .widgets import BandChart, ScoreBadge

SLACK = 1.5             # recording runs up to this × the passage's nominal length
MIN_VOICE_S = 5.0       # "Done reading" before this cancels instead


class MeasurePage(QWidget):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.s = session
        self.recording = None
        self.current = None

        root = QVBoxLayout(self)
        self.mic_box = QGroupBox("This microphone")
        ml = QVBoxLayout(self.mic_box)
        self.mic_summary = wrap_label()
        ml.addWidget(self.mic_summary)
        root.addWidget(self.mic_box)

        top = QHBoxLayout()
        voice = QGroupBox("Mic test: read this aloud")
        vl = QVBoxLayout(voice)
        row = QHBoxLayout()
        row.addWidget(QLabel("Text"))
        self.passage = QComboBox()
        for p in passages.PASSAGES:
            self.passage.addItem(passages.label(p), p[0])
        index = self.passage.findData(self.s.settings.get("voice_passage", "60-email"))
        self.passage.setCurrentIndex(max(0, index))
        self.passage.currentIndexChanged.connect(self._show_passage)
        row.addWidget(self.passage, 1)
        vl.addLayout(row)
        self.reader = QTextBrowser()
        font = QFont(self.reader.font())
        font.setPointSizeF(font.pointSizeF() * 1.25)
        self.reader.setFont(font)
        self.reader.setMinimumHeight(170)
        vl.addWidget(self.reader, 1)
        vl.addWidget(wrap_label(
            "<small>Sit as you do when dictating and read at your normal pace. Press <b>Done reading</b> "
            "at the end. Longer texts show whether your level stays steady.</small>"))
        row = QHBoxLayout()
        row.addStretch(1)
        self.voice_cancel = QPushButton("Cancel")
        self.voice_cancel.clicked.connect(lambda: self._cancel("Cancelled."))
        self.voice_cancel.hide()
        row.addWidget(self.voice_cancel)
        self.voice_button = QPushButton("Start reading")
        self.voice_button.setDefault(True)
        self.voice_button.clicked.connect(self._voice_clicked)
        row.addWidget(self.voice_button)
        vl.addLayout(row)
        top.addWidget(voice, 3)

        room = QGroupBox("Room tone (optional, first)")
        rl = QVBoxLayout(room)
        rl.addWidget(wrap_label(
            "Stay silent and let the room sound as it normally does while you dictate: "
            "fans, traffic, the fridge. Saved as a room profile, it makes the mic test's "
            "signal-to-noise figure exact instead of estimated from your pauses."))
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
        self.apply_button = QPushButton("Apply recommended gain")
        self.apply_button.setToolTip("Sets the gain and remembers it for this microphone "
                                     "(what Restore, the tray and the login unit reapply)")
        self.apply_button.clicked.connect(self._apply_suggestion)
        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(lambda: self.current and store.play(self.current.get("sample_path")))
        self.ai_button = QPushButton("Ask AI")
        self.ai_button.clicked.connect(self._ask_ai)
        self.chain_button = QPushButton("Build personal chain")
        self.chain_button.setToolTip("An ASR Mic chain fitted to this voice on this mic: high-pass under "
                                     "your lowest pitch, gate and auto gain set from your levels. Opens in ASR "
                                     "Effects for review.")
        self.chain_button.clicked.connect(self._personal_chain)
        for b in (self.profile_button, self.apply_button, self.chain_button, self.play_button, self.ai_button):
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
        self.s.history_changed.connect(self._update_mic_summary)
        self.s.gain_changed.connect(self._update_mic_summary)
        self._show_passage()
        self._update_mic_summary()

    # -- recording --
    def _on_device(self):
        if self.recording:
            self._cancel("Cancelled — device changed.")
        self._update_mic_summary()

    def _show_passage(self):
        p = passages.by_id(self.passage.currentData())
        self.reader.setHtml(f"<p style='line-height:140%'>{esc(p[3])}</p>")
        self.s.settings["voice_passage"] = p[0]

    def _voice_clicked(self):
        if self.recording and self.recording["kind"] == "voice":
            self._finish()
        else:
            self.start("voice")

    def _update_mic_summary(self):
        """The per-microphone link: last mic test, what it recommended,
        what is remembered now, and the bound effects preset."""
        d = self.s.device
        if d is None:
            self.mic_summary.setText("No microphone selected.")
            return
        self.mic_box.setTitle(f"This microphone: {d.description}")
        parts = []
        last = self.s.store.measurements(device=d.name, kind="voice", limit=1)
        if last:
            m = last[0]
            mm = m.get("metrics") or {}
            r = mm.get("readiness") or {}
            parts.append(
                f"Last mic test {esc(m['created'][:16].replace('T', ' '))}: <b>{r.get('score', '—')}</b> "
                f"({esc(r.get('grade', ''))}), speech peaks {fmt_level(mm.get('speech_peak_p95_dbfs'))} "
                f"at gain {fmt_gain(m.get('gain_db'))}.")
        else:
            parts.append("No mic test yet for this microphone. Read a text below to get one.")
        remembered = (self.s.remembered() or {}).get("index")
        if remembered is not None and self.s.curve is not None:
            parts.append(f"Remembered gain {fmt_gain(self.s.curve.db(remembered))} (index {remembered}).")
        elif self.s.soft_only:
            parts.append("No analog gain control: adjustments use software volume.")
        from . import engine
        name = (self.s.settings.get("chain_bindings") or {}).get(engine.binding_key(d.name, d.port))
        if name:
            parts.append(f"ASR chain: <b>{esc(name)}</b>" + ("" if name in engine.chain_names() else
                         " <span style='color:#d43333'>(chain missing)</span>") + " (ASR Effects tab).")
        self.mic_summary.setText(" ".join(parts))

    def start(self, kind):
        if self.recording:
            self._cancel("Cancelled.")
            return
        a = self.s.analyser
        if not a or not a.alive:
            self.state.setText("<span style='color:#d43333'>No capture stream.</span>")
            return
        if kind == "room":
            seconds = self.room_seconds.value()
            self.s.settings["room_seconds"] = seconds
            nominal = seconds
        else:
            nominal = passages.by_id(self.passage.currentData())[2]
            seconds = nominal * SLACK
        self.s.save_settings()
        now = time.monotonic()
        self.recording = {"kind": kind, "start": now, "until": now + seconds, "length": seconds, "nominal": nominal}
        if kind == "room":
            self.room_button.setText("Cancel")
            self.voice_button.setEnabled(False)
            self.state.setText("<b>Stay silent…</b>")
        else:
            self.voice_button.setText("Done reading")
            self.voice_cancel.show()
            self.passage.setEnabled(False)
            self.room_button.setEnabled(False)
            self.reader.verticalScrollBar().setValue(0)
            self.state.setText("<b>Read the text aloud…</b>")
        self.progress.show()
        a.begin_record()
        QTimer.singleShot(100, self._tick)

    def _tick(self):
        if not self.recording:
            return
        now = time.monotonic()
        remaining = self.recording["until"] - now
        if remaining > 0:
            if self.recording["kind"] == "voice":
                elapsed = now - self.recording["start"]
                self.progress.setValue(int(min(100, 100 * elapsed / self.recording["nominal"])))
                self.progress.setFormat(f"{elapsed:.0f} s of about {self.recording['nominal']} s")
            else:
                self.progress.setValue(int(100 * (1 - remaining / self.recording["length"])))
                self.progress.setFormat(f"{remaining:.1f} s")
            QTimer.singleShot(100, self._tick)
            return
        self._finish()

    def _finish(self):
        if not self.recording:
            return
        if self.recording["kind"] == "voice" and time.monotonic() - self.recording["start"] < MIN_VOICE_S:
            self._cancel(f"Too short: read for at least {MIN_VOICE_S:.0f} s.")
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
        self.voice_button.setText("Start reading")
        self.voice_cancel.hide()
        self.passage.setEnabled(True)
        self.room_button.setEnabled(True)
        self.voice_button.setEnabled(True)

    # -- result --
    def show_measurement(self, m):
        self.current = m
        metrics = m.get("metrics") or {}
        kind = m["kind"]
        self.result_box.show()
        self.result_box.setTitle("Room tone" if kind == "room" else "Mic test result")
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

        self._suggestion = None
        peak = metrics.get("speech_peak_p95_dbfs")
        if kind == "voice" and peak is not None and self.s.device and m["device"] == self.s.device.name:
            # Relative to the gain the sample was taken at, not whatever is set now.
            if self.s.gain is not None and self.s.curve is not None and m.get("gain_index") is not None:
                index = analysis.suggest_index(self.s.curve, m["gain_index"], peak, target_peak_db(self.s.settings))
                if index is not None and index != self.s.gain.index:
                    self._suggestion = ("index", index)
                    change = self.s.curve.db(index) - self.s.curve.db(self.s.gain.index)
                    self.apply_button.setText(f"Apply recommended gain: {fmt_gain(self.s.curve.db(index))} "
                                              f"({change:+.1f} dB) and remember it")
            elif self.s.soft_only and self.s.device.pw_percent:
                # PipeWire software volume is cubic: dB = 60·log10(v).
                wanted = target_peak_db(self.s.settings) - peak
                pct = max(1, min(150, int(round(self.s.device.pw_percent * 10 ** (wanted / 60.0)))))
                if abs(wanted) >= 1.5 and pct != self.s.device.pw_percent:
                    self._suggestion = ("soft", pct)
                    self.apply_button.setText(f"Apply recommended level: {pct}% ({wanted:+.1f} dB)")
        self.apply_button.setVisible(self._suggestion is not None)
        self.profile_button.setText("Save as room profile…" if kind == "room" else "Save as speaker profile…")
        self.chain_button.setVisible(kind == "voice")
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
        default = self.current.get("location") or ("Room" if kind == "room" else "Me")
        name = ask_text(self, "Save profile", "Name for this room profile:" if kind == "room"
                        else "Speaker name for this profile (it is tied to this microphone):", default)
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
        if self._suggestion is None:
            return
        how, value = self._suggestion
        if how == "index":
            ok = self.s.set_index(value)
            if ok:
                self.s.remember_current()
        else:
            ok = self.s.set_soft(value)
        if ok:
            self.apply_button.hide()
            self._update_mic_summary()
            self.state.setText("Applied and remembered for this microphone. Run the test again to confirm.")

    def _personal_chain(self):
        if not self.current or self.current["kind"] != "voice":
            return
        from .engine import personal_chain
        room = self.s.active_room_profile()
        chain, notes = personal_chain(self.current.get("metrics") or {}, (room or {}).get("metrics"))
        label = self.current.get("device_label") or self.current["device"]
        self.s.open_chain.emit(chain, f"ASR Personal ({label.split('  [')[0]})", notes)

    def _ask_ai(self):
        if self.current:
            self.ai_view.show()
            ask_ai(self.s, self.current, self.ai_view, self.ai_button)
