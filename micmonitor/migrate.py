"""One-time migration from the pre-0.3 name, micscope.

v0.1–0.2 were called micscope and kept state under that name. On first run
as mic-monitor this moves the config and data directories, rewrites the
absolute sample paths stored in history.db, and replaces the old KWin rule,
autostart entry and login unit with renamed ones. Each step only acts if the
old artefact exists and the new one does not, so it is safe to call on
every start.
"""

import os
import shutil
import sqlite3
import subprocess
from pathlib import Path

OLD = "micscope"

_config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
_data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")


def migrate():
    from .core import CONFIG_DIR, UNIT_FILE, UNIT_NAME, set_unit_enabled
    from .store import DATA_DIR, DB_FILE

    notes = []
    old_config, old_data = _config / OLD, _data / OLD
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

    old_autostart = _config / "autostart" / f"{OLD}.desktop"
    if old_autostart.exists():
        from . import autostart
        old_autostart.unlink()
        autostart.set_enabled(True)
        notes.append("autostart entry renamed")

    old_unit = UNIT_FILE.parent / f"{OLD}-restore.service"
    if old_unit.exists():
        subprocess.run(["systemctl", "--user", "disable", old_unit.name], capture_output=True)
        old_unit.unlink(missing_ok=True)
        if not UNIT_FILE.exists():
            set_unit_enabled(True)
        notes.append(f"login unit renamed to {UNIT_NAME}")

    from . import kwin
    if kwin.available() and f"{OLD}-tray-popup" in kwin._read("General", "rules").split(","):
        kwin.remove_rule(f"{OLD}-tray-popup")
        notes.append("old KWin rule removed")
    return notes
