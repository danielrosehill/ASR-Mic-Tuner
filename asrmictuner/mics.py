"""The microphone catalogue (v3.0): human notes, benchmarks and reference clips per mic.

Everything here is keyed by `Device.key` (docs/mic-identity.md), like the rest
of what is remembered about a mic. See docs/mics.md.

- **About** (`about`, shared across computers, in the mic's profile file):
  what a person knows and the USB descriptor doesn't. The Acer OHW325 headset
  reports itself as "DSEA A/S SC230"; `about` is where "Acer OHW325, USB-C
  headset" lives.

      {"maker": "Acer", "model": "OHW325", "type": "headset",
       "connection": "usb-c", "notes": "...", "updated": "<ISO>"}

- **Benchmarks** (`benchmarks`, shared, newest last, capped at BENCH_KEEP): the
  ASR scorecard summary of every benchmark run, with computer, place and gain,
  so one mic's results on two machines sit side by side.

- **Reference clips** (machine-local, not synced: audio is large):
  `~/.local/share/asr-mic-tuner/references/<mic file name>/<id>.flac` plus
  `<id>.json` (gain, port, computer, place, duration). A clip is a recording an
  agent or the app can re-analyse, or run a chain over, without asking the
  person to speak again.
"""

import hashlib
import json
import shutil
import subprocess
import wave
from datetime import datetime
from pathlib import Path

from . import profiles
from .identity import filename, machine_label, this_machine

TYPES = [
    ("", "Not set"),
    ("headset", "Headset (boom mic)"),
    ("earbuds", "Earbuds / in-line mic"),
    ("usb-desk", "USB desk mic"),
    ("lavalier", "Lavalier (clip-on)"),
    ("studio-condenser", "Studio condenser (XLR)"),
    ("studio-dynamic", "Studio dynamic (XLR)"),
    ("shotgun", "Shotgun"),
    ("conference", "Conference / speakerphone"),
    ("webcam", "Webcam mic"),
    ("laptop-internal", "Laptop built-in"),
    ("other", "Other"),
]
CONNECTIONS = [
    ("", "Not set"),
    ("usb-c", "USB-C"),
    ("usb-a", "USB-A"),
    ("bluetooth", "Bluetooth"),
    ("wireless-dongle", "Wireless (USB dongle)"),
    ("3.5mm", "3.5 mm jack"),
    ("xlr", "XLR via interface"),
    ("internal", "Built-in"),
    ("other", "Other"),
]
ABOUT_FIELDS = ("maker", "model", "type", "connection", "notes")
BENCH_KEEP = 50


def _now():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _read_file(key):
    path = profiles.profiles_dir() / f"{filename(key)}.json"
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def guess_about(device_or_ident):
    """Defaults from the identity, for an empty form: never saved by itself."""
    ident = getattr(device_or_ident, "ident", device_or_ident) or {}
    bus = ident.get("bus")
    if bus in ("pci", "") and ident.get("codec"):
        m = ident.get("machine") or {}
        return {"maker": m.get("vendor", ""), "model": f"{m.get('version') or m.get('product', '')} built-in".strip(),
                "type": "laptop-internal", "connection": "internal"}
    if bus == "bluetooth":
        return {"maker": "", "model": ident.get("product_name", ""), "type": "headset", "connection": "bluetooth"}
    if bus == "usb":
        return {"maker": ident.get("vendor_name", ""), "model": ident.get("product_name", ""), "type": "",
                "connection": ""}
    return {}


def about(key):
    return dict(_read_file(key).get("about") or {})


def set_about(key, ident=None, **fields):
    """Merge `fields` (maker, model, type, connection, notes) into the mic's notes.
    An empty string clears a field. Returns the new `about`."""
    unknown = set(fields) - set(ABOUT_FIELDS)
    if unknown:
        raise KeyError(", ".join(sorted(unknown)))
    if "type" in fields and fields["type"] not in dict(TYPES):
        raise ValueError(f"type must be one of {[t for t, _ in TYPES if t]}")
    if "connection" in fields and fields["connection"] not in dict(CONNECTIONS):
        raise ValueError(f"connection must be one of {[c for c, _ in CONNECTIONS if c]}")
    data = _read_file(key)
    new = dict(data.get("about") or {})
    for name, value in fields.items():
        value = (value or "").strip()
        if value:
            new[name] = value
        else:
            new.pop(name, None)
    new.pop("updated", None)
    if new:
        new["updated"] = _now()
    extra = {}
    if ident and not data.get("identity"):
        extra["identity"] = ident
    profiles.update(key, about=new or None, **extra)
    return new


def display_name(key, ident=None, fallback=""):
    a = about(key)
    name = " ".join(x for x in (a.get("maker"), a.get("model")) if x)
    return name or fallback or (ident or {}).get("product_name") or key


# -- catalogue --

def catalogue(devices=None):
    """Every mic known on this computer (profile files) or connected now."""
    from .core import default_source, list_devices, resolve_hardware_for
    devices = list_devices() if devices is None else devices
    default = default_source()
    if default and not any(d.name == default and d.is_hardware for d in devices):
        default = resolve_hardware_for(default) or default
    live = {d.key: d for d in devices}
    files = profiles.read_all()
    out = []
    for key in sorted(set(files) | {d.key for d in devices if d.is_hardware}):
        prof = files.get(key, {})
        d = live.get(key)
        ident = (d.ident if d else None) or prof.get("identity") or {}
        refs = references(key)
        bench = prof.get("benchmarks") or []
        out.append({
            "mic_key": key,
            "name": display_name(key, ident, d.description if d else ""),
            "about": prof.get("about") or {},
            "suggested_about": guess_about(ident) if not prof.get("about") else None,
            "connected": d is not None,
            "system_default": bool(d and d.name == default),
            "node_name": d.name if d else None,
            "pipewire_description": d.description if d else None,
            "port": d.port if d else None,
            "usb_descriptor": {k: ident.get(k) for k in ("vendor_id", "product_id", "vendor_name", "product_name",
                                                         "serial") if ident.get(k)},
            "places_on_this_computer": sorted((_this_bucket(prof, "presets") or {}).keys()),
            "latest_benchmark": bench[-1] if bench else None,
            "benchmarks": len(bench),
            "reference_clips": len(refs),
        })
    return out


def _this_bucket(prof, field):
    buckets = profiles.machine_buckets(prof.get("mic_key", ""), prof)
    return (buckets or {}).get(field, {}).get(this_machine()["id"])


# -- reference clips --

def references_root():
    from .store import DATA_DIR
    return DATA_DIR / "references"


def references(key=None):
    root = references_root()
    if not root.is_dir():
        return []
    dirs = [root / filename(key)] if key else sorted(p for p in root.iterdir() if p.is_dir())
    out = []
    for d in dirs:
        for meta in sorted(d.glob("*.json")):
            try:
                ref = json.loads(meta.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            ref["meta_path"] = str(meta)
            out.append(ref)
    return out


def reference(ref_id):
    """A clip by id (or a unique id prefix)."""
    hits = [r for r in references() if r["id"] == ref_id] or \
           [r for r in references() if r["id"].startswith(ref_id)]
    return hits[0] if len(hits) == 1 else None


def add_reference(src_path, key, *, device_label="", port=None, gain_control=None, gain_index=None,
                  gain_db=None, soft_percent=None, rate=None, channels=None, seconds=None, place=None,
                  name=None, notes=None, recorded=None, move=False):
    """File an audio file as a reference clip of mic `key`. Returns the clip's metadata."""
    src = Path(src_path)
    stamp = (recorded or _now())[:19].replace(":", "").replace("-", "").replace("T", "-")
    ref_id = f"{stamp}-{hashlib.sha1(key.encode()).hexdigest()[:6]}"
    d = references_root() / filename(key)
    d.mkdir(parents=True, exist_ok=True)
    dest = d / f"{ref_id}{src.suffix}"
    (shutil.move if move else shutil.copy2)(str(src), str(dest))
    meta = {
        "id": ref_id, "name": name or f"Reference {stamp}", "mic_key": key, "device_label": device_label,
        "audio": str(dest), "rate": rate, "channels": channels, "seconds": seconds, "port": port,
        "gain_control": gain_control, "gain_index": gain_index, "gain_db": gain_db, "soft_percent": soft_percent,
        "computer": machine_label(this_machine()), "computer_id": this_machine()["id"],
        "place": place or "", "notes": notes or "", "recorded": recorded or _now(),
    }
    (d / f"{ref_id}.json").write_text(json.dumps(meta, indent=2) + "\n")
    return meta


def delete_reference(ref):
    Path(ref["audio"]).unlink(missing_ok=True)
    Path(ref["meta_path"]).unlink(missing_ok=True)


def read_audio(path):
    """int16 (frames, channels) and rate from a WAV, or a FLAC via sox."""
    import numpy as np
    path = Path(path)
    if path.suffix.lower() != ".wav":
        if not shutil.which("sox"):
            raise RuntimeError("sox is needed to read FLAC reference clips")
        info = subprocess.run(["soxi", "-r", str(path)], capture_output=True, text=True)
        chans = subprocess.run(["soxi", "-c", str(path)], capture_output=True, text=True)
        rate, channels = int(info.stdout.strip()), int(chans.stdout.strip())
        raw = subprocess.run(["sox", str(path), "-t", "raw", "-e", "signed", "-b", "16", "-L", "-"],
                             capture_output=True, check=True).stdout
        return np.frombuffer(raw, dtype="<i2").reshape(-1, channels).copy(), rate
    with wave.open(str(path), "rb") as w:
        rate, channels = w.getframerate(), w.getnchannels()
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype="<i2").reshape(-1, channels).copy(), rate


# -- benchmark --

def scorecard(metrics, rate, channels):
    """The ASR-facing summary of a voice analysis (analysis.analyse_voice)."""
    r = metrics.get("readiness") or {}
    bw = metrics.get("bandwidth_hz")
    asr = []
    if rate and rate < 16000:
        asr.append(("fail", f"Native rate {rate} Hz is below the 16 kHz most ASR models expect."))
    if bw is not None:
        if bw >= 7000:
            asr.append(("pass", f"Bandwidth {bw:.0f} Hz covers the 0–8 kHz a 16 kHz ASR model hears."))
        else:
            asr.append(("warn", f"Bandwidth only {bw:.0f} Hz: narrowband (Bluetooth HFP or heavy filtering); "
                                "expect more errors on consonants."))
    asr = [{"level": lv, "text": t} for lv, t in asr]
    if channels and channels > 1 and (metrics.get("channel_imbalance_db") or 0) >= 6:
        asr.append({"level": "warn", "text": "Channels differ by 6 dB or more; ASR downmixes, so one weak channel dilutes the mix."})
    keys = ("speech_peak_p95_dbfs", "speech_rms_dbfs", "noise_reference_dbfs", "snr_db", "bandwidth_hz",
            "spectral_centroid_hz", "spectral_tilt_db_per_octave", "low_vs_body_db", "presence_vs_body_db",
            "sibilance_vs_body_db", "plosive_pops", "dropouts", "level_spread_db", "speech_ratio",
            "f0_median_hz", "channel_imbalance_db")
    return {
        "score": r.get("score"), "grade": r.get("grade"), "deductions": r.get("deductions", []),
        "findings": metrics.get("findings", []), "asr_checks": asr,
        "rate": rate, "channels": channels,
        "clipped_runs": sum(c.get("clipped_runs", 0) for c in metrics.get("channels", [])),
        "metrics": {k: metrics.get(k) for k in keys if metrics.get(k) is not None},
    }


def record_benchmark(key, card, *, place="", port=None, gain_db=None, soft_percent=None, reference_id=None,
                     ident=None):
    """Append the summary of a scorecard to the mic's shared benchmark list."""
    m = card["metrics"]
    entry = {
        "at": _now(), "computer": machine_label(this_machine()), "computer_id": this_machine()["id"],
        "place": place, "port": port, "gain_db": gain_db, "soft_percent": soft_percent,
        "score": card["score"], "grade": card["grade"], "snr_db": m.get("snr_db"),
        "speech_peak_p95_dbfs": m.get("speech_peak_p95_dbfs"), "noise_dbfs": m.get("noise_reference_dbfs"),
        "bandwidth_hz": m.get("bandwidth_hz"), "clipped_runs": card["clipped_runs"], "reference": reference_id,
    }
    data = _read_file(key)
    bench = list(data.get("benchmarks") or [])
    if reference_id:
        # Re-scoring the same clip replaces its entry instead of logging it twice.
        bench = [b for b in bench if b.get("reference") != reference_id]
    bench.append(entry)
    extra = {"identity": ident} if ident and not data.get("identity") else {}
    profiles.update(key, benchmarks=bench[-BENCH_KEEP:], **extra)
    return entry
