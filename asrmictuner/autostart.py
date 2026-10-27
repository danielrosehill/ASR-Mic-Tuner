"""Start-in-tray at login, via an XDG autostart entry (Plasma honours it)."""

import os
import shutil
from pathlib import Path

AUTOSTART = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "autostart" / "asr-mic-tuner.desktop"

ENTRY = """[Desktop Entry]
Type=Application
Name=ASR Mic Tuner
Comment=Microphone monitor for dictation, in the system tray
Exec={exec} --tray
Icon=audio-input-microphone
X-GNOME-Autostart-enabled=true
X-KDE-autostart-phase=2
NoDisplay=true
"""


def enabled():
    return AUTOSTART.exists()


def set_enabled(on):
    if on:
        AUTOSTART.parent.mkdir(parents=True, exist_ok=True)
        AUTOSTART.write_text(ENTRY.format(exec=shutil.which("asr-mic-tuner") or "asr-mic-tuner"))
    else:
        AUTOSTART.unlink(missing_ok=True)
