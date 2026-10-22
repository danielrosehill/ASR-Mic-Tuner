"""Live capture: a `parec` subprocess read on a thread, analysed per channel.

Channels are analysed independently and never downmixed: averaging hides
clipping that affects one channel only, and on real hardware the two
internal-mic channels do not behave identically.
"""

import shutil
import subprocess
import threading
import time
from collections import deque

import numpy as np

from . import APP
from .core import (
    CHUNK_MS, CLIP_LEVEL, CLIP_RUN, FLOOR_DB, FULL_SCALE, NOISE_WINDOW_S,
    PEAK_DECAY_DB_S, PEAK_HOLD_S, RECENT_WINDOW_S, MIN_SPEECH_BLOCKS,
    percentile,
)

SPECTRUM_FRAMES = 8192          # ~170 ms at 48 kHz: enough for 50 Hz hum lines
DC_SMOOTHING = 0.02             # per-block EMA weight for the DC estimate


def dbfs(magnitude):
    if magnitude <= 0:
        return FLOOR_DB
    return max(FLOOR_DB, 20.0 * float(np.log10(magnitude / FULL_SCALE)))


class ChannelStats:
    def __init__(self):
        self.rms_db = FLOOR_DB
        self.peak_db = FLOOR_DB
        self.hold_db = FLOOR_DB
        self.hold_at = 0.0
        self.dc = 0.0                   # fraction of full scale, signed
        self.clipped = 0
        self.runs = 0
        self.longest_run = 0
        self.last_clip_at = None
        self._run = 0
        self._rms_history = deque(maxlen=int(NOISE_WINDOW_S * 1000 / CHUNK_MS))

    @property
    def noise_floor_db(self):
        return percentile(list(self._rms_history), 10)

    def reset_clips(self):
        self.clipped = 0
        self.runs = 0
        self.longest_run = 0
        self.last_clip_at = None
        self._run = 0


class Analyser:
    """Reads `parec` and keeps per-channel level, clipping and DC statistics,
    a short ring of raw audio for the spectrum view, and optional collection
    buffers for calibration and recordings."""

    def __init__(self, source, channels, rate):
        self.source = source
        self.channels = max(1, channels)
        self.rate = rate
        self.stats = [ChannelStats() for _ in range(self.channels)]
        self.error = None
        self.frames = 0
        self.started_at = time.monotonic()
        self._proc = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._collect = None            # [(worst rms, worst peak)] per block
        self._record = None             # [ndarray (frames, channels)]
        self._recent = deque()          # (timestamp, worst-channel peak dBFS)
        self._ring = deque()            # recent blocks for the spectrum
        self._ring_frames = 0

    # -- lifecycle --
    def start(self):
        if not shutil.which("parec"):
            self.error = "parec not found — install pulseaudio-utils"
            return
        try:
            self._proc = subprocess.Popen(
                [
                    "parec",
                    f"--device={self.source}",
                    "--raw",
                    "--format=s16le",
                    f"--rate={self.rate}",
                    f"--channels={self.channels}",
                    "--latency-msec=25",
                    f"--client-name={APP}",
                    "--property=media.role=Production",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            self.error = f"could not start parec: {exc}"
            return
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    @property
    def alive(self):
        return self._proc is not None and self._proc.poll() is None

    # -- collection hooks --
    def begin_collect(self):
        with self._lock:
            self._collect = []

    def end_collect(self):
        with self._lock:
            collected, self._collect = self._collect or [], None
        return collected

    def begin_record(self):
        """Start keeping raw audio, for room-tone and voice samples."""
        with self._lock:
            self._record = []

    def end_record(self):
        """Raw int16 audio since begin_record(), shape (frames, channels)."""
        with self._lock:
            blocks, self._record = self._record or [], None
        if not blocks:
            return np.zeros((0, self.channels), dtype=np.int16)
        return np.concatenate(blocks, axis=0)

    @property
    def recording(self):
        return self._record is not None

    def reset_clips(self):
        with self._lock:
            for stats in self.stats:
                stats.reset_clips()
            self.frames = 0
            self.started_at = time.monotonic()

    def recent_audio(self):
        """The last ~170 ms of audio, (frames, channels) int16, for the spectrum."""
        with self._lock:
            blocks = list(self._ring)
        if not blocks:
            return None
        return np.concatenate(blocks, axis=0)[-SPECTRUM_FRAMES:]

    # -- the loop --
    def _reader(self):
        frames = max(1, self.rate * CHUNK_MS // 1000)
        want = frames * self.channels * 2
        stream = self._proc.stdout
        while not self._stop.is_set():
            data = stream.read(want)
            if not data or len(data) < want:
                break
            block = np.frombuffer(data, dtype="<i2").reshape(-1, self.channels)
            self._analyse(block)
        if not self._stop.is_set():
            self.error = "capture stream ended — device gone or in use exclusively"

    def _analyse(self, block):
        now = time.monotonic()
        worst_rms = FLOOR_DB
        worst_peak = FLOOR_DB
        wide = block.astype(np.int32)
        peaks = np.abs(wide).max(axis=0)
        rms = np.sqrt(np.mean(wide.astype(np.float64) ** 2, axis=0))
        means = wide.mean(axis=0) / FULL_SCALE

        with self._lock:
            self.frames += block.shape[0]
            for channel in range(self.channels):
                stats = self.stats[channel]
                stats.rms_db = dbfs(rms[channel])
                stats.peak_db = dbfs(peaks[channel])
                stats._rms_history.append(stats.rms_db)
                stats.dc += DC_SMOOTHING * (means[channel] - stats.dc)

                # Peak hold: freeze at the highest peak, then bleed away.
                if stats.peak_db >= stats.hold_db:
                    stats.hold_db = stats.peak_db
                    stats.hold_at = now
                elif now - stats.hold_at > PEAK_HOLD_S:
                    elapsed = now - stats.hold_at - PEAK_HOLD_S
                    stats.hold_db = max(
                        stats.peak_db, stats.hold_db - PEAK_DECAY_DB_S * elapsed
                    )
                    stats.hold_at = now - PEAK_HOLD_S

                # The per-sample run scan only happens when a block holds a
                # full-scale sample, which is rare at a sane gain.
                if peaks[channel] >= CLIP_LEVEL:
                    self._count_clips(stats, wide[:, channel], now)
                else:
                    stats._run = 0

                worst_rms = max(worst_rms, stats.rms_db)
                worst_peak = max(worst_peak, stats.peak_db)

            if self._collect is not None:
                self._collect.append((worst_rms, worst_peak))
            if self._record is not None:
                self._record.append(block.copy())

            self._ring.append(block)
            self._ring_frames += block.shape[0]
            while self._ring and self._ring_frames - self._ring[0].shape[0] >= SPECTRUM_FRAMES:
                self._ring_frames -= self._ring.popleft().shape[0]

            self._recent.append((now, worst_peak))
            while self._recent and now - self._recent[0][0] > RECENT_WINDOW_S:
                self._recent.popleft()

    @staticmethod
    def _count_clips(stats, data, now):
        clipped = np.abs(data) >= CLIP_LEVEL
        run_length = stats._run
        for hit in clipped.tolist():
            if hit:
                stats.clipped += 1
                run_length += 1
                if run_length == CLIP_RUN:
                    stats.runs += 1
                if run_length > stats.longest_run:
                    stats.longest_run = run_length
                stats.last_clip_at = now
            else:
                run_length = 0
        stats._run = run_length

    def speech_peak(self, window, gate_db):
        """95th-percentile peak over the last `window` seconds, counting only
        blocks loud enough to be speech. None until there is enough speech."""
        cutoff = time.monotonic() - window
        with self._lock:
            peaks = [p for t, p in self._recent if t >= cutoff and p >= gate_db]
        if len(peaks) < MIN_SPEECH_BLOCKS:
            return None
        return percentile(peaks, 95)

    def snapshot(self):
        with self._lock:
            return [
                {
                    "rms": s.rms_db,
                    "peak": s.peak_db,
                    "hold": s.hold_db,
                    "noise": s.noise_floor_db,
                    "dc": s.dc,
                    "clipped": s.clipped,
                    "runs": s.runs,
                    "longest": s.longest_run,
                    "last_clip": s.last_clip_at,
                }
                for s in self.stats
            ], self.frames / float(self.rate)
