"""One-time migration from the earlier names, micscope and mic-monitor.

v0.1–0.2 were called micscope and v0.3 mic-monitor, and each kept state under
its own name. On first run as asr-mic-tuner this moves the config and data
directories, rewrites the absolute sample paths stored in history.db, and
replaces the old KWin rule, autostart entry and login unit with renamed ones.
Each step only acts if the old artefact exists and the new one does not, so it
is safe to call on every start. The newest old name is tried first, so a
machine that went through both renames keeps its mic-monitor state.
"""

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

OLD_NAMES = ("mic-monitor", "micscope")

_config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
_data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def migrate():
    notes = []
    for old in OLD_NAMES:
        notes += [f"from {old}: {note}" for note in _migrate_from(old)]
    return notes


def _migrate_from(old):
    from .core import CONFIG_DIR, UNIT_FILE, UNIT_NAME, set_unit_enabled
    from .store import DATA_DIR, DB_FILE

    notes = []
    old_config, old_data = _config / old, _data / old
    if old_config.is_dir() and not CONFIG_DIR.exists():
        shutil.move(str(old_config), str(CONFIG_DIR))
        notes.append(f"moved {old_config} -> {CONFIG_DIR}")
    if old_data.is_dir() and not DATA_DIR.exists():
        shutil.move(str(old_data), str(DATA_DIR))
        notes.append(f"moved {old_data} -> {DATA_DIR}")
        if DB_FILE.exists():
            db = sqlite3.connect(str(DB_FILE))
            for col in ("sample_path", "noiseprof"):
                db.execute(f"UPDATE measurements SET {col} = replace({col}, ?, ?) "
                           f"WHERE {col} LIKE ?", (str(old_data), str(DATA_DIR), f"{old_data}%"))
            db.commit()
            db.close()

    old_autostart = _config / "autostart" / f"{old}.desktop"
    if old_autostart.exists():
        from . import autostart
        old_autostart.unlink()
        autostart.set_enabled(True)
        notes.append("autostart entry renamed")

    old_unit = UNIT_FILE.parent / f"{old}-restore.service"
    if old_unit.exists():
        subprocess.run(["systemctl", "--user", "disable", old_unit.name], capture_output=True)
        old_unit.unlink(missing_ok=True)
        if not UNIT_FILE.exists():
            set_unit_enabled(True)
        notes.append(f"login unit renamed to {UNIT_NAME}")

    from . import kwin
    if kwin.available() and f"{old}-tray-popup" in kwin._read("General", "rules").split(","):
        kwin.remove_rule(f"{old}-tray-popup")
        notes.append("old KWin rule removed")
    return notes
