"""Persistent state beyond devices.json: measurement history, room and voice
profiles, recorded samples, and app settings.

Layout (XDG):

    ~/.config/micscope/devices.json     remembered gain, presets, curve cache (core.py)
    ~/.config/micscope/settings.json    app settings, incl. the OpenRouter key
    ~/.local/share/micscope/history.db  SQLite: measurements, profiles
    ~/.local/share/micscope/samples/    recorded room tone and voice, FLAC (or WAV)
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

from .core import CONFIG_DIR

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "micscope"
SAMPLES_DIR = DATA_DIR / "samples"
DB_FILE = DATA_DIR / "history.db"
SETTINGS_FILE = CONFIG_DIR / "settings.json"

DEFAULT_SETTINGS = {
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
    "voice_seconds": 20,
    "calibration_mode": "standard",
    "active_room_profile": {},          # device name -> profile id
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
    try:
        data = json.loads(SETTINGS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    merged = dict(DEFAULT_SETTINGS)
    merged.update(data)
    return merged


def save_settings(settings):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SETTINGS_FILE.write_text(json.dumps(settings, indent=2) + "\n")
    try:
        SETTINGS_FILE.chmod(0o600)          # holds an API key
    except OSError:
        pass


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

    def measurements(self, device=None, kind=None, limit=500):
        sql, args = "SELECT * FROM measurements WHERE 1=1", []
        if device:
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
            "INSERT INTO profiles (created, kind, name, device, measurement_id, notes) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now_iso(), kind, name, device, measurement_id, notes),
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
