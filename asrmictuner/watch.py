"""Mic-too-quiet alert: listens only while another application is recording.

Every few seconds the watcher asks PipeWire whether anything other than a
level meter (or EasyEffects' own pull) is capturing the selected microphone.
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
"""

import time
from collections import deque

from PyQt6.QtCore import QObject, QTimer, pyqtSignal

from .core import (
    CALIB_TARGET_PEAK_DB, CHUNK_MS, SPEECH_GATE_OVER_NOISE_DB, TARGET_HI_DB, TARGET_LO_DB, fmt_level,
    percentile, recording_clients,
)

TICK_MS = 3000
MIN_SPEECH_S = 20.0         # speech needed in the window before judging
WATCH_GATE_FLOOR_DB = -60.0  # lower than the live verdict's, so a mic that is far too quiet still registers
STALE_GAP_S = 1800          # a gap in recording this long throws the window away
REPEAT_S = 1800             # at most one alert per half hour


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

    # -- state --
    def reset(self):
        self._speech.clear()
        self._clock = 0.0

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
        if not self.enabled():
            return "Off."
        if not self.watching:
            return "Waiting for an application to record from this microphone."
        speech = len(self._speech) * CHUNK_MS / 1000.0
        return (f"Listening alongside {', '.join(sorted(set(self.clients)))}: "
                f"{min(self._clock, self.window_s()) / 60:.1f} of {self.window_s() / 60:.0f} min recorded, {speech:.0f} s of speech.")

    # -- the loop --
    def _tick(self):
        now = time.monotonic()
        if not self.enabled() or self.s.device is None:
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
        blocks = analyser.peaks_since(self._since)
        if blocks:
            self._since = blocks[-1][0]
        self._speech.extend((self._clock, p) for _t, p in blocks if p >= gate)
        window = self.window_s()
        while self._speech and self._clock - self._speech[0][0] > window:
            self._speech.popleft()
        self._judge(window)

    def _judge(self, window):
        if self._clock < window or time.monotonic() < self._quiet_until:
            return
        if len(self._speech) * CHUNK_MS / 1000.0 < MIN_SPEECH_S:
            return
        level = percentile([p for _c, p in self._speech], 95)
        short = TARGET_LO_DB - level
        if short < self.margin_db():
            return
        minutes = window / 60
        self.alert.emit(
            "Microphone too quiet for dictation",
            f"{self.s.device.description}: over the last {minutes:.0f} min of recording your speech peaked "
            f"around {fmt_level(level)}, {short:.0f} dB under the {TARGET_LO_DB:.0f}…{TARGET_HI_DB:.0f} dBFS "
            f"target. Raise the gain about {CALIB_TARGET_PEAK_DB - level:.0f} dB, or move closer to the mic.")
        self._quiet_until = time.monotonic() + REPEAT_S
        self.reset()

