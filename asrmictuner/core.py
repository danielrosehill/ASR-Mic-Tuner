"""Toolkit-independent core: the ALSA gain curve, mixer controls, capture
devices, saved settings, and the headless report / restore paths.

Standing constraint (docs/spec.md §3): ASR Mic Tuner never modifies the audio
graph. It runs `parec` to listen and `amixer` to read and set the analog gain.
It loads no module, creates no node, and changes no default.
"""

import json
import math
import operator
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import APP

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP
DEVICES_FILE = CONFIG_DIR / "devices.json"
UNIT_NAME = "asr-mic-tuner-restore.service"
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


def set_source_percent(name, percent):
    """Software (digital) volume of a source, for devices with no analog gain.

    Only ever used where there is no ALSA capture control to set instead:
    Bluetooth headsets, network sources, processed virtual nodes. On a
    hardware device PipeWire's volume is the mapping docs/gain-mapping.md
    exists to warn about, so the GUI never offers it there.
    """
    percent = max(0, min(150, int(percent)))
    proc = subprocess.run(
        ["pactl", "set-source-volume", name, f"{percent}%"],
        capture_output=True, text=True,
    )
    return proc.returncode == 0


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
    """devices.json: remembered gain per device, named presets, probe cache.

    `devices[name]` is the remembered (default) gain that `--restore`
    reapplies; `presets[name][preset]` are the named alternatives, one of
    which can be promoted to the remembered value.
    """
    try:
        data = json.loads(DEVICES_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    data.setdefault("devices", {})
    data.setdefault("curves", {})
    data.setdefault("presets", {})
    return data


def save_config(config):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DEVICES_FILE.write_text(json.dumps(config, indent=2) + "\n")


def curve_cache_key(device, control):
    return f"{device.card_name}|{device.alsa_name}|{control.name}"



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
        print("asr-mic-tuner: nothing saved")
        return 0
    devices = {d.name: d for d in list_devices()}
    applied = 0
    for name, entry in saved.items():
        device = devices.get(name)
        if device is None or not device.is_hardware:
            print(f"asr-mic-tuner: {name} not present, skipped")
            continue
        control = pick_gain_control(read_controls(device.card))
        if control is None or control.numid != entry.get("numid"):
            print(f"asr-mic-tuner: control moved on {name}, skipped")
            continue
        if control.index == entry["index"]:
            print(f"asr-mic-tuner: {name} already at index {control.index}")
            continue
        if control.set_index(entry["index"]):
            print(f"asr-mic-tuner: {name} set to index {entry['index']}")
            applied += 1
    return 0 if applied or saved else 1


# ---------------------------------------------------------------------------
# the optional login unit
# ---------------------------------------------------------------------------

def own_exec_path():
    return shutil.which(APP) or os.path.abspath(__file__)


UNIT_TEMPLATE = """[Unit]
Description=Reapply the microphone analog gain saved by ASR Mic Tuner
Documentation=file:///usr/share/doc/asr-mic-tuner/spec.md
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
        run(["systemctl", "--user", "enable", UNIT_NAME])
        return (
            unit_enabled(),
            f"Gain will be reapplied at login ({UNIT_NAME}).",
        )
    run(["systemctl", "--user", "disable", UNIT_NAME])
    UNIT_FILE.unlink(missing_ok=True)
    run(["systemctl", "--user", "daemon-reload"])
    return True, "Login reapply disabled."

