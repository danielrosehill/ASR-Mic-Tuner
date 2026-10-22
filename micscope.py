#!/usr/bin/env python3
"""micscope — see what your microphone is really doing.

Shows the true analog capture gain of a device in dB, read from the ALSA
hardware control rather than from PipeWire's volume, alongside per-channel
dBFS meters with peak hold and a clipped-sample counter. Gain is set by ALSA
control index, so the number on screen is the number in the preamp.

Standing constraint (docs/spec.md §3): micscope never modifies the audio
graph. It runs `parec` to listen and `amixer` to read and set the analog gain.
It loads no module, creates no node, and changes no default.
"""

import argparse
import array
import json
import math
import operator
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

APP = "micscope"
VERSION = "0.1.0"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP
DEVICES_FILE = CONFIG_DIR / "devices.json"
UNIT_NAME = "micscope-restore.service"
UNIT_FILE = (
    Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    / "systemd/user"
    / UNIT_NAME
)

# Audio analysis ------------------------------------------------------------
FULL_SCALE = 32768.0
# A small margin below full scale: the s32->s16 conversion on the way out of
# PipeWire rounds, so a genuinely saturated sample can land a bit under 32767.
CLIP_LEVEL = 32764
CLIP_RUN = 3            # consecutive clipped samples that count as saturation
CHUNK_MS = 25           # analysis block; also the meter's floor on latency
FLOOR_DB = -100.0       # stand-in for digital silence
NOISE_WINDOW_S = 10.0
PEAK_HOLD_S = 1.5
PEAK_DECAY_DB_S = 20.0

# Meter scale ---------------------------------------------------------------
METER_MIN_DB = -60.0
TARGET_LO_DB = -18.0    # speech peaks belong between these two
TARGET_HI_DB = -10.0
RED_DB = -3.0

# Live verdict --------------------------------------------------------------
RECENT_WINDOW_S = 6.0   # how much peak history the analyser keeps
VERDICT_WINDOW_S = 3.0  # how much of it the verdict judges on
MIN_SPEECH_BLOCKS = 8   # ~0.2 s of speech before the verdict commits
SPEECH_GATE_OVER_NOISE_DB = 12.0
SPEECH_GATE_FLOOR_DB = -50.0

# Analog gain zones, in dB of actual preamp gain. Derived per device from its
# own curve at draw time; these are the dB boundaries, never percentages.
ZONE_SAFE_LO = 0.0
ZONE_SAFE_HI = 6.0
ZONE_CAUTION_HI = 15.0

# Calibration ---------------------------------------------------------------
CALIB_QUIET_S = 3.0
CALIB_SPEECH_S = 8.0
CALIB_TARGET_PEAK_DB = -12.0
CALIB_MAX_NOISE_DB = -50.0
CALIB_MIN_HEADROOM_DB = 12.0

# Preference order when a card exposes more than one capture volume.
GAIN_PREFERENCE = (
    "Capture Volume",
    "Digital Capture Volume",
    "Mic Capture Volume",
    "Line Capture Volume",
)

try:                                    # Python 3.12+
    from math import sumprod as _sumprod
except ImportError:                     # pragma: no cover - older interpreters
    def _sumprod(a, b):
        return sum(map(operator.mul, a, b))


# ---------------------------------------------------------------------------
# shell helpers
# ---------------------------------------------------------------------------

def run(cmd, timeout=8):
    """Run a command and return stdout, or "" if it failed in any way."""
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout


def pactl_json(*args):
    out = run(["pactl", "-f", "json", *args])
    if not out.strip():
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def dbfs(magnitude):
    """Sample magnitude (0..32768) to dBFS."""
    if magnitude <= 0:
        return FLOOR_DB
    return max(FLOOR_DB, 20.0 * math.log10(magnitude / FULL_SCALE))


def fmt_gain(db, digits=2):
    """Format an analog gain figure, which is signed and can be -inf (mute)."""
    if db is None:
        return "—"
    if db == float("-inf") or db <= -100.0:
        return "mute"
    return f"{db:+.{digits}f} dB"


def fmt_level(db, digits=1):
    """Format a dBFS figure, which is negative by nature so takes no sign."""
    if db is None:
        return "—"
    if db <= FLOOR_DB:
        return "−∞ dBFS"
    return f"{db:.{digits}f} dBFS"


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * q / 100.0
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def zone_for_db(db):
    if db is None or db < ZONE_SAFE_LO:
        return "low"
    if db <= ZONE_SAFE_HI:
        return "safe"
    if db <= ZONE_CAUTION_HI:
        return "caution"
    return "danger"


# ---------------------------------------------------------------------------
# the gain curve
# ---------------------------------------------------------------------------

_TLV_SCALE = re.compile(r"dBscale-min=(-?[\d.]+)dB,step=(-?[\d.]+)dB(?:,mute=(\d))?")
_TLV_MINMAX = re.compile(r"dBminmax-min=(-?[\d.]+)dB,max=(-?[\d.]+)dB")
_TLV_RANGE = re.compile(r"rangemin=(-?\d+),,rangemax=(-?\d+)")


class Curve:
    """Index -> dB mapping for one ALSA integer control.

    Materialised as a table so that a curve declared by the device's TLV and
    one recovered by probing are indistinguishable to everything downstream.
    Never hard-code a dB figure or a percentage: build this from the device.
    """

    def __init__(self, imin, imax, table, kind):
        self.imin = imin
        self.imax = imax
        self.table = table              # table[i - imin] = dB
        self.kind = kind                # "declared" (TLV) or "probed"

    # -- lookups --
    def db(self, index):
        i = min(max(int(index), self.imin), self.imax)
        return self.table[i - self.imin]

    def db_drawable(self, index):
        """As db(), but with -inf clamped so it can be positioned on a scale."""
        value = self.db(index)
        return -120.0 if value == float("-inf") else value

    def index_for_db(self, target):
        """Nearest index to a wanted dB figure."""
        best, best_err = self.imin, None
        for i in range(self.imin, self.imax + 1):
            value = self.table[i - self.imin]
            if value == float("-inf"):
                continue
            err = abs(value - target)
            if best_err is None or err < best_err:
                best, best_err = i, err
        return best

    # -- description --
    @property
    def min_db(self):
        return self.table[0]

    @property
    def max_db(self):
        return self.table[-1]

    @property
    def step_db(self):
        """Uniform step size, or None if the curve is piecewise."""
        finite = [v for v in self.table if v != float("-inf")]
        if len(finite) < 2:
            return None
        step = (finite[1] - finite[0])
        for a, b in zip(finite, finite[1:]):
            if abs((b - a) - step) > 1e-6:
                return None
        return step

    def describe(self):
        step = self.step_db
        step_text = (
            f"{step:.2f} dB per step" if step is not None else "non-uniform steps"
        )
        return (
            f"range {fmt_gain(self.min_db)} … {fmt_gain(self.max_db)}, "
            f"{step_text} ({self.kind})"
        )

    # -- serialisation, for the probe cache --
    def to_json(self):
        return {
            "imin": self.imin,
            "imax": self.imax,
            "kind": self.kind,
            "table": [None if v == float("-inf") else v for v in self.table],
        }

    @classmethod
    def from_json(cls, data):
        try:
            table = [float("-inf") if v is None else float(v) for v in data["table"]]
            return cls(int(data["imin"]), int(data["imax"]), table, data["kind"])
        except (KeyError, TypeError, ValueError):
            return None


def curve_from_tlv(tlv_lines, imin, imax):
    """Build a Curve from the `| ...` block that `amixer contents` prints.

    Handles dBscale, dBminmax, and dBrange (a sequence of either, each scoped
    to a sub-range of the index space). Returns None if the device declared
    nothing usable, which is the signal to fall back to probing.
    """
    size = imax - imin + 1
    if size <= 0:
        return None
    table = [None] * size
    pending = None
    mute_min = False

    for line in tlv_lines:
        match = _TLV_RANGE.search(line)
        if match:
            pending = (int(match.group(1)), int(match.group(2)))
            continue
        lo, hi = pending if pending else (imin, imax)
        lo, hi = max(lo, imin), min(hi, imax)

        match = _TLV_SCALE.search(line)
        if match:
            base, step = float(match.group(1)), float(match.group(2))
            if match.group(3) == "1":
                mute_min = True
            for i in range(lo, hi + 1):
                table[i - imin] = base + step * (i - lo)
            pending = None
            continue

        match = _TLV_MINMAX.search(line)
        if match:
            db_lo, db_hi = float(match.group(1)), float(match.group(2))
            span = max(hi - lo, 1)
            for i in range(lo, hi + 1):
                table[i - imin] = db_lo + (db_hi - db_lo) * (i - lo) / span
            pending = None
            continue

    if any(v is None for v in table):
        return None
    # ALSA's convention: mute=1 means the lowest index is off, not a dB figure.
    if mute_min:
        table[0] = float("-inf")
    # Some cards declare an "off" step as an absurd negative rather than a
    # mute flag; treat anything below -200 dB as off so it prints honestly.
    table = [float("-inf") if v <= -200.0 else v for v in table]
    return Curve(imin, imax, table, "declared")


# ---------------------------------------------------------------------------
# ALSA mixer controls
# ---------------------------------------------------------------------------

_C_HEAD = re.compile(r"^numid=(\d+),iface=(\w+),name='(.*)'\s*$")
_C_TYPE = re.compile(
    r"type=(\w+),access=([\w-]+),values=(\d+)"
    r"(?:,min=(-?\d+),max=(-?\d+),step=(-?\d+))?"
)
_C_VALUES = re.compile(r"^\s*: values=(.*)$")
_SGET_DB = re.compile(r"\[(-?[\d.]+)dB\]")


class MixerControl:
    def __init__(self, card, numid, iface, name):
        self.card = card
        self.numid = numid
        self.iface = iface
        self.name = name
        self.ctype = ""
        self.access = ""
        self.nvalues = 1
        self.imin = 0
        self.imax = 0
        self.values = []
        self.tlv = []
        self.curve = None

    @property
    def writable(self):
        return len(self.access) > 1 and self.access[1] == "w"

    @property
    def simple_name(self):
        """The name alsa-lib's simple mixer uses, e.g. 'Capture Volume' -> 'Capture'."""
        return re.sub(r" (Volume|Switch)$", "", self.name)

    @property
    def index(self):
        return max(self.values) if self.values else self.imin

    @property
    def db(self):
        return self.curve.db(self.index) if self.curve else None

    def set_index(self, index):
        index = min(max(int(index), self.imin), self.imax)
        payload = ",".join([str(index)] * max(self.nvalues, 1))
        out = run(
            ["amixer", "-c", str(self.card), "cset", f"numid={self.numid}", payload]
        )
        if out:
            self.values = [index] * max(self.nvalues, 1)
            return True
        return False

    def read_db_from_alsa(self):
        """What `amixer sget` says right now — the independent check on the TLV."""
        out = run(["amixer", "-c", str(self.card), "sget", self.simple_name])
        match = _SGET_DB.search(out)
        return float(match.group(1)) if match else None


def read_controls(card):
    """Every control on a card, with its TLV, from a single amixer call."""
    out = run(["amixer", "-c", str(card), "contents"])
    controls = []
    current = None
    in_tlv = False

    for line in out.splitlines():
        head = _C_HEAD.match(line)
        if head:
            current = MixerControl(
                card, int(head.group(1)), head.group(2), head.group(3)
            )
            controls.append(current)
            in_tlv = False
            continue
        if current is None:
            continue

        stripped = line.strip()
        if stripped.startswith(";"):
            in_tlv = False
            match = _C_TYPE.search(line)
            if match:
                current.ctype = match.group(1)
                current.access = match.group(2)
                current.nvalues = int(match.group(3))
                if match.group(4) is not None:
                    current.imin = int(match.group(4))
                    current.imax = int(match.group(5))
            continue
        if stripped.startswith(":"):
            in_tlv = False
            match = _C_VALUES.match(line)
            if match and current.ctype == "INTEGER":
                try:
                    current.values = [int(v) for v in match.group(1).split(",")]
                except ValueError:
                    current.values = []
            continue
        if stripped.startswith("|"):
            in_tlv = True
            current.tlv.append(stripped)
            continue
        if in_tlv and line.startswith("    "):
            current.tlv.append(stripped)

    for control in controls:
        if control.ctype == "INTEGER" and control.imax > control.imin:
            control.curve = curve_from_tlv(control.tlv, control.imin, control.imax)
    return controls


def pick_gain_control(controls):
    candidates = [
        c for c in controls
        if c.iface == "MIXER" and c.ctype == "INTEGER" and c.writable
    ]
    for wanted in GAIN_PREFERENCE:
        for control in candidates:
            if control.name == wanted:
                return control
    for control in candidates:
        if control.name.endswith("Capture Volume"):
            return control
    return None


def pick_boost_controls(controls):
    return [
        c for c in controls
        if c.iface == "MIXER" and c.ctype == "INTEGER"
        and "Boost" in c.name and "Volume" in c.name
    ]


def probe_curve(control, points=5):
    """Recover a curve by writing indices and reading back the dB alsa reports.

    Only used when the device declares no usable TLV. Writes to the control
    and restores the original value; the caller caches the result so this
    happens once per device rather than once per launch.
    """
    if not control.writable or control.imax <= control.imin:
        return None
    original = list(control.values)
    span = control.imax - control.imin
    probes = sorted({
        control.imin + round(span * n / (points - 1)) for n in range(points)
    })

    measured = []
    try:
        for index in probes:
            if not control.set_index(index):
                return None
            db = control.read_db_from_alsa()
            if db is None:
                return None
            measured.append((index, db))
    finally:
        if original:
            payload = ",".join(str(v) for v in original)
            run(["amixer", "-c", str(control.card), "cset",
                 f"numid={control.numid}", payload])
            control.values = original

    if len(measured) < 2:
        return None
    table = []
    for i in range(control.imin, control.imax + 1):
        lo = max([p for p in measured if p[0] <= i], key=lambda p: p[0], default=None)
        hi = min([p for p in measured if p[0] >= i], key=lambda p: p[0], default=None)
        if lo is None:
            table.append(hi[1])
        elif hi is None or hi[0] == lo[0]:
            table.append(lo[1])
        else:
            frac = (i - lo[0]) / (hi[0] - lo[0])
            table.append(lo[1] + (hi[1] - lo[1]) * frac)
    return Curve(control.imin, control.imax, table, "probed")


# ---------------------------------------------------------------------------
# devices
# ---------------------------------------------------------------------------

_SPEC = re.compile(r"(\d+)ch")
_RATE = re.compile(r"(\d+)Hz")


class Device:
    def __init__(self, entry):
        props = entry.get("properties") or {}
        self.name = entry.get("name", "")
        self.description = entry.get("description") or self.name
        self.card = None
        if props.get("device.api") == "alsa" and props.get("alsa.card") is not None:
            try:
                self.card = int(props["alsa.card"])
            except (TypeError, ValueError):
                self.card = None
        self.card_name = props.get("alsa.card_name", "")
        self.alsa_name = props.get("alsa.name", "")

        spec = entry.get("sample_specification", "")
        channels = _SPEC.search(spec)
        rate = _RATE.search(spec)
        self.channels = int(channels.group(1)) if channels else 2
        self.rate = int(rate.group(1)) if rate else 48000
        self.channel_map = [
            c.strip() for c in (entry.get("channel_map") or "").split(",") if c.strip()
        ]

        volume = entry.get("volume") or {}
        percents, dbs = [], []
        for chan in volume.values():
            if isinstance(chan, dict):
                text = str(chan.get("value_percent", "")).rstrip("%")
                try:
                    percents.append(int(text))
                except ValueError:
                    pass
                match = re.search(r"(-?[\d.]+)", str(chan.get("db", "")))
                if match:
                    dbs.append(float(match.group(1)))
        self.pw_percent = max(percents) if percents else None
        self.pw_db = max(dbs) if dbs else None

    @property
    def is_hardware(self):
        return self.card is not None

    @property
    def label(self):
        kind = "hardware" if self.is_hardware else "processed / virtual"
        return f"{self.description}  [{kind}]"

    def channel_labels(self):
        if len(self.channel_map) == self.channels:
            return [c.replace("-", " ").title() for c in self.channel_map]
        return [f"Channel {i + 1}" for i in range(self.channels)]


def list_devices():
    """Capture sources, hardware first, monitors excluded."""
    entries = pactl_json("list", "sources") or []
    devices = [
        Device(e) for e in entries
        if not str(e.get("name", "")).endswith(".monitor")
    ]
    devices.sort(key=lambda d: (not d.is_hardware, d.description.lower()))
    return devices


def default_source():
    return run(["pactl", "get-default-source"]).strip()


def resolve_hardware_for(virtual_name):
    """Best effort: walk pw-link backwards from a virtual source to its input.

    PipeWire node names do not always equal PulseAudio source names (a source
    `asr_mic` shows up as node `asr_mic_capture`), so matching is by prefix.
    """
    out = run(["pw-link", "-l"])
    node = None
    for line in out.splitlines():
        if not line.startswith((" ", "\t")):
            node = line.split(":", 1)[0].strip()
            continue
        if node is None or not node.startswith(virtual_name):
            continue
        stripped = line.strip()
        if stripped.startswith("|<-"):
            upstream = stripped[3:].strip().split(":", 1)[0]
            if upstream.startswith("alsa_input."):
                return upstream
    return None


# ---------------------------------------------------------------------------
# saved settings
# ---------------------------------------------------------------------------

def load_config():
    try:
        data = json.loads(DEVICES_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"devices": {}, "curves": {}}
    data.setdefault("devices", {})
    data.setdefault("curves", {})
    return data


def save_config(config):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DEVICES_FILE.write_text(json.dumps(config, indent=2) + "\n")


def curve_cache_key(device, control):
    return f"{device.card_name}|{device.alsa_name}|{control.name}"


# ---------------------------------------------------------------------------
# audio analysis
# ---------------------------------------------------------------------------

class ChannelStats:
    def __init__(self):
        self.rms_db = FLOOR_DB
        self.peak_db = FLOOR_DB
        self.hold_db = FLOOR_DB
        self.hold_at = 0.0
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
    """Reads `parec` and keeps per-channel level and clipping statistics.

    Channels are analysed independently and never downmixed: averaging hides
    clipping that affects one channel only, and on real hardware the two
    internal-mic channels do not behave identically.
    """

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
        self._collect = None
        self._recent = deque()          # (timestamp, worst-channel peak dBFS)

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

    # -- calibration hooks --
    def begin_collect(self):
        with self._lock:
            self._collect = []

    def end_collect(self):
        with self._lock:
            collected, self._collect = self._collect or [], None
        return collected

    def reset_clips(self):
        with self._lock:
            for stats in self.stats:
                stats.reset_clips()
            self.frames = 0
            self.started_at = time.monotonic()

    # -- the loop --
    def _reader(self):
        frames = max(1, self.rate * CHUNK_MS // 1000)
        want = frames * self.channels * 2
        stream = self._proc.stdout
        while not self._stop.is_set():
            data = stream.read(want)
            if not data or len(data) < want:
                break
            samples = array.array("h")
            samples.frombytes(data)
            if sys.byteorder != "little":
                samples.byteswap()
            self._analyse(samples, frames)
        if not self._stop.is_set():
            self.error = "capture stream ended — device gone or in use exclusively"

    def _analyse(self, samples, frames):
        now = time.monotonic()
        worst_rms = FLOOR_DB
        worst_peak = FLOOR_DB

        with self._lock:
            self.frames += frames
            for channel in range(self.channels):
                data = samples[channel::self.channels]
                stats = self.stats[channel]
                if not data:
                    continue

                peak = max(max(data), -min(data))
                rms = math.sqrt(_sumprod(data, data) / len(data))
                stats.rms_db = dbfs(rms)
                stats.peak_db = dbfs(peak)
                stats._rms_history.append(stats.rms_db)

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

                # Fast path: without a full-scale sample there is nothing to
                # count, so the per-sample scan only runs when it can matter.
                if peak >= CLIP_LEVEL:
                    self._count_clips(stats, data, now)
                else:
                    stats._run = 0

                worst_rms = max(worst_rms, stats.rms_db)
                worst_peak = max(worst_peak, stats.peak_db)

            if self._collect is not None:
                self._collect.append((worst_rms, worst_peak))

            self._recent.append((now, worst_peak))
            while self._recent and now - self._recent[0][0] > RECENT_WINDOW_S:
                self._recent.popleft()

    def speech_peak(self, window, gate_db):
        """95th-percentile peak over the last `window` seconds, counting only
        blocks loud enough to be speech.

        The live verdict has to answer "how loud am I *talking*", so blocks of
        room tone are excluded rather than averaged in. Returns None until
        there is enough speech to judge.
        """
        cutoff = time.monotonic() - window
        with self._lock:
            peaks = [p for t, p in self._recent if t >= cutoff and p >= gate_db]
        if len(peaks) < MIN_SPEECH_BLOCKS:
            return None
        return percentile(peaks, 95)

    @staticmethod
    def _count_clips(stats, data, now):
        run_length = stats._run
        for sample in data:
            if sample >= CLIP_LEVEL or sample <= -CLIP_LEVEL:
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

    def snapshot(self):
        with self._lock:
            return [
                {
                    "rms": s.rms_db,
                    "peak": s.peak_db,
                    "hold": s.hold_db,
                    "noise": s.noise_floor_db,
                    "clipped": s.clipped,
                    "runs": s.runs,
                    "longest": s.longest_run,
                    "last_clip": s.last_clip_at,
                }
                for s in self.stats
            ], self.frames / float(self.rate)


# ---------------------------------------------------------------------------
# headless modes
# ---------------------------------------------------------------------------

def device_report():
    lines = []
    default = default_source()
    for device in list_devices():
        mark = " (system default)" if device.name == default else ""
        lines.append(f"{device.name}{mark}")
        lines.append(f"  {device.description}")
        lines.append(
            f"  {device.channels} ch @ {device.rate} Hz | "
            + ("hardware, card %d" % device.card if device.is_hardware
               else "processed / virtual — no analog gain")
        )
        if device.pw_percent is not None:
            lines.append(
                f"  PipeWire reports: {device.pw_percent}% / "
                f"{fmt_gain(device.pw_db)}  (not the analog gain)"
            )
        if not device.is_hardware:
            underlying = resolve_hardware_for(device.name)
            if underlying:
                lines.append(f"  fed by: {underlying}")
            lines.append("")
            continue

        controls = read_controls(device.card)
        gain = pick_gain_control(controls)
        if gain is None:
            lines.append("  no capture volume control found on this card")
            lines.append("")
            continue
        curve = gain.curve or Curve.from_json(
            load_config()["curves"].get(curve_cache_key(device, gain), {})
        )
        if curve is None:
            lines.append(f"  {gain.name}: index {gain.index}/{gain.imax}, curve unknown")
        else:
            reported = curve.db(gain.index)
            actual = gain.read_db_from_alsa()
            agree = actual is None or abs(actual - reported) < 0.05
            lines.append(
                f"  {gain.name}: index {gain.index}/{gain.imax} = "
                f"{fmt_gain(reported)}   [{curve.describe()}]"
            )
            lines.append(
                f"  amixer sget says {fmt_gain(actual)} — "
                + ("matches" if agree else "MISMATCH")
            )
        for boost in pick_boost_controls(controls):
            note = "" if boost.index == boost.imin else "  <-- stacks on capture gain"
            lines.append(
                f"  {boost.name}: index {boost.index}/{boost.imax} = "
                f"{fmt_gain(boost.db)}{note}"
            )
        lines.append("")
    return "\n".join(lines)


def restore_saved(delay=0.0):
    """Reapply saved analog gain. Used by the optional user systemd unit,
    because wireplumber restores its own per-route volume after login."""
    if delay:
        time.sleep(delay)
    config = load_config()
    saved = config.get("devices", {})
    if not saved:
        print("micscope: nothing saved")
        return 0
    devices = {d.name: d for d in list_devices()}
    applied = 0
    for name, entry in saved.items():
        device = devices.get(name)
        if device is None or not device.is_hardware:
            print(f"micscope: {name} not present, skipped")
            continue
        control = pick_gain_control(read_controls(device.card))
        if control is None or control.numid != entry.get("numid"):
            print(f"micscope: control moved on {name}, skipped")
            continue
        if control.index == entry["index"]:
            print(f"micscope: {name} already at index {control.index}")
            continue
        if control.set_index(entry["index"]):
            print(f"micscope: {name} set to index {entry['index']}")
            applied += 1
    return 0 if applied or saved else 1


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def build_gui():
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import GLib, Gtk

    C_TRACK = (0.11, 0.11, 0.12)
    C_LOW = (0.23, 0.48, 0.84)
    C_TARGET = (0.18, 0.64, 0.31)
    C_CAUTION = (0.85, 0.64, 0.25)
    C_DANGER = (0.83, 0.20, 0.20)
    C_HOLD = (0.95, 0.95, 0.95)
    C_QUIET = (0.30, 0.42, 0.60)

    # (from dB, to dB, colour, alpha) painted across the whole meter track.
    METER_BANDS = (
        (METER_MIN_DB, TARGET_LO_DB, C_QUIET, 0.30),
        (TARGET_LO_DB, TARGET_HI_DB, C_TARGET, 0.55),
        (TARGET_HI_DB, RED_DB, C_DANGER, 0.40),
        (RED_DB, 0.0, C_DANGER, 0.75),
    )

    # Captions under the scale, so the bands need no legend.
    BAND_CAPTIONS = (
        (METER_MIN_DB, TARGET_LO_DB, "too quiet"),
        (TARGET_LO_DB, TARGET_HI_DB, "good"),
        (TARGET_HI_DB, 0.0, "too loud"),
    )

    def db_to_x(db, width):
        span = 0.0 - METER_MIN_DB
        frac = (max(db, METER_MIN_DB) - METER_MIN_DB) / span
        return max(0.0, min(1.0, frac)) * width

    def level_colour(db):
        if db > RED_DB:
            return C_DANGER
        if db > TARGET_HI_DB:
            return C_CAUTION
        if db >= TARGET_LO_DB:
            return C_TARGET
        return C_LOW

    class LevelMeter(Gtk.DrawingArea):
        """Per-channel dBFS meters: RMS bar, peak-hold marker, target band.

        Carries its own numbers so the window needs no separate readout table.
        """

        BAR_H = 22
        GAP = 5
        SCALE_H = 28
        LABEL_W = 78
        NUMBER_W = 140

        def __init__(self):
            super().__init__()
            self.labels = ["Channel 1"]
            self.data = []
            self.connect("draw", self._draw)
            self._resize()

        def configure(self, labels):
            self.labels = labels
            self.data = []
            self._resize()

        def _resize(self):
            count = max(1, len(self.labels))
            height = count * (self.BAR_H + self.GAP) + self.SCALE_H
            self.set_size_request(-1, height)

        def update(self, data):
            self.data = data
            self.queue_draw()

        def _draw(self, _widget, cr):
            width = self.get_allocated_width()
            track_x = self.LABEL_W
            track_w = max(60, width - self.LABEL_W - self.NUMBER_W)
            # Text outside the (always dark) track has to read against whatever
            # the user's theme paints behind it, so take the theme's own colour.
            style = self.get_style_context()
            fg = style.get_color(style.get_state())
            ink = (fg.red, fg.green, fg.blue)

            cr.select_font_face("Sans")
            cr.set_font_size(10)

            for row, name in enumerate(self.labels):
                y = row * (self.BAR_H + self.GAP)
                stats = self.data[row] if row < len(self.data) else None

                cr.set_source_rgba(*ink, 0.85)
                cr.move_to(0, y + self.BAR_H - 7)
                cr.show_text(name[:12])

                cr.set_source_rgb(*C_TRACK)
                cr.rectangle(track_x, y, track_w, self.BAR_H)
                cr.fill()

                # The whole track is banded, so where a peak lands answers
                # "too quiet / good / too loud" without reading any number.
                for lo_db, hi_db, colour, alpha in METER_BANDS:
                    lo = db_to_x(lo_db, track_w)
                    hi = db_to_x(hi_db, track_w)
                    cr.set_source_rgba(*colour, alpha)
                    cr.rectangle(track_x + lo, y, hi - lo, self.BAR_H)
                    cr.fill()

                if stats is not None:
                    bar = db_to_x(stats["rms"], track_w)
                    cr.set_source_rgb(*level_colour(stats["rms"]))
                    cr.rectangle(track_x, y + 2, bar, self.BAR_H - 4)
                    cr.fill()

                    hold_x = db_to_x(stats["hold"], track_w)
                    if stats["hold"] > METER_MIN_DB:
                        cr.set_source_rgb(*C_HOLD)
                        cr.rectangle(track_x + max(0, hold_x - 1), y, 2, self.BAR_H)
                        cr.fill()

                cr.set_source_rgba(*ink, 0.35)
                cr.set_line_width(1)
                cr.rectangle(track_x + 0.5, y + 0.5, track_w - 1, self.BAR_H - 1)
                cr.stroke()

                if stats is not None:
                    cr.select_font_face("Monospace")
                    cr.set_font_size(10)
                    cr.set_source_rgba(*ink, 0.9)
                    cr.move_to(track_x + track_w + 8, y + self.BAR_H - 7)
                    cr.show_text(
                        f"{stats['rms']:6.1f}  peak {stats['hold']:6.1f}"
                    )
                    cr.select_font_face("Sans")
                    cr.set_font_size(10)

            base = len(self.labels) * (self.BAR_H + self.GAP)
            cr.set_source_rgba(*ink, 0.7)
            cr.set_font_size(9)
            for db in range(int(METER_MIN_DB), 1, 10):
                x = track_x + db_to_x(db, track_w)
                cr.move_to(x, base)
                cr.line_to(x, base + 3)
                cr.stroke()
                text = str(db)
                extents = cr.text_extents(text)
                cr.move_to(x - extents.width / 2, base + 12)
                cr.show_text(text)
            cr.move_to(0, base + 12)
            cr.show_text("dBFS")

            for lo_db, hi_db, caption in BAND_CAPTIONS:
                lo = track_x + db_to_x(lo_db, track_w)
                hi = track_x + db_to_x(hi_db, track_w)
                extents = cr.text_extents(caption)
                if extents.width > hi - lo - 4:
                    continue
                cr.move_to((lo + hi - extents.width) / 2, base + 24)
                cr.show_text(caption)

    class ZoneStrip(Gtk.DrawingArea):
        """Colour bar above the gain slider: which part of this device's own
        curve is safe, and where speech starts saturating the ADC.

        Tracks the slider's trough rather than its own width, so the colours
        line up with the handle instead of drifting from it.
        """

        HANDLE_INSET = 10       # half a slider handle; GTK exposes no metric

        def __init__(self, scale):
            super().__init__()
            self.scale = scale
            self.curve = None
            self.index = None
            self.set_size_request(-1, 9)
            self.connect("draw", self._draw)

        def configure(self, curve, index):
            self.curve = curve
            self.index = index
            self.queue_draw()

        def _draw(self, _widget, cr):
            height = self.get_allocated_height()
            if self.curve is None:
                return
            rect = self.scale.get_range_rect()
            left = rect.x + self.HANDLE_INSET
            width = max(1, rect.width - 2 * self.HANDLE_INSET)

            curve = self.curve
            steps = curve.imax - curve.imin + 1
            colours = {
                "low": C_LOW, "safe": C_TARGET,
                "caution": C_CAUTION, "danger": C_DANGER,
            }
            for n in range(steps):
                x0 = left + width * n / steps
                x1 = left + width * (n + 1) / steps
                zone = zone_for_db(curve.db_drawable(curve.imin + n))
                cr.set_source_rgba(*colours[zone], 0.8)
                cr.rectangle(x0, 0, x1 - x0 + 0.6, height)
                cr.fill()
            if self.index is not None:
                x = left + width * (self.index - curve.imin + 0.5) / steps
                cr.set_source_rgb(*C_HOLD)
                cr.rectangle(x - 1, 0, 2, height)
                cr.fill()

    def left_label(markup=""):
        label = Gtk.Label(xalign=0)
        label.set_markup(markup)
        label.set_line_wrap(True)
        return label

    class MicScope(Gtk.Window):
        def __init__(self):
            super().__init__(title="micscope")
            self.set_default_size(820, 430)
            self.set_border_width(0)

            self.config = load_config()
            self.devices = []
            self.device = None
            self.controls = []
            self.gain = None
            self.curve = None
            self.boosts = []
            self.analyser = None
            self.calibration = None
            self.recommendation = None
            self._fix_target = None
            self._label_cache = {}
            self._setting_slider = False
            self._filling_devices = False

            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            self.add(scroller)

            root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            root.set_border_width(12)
            scroller.add(root)

            root.pack_start(self._build_header(), False, False, 0)
            self.warnings = left_label()
            root.pack_start(self.warnings, False, False, 0)
            root.pack_start(self._build_gain(), False, False, 4)
            root.pack_start(self._build_level(), False, False, 4)
            root.pack_start(self._build_calibration(), False, False, 0)
            root.pack_start(self._build_details(), False, False, 0)
            self.status = left_label()
            root.pack_start(self.status, False, False, 0)

            self.rescan()
            GLib.timeout_add(33, self._fast_tick)
            GLib.timeout_add(2000, self._slow_tick)
            self.connect("destroy", self._on_destroy)

        # -- construction --
        def _build_header(self):
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            box.pack_start(Gtk.Label(label="Device"), False, False, 0)
            self.device_combo = Gtk.ComboBoxText()
            self.device_combo.connect("changed", self._on_device_changed)
            box.pack_start(self.device_combo, True, True, 0)
            button = Gtk.Button(label="Rescan")
            button.connect("clicked", lambda _b: self.rescan())
            box.pack_start(button, False, False, 0)
            return box

        def _build_gain(self):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            self.gain_value = left_label()
            self.gain_value.set_markup("<span size='xx-large'>—</span>")
            box.pack_start(self.gain_value, False, False, 0)

            self.gain_scale = Gtk.Scale.new_with_range(
                Gtk.Orientation.HORIZONTAL, 0, 1, 1
            )
            self.gain_scale.set_draw_value(False)
            self.gain_scale.connect("value-changed", self._on_gain_changed)
            self.zone_strip = ZoneStrip(self.gain_scale)
            box.pack_start(self.zone_strip, False, False, 0)
            box.pack_start(self.gain_scale, False, False, 0)
            return box

        def _build_level(self):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            self.meter = LevelMeter()
            box.pack_start(self.meter, False, False, 0)

            verdict_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            self.verdict = left_label()
            verdict_row.pack_start(self.verdict, True, True, 0)
            self.fix_button = Gtk.Button(label="Fix it")
            self.fix_button.set_tooltip_text(
                "Move the gain so your speech peaks land at −12 dBFS"
            )
            self.fix_button.connect("clicked", self._on_fix)
            self.fix_button.set_no_show_all(True)
            verdict_row.pack_end(self.fix_button, False, False, 0)
            box.pack_start(verdict_row, False, False, 4)

            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            self.clip_summary = left_label()
            row.pack_start(self.clip_summary, True, True, 0)
            reset = Gtk.Button(label="Reset")
            reset.set_tooltip_text("Restart the clipping counters and the timer")
            reset.connect("clicked", self._on_reset_clips)
            row.pack_end(reset, False, False, 0)
            box.pack_start(row, False, False, 0)
            return box

        def _build_calibration(self):
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            self.calib_button = Gtk.Button(label="Calibrate")
            self.calib_button.set_tooltip_text(
                "Measure the room, then your voice, and recommend a gain that "
                "puts speech peaks at −12 dBFS"
            )
            self.calib_button.connect("clicked", self._on_calibrate)
            row.pack_start(self.calib_button, False, False, 0)
            self.apply_button = Gtk.Button(label="Apply")
            self.apply_button.connect("clicked", self._on_apply_recommendation)
            self.apply_button.set_sensitive(False)
            row.pack_start(self.apply_button, False, False, 0)
            self.calib_progress = Gtk.ProgressBar()
            self.calib_progress.set_show_text(True)
            self.calib_progress.set_valign(Gtk.Align.CENTER)
            self.calib_progress.set_no_show_all(True)
            row.pack_start(self.calib_progress, True, True, 0)
            box.pack_start(row, False, False, 0)

            self.calib_result = left_label()
            box.pack_start(self.calib_result, False, False, 0)
            return box

        def _build_details(self):
            """Everything true but rarely needed, folded away by default."""
            expander = Gtk.Expander(label="Details")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.set_border_width(8)
            self.curve_label = left_label()
            self.boost_label = left_label()
            self.pw_label = left_label()
            for widget in (self.curve_label, self.pw_label, self.boost_label):
                box.pack_start(widget, False, False, 0)

            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            self.remember_check = Gtk.CheckButton(label="Remember this gain")
            self.remember_check.connect("toggled", self._on_remember)
            row.pack_start(self.remember_check, False, False, 0)
            self.login_check = Gtk.CheckButton(label="Reapply at login")
            self.login_check.connect("toggled", self._on_login)
            row.pack_start(self.login_check, False, False, 0)
            self.restore_button = Gtk.Button(label="Restore remembered")
            self.restore_button.connect("clicked", self._on_restore)
            self.restore_button.set_no_show_all(True)
            row.pack_end(self.restore_button, False, False, 0)
            box.pack_start(row, False, False, 0)

            self.drift_label = left_label()
            box.pack_start(self.drift_label, False, False, 0)
            expander.add(box)
            return expander

        # -- device handling --
        def rescan(self):
            self.devices = list_devices()
            selected = self.device.name if self.device else None
            self._filling_devices = True
            self.device_combo.remove_all()
            for device in self.devices:
                self.device_combo.append_text(device.label)
            self._filling_devices = False
            if not self.devices:
                self.status.set_markup(
                    "<span foreground='#c22'>No capture sources found. "
                    "Is PipeWire running?</span>"
                )
                return
            index = 0
            for n, device in enumerate(self.devices):
                if device.name == selected:
                    index = n
                    break
            else:
                for n, device in enumerate(self.devices):
                    if device.is_hardware:
                        index = n
                        break
            self.device_combo.set_active(index)

        def _on_device_changed(self, combo):
            if self._filling_devices:
                return
            active = combo.get_active()
            if active < 0 or active >= len(self.devices):
                return
            self.select_device(self.devices[active])

        def select_device(self, device):
            self._stop_analyser()
            self.device = device
            self.calibration = None
            self.recommendation = None
            self.apply_button.set_sensitive(False)
            self.calib_result.set_markup("")
            self._load_mixer()
            self.meter.configure(device.channel_labels())
            self.analyser = Analyser(device.name, device.channels, device.rate)
            self.analyser.start()
            self._refresh_gain_pane()
            self._refresh_warnings()

        def _load_mixer(self):
            self.controls, self.gain, self.curve, self.boosts = [], None, None, []
            if not self.device or not self.device.is_hardware:
                return
            self.controls = read_controls(self.device.card)
            self.gain = pick_gain_control(self.controls)
            self.boosts = pick_boost_controls(self.controls)
            if self.gain is None:
                return
            self.curve = self.gain.curve
            if self.curve is not None:
                return
            # No usable TLV: use the cached probe if we have one, else probe
            # once (which writes, and restores) and cache it.
            key = curve_cache_key(self.device, self.gain)
            cached = self.config["curves"].get(key)
            if cached:
                self.curve = Curve.from_json(cached)
            if self.curve is None:
                self.status.set_markup(
                    "<small>Probing the gain curve (device declares none)…</small>"
                )
                self.curve = probe_curve(self.gain)
                if self.curve is not None:
                    self.config["curves"][key] = self.curve.to_json()
                    save_config(self.config)

        def _stop_analyser(self):
            if self.analyser:
                self.analyser.stop()
                self.analyser = None

        # -- gain pane --
        def _refresh_gain_pane(self):
            saved = self.config["devices"].get(self.device.name) if self.device else None
            self.remember_check.handler_block_by_func(self._on_remember)
            self.remember_check.set_active(saved is not None)
            self.remember_check.handler_unblock_by_func(self._on_remember)
            self.login_check.handler_block_by_func(self._on_login)
            self.login_check.set_active(unit_enabled())
            self.login_check.handler_unblock_by_func(self._on_login)

            if not self.device:
                return
            if not self.device.is_hardware:
                underlying = resolve_hardware_for(self.device.name)
                hint = (
                    f"fed by <b>{underlying}</b> — select it to set the gain"
                    if underlying else
                    "no underlying hardware device found"
                )
                self.gain_value.set_markup(
                    "<span size='large'>Processed stream — no analog gain</span>"
                    f"\n<small>{hint}</small>"
                )
                self.gain_scale.set_sensitive(False)
                self.zone_strip.configure(None, None)
                self.curve_label.set_markup("")
                self.boost_label.set_markup("")
                self._set_pw_label()
                self.drift_label.set_markup("")
                self.remember_check.set_sensitive(False)
                return

            self.remember_check.set_sensitive(self.gain is not None)
            if self.gain is None:
                self.gain_value.set_markup(
                    "<span size='x-large'>No capture volume control on this card"
                    "</span>"
                )
                self.gain_scale.set_sensitive(False)
                self.zone_strip.configure(None, None)
                self.curve_label.set_markup("")
                self._set_pw_label()
                return

            self.gain_scale.set_sensitive(True)
            self._setting_slider = True
            self.gain_scale.set_range(self.gain.imin, self.gain.imax)
            self.gain_scale.set_increments(1, 4)
            self.gain_scale.clear_marks()
            if self.curve is not None:
                for db, text in (
                    (ZONE_SAFE_LO, "0 dB"),
                    (ZONE_SAFE_HI, f"+{ZONE_SAFE_HI:.0f}"),
                    (ZONE_CAUTION_HI, f"+{ZONE_CAUTION_HI:.0f}"),
                ):
                    if self.curve.min_db <= db <= self.curve.max_db:
                        self.gain_scale.add_mark(
                            self.curve.index_for_db(db),
                            Gtk.PositionType.BOTTOM,
                            text,
                        )
            self.gain_scale.set_value(self.gain.index)
            self._setting_slider = False

            self._update_gain_readout()

        def _update_gain_readout(self):
            if self.gain is None:
                return
            index = self.gain.index
            if self.curve is None:
                self.gain_value.set_markup(
                    f"<span size='xx-large'>index {index} / {self.gain.imax}</span>"
                    "\n<small>device declares no dB curve and probing failed"
                    "</small>"
                )
                self.curve_label.set_markup("")
            else:
                db = self.curve.db(index)
                colour = {
                    "low": "#3d7ad6", "safe": "#2ea44f",
                    "caution": "#c99a2e", "danger": "#c22",
                }[zone_for_db(db)]
                self.gain_value.set_markup(
                    f"<span size='xx-large' foreground='{colour}'><b>"
                    f"{fmt_gain(db)}</b></span>"
                    f"  <span size='large'>(index {index} / {self.gain.imax})</span>"
                )
                self.curve_label.set_markup(
                    f"<small>{self.gain.name}: {self.curve.describe()}</small>"
                )
                self.zone_strip.configure(self.curve, index)

            boosts = []
            for boost in self.boosts:
                text = f"{boost.name}: {fmt_gain(boost.db)} (index {boost.index}/{boost.imax})"
                if boost.index > boost.imin:
                    text = (
                        f"<span foreground='#c22'>{text} — stacks on top of "
                        f"capture gain</span>"
                    )
                boosts.append(text)
            self.boost_label.set_markup("<small>" + "\n".join(boosts) + "</small>")
            self._set_pw_label()
            self._refresh_drift()

        def _set_pw_label(self):
            if not self.device or self.device.pw_percent is None:
                self.pw_label.set_markup("")
                return
            self.pw_label.set_markup(
                f"<small>PipeWire reports: <b>{self.device.pw_percent}% / "
                f"{fmt_gain(self.device.pw_db)}</b> — "
                "<i>this is not the analog gain</i></small>"
            )

        def _drift_text(self):
            """The remembered gain no longer being the live one, or None."""
            saved = self.config["devices"].get(self.device.name) if self.device else None
            if not saved or self.gain is None or saved.get("index") == self.gain.index:
                return None
            now = fmt_gain(self.curve.db(self.gain.index)) if self.curve else "?"
            return (
                f"Gain has drifted: now {now} (index {self.gain.index}), "
                f"remembered {fmt_gain(saved.get('db'))} (index {saved['index']}). "
                "Something else moved it."
            )

        def _refresh_drift(self):
            saved = self.config["devices"].get(self.device.name) if self.device else None
            drifted = self._drift_text()
            if not saved:
                self.drift_label.set_markup("")
            elif drifted:
                self.drift_label.set_markup(f"<span foreground='#c22'>{drifted}</span>")
            else:
                self.drift_label.set_markup(
                    f"<small>Remembered: index {saved['index']} "
                    f"({fmt_gain(saved.get('db'))})</small>"
                )
            self.restore_button.show() if drifted else self.restore_button.hide()

        def _on_gain_changed(self, scale):
            if self._setting_slider or self.gain is None:
                return
            index = int(round(scale.get_value()))
            if index == self.gain.index:
                return
            if not self.gain.set_index(index):
                self.status.set_markup(
                    "<span foreground='#c22'>amixer refused the gain change"
                    "</span>"
                )
                return
            # A change made here is deliberate, so it becomes the remembered
            # value; drift detection is for changes made behind our back.
            saved = self.config["devices"].get(self.device.name)
            if saved is not None:
                self._save_current_gain()
            self._update_gain_readout()

        def _save_current_gain(self):
            if not self.device or self.gain is None:
                return
            self.config["devices"][self.device.name] = {
                "card_name": self.device.card_name,
                "control": self.gain.name,
                "numid": self.gain.numid,
                "index": self.gain.index,
                "db": None if self.curve is None else self.curve.db(self.gain.index),
                "saved": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            save_config(self.config)

        def _on_remember(self, check):
            if not self.device:
                return
            if check.get_active():
                self._save_current_gain()
            else:
                self.config["devices"].pop(self.device.name, None)
                save_config(self.config)
            self._refresh_drift()

        def _on_restore(self, _button):
            saved = self.config["devices"].get(self.device.name)
            if saved and self.gain and self.gain.set_index(saved["index"]):
                self._setting_slider = True
                self.gain_scale.set_value(self.gain.index)
                self._setting_slider = False
                self._update_gain_readout()

        def _on_login(self, check):
            ok, message = set_unit_enabled(check.get_active())
            self.status.set_markup(f"<small>{message}</small>")
            if not ok:
                check.handler_block_by_func(self._on_login)
                check.set_active(not check.get_active())
                check.handler_unblock_by_func(self._on_login)

        # -- warnings --
        def _refresh_warnings(self):
            """The only red text in the window, so it stays worth reading."""
            lines = []
            default = default_source()
            if self.device and self.device.is_hardware and default != self.device.name:
                target = next(
                    (d for d in self.devices if d.name == default), None
                )
                if target is not None and not target.is_hardware:
                    lines.append(
                        f"The system default source is <b>{default}</b>, a "
                        "processed node. Apps are recording that, not the "
                        "device shown here."
                    )
            hot = [b for b in self.boosts if b.index > b.imin]
            if hot:
                lines.append(
                    ", ".join(f"{b.name} is at {fmt_gain(b.db)}" for b in hot)
                    + " — that stacks on top of the capture gain above."
                )
            drifted = self._drift_text()
            if drifted:
                lines.append(drifted)
            if self.analyser and self.analyser.error:
                lines.append(self.analyser.error)
            self.warnings.set_markup(
                "<span foreground='#c22'>" + "\n".join(lines) + "</span>"
                if lines else ""
            )

        # -- periodic updates --
        def _fast_tick(self):
            if not self.analyser:
                return True
            data, duration = self.analyser.snapshot()
            self.meter.update(data)
            self._set_cached("clip", self.clip_summary, self._clip_markup(data, duration))
            self._set_cached("verdict", self.verdict, self._verdict_markup(data))
            return True

        def _set_cached(self, key, label, markup):
            """Only touch a label when its text actually changed: at 30 Hz,
            re-setting identical markup makes the window flicker."""
            if self._label_cache.get(key) != markup:
                self._label_cache[key] = markup
                label.set_markup(markup)

        def _verdict_markup(self, data):
            """The live answer to 'am I speaking at the right level?'"""
            noise = max(
                (s["noise"] for s in data if s["noise"] is not None), default=None
            )
            gate = max(
                SPEECH_GATE_FLOOR_DB,
                (noise + SPEECH_GATE_OVER_NOISE_DB) if noise is not None
                else SPEECH_GATE_FLOOR_DB,
            )
            peak = self.analyser.speech_peak(VERDICT_WINDOW_S, gate)
            self._fix_target = None

            if peak is None:
                self.fix_button.hide()
                return (
                    "<span size='large'>Speak a few words…</span>"
                    "  <small>the verdict follows your voice, not the room</small>"
                )

            correction = self._correction_for(peak)
            detail = f"<small>speech peaks {fmt_level(peak)}</small>"
            if TARGET_LO_DB <= peak <= TARGET_HI_DB:
                self.fix_button.hide()
                return (
                    f"<span size='large' foreground='#2ea44f'><b>Good level</b>"
                    f"</span>  {detail}"
                )

            if peak < TARGET_LO_DB:
                headline = "Too quiet — raise the gain"
                colour = "#3d7ad6"
            else:
                headline = "Too loud — lower the gain"
                colour = "#c22"
            if correction:
                steps, delta = correction
                self._fix_target = steps
                self.fix_button.set_label(f"Fix it ({delta:+.1f} dB)")
                self.fix_button.show()
                detail += f" <small>· needs {delta:+.1f} dB</small>"
            else:
                self.fix_button.hide()
                if self.gain is not None and self.curve is not None:
                    detail += (
                        " <small>· the gain control has nothing left to give;"
                        " move closer to the mic, or further away</small>"
                    )
            return (
                f"<span size='large' foreground='{colour}'><b>{headline}</b>"
                f"</span>  {detail}"
            )

        def _correction_for(self, peak):
            """(target index, dB actually gained) to land peaks at −12 dBFS."""
            if self.gain is None or self.curve is None:
                return None
            current = self.gain.index
            here = self.curve.db(current)
            target = self.curve.index_for_db(here + CALIB_TARGET_PEAK_DB - peak)
            if target == current:
                return None
            return target, self.curve.db(target) - here

        def _on_fix(self, _button):
            if self._fix_target is None or self.gain is None:
                return
            if not self.gain.set_index(self._fix_target):
                return
            if self.device.name in self.config["devices"]:
                self._save_current_gain()
            self._setting_slider = True
            self.gain_scale.set_value(self.gain.index)
            self._setting_slider = False
            self._update_gain_readout()

        def _clip_markup(self, data, duration):
            """One line. Per-channel detail appears only when it says something."""
            labels = self.device.channel_labels() if self.device else []
            noise = max(
                (s["noise"] for s in data if s["noise"] is not None),
                default=None,
            )
            tail = (
                f"  <small>noise floor {fmt_level(noise)} · {duration:.0f} s</small>"
            )
            clipping = [
                (name, s) for name, s in zip(labels, data) if s["runs"]
            ]
            if not clipping:
                return (
                    "<span foreground='#2ea44f'>No clipping</span>" + tail
                )
            detail = "; ".join(
                f"{name} {s['clipped']} samples in {s['runs']} runs "
                f"(longest {s['longest']})"
                for name, s in clipping
            )
            return (
                f"<span foreground='#c22'><b>Clipping</b> — {detail}. Lower the "
                "analog gain; nothing downstream can undo this.</span>" + tail
            )

        def _slow_tick(self):
            if self.device and self.device.is_hardware and self.gain is not None:
                fresh = read_controls(self.device.card)
                by_numid = {c.numid: c for c in fresh}
                for control in [self.gain] + self.boosts:
                    updated = by_numid.get(control.numid)
                    if updated is not None:
                        control.values = updated.values
                if not self._setting_slider:
                    self._setting_slider = True
                    self.gain_scale.set_value(self.gain.index)
                    self._setting_slider = False
                self._update_gain_readout()
            # PipeWire's own reported volume moves independently of ALSA.
            for device in list_devices():
                if self.device and device.name == self.device.name:
                    self.device.pw_percent = device.pw_percent
                    self.device.pw_db = device.pw_db
            self._refresh_warnings()
            if self.analyser and not self.analyser.alive and not self.calibration:
                self.analyser.stop()
                self.analyser = Analyser(
                    self.device.name, self.device.channels, self.device.rate
                )
                self.analyser.start()
            return True

        def _on_reset_clips(self, _button):
            if self.analyser:
                self.analyser.reset_clips()

        # -- calibration --
        def _on_calibrate(self, _button):
            if self.calibration:
                self._end_calibration("Cancelled.")
                return
            if not self.analyser or not self.analyser.alive:
                self.calib_result.set_markup(
                    "<span foreground='#c22'>No capture stream.</span>"
                )
                return
            self.recommendation = None
            self.apply_button.set_sensitive(False)
            self.calib_button.set_label("Cancel")
            self.calib_progress.show()
            self.calibration = {"phase": "quiet", "verify": False, "quiet": None}
            self._begin_phase("quiet", CALIB_QUIET_S, "Stay quiet…")

        def _begin_phase(self, phase, seconds, message):
            self.calibration["phase"] = phase
            self.calibration["until"] = time.monotonic() + seconds
            self.calibration["length"] = seconds
            self.calib_result.set_markup(f"<b>{message}</b>")
            self.analyser.begin_collect()
            GLib.timeout_add(100, self._calibration_tick)

        def _calibration_tick(self):
            if not self.calibration:
                return False
            remaining = self.calibration["until"] - time.monotonic()
            if remaining > 0:
                fraction = 1.0 - remaining / self.calibration["length"]
                self.calib_progress.set_fraction(max(0.0, min(1.0, fraction)))
                self.calib_progress.set_text(f"{remaining:.1f} s")
                return True
            self.calib_progress.set_fraction(1.0)
            self.calib_progress.set_text("")
            self._finish_phase(self.analyser.end_collect())
            return False

        def _finish_phase(self, samples):
            phase = self.calibration["phase"]
            if not samples:
                self._end_calibration("No audio captured during the measurement.")
                return
            if phase == "quiet":
                self.calibration["quiet"] = samples
                self._begin_phase(
                    "speech", CALIB_SPEECH_S,
                    "Speak normally, at your usual distance…",
                )
                return
            self._evaluate(self.calibration["quiet"], samples,
                           verify=self.calibration.get("verify", False))

        def _evaluate(self, quiet, speech, verify):
            noise_rms = percentile([r for r, _ in quiet], 50)
            speech_peak = percentile([p for _, p in speech], 95)
            speech_rms = percentile([r for r, _ in speech], 50)
            headroom = speech_rms - noise_rms

            if verify:
                verdict = (
                    "on target" if TARGET_LO_DB <= speech_peak <= TARGET_HI_DB
                    else "outside the −18…−10 dBFS band; run calibration again"
                )
                self._end_calibration(
                    f"Re-measured: speech peaks {fmt_level(speech_peak)} — {verdict}."
                )
                return

            lines = [
                f"Noise floor {fmt_level(noise_rms)}, speech {fmt_level(speech_rms)} "
                f"RMS / {fmt_level(speech_peak)} peak, "
                f"separation {headroom:.1f} dB."
            ]

            if self.gain is None or self.curve is None:
                lines.append(
                    "No analog gain control here, so there is nothing to recommend."
                )
                self._end_calibration("\n".join(lines))
                return

            current_index = self.gain.index
            current_db = self.curve.db(current_index)
            delta = CALIB_TARGET_PEAK_DB - speech_peak
            target_index = self.curve.index_for_db(current_db + delta)
            achieved = self.curve.db(target_index) - current_db
            predicted_peak = speech_peak + achieved
            predicted_noise = noise_rms + achieved

            lines.append(
                f"Recommendation: <b>index {current_index} → {target_index}</b>, "
                f"{fmt_gain(current_db)} → <b>{fmt_gain(self.curve.db(target_index))}</b>, "
                f"predicted speech peak {fmt_level(predicted_peak)}."
            )

            problems = []
            if predicted_noise > CALIB_MAX_NOISE_DB:
                problems.append(
                    f"the noise floor would sit at {fmt_level(predicted_noise)}, "
                    f"above the −50 dBFS limit"
                )
            if headroom < CALIB_MIN_HEADROOM_DB:
                problems.append(
                    f"speech is only {headroom:.1f} dB above the room, "
                    f"under the {CALIB_MIN_HEADROOM_DB:.0f} dB minimum"
                )
            if problems:
                lines.append(
                    "<span foreground='#c22'><b>Gain is not the problem here:</b> "
                    + "; and ".join(problems)
                    + ". That is the room or the microphone — a quieter room, a "
                    "closer or better mic. Changing the gain will move speech and "
                    "noise together.</span>"
                )
            if target_index == current_index:
                lines.append("Current gain is already the best available step.")

            self.recommendation = target_index
            self.apply_button.set_sensitive(target_index != current_index)
            self._end_calibration("\n".join(lines))

        def _end_calibration(self, message):
            self.calibration = None
            self.calib_button.set_label("Calibrate")
            self.calib_progress.set_fraction(0.0)
            self.calib_progress.set_text("")
            self.calib_progress.hide()
            self.calib_result.set_markup(message)

        def _on_apply_recommendation(self, _button):
            if self.recommendation is None or self.gain is None:
                return
            if not self.gain.set_index(self.recommendation):
                self.calib_result.set_markup(
                    "<span foreground='#c22'>amixer refused the change.</span>"
                )
                return
            self._setting_slider = True
            self.gain_scale.set_value(self.gain.index)
            self._setting_slider = False
            if self.device.name in self.config["devices"]:
                self._save_current_gain()
            self._update_gain_readout()
            self.apply_button.set_sensitive(False)
            self.recommendation = None
            if self.analyser:
                self.analyser.reset_clips()
            self.calibration = {"phase": "speech", "verify": True, "quiet": None}
            self.calib_button.set_label("Cancel")
            self.calib_progress.show()
            # Let the new gain settle through the capture path before measuring.
            GLib.timeout_add(
                400,
                lambda: (
                    self._begin_phase(
                        "speech", CALIB_SPEECH_S,
                        "Applied. Speak normally again to confirm…",
                    ),
                    False,
                )[1],
            )

        def _on_destroy(self, *_args):
            self._stop_analyser()
            Gtk.main_quit()

    return Gtk, MicScope


# ---------------------------------------------------------------------------
# the optional login unit
# ---------------------------------------------------------------------------

def own_exec_path():
    return shutil.which(APP) or os.path.abspath(__file__)


UNIT_TEMPLATE = """[Unit]
Description=Reapply the microphone analog gain saved by micscope
Documentation=file:///usr/share/doc/micscope/spec.md
After=pipewire.service wireplumber.service

[Service]
Type=oneshot
ExecStart={exec} --restore --delay 8

[Install]
WantedBy=default.target
"""


def unit_enabled():
    if not UNIT_FILE.exists():
        return False
    out = run(["systemctl", "--user", "is-enabled", UNIT_NAME])
    return out.strip() == "enabled"


def set_unit_enabled(enable):
    if not shutil.which("systemctl"):
        return False, "systemctl not available"
    if enable:
        UNIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        UNIT_FILE.write_text(UNIT_TEMPLATE.format(exec=own_exec_path()))
        run(["systemctl", "--user", "daemon-reload"])
        out = run(["systemctl", "--user", "enable", UNIT_NAME])
        return (
            unit_enabled(),
            f"Gain will be reapplied at login ({UNIT_NAME}).",
        )
    run(["systemctl", "--user", "disable", UNIT_NAME])
    UNIT_FILE.unlink(missing_ok=True)
    run(["systemctl", "--user", "daemon-reload"])
    return True, "Login reapply disabled."


# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        prog=APP,
        description="See the true analog gain, level and clipping of a microphone.",
    )
    parser.add_argument("--version", action="version", version=f"{APP} {VERSION}")
    parser.add_argument(
        "--list", action="store_true",
        help="print every capture device with its real analog gain, and exit",
    )
    parser.add_argument(
        "--restore", action="store_true",
        help="reapply saved gain settings and exit (used by the login unit)",
    )
    parser.add_argument(
        "--delay", type=float, default=0.0,
        help="seconds to wait before --restore, to let wireplumber settle",
    )
    args = parser.parse_args(argv)

    if args.list:
        print(device_report())
        return 0
    if args.restore:
        return restore_saved(args.delay)

    try:
        Gtk, MicScope = build_gui()
    except (ImportError, ValueError) as exc:
        print(f"{APP}: GTK 3 not available ({exc}).", file=sys.stderr)
        print(f"Try `{APP} --list` for the same facts on the terminal.",
              file=sys.stderr)
        return 1

    signal.signal(signal.SIGINT, signal.SIG_DFL)
    window = MicScope()
    window.show_all()
    window.restore_button.hide()
    window.fix_button.hide()
    Gtk.main()
    return 0


if __name__ == "__main__":
    sys.exit(main())
