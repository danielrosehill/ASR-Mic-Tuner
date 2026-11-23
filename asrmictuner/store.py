"""Persistent state beyond devices.json: measurement history, room and voice
profiles, recorded samples, and app settings.

Layout (XDG):

    ~/.config/asr-mic-tuner/profiles/*.json  one file per mic: identity, remembered gain,
                                        profiles, chain bindings (profiles.py, v2.7)
    ~/.config/asr-mic-tuner/devices.json     gain-curve probe cache, unmigrated legacy entries (core.py)
    ~/.config/asr-mic-tuner/settings.json    app settings, incl. the OpenRouter key
    ~/.local/share/asr-mic-tuner/history.db  SQLite: measurements, profiles
    ~/.local/share/asr-mic-tuner/samples/    recorded room tone and voice, FLAC (or WAV)
                                        plus a sox noise profile per room sample

Samples are kept so a room's noise can later be used for cleanup:
`sox in.wav out.wav noisered <room>.noiseprof 0.21`.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import wave
from datetime import datetime
from pathlib import Path

from . import APP, profiles
from .core import CONFIG_DIR

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / APP
SAMPLES_DIR = DATA_DIR / "samples"
DB_FILE = DATA_DIR / "history.db"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

DEFAULT_SETTINGS = {
    "sync_folder": "",                  # profile folder (sync.py); "" = off
    "sync_git": False,                  # commit and push the folder with Git
    "sync_auto": True,                  # sync at start, after changes, hourly
    "openrouter_api_key": "",
    "openrouter_model": "google/gemini-3.8-flash",
    "ai_include_audio": False,
    "tray_enabled": True,
    "close_to_tray": True,
    "tray_live_meter": False,
    "auto_restore": False,
    "pin_popup": True,
    "location": "",
    "locations": [],
    "room_seconds": 15,
    "voice_seconds": 20,                # --measure voice default
    "voice_passage": "60-email",        # mic test text, passages.py
    "calibration_mode": "standard",
    "active_room_profile": {},          # device name -> profile id
    "chain_bindings": {},                # "<device>:<port>" -> chain name (engine.py)
    "asr_mic_source": "",               # mic feeding ASR Mic; "" follows the default input
    "asr_mic_bypass": False,
    "noise_reduction": "off",           # quick denoiser over the chain: off, light, strong (engine.NOISE_MODES)
    "asr_autogain": True,               # slow analog auto gain while dictating (watch.py); on by default again since v3.1
    "asr_autogain_max_db": 30,          # analog ceiling; was 15 until v2.6, too low for laptop mics
    "follow_default_mic": True,         # switch to the new default input and apply its profile (v2.6)
    "tune_seconds": 30,                 # quick tune listening time (tune.py)
    "settings_rev": 28,
    "quiet_alert": True,                # notify when dictation runs too quiet (watch.py)
    "quiet_window_min": 3,
    "quiet_margin_db": 6,
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS measurements (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created       TEXT NOT NULL,
    kind          TEXT NOT NULL,        -- room | voice | calibration | snapshot
    device        TEXT NOT NULL,
    device_label  TEXT,
    gain_control  TEXT,
    gain_index    INTEGER,
    gain_db       REAL,
    soft_percent  INTEGER,
    location      TEXT,
    notes         TEXT,
    duration_s    REAL,
    metrics       TEXT,                 -- JSON from analysis.py
    sample_path   TEXT,
    noiseprof     TEXT,
    ai            TEXT                  -- JSON: last AI review
);
CREATE INDEX IF NOT EXISTS measurements_device ON measurements(device, created);
CREATE TABLE IF NOT EXISTS profiles (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created        TEXT NOT NULL,
    kind           TEXT NOT NULL,       -- room | voice
    name           TEXT NOT NULL,
    device         TEXT NOT NULL,
    measurement_id INTEGER REFERENCES measurements(id) ON DELETE SET NULL,
    notes          TEXT
);
"""


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------

def load_settings():
    """App settings. `chain_bindings[mic_key][port]` comes from the per-mic
    profile files (profiles.py); `active_room_profile[mic_key]` stays here,
    since room profiles are rows in this machine's history.db."""
    try:
        data = json.loads(SETTINGS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    merged = dict(DEFAULT_SETTINGS)
    merged.update(data)
    if int(data.get("settings_rev", 0)) < 26:
        # v2.6: the old +15 dB auto gain ceiling held laptop mics well under target
        # (internal ALC257 needs about +20 dB). Lift it unless the user changed it.
        if merged.get("asr_autogain_max_db") == 15:
            merged["asr_autogain_max_db"] = 30
        merged["settings_rev"] = 26
    if int(merged.get("settings_rev", 0)) < 27:
        # v2.7: bindings were "<node name>:<port>" -> chain, room profiles by node name.
        legacy = merged.setdefault("legacy", {})
        flat = {}
        for k, chain in (merged.get("chain_bindings") or {}).items():
            name, _, port = k.partition(":")
            flat.setdefault(name, {})[port] = chain
        legacy["chain_bindings"] = flat
        legacy["active_room_profile"] = dict(merged.get("active_room_profile") or {})
        merged["active_room_profile"] = {}
        merged["settings_rev"] = 27
    if int(merged.get("settings_rev", 0)) < 28:
        # v3.1 (Daniel, 2026-11-23): auto gain on by default. The saved False was the v2.5 default, not a choice;
        # it left a headset clipping through an 8-minute conversation at a gain tuned for reading.
        merged["asr_autogain"] = True
        merged["settings_rev"] = 28
    merged["chain_bindings"] = {k: dict(p["chain_bindings"]) for k, p in profiles.read_all().items()
                                if p.get("chain_bindings")}
    merged["_bindings_snapshot"] = json.loads(json.dumps(merged["chain_bindings"]))
    if any((merged.get("legacy") or {}).values()):
        from .core import list_devices
        _adopt_legacy_settings(merged, list_devices())
    return merged


def _adopt_legacy_settings(settings, devices):
    legacy = settings["legacy"]
    moved = False
    for d in devices:
        old = (legacy.get("chain_bindings") or {}).pop(d.name, None)
        if old:
            merged = dict(old)
            merged.update(settings["chain_bindings"].get(d.key) or {})
            settings["chain_bindings"][d.key] = merged
            moved = True
        pid = (legacy.get("active_room_profile") or {}).pop(d.name, None)
        if pid is not None:
            settings["active_room_profile"].setdefault(d.key, pid)
            moved = True
    settings["legacy"] = {k: v for k, v in legacy.items() if v}
    if moved:
        save_settings(settings)


def save_settings(settings):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    local = {k: v for k, v in settings.items() if k not in ("chain_bindings", "_bindings_snapshot")}
    if not local.get("legacy"):
        local.pop("legacy", None)
    SETTINGS_FILE.write_text(json.dumps(local, indent=2) + "\n")
    try:
        SETTINGS_FILE.chmod(0o600)          # holds an API key
    except OSError:
        pass
    # Chain bindings go to each mic's profile file; only mics whose bindings changed.
    bindings = settings.get("chain_bindings") or {}
    snapshot = settings.setdefault("_bindings_snapshot", {})
    for key in set(bindings) | set(snapshot):
        if bindings.get(key) != snapshot.get(key):
            profiles.update(key, chain_bindings=bindings.get(key))
            snapshot[key] = json.loads(json.dumps(bindings.get(key)))


# ---------------------------------------------------------------------------
# database
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, path=DB_FILE):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(measurements)")}
        if "mic_key" not in cols:           # v2.7: identity that follows the mic
            self.db.execute("ALTER TABLE measurements ADD COLUMN mic_key TEXT")
            self.db.execute("ALTER TABLE profiles ADD COLUMN mic_key TEXT")
        self.db.commit()

    def backfill_keys(self, devices):
        """Set mic_key on older rows recorded from a mic that is present now."""
        for d in devices:
            for table in ("measurements", "profiles"):
                self.db.execute(f"UPDATE {table} SET mic_key = ? WHERE mic_key IS NULL AND device = ?",
                                (d.key, d.name))
        self.db.commit()

    # -- measurements --
    def add_measurement(self, **fields):
        fields.setdefault("created", now_iso())
        for key in ("metrics", "ai"):
            if key in fields and not isinstance(fields[key], (str, type(None))):
                fields[key] = json.dumps(fields[key])
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        cur = self.db.execute(
            f"INSERT INTO measurements ({cols}) VALUES ({marks})", list(fields.values())
        )
        self.db.commit()
        return cur.lastrowid

    def update_measurement(self, mid, **fields):
        for key in ("metrics", "ai"):
            if key in fields and not isinstance(fields[key], (str, type(None))):
                fields[key] = json.dumps(fields[key])
        sets = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE measurements SET {sets} WHERE id = ?",
                        [*fields.values(), mid])
        self.db.commit()

    def measurement(self, mid):
        row = self.db.execute("SELECT * FROM measurements WHERE id = ?", (mid,)).fetchone()
        return _decode(row)

    def measurements(self, device=None, kind=None, limit=500, mic_key=None):
        sql, args = "SELECT * FROM measurements WHERE 1=1", []
        if mic_key:
            sql += " AND (mic_key = ? OR (mic_key IS NULL AND device = ?))"
            args += [mic_key, device or ""]
        elif device:
            sql += " AND device = ?"
            args.append(device)
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        sql += " ORDER BY created DESC, id DESC LIMIT ?"
        args.append(limit)
        return [_decode(r) for r in self.db.execute(sql, args).fetchall()]

    def delete_measurement(self, mid, remove_files=True):
        row = self.measurement(mid)
        if row and remove_files:
            for key in ("sample_path", "noiseprof"):
                if row.get(key):
                    Path(row[key]).unlink(missing_ok=True)
        self.db.execute("DELETE FROM measurements WHERE id = ?", (mid,))
        self.db.commit()

    # -- profiles --
    def add_profile(self, kind, name, device, measurement_id, notes=""):
        cur = self.db.execute(
            "INSERT INTO profiles (created, kind, name, device, measurement_id, notes, mic_key) "
            "VALUES (?, ?, ?, ?, ?, ?, (SELECT mic_key FROM measurements WHERE id = ?))",
            (now_iso(), kind, name, device, measurement_id, notes, measurement_id),
        )
        self.db.commit()
        return cur.lastrowid

    def profiles(self, kind=None, device=None):
        sql = ("SELECT p.*, m.metrics AS metrics, m.sample_path AS sample_path, "
               "m.noiseprof AS noiseprof, m.location AS location, m.gain_db AS gain_db "
               "FROM profiles p LEFT JOIN measurements m ON m.id = p.measurement_id WHERE 1=1")
        args = []
        if kind:
            sql += " AND p.kind = ?"
            args.append(kind)
        if device:
            sql += " AND p.device = ?"
            args.append(device)
        sql += " ORDER BY p.created DESC"
        return [_decode(r) for r in self.db.execute(sql, args).fetchall()]

    def profile(self, pid):
        rows = [p for p in self.profiles() if p["id"] == pid]
        return rows[0] if rows else None

    def delete_profile(self, pid):
        self.db.execute("DELETE FROM profiles WHERE id = ?", (pid,))
        self.db.commit()

    def export(self):
        return {
            "exported": now_iso(),
            "measurements": self.measurements(limit=100000),
            "profiles": self.profiles(),
        }


def _decode(row):
    if row is None:
        return None
    data = dict(row)
    for key in ("metrics", "ai"):
        if data.get(key):
            try:
                data[key] = json.loads(data[key])
            except (TypeError, json.JSONDecodeError):
                pass
    return data


# ---------------------------------------------------------------------------
# samples
# ---------------------------------------------------------------------------

def save_sample(audio, rate, kind, device_name):
    """Write int16 audio (frames, channels) and return the path. FLAC when sox
    is present (a third of the size), WAV otherwise."""
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in device_name)[-60:]
    wav_path = SAMPLES_DIR / f"{stamp}_{kind}_{safe}.wav"
    with wave.open(str(wav_path), "wb") as out:
        out.setnchannels(audio.shape[1])
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(audio.astype("<i2").tobytes())
    if shutil.which("sox"):
        flac_path = wav_path.with_suffix(".flac")
        proc = subprocess.run(["sox", str(wav_path), str(flac_path)],
                              capture_output=True)
        if proc.returncode == 0 and flac_path.exists():
            wav_path.unlink(missing_ok=True)
            return flac_path
    return wav_path


def make_noise_profile(sample_path):
    """sox noise profile for a room-tone sample, for later `noisered`."""
    if not shutil.which("sox"):
        return None
    prof = Path(sample_path).with_suffix(".noiseprof")
    proc = subprocess.run(
        ["sox", str(sample_path), "-n", "noiseprof", str(prof)], capture_output=True
    )
    return prof if proc.returncode == 0 and prof.exists() else None


def play(path):
    """Fire-and-forget playback through PipeWire."""
    player = shutil.which("pw-play") or shutil.which("paplay")
    if not player or not path or not Path(path).exists():
        return None
    return subprocess.Popen([player, str(path)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
