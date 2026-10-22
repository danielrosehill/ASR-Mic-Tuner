"""Monitor tab: the four facts at once — analog gain, level, clipping, and
the live verdict — plus the spectrum, quick calibration and details."""

import time

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QGroupBox, QHBoxLayout, QProgressBar,
    QPushButton, QToolButton, QVBoxLayout, QWidget,
)

from . import analysis
from .core import (
    CALIB_MAX_NOISE_DB, CALIB_MIN_HEADROOM_DB, CALIB_TARGET_PEAK_DB,
    SPEECH_GATE_FLOOR_DB, SPEECH_GATE_OVER_NOISE_DB, TARGET_HI_DB, TARGET_LO_DB,
    VERDICT_WINDOW_S, fmt_gain, fmt_level, percentile, resolve_hardware_for,
    default_source, set_unit_enabled, unit_enabled, zone_for_db,
)
from .qtutil import wrap_label
from .widgets import ZONE_HEX, GainSlider, LevelMeter, SpectrumView

CALIBRATION_MODES = {
    "quick": ("Quick (3 s quiet + 8 s speech)", 3.0, 8.0),
    "standard": ("Standard (5 s + 15 s)", 5.0, 15.0),
    "thorough": ("Thorough (10 s + 30 s)", 10.0, 30.0),
}


def live_verdict(session, data):
    """(headline, colour, detail, fix) for the current speech level, shared
    with the tray pop-up. `fix` is (target index or soft %, dB) or None."""
    analyser = session.analyser
    if analyser is None:
        return "Not listening", "#888", "", None
    noise = max((s["noise"] for s in data if s["noise"] is not None), default=None)
    gate = max(SPEECH_GATE_FLOOR_DB,
               (noise + SPEECH_GATE_OVER_NOISE_DB) if noise is not None else SPEECH_GATE_FLOOR_DB)
    peak = analyser.speech_peak(VERDICT_WINDOW_S, gate)
    if peak is None:
        return "Speak a few words…", None, "the verdict follows your voice, not the room", None
    detail = f"speech peaks {fmt_level(peak)}"
    if TARGET_LO_DB <= peak <= TARGET_HI_DB:
        return "Good level", "#2ea44f", detail, None
    headline, colour = (("Too quiet — raise the gain", "#3b7ad6") if peak < TARGET_LO_DB
                        else ("Too loud — lower the gain", "#d43333"))
    wanted = CALIB_TARGET_PEAK_DB - peak
    fix = None
    if session.gain is not None and session.curve is not None:
        here = session.curve.db(session.gain.index)
        target = session.curve.index_for_db(here + wanted)
        if target != session.gain.index:
            fix = (target, session.curve.db(target) - here)
            detail += f" · needs {fix[1]:+.1f} dB"
        else:
            detail += " · the gain control has nothing left to give; change distance to the mic"
    elif session.soft_only and session.device.pw_percent:
        # Software volume is cubic in PipeWire: dB = 60·log10(v). Invert it.
        pct = session.device.pw_percent
        new = int(round(pct * 10 ** (wanted / 60.0)))
        new = max(1, min(150, new))
        if new != pct:
            fix = (new, wanted)
            detail += f" · needs about {wanted:+.1f} dB"
    return headline, colour, detail, fix


class MonitorPage(QWidget):
    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.s = session
        self.calibration = None
        self.recommendation = None
        self._fix = None
        self._cache = {}
        self._setting = False
        self._last_extra = 0.0

        root = QVBoxLayout(self)
        root.setSpacing(8)
        self.warnings = wrap_label()
        root.addWidget(self.warnings)

        # gain
        gain_box = QVBoxLayout()
        self.gain_value = wrap_label("<span style='font-size:22pt'>—</span>")
        gain_box.addWidget(self.gain_value)
        row = QHBoxLayout()
        self.minus = QPushButton("−")
        self.plus = QPushButton("+")
        for b, d in ((self.minus, -1), (self.plus, 1)):
            b.setFixedWidth(36)
            b.setAutoRepeat(True)
            b.setAutoRepeatDelay(400)
            b.setAutoRepeatInterval(120)
            b.clicked.connect(lambda _c=False, d=d: self.s.step(d))
        self.slider = GainSlider()
        self.slider.valueChanged.connect(self._on_slider)
        row.addWidget(self.minus)
        row.addWidget(self.slider, 1)
        row.addWidget(self.plus)
        gain_box.addLayout(row)
        self.step_label = wrap_label()
        gain_box.addWidget(self.step_label)
        root.addLayout(gain_box)

        # level
        self.meter = LevelMeter()
        root.addWidget(self.meter)
        vrow = QHBoxLayout()
        self.verdict = wrap_label()
        vrow.addWidget(self.verdict, 1)
        self.fix_button = QPushButton("Fix it")
        self.fix_button.setToolTip("Move the gain so speech peaks land at −12 dBFS")
        self.fix_button.clicked.connect(self._on_fix)
        self.fix_button.hide()
        vrow.addWidget(self.fix_button)
        root.addLayout(vrow)

        crow = QHBoxLayout()
        self.clip_summary = wrap_label()
        crow.addWidget(self.clip_summary, 1)
        log_btn = QPushButton("Log reading")
        log_btn.setToolTip("Save the current readings to History, with the location")
        log_btn.clicked.connect(self._on_log)
        crow.addWidget(log_btn)
        reset = QPushButton("Reset")
        reset.setToolTip("Restart the clipping counters and the timer")
        reset.clicked.connect(lambda: self.s.analyser and self.s.analyser.reset_clips())
        crow.addWidget(reset)
        root.addLayout(crow)
        self.extras = wrap_label()
        root.addWidget(self.extras)

        # spectrum
        self.spec_box = QGroupBox("Spectrum")
        self.spec_box.setCheckable(True)
        self.spec_box.setChecked(True)
        sl = QVBoxLayout(self.spec_box)
        self.spectrum = SpectrumView()
        sl.addWidget(self.spectrum)
        self.spec_note = wrap_label("<small>Green: speech band. Dashed amber: the active "
                                    "room profile's noise.</small>")
        sl.addWidget(self.spec_note)
        self.spec_box.toggled.connect(lambda on: (self.spectrum.setVisible(on), self.spec_note.setVisible(on)))
        root.addWidget(self.spec_box)

        # calibration
        cal = QHBoxLayout()
        self.mode = QComboBox()
        for key, (label, _q, _s) in CALIBRATION_MODES.items():
            self.mode.addItem(label, key)
        idx = self.mode.findData(self.s.settings.get("calibration_mode", "standard"))
        self.mode.setCurrentIndex(max(0, idx))
        self.mode.currentIndexChanged.connect(self._on_mode)
        cal.addWidget(self.mode)
        self.calib_button = QPushButton("Calibrate")
        self.calib_button.setToolTip("Measure the room, then your voice, and recommend a gain "
                                     "that puts speech peaks at −12 dBFS")
        self.calib_button.clicked.connect(self.toggle_calibration)
        cal.addWidget(self.calib_button)
        self.apply_button = QPushButton("Apply")
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._on_apply)
        cal.addWidget(self.apply_button)
        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.hide()
        cal.addWidget(self.progress, 1)
        root.addLayout(cal)
        self.calib_result = wrap_label()
        root.addWidget(self.calib_result)

        # details
        self.details_toggle = QToolButton()
        self.details_toggle.setText("Details")
        self.details_toggle.setCheckable(True)
        self.details_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.details_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.details_toggle.toggled.connect(self._toggle_details)
        root.addWidget(self.details_toggle)
        self.details = QFrame()
        dl = QVBoxLayout(self.details)
        dl.setContentsMargins(16, 0, 0, 0)
        self.curve_label = wrap_label()
        self.pw_label = wrap_label()
        self.boost_label = wrap_label()
        for w in (self.curve_label, self.pw_label, self.boost_label):
            dl.addWidget(w)
        rrow = QHBoxLayout()
        self.remember = QCheckBox("Remember this gain")
        self.remember.toggled.connect(self._on_remember)
        rrow.addWidget(self.remember)
        self.login = QCheckBox("Reapply at login")
        self.login.toggled.connect(self._on_login)
        rrow.addWidget(self.login)
        rrow.addStretch(1)
        self.restore_button = QPushButton("Restore remembered")
        self.restore_button.clicked.connect(self.s.restore_remembered)
        self.restore_button.hide()
        rrow.addWidget(self.restore_button)
        dl.addLayout(rrow)
        self.drift_label = wrap_label()
        dl.addWidget(self.drift_label)
        self.details.hide()
        root.addWidget(self.details)
        root.addStretch(1)

        self.s.device_changed.connect(self._on_device)
        self.s.gain_changed.connect(self._update_gain)
        self.s.presets_changed.connect(self._update_gain)
        self.s.profiles_changed.connect(self._update_room_overlay)

        self.fast = QTimer(self)
        self.fast.timeout.connect(self._fast_tick)
        self.fast.start(33)
        self.slow = QTimer(self)
        self.slow.timeout.connect(self._slow_tick)
        self.slow.start(2000)

    # -- device / gain --
    def _on_device(self):
        if self.calibration:
            self._end_calibration("Cancelled — device changed.")
        self.recommendation = None
        self.apply_button.setEnabled(False)
        self.calib_result.setText("")
        dev = self.s.device
        self.meter.configure(dev.channel_labels() if dev else [])
        self._update_gain()
        self._update_room_overlay()
        self._refresh_warnings()

    def _update_room_overlay(self):
        room = self.s.active_room_profile()
        self.spectrum.set_room((room or {}).get("metrics", {}).get("bands") if room else None)

    def _update_gain(self):
        s, dev = self.s, self.s.device
        self._setting = True
        try:
            if dev is None:
                self.gain_value.setText("<span style='font-size:18pt'>No capture device</span>")
                self.slider.setEnabled(False)
                return
            self.remember.blockSignals(True)
            self.remember.setChecked(s.remembered() is not None)
            self.remember.setEnabled(s.gain is not None)
            self.remember.blockSignals(False)
            self.login.blockSignals(True)
            self.login.setChecked(unit_enabled())
            self.login.blockSignals(False)

            if s.gain is None:
                # No analog control: software volume, honestly labelled.
                self.slider.set_curve(None)
                pct = dev.pw_percent
                self.slider.setRange(0, 150)
                self.slider.setSingleStep(1)
                self.slider.setPageStep(5)
                self.slider.setEnabled(pct is not None)
                if pct is not None:
                    self.slider.setValue(pct)
                underlying = None if dev.is_hardware else resolve_hardware_for(dev.name)
                hint = (f"fed by <b>{underlying}</b> — select it to set the analog gain"
                        if underlying else "this device exposes no analog gain control")
                self.gain_value.setText(
                    f"<span style='font-size:20pt'><b>{pct if pct is not None else '—'}%</b></span>"
                    f" <span style='font-size:12pt'>software volume ({fmt_gain(dev.pw_db)})</span>"
                    f"<br><small>Digital only — {hint}.</small>")
                self.step_label.setText("<small>± steps 1 % of software volume.</small>")
                self.curve_label.setText("")
                self.boost_label.setText("")
                self._set_pw_label()
                self.drift_label.setText("")
                self.restore_button.hide()
                return

            self.slider.setEnabled(True)
            self.slider.setRange(s.gain.imin, s.gain.imax)
            self.slider.setSingleStep(1)
            self.slider.setPageStep(4)
            self.slider.set_curve(s.curve)
            self.slider.setValue(s.gain.index)
            index = s.gain.index
            if s.curve is None:
                self.gain_value.setText(
                    f"<span style='font-size:22pt'>index {index} / {s.gain.imax}</span>"
                    "<br><small>device declares no dB curve and probing failed</small>")
                self.curve_label.setText("")
                self.step_label.setText("")
            else:
                db = s.curve.db(index)
                zone = zone_for_db(db)
                self.gain_value.setText(
                    f"<span style='font-size:22pt;color:{ZONE_HEX[zone]}'><b>{fmt_gain(db)}</b></span>"
                    f"&nbsp;&nbsp;<span style='font-size:12pt'>analog gain · index {index} / {s.gain.imax}"
                    f" · <span style='color:{ZONE_HEX[zone]}'>{zone}</span></span>")
                step = s.curve.step_db
                self.step_label.setText(
                    f"<small>± steps one hardware index"
                    + (f" ({step:.2f} dB)" if step else "")
                    + ". Arrow keys on the slider do the same.</small>")
                self.curve_label.setText(f"<small>{s.gain.name}: {s.curve.describe()}</small>")
            boosts = []
            for b in s.boosts:
                text = f"{b.name}: {fmt_gain(b.db)} (index {b.index}/{b.imax})"
                if b.index > b.imin:
                    text = f"<span style='color:#d43333'>{text} — stacks on top of capture gain</span>"
                boosts.append(text)
            self.boost_label.setText("<small>" + "<br>".join(boosts) + "</small>")
            self._set_pw_label()
            self._refresh_drift()
        finally:
            self._setting = False

    def _set_pw_label(self):
        dev = self.s.device
        if not dev or dev.pw_percent is None or self.s.gain is None:
            self.pw_label.setText("")
            return
        self.pw_label.setText(
            f"<small>PipeWire reports: <b>{dev.pw_percent}% / {fmt_gain(dev.pw_db)}</b> — "
            "<i>this is not the analog gain</i></small>")

    def _drift_text(self):
        d = self.s.drift()
        if not d:
            return None
        saved = self.s.remembered()
        return (f"Gain has drifted: now {fmt_gain(self.s.gain_db())} (index {d[1]}), "
                f"remembered {fmt_gain(saved.get('db'))} (index {d[0]}). Something else moved it.")

    def _refresh_drift(self):
        saved = self.s.remembered()
        drifted = self._drift_text()
        if not saved:
            self.drift_label.setText("")
        elif drifted:
            self.drift_label.setText(f"<span style='color:#d43333'>{drifted}</span>")
        else:
            label = self.s.default_preset_label()
            self.drift_label.setText(
                f"<small>Remembered: index {saved['index']} ({fmt_gain(saved.get('db'))})"
                + (f" — preset “{label}”" if label else "") + "</small>")
        self.restore_button.setVisible(bool(drifted))

    def _on_slider(self, value):
        if self._setting:
            return
        if self.s.gain is not None:
            if value != self.s.gain.index:
                self.s.set_index(value)
        elif self.s.device is not None:
            self.s.set_soft(value)

    def _on_remember(self, on):
        self.s.remember_current() if on else self.s.forget()
        self._refresh_drift()

    def _on_login(self, on):
        ok, message = set_unit_enabled(on)
        self.s.status.emit(message)
        if not ok:
            self.login.blockSignals(True)
            self.login.setChecked(not on)
            self.login.blockSignals(False)

    def _toggle_details(self, on):
        self.details.setVisible(on)
        self.details_toggle.setArrowType(Qt.ArrowType.DownArrow if on else Qt.ArrowType.RightArrow)

    # -- warnings --
    def _refresh_warnings(self):
        lines = []
        s = self.s
        default = default_source()
        if s.device and s.device.is_hardware and default != s.device.name:
            target = next((d for d in s.devices if d.name == default), None)
            if target is not None and not target.is_hardware:
                lines.append(f"The system default source is <b>{default}</b>, a processed node. "
                             "Apps are recording that, not the device shown here.")
        hot = [b for b in s.boosts if b.index > b.imin]
        if hot:
            lines.append(", ".join(f"{b.name} is at {fmt_gain(b.db)}" for b in hot)
                         + " — that stacks on top of the capture gain.")
        drifted = self._drift_text()
        if drifted:
            lines.append(drifted)
        if s.analyser and s.analyser.error:
            lines.append(s.analyser.error)
        text = "<span style='color:#d43333'>" + "<br>".join(lines) + "</span>" if lines else ""
        if self._cache.get("warn") != text:
            self._cache["warn"] = text
            self.warnings.setText(text)
            self.warnings.setVisible(bool(text))

    # -- ticks --
    def _set_cached(self, key, label, text):
        if self._cache.get(key) != text:
            self._cache[key] = text
            label.setText(text)

    def _fast_tick(self):
        a = self.s.analyser
        if a is None or not self.isVisible():
            return
        data, duration = a.snapshot()
        self.meter.set_data(data)
        headline, colour, detail, fix = live_verdict(self.s, data)
        style = f" style='color:{colour}'" if colour else ""
        self._set_cached("verdict", self.verdict,
                         f"<span style='font-size:13pt'{style}><b>{headline}</b></span>"
                         f"&nbsp;&nbsp;<small>{detail}</small>")
        self._fix = fix
        if fix:
            self.fix_button.setText(f"Fix it ({fix[1]:+.1f} dB)")
        self.fix_button.setVisible(bool(fix))
        self._set_cached("clip", self.clip_summary, self._clip_markup(data, duration))

        now = time.monotonic()
        if now - self._last_extra >= 0.1:
            self._last_extra = now
            if self.spec_box.isChecked():
                spec = analysis.live_spectrum(a.recent_audio(), a.rate)
                if spec is not None:
                    self.spectrum.set_spectrum(*spec)
            self._set_cached("extras", self.extras, self._extras_markup(data))

    def _clip_markup(self, data, duration):
        labels = self.s.device.channel_labels() if self.s.device else []
        noise = max((s["noise"] for s in data if s["noise"] is not None), default=None)
        tail = f"&nbsp;&nbsp;<small>noise floor {fmt_level(noise)} · {duration:.0f} s</small>"
        clipping = [(n, s) for n, s in zip(labels, data) if s["runs"]]
        if not clipping:
            return "<span style='color:#2ea44f'>No clipping</span>" + tail
        detail = "; ".join(f"{n} {s['clipped']} samples in {s['runs']} runs (longest {s['longest']})"
                           for n, s in clipping)
        return (f"<span style='color:#d43333'><b>Clipping</b> — {detail}. Lower the analog gain; "
                "nothing downstream can undo this.</span>" + tail)

    def _extras_markup(self, data):
        parts = []
        dc = max((abs(s["dc"]) for s in data), default=0.0)
        colour = "#d9a340" if dc >= analysis.DC_WARN else None
        dc_text = f"DC offset {dc * 100:.2f} %"
        parts.append(f"<span style='color:{colour}'>{dc_text}</span>" if colour else dc_text)
        if len(data) >= 2:
            levels = [s["noise"] for s in data if s["noise"] is not None]
            if len(levels) >= 2:
                parts.append(f"channel balance {max(levels) - min(levels):.1f} dB (noise)")
        a = self.s.analyser
        noise = max((s["noise"] for s in data if s["noise"] is not None), default=None)
        if a and noise is not None:
            gate = max(SPEECH_GATE_FLOOR_DB, noise + SPEECH_GATE_OVER_NOISE_DB)
            peak = a.speech_peak(VERDICT_WINDOW_S * 2, gate)
            if peak is not None:
                parts.append(f"est. speech-to-noise {peak - noise:.0f} dB (peak vs floor)")
        room = self.s.active_room_profile()
        if room:
            parts.append(f"room profile: {room['name']}")
        if self.s.device:
            parts.append(f"{self.s.device.channels} ch · {self.s.device.rate / 1000:g} kHz")
        return "<small>" + " · ".join(parts) + "</small>"

    def _slow_tick(self):
        if not self.isVisible():
            return
        if not self.calibration:
            self.s.refresh_controls()
            self.s.ensure_capture()
        self._refresh_warnings()
        self._refresh_drift()

    def _on_fix(self):
        if not self._fix:
            return
        target, _db = self._fix
        if self.s.gain is not None:
            self.s.set_index(target)
        else:
            self.s.set_soft(target)

    def _on_log(self):
        a = self.s.analyser
        if not a:
            return
        data, duration = a.snapshot()
        noise = max((s["noise"] for s in data if s["noise"] is not None), default=None)
        gate = max(SPEECH_GATE_FLOOR_DB, (noise or -100) + SPEECH_GATE_OVER_NOISE_DB)
        metrics = {
            "kind": "snapshot",
            "noise_dbfs": noise,
            "speech_peak_dbfs": a.speech_peak(6.0, gate),
            "clipped_runs": sum(s["runs"] for s in data),
            "clipped_samples": sum(s["clipped"] for s in data),
            "dc_offset": max((abs(s["dc"]) for s in data), default=0.0),
            "monitored_s": duration,
        }
        self.s.log("snapshot", metrics)
        self.s.status.emit("Reading logged to History.")

    # -- calibration --
    def _on_mode(self):
        self.s.settings["calibration_mode"] = self.mode.currentData()
        self.s.save_settings()

    def toggle_calibration(self):
        if self.calibration:
            self._end_calibration("Cancelled.")
            return
        a = self.s.analyser
        if not a or not a.alive:
            self.calib_result.setText("<span style='color:#d43333'>No capture stream.</span>")
            return
        _label, quiet_s, speech_s = CALIBRATION_MODES[self.mode.currentData()]
        self.recommendation = None
        self.apply_button.setEnabled(False)
        self.calib_button.setText("Cancel")
        self.progress.show()
        self.calibration = {"phase": "quiet", "verify": False, "quiet": None,
                            "speech_s": speech_s, "mode": self.mode.currentData()}
        self._begin_phase("quiet", quiet_s, "Stay quiet…")

    def _begin_phase(self, phase, seconds, message):
        self.calibration.update(phase=phase, until=time.monotonic() + seconds, length=seconds)
        self.calib_result.setText(f"<b>{message}</b>")
        self.s.analyser.begin_collect()
        QTimer.singleShot(100, self._calibration_tick)

    def _calibration_tick(self):
        if not self.calibration or not self.s.analyser:
            return
        remaining = self.calibration["until"] - time.monotonic()
        if remaining > 0:
            self.progress.setValue(int(100 * (1 - remaining / self.calibration["length"])))
            self.progress.setFormat(f"{remaining:.1f} s")
            QTimer.singleShot(100, self._calibration_tick)
            return
        self.progress.setValue(100)
        self.progress.setFormat("")
        samples = self.s.analyser.end_collect()
        if not samples:
            self._end_calibration("No audio captured during the measurement.")
            return
        if self.calibration["phase"] == "quiet":
            self.calibration["quiet"] = samples
            self._begin_phase("speech", self.calibration["speech_s"],
                              "Speak normally, at your usual distance…")
            return
        self._evaluate(self.calibration["quiet"], samples, self.calibration.get("verify", False))

    def _evaluate(self, quiet, speech, verify):
        s = self.s
        speech_peak = percentile([p for _, p in speech], 95)
        speech_rms = percentile([r for r, _ in speech], 50)
        if verify:
            verdict = ("on target" if TARGET_LO_DB <= speech_peak <= TARGET_HI_DB
                       else "outside the −18…−10 dBFS band; run calibration again")
            self._end_calibration(f"Re-measured: speech peaks {fmt_level(speech_peak)} — {verdict}.")
            return
        noise_rms = percentile([r for r, _ in quiet], 50)
        headroom = speech_rms - noise_rms
        lines = [f"Noise floor {fmt_level(noise_rms)}, speech {fmt_level(speech_rms)} RMS / "
                 f"{fmt_level(speech_peak)} peak, separation {headroom:.1f} dB."]
        metrics = {"kind": "calibration", "mode": self.calibration.get("mode"),
                   "noise_dbfs": noise_rms, "speech_rms_dbfs": speech_rms,
                   "speech_peak_dbfs": speech_peak, "separation_db": headroom}

        if s.gain is None or s.curve is None:
            lines.append("No analog gain control here, so there is nothing to recommend.")
            s.log("calibration", metrics)
            self._end_calibration("<br>".join(lines))
            return

        current = s.gain.index
        current_db = s.curve.db(current)
        target = s.curve.index_for_db(current_db + CALIB_TARGET_PEAK_DB - speech_peak)
        achieved = s.curve.db(target) - current_db
        predicted_noise = noise_rms + achieved
        lines.append(f"Recommendation: <b>index {current} → {target}</b>, {fmt_gain(current_db)} → "
                     f"<b>{fmt_gain(s.curve.db(target))}</b>, predicted speech peak "
                     f"{fmt_level(speech_peak + achieved)}.")
        problems = []
        if predicted_noise > CALIB_MAX_NOISE_DB:
            problems.append(f"the noise floor would sit at {fmt_level(predicted_noise)}, above −50 dBFS")
        if headroom < CALIB_MIN_HEADROOM_DB:
            problems.append(f"speech is only {headroom:.1f} dB above the room, under the "
                            f"{CALIB_MIN_HEADROOM_DB:.0f} dB minimum")
        if problems:
            lines.append("<span style='color:#d43333'><b>Gain is not the problem here:</b> "
                         + "; and ".join(problems) + ". That is the room or the microphone — gain "
                         "moves speech and noise together.</span>")
        if target == current:
            lines.append("Current gain is already the best available step.")
        metrics.update(recommended_index=target, recommended_db=s.curve.db(target),
                       problems=problems)
        s.log("calibration", metrics)
        self.recommendation = target
        self.apply_button.setEnabled(target != current)
        self._end_calibration("<br>".join(lines))

    def _end_calibration(self, message):
        if self.s.analyser:
            self.s.analyser.end_collect()
        self.calibration = None
        self.calib_button.setText("Calibrate")
        self.progress.setValue(0)
        self.progress.hide()
        self.calib_result.setText(message)

    def _on_apply(self):
        if self.recommendation is None or not self.s.set_index(self.recommendation):
            return
        self.apply_button.setEnabled(False)
        self.recommendation = None
        if self.s.analyser:
            self.s.analyser.reset_clips()
        _label, _q, speech_s = CALIBRATION_MODES[self.mode.currentData()]
        self.calibration = {"phase": "speech", "verify": True, "quiet": None, "speech_s": speech_s}
        self.calib_button.setText("Cancel")
        self.progress.show()
        # Let the new gain settle through the capture path before measuring.
        QTimer.singleShot(400, lambda: self.calibration and self._begin_phase(
            "speech", speech_s, "Applied. Speak normally again to confirm…"))
