"""Dictation watcher: the ASR auto gain and the mic-too-quiet alert. It
listens only while another application is recording.

Every few seconds the watcher asks PipeWire whether anything other than a
level meter (or ASR Mic's own passive capture) is capturing the selected microphone.
While something is, it holds a capture of its own — Plasma's in-use
indicator is already lit, so this adds nothing visible — and keeps the peaks
of the speech blocks. Silence and room noise are gated out, so pauses in
dictation do not count as "quiet".

The window is measured in *recording* time, not wall time: dictation tools
that open the mic only while you talk accumulate towards it across bursts.
Once the window holds enough speech, its level is judged with the same
statistic as the live verdict (95th-percentile block peak); if that sits
`margin` dB or more under the target band, one notification goes out and the
window starts again. Moving the gain or switching device also starts it again.

ASR auto gain (on by default) works on the same speech blocks, but it moves the
**analog** gain rather than processing audio. A DSP auto gain reacts word by
word and pumps the room noise up in every pause, which is what goes wrong with
it for ASR. This one:
- judges the last 30 s of recording, and only once it holds ≥ 8 s of speech;
- moves at most 3 dB at a time, at most once per 30 s of recording;
- leaves the gain alone while speech peaks sit in a ±2 dB dead band around
  the −18…−10 dBFS target;
- changes the gain only in a pause (the last 300 ms below the speech gate),
  so no word straddles a step;
- steps down 3 dB at once on clipping (at the next pause);
- never raises the gain above a ceiling (default +15 dB of analog gain, the
  top of the caution zone; 100 % on software-volume devices).
Each change is deliberate, so it becomes the remembered gain and does not
trip drift detection. With auto gain on, the quiet alert fires only once the
gain is at the ceiling, because below that auto gain is already fixing it.
"""

import math
import time
from collections import deque

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .core import (
    CHUNK_MS, CONSERVATIVE_RAISE_STEP_DB, SPEECH_GATE_OVER_NOISE_DB, TARGET_HI_DB, TARGET_LO_DB, ZONE_CAUTION_HI,
    fmt_gain, fmt_level, percentile, recording_clients, target_peak_db,
)

TICK_MS = 3000
MIN_SPEECH_S = 20.0         # speech needed in the window before judging
WATCH_GATE_FLOOR_DB = -60.0  # lower than the live verdict's, so a mic that is far too quiet still registers
STALE_GAP_S = 1800          # a gap in recording this long throws the window away
REPEAT_S = 1800             # at most one alert per half hour

AUTO_WINDOW_S = 30.0        # auto gain judges this much recording
AUTO_MIN_SPEECH_S = 8.0
AUTO_DEADBAND_DB = 2.0      # beyond the target band before acting
AUTO_MAX_STEP_DB = 3.0
PAUSE_S = 0.3
SOFT_CEILING_PCT = 100


class QuietWatch(QObject):
    alert = pyqtSignal(str, str)        # title, body
    state_changed = pyqtSignal()

    def __init__(self, session):
        super().__init__()
        self.s = session
        self.watching = False
        self.clients = []
        self._speech = deque()          # (recording clock, peak dBFS)
        self._clock = 0.0               # seconds of recording observed
        self._last_tick = None
        self._last_seen = 0.0           # monotonic time recording was last seen
        self._since = 0.0               # newest analyser block already taken
        self._quiet_until = 0.0
        self._last_adjust = 0.0         # recording clock of the last auto gain step
        self._clip_runs = None
        self._clipped = False
        self._gate = WATCH_GATE_FLOOR_DB
        self.adjustments = deque(maxlen=5)   # (wall time, text) for the Settings page
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(TICK_MS)
        self.s.gain_changed.connect(self.reset)
        self.s.device_changed.connect(self._device_changed)

    # -- settings --
    def enabled(self):
        return bool(self.s.settings.get("quiet_alert", True))

    def window_s(self):
        return 60.0 * float(self.s.settings.get("quiet_window_min", 3))

    def margin_db(self):
        return float(self.s.settings.get("quiet_margin_db", 6))

    def autogain_enabled(self):
        return bool(self.s.settings.get("asr_autogain", True))

    def ceiling_db(self):
        return float(self.s.settings.get("asr_autogain_max_db", ZONE_CAUTION_HI))

    # -- state --
    def reset(self):
        self._speech.clear()
        self._clock = 0.0
        self._last_adjust = 0.0          # auto gain waits a full window after any gain change

    def _device_changed(self):
        self.reset()
        self._since = time.monotonic()

    def _stop(self):
        if self.watching:
            self.watching = False
            self.clients = []
            self.s.release("watch")
            self.state_changed.emit()

    def status(self):
        """One line for the Settings page."""
        if not (self.enabled() or self.autogain_enabled()):
            return "Off."
        if not self.watching:
            return "Waiting for an application to record from this microphone."
        speech = len(self._speech) * CHUNK_MS / 1000.0
        return (f"Listening alongside {', '.join(sorted(set(self.clients)))}: "
                f"{min(self._clock, self.window_s()) / 60:.1f} of {self.window_s() / 60:.0f} min recorded, {speech:.0f} s of speech.")

    # -- the loop --
    def _tick(self):
        now = time.monotonic()
        if not (self.enabled() or self.autogain_enabled()) or self.s.device is None:
            self._stop()
            return
        clients = recording_clients(self.s.device.name)
        if not clients:
            self._stop()
            self._last_tick = None
            return
        if now - self._last_seen > STALE_GAP_S:
            self.reset()
        self._last_seen = now
        if not self.watching:
            self.watching = True
            self._since = now
            self._last_tick = now
            self.s.acquire("watch")
        self.clients = clients
        self.s.ensure_capture()
        self.state_changed.emit()

        if self._last_tick is not None:
            self._clock += now - self._last_tick
        self._last_tick = now
        analyser = self.s.analyser
        if analyser is None:
            return
        data, _ = analyser.snapshot()
        noise = max((c["noise"] for c in data if c["noise"] is not None), default=None)
        gate = max(WATCH_GATE_FLOOR_DB, noise + SPEECH_GATE_OVER_NOISE_DB if noise is not None else WATCH_GATE_FLOOR_DB)
        self._gate = gate
        runs = sum(c.get("runs", 0) for c in data)
        if self._clip_runs is not None and runs > self._clip_runs:
            self._clipped = True
        self._clip_runs = runs
        blocks = analyser.peaks_since(self._since)
        if blocks:
            self._since = blocks[-1][0]
        self._speech.extend((self._clock, p) for _t, p in blocks if p >= gate)
        window = self.window_s()
        while self._speech and self._clock - self._speech[0][0] > max(window, AUTO_WINDOW_S):
            self._speech.popleft()
        if self.autogain_enabled() and self._autogain(analyser):
            return
        if self.enabled():
            self._judge(window)

    # -- auto gain --
    def _in_pause(self, analyser):
        recent = analyser.peaks_since(time.monotonic() - PAUSE_S)
        return bool(recent) and all(p < self._gate for _t, p in recent)

    def _autogain(self, analyser):
        """One step towards the target if due. True if the gain moved."""
        if self._clipped:
            step = -AUTO_MAX_STEP_DB
            why = "clipping"
        else:
            if self._clock - self._last_adjust < AUTO_WINDOW_S:
                return False
            recent = [p for c, p in self._speech if self._clock - c <= AUTO_WINDOW_S]
            if len(recent) * CHUNK_MS / 1000.0 < AUTO_MIN_SPEECH_S:
                return False
            level = percentile(recent, 95)
            if TARGET_LO_DB - AUTO_DEADBAND_DB <= level <= TARGET_HI_DB + AUTO_DEADBAND_DB:
                return False
            raise_max = CONSERVATIVE_RAISE_STEP_DB if self.s.settings.get("conservative_gain") else AUTO_MAX_STEP_DB
            step = max(-AUTO_MAX_STEP_DB, min(raise_max, target_peak_db(self.s.settings) - level))
            why = f"speech peaks {fmt_level(level)}"
        if not self._in_pause(analyser):
            return False                 # try again at the next tick
        moved = self._step(step)
        self._clipped = False
        if moved is None:
            return False
        text = f"Auto gain {moved:+.1f} dB ({why}) → {self._gain_text()}"
        self.adjustments.append((time.strftime("%H:%M"), text))
        self.s.status.emit(text)
        self.reset()                     # judge the new gain afresh
        return True

    def at_ceiling(self):
        s = self.s
        if s.gain is not None and s.curve is not None:
            return s.curve.db(s.gain.index) >= self.ceiling_db() - 0.5
        if s.soft_only and s.device.pw_percent is not None:
            return s.device.pw_percent >= SOFT_CEILING_PCT
        return True

    def _gain_text(self):
        s = self.s
        if s.gain is not None and s.curve is not None:
            return fmt_gain(s.curve.db(s.gain.index))
        return f"{s.device.pw_percent}%" if s.device and s.device.pw_percent is not None else "?"

    def _step(self, db):
        """Move the gain by about `db`, within the floor and ceiling. Returns
        the dB actually moved, or None."""
        s = self.s
        if s.gain is not None and s.curve is not None:
            here = s.curve.db(s.gain.index)
            target = min(here + db, self.ceiling_db()) if db > 0 else here + db
            index = s.curve.index_for_db(target)
            if index == s.gain.index or (db > 0 and s.curve.db(index) > self.ceiling_db() + 0.01):
                return None
            return s.curve.db(index) - here if s.set_index(index) else None
        if s.soft_only and s.device.pw_percent:
            pct = s.device.pw_percent
            new = max(1, min(SOFT_CEILING_PCT if db > 0 else 150, int(round(pct * 10 ** (db / 60.0)))))
            if new == pct:
                return None
            return 60.0 * math.log10(new / pct) if s.set_soft(new) else None
        return None

    def _judge(self, window):
        if self._clock < window or time.monotonic() < self._quiet_until:
            return
        if self.autogain_enabled() and not self.at_ceiling():
            return                       # auto gain is handling it
        if len(self._speech) * CHUNK_MS / 1000.0 < MIN_SPEECH_S:
            return
        level = percentile([p for _c, p in self._speech], 95)
        short = TARGET_LO_DB - level
        if short < self.margin_db():
            return
        minutes = window / 60
        if self.autogain_enabled():
            self.alert.emit(
                "Microphone too quiet for dictation",
                f"{self.s.device.description}: speech peaks around {fmt_level(level)} with auto gain already "
                f"at its ceiling ({self._gain_text()}). Move closer to the mic, or raise the ceiling in Settings.")
            self._quiet_until = time.monotonic() + REPEAT_S
            self.reset()
            return
        self.alert.emit(
            "Microphone too quiet for dictation",
            f"{self.s.device.description}: over the last {minutes:.0f} min of recording your speech peaked "
            f"around {fmt_level(level)}, {short:.0f} dB under the {TARGET_LO_DB:.0f}…{TARGET_HI_DB:.0f} dBFS "
            f"target. Raise the gain about {target_peak_db(self.s.settings) - level:.0f} dB, or move closer to the mic.")
        self._quiet_until = time.monotonic() + REPEAT_S
        self.reset()

