"""ASR Mic: the processed microphone as an input of its own.

A systemd user unit (`asr-mic-tuner-asr-mic.service`, WantedBy
pipewire.service) runs `asr-mic-tuner --asr-mic`, the supervisor below.
Every POLL_S seconds it works out which microphone feeds ASR Mic and which
chain is bound to it (engine.py), and keeps exactly one `pipewire -c` process
running that chain. When the microphone, the chain, or a plugin changes, it
regenerates the config and restarts the process. That takes about a second,
and ASR Mic disappears briefly while it happens.

The raw microphone stays the system default and is never altered; only apps
pointed at "ASR Mic" get the chain. The capture side uses
`node.dont-fallback`, so if the microphone goes away, ASR Mic goes silent rather
than quietly switching to another mic.

Node names start with "asr-mic-tuner." so the quiet-mic watcher can tell the
chain's own capture apart from a real recorder. The name `asr_mic` is avoided
on purpose: the older sibling project dictamic creates a source with that name.
"""

import json
import shutil
import signal
import subprocess
import sys
import time

from . import APP, engine
from .core import ASR_MIC_NODE, UNIT_FILE, default_source, list_devices, own_exec_path, pactl_json, run

UNIT_NAME = f"{APP}-asr-mic.service"
UNIT_PATH = UNIT_FILE.parent / UNIT_NAME
STATE_FILE = engine.CONFIG_DIR / "asr-mic-state.json"
POLL_S = 3

UNIT = """[Unit]
Description=ASR Mic: the microphone through its ASR effects chain (ASR Mic Tuner)
Documentation=file:///usr/share/doc/@APP@/asr-mic.md
After=pipewire.service wireplumber.service
PartOf=pipewire.service

[Service]
ExecStart=@EXEC@ --asr-mic
Restart=always
RestartSec=5

[Install]
WantedBy=pipewire.service
"""


def unit_text():
    return UNIT.replace("@EXEC@", own_exec_path()).replace("@APP@", APP)


def available():
    return bool(shutil.which("pipewire") and shutil.which("systemctl"))


def enabled():
    return UNIT_PATH.exists() and run(["systemctl", "--user", "is-enabled", UNIT_NAME]).strip() == "enabled"


def running():
    return run(["systemctl", "--user", "is-active", UNIT_NAME]).strip() == "active"


def set_running(on):
    """Start or stop for now, leaving the at-login setting alone: for a call
    where you want ASR Mic out of the device list without turning it off."""
    run(["systemctl", "--user", "start" if on else "stop", UNIT_NAME])
    return running() == on


def present():
    """The source exists in the graph right now."""
    return any(e.get("name") == ASR_MIC_NODE for e in pactl_json("list", "sources") or [])


def read_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def state():
    """One word for the UI."""
    if not enabled():
        return "off"
    if not running():
        return "paused"
    return "active" if present() else "starting"


def set_enabled(on):
    if not available():
        return False, "pipewire or systemctl is missing."
    if on:
        UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        UNIT_PATH.write_text(unit_text())
        run(["systemctl", "--user", "daemon-reload"])
        run(["systemctl", "--user", "enable", "--now", UNIT_NAME])
        return enabled(), "ASR Mic created. Pick “ASR Mic” as the input in your dictation app."
    run(["systemctl", "--user", "disable", "--now", UNIT_NAME])
    UNIT_PATH.unlink(missing_ok=True)
    run(["systemctl", "--user", "daemon-reload"])
    return True, "ASR Mic removed."


# ---------------------------------------------------------------------------
# the supervisor
# ---------------------------------------------------------------------------

def pick_device(settings):
    """The microphone that feeds ASR Mic: the one chosen in ASR Effects, or
    else the hardware behind the system default input."""
    devices = [d for d in list_devices() if not d.name.startswith(ASR_MIC_NODE)]
    by_name = {d.name: d for d in devices}
    chosen = settings.get("asr_mic_source") or ""
    if chosen in by_name:
        return by_name[chosen]
    default = by_name.get(default_source())
    if default and default.is_hardware:
        return default
    return next((d for d in devices if d.is_hardware), default)


def plan(settings):
    """(device, chain name, effects, input mix) for the current settings."""
    device = pick_device(settings)
    if device is None:
        return None, None, [], engine.INPUT_MIX[0]
    name = (settings.get("chain_bindings") or {}).get(engine.binding_key(device.name, device.port))
    effects, mix = [], engine.INPUT_MIX[0]
    if name and not settings.get("asr_mic_bypass"):
        try:
            effects, mix = engine.load_chain(name)
        except (OSError, ValueError):
            name = None
    if not settings.get("asr_mic_bypass"):
        effects = engine.with_noise_reduction(effects, settings.get("noise_reduction", "off"))
    return device, name, effects, mix


def serve():
    from .store import load_settings
    proc = None

    def stop(*_):
        if proc and proc.poll() is None:
            proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    pipewire = shutil.which("pipewire")
    if not pipewire:
        print("pipewire binary not found", file=sys.stderr)
        return 1
    last = None
    while True:
        settings = load_settings()
        device, name, effects, mix = plan(settings)
        text, skipped = engine.config_text(device, effects, mix) if device else (None, [])
        if text != last or (proc is not None and proc.poll() is not None):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            proc = None
            if text:
                engine.RUN_CONF.parent.mkdir(parents=True, exist_ok=True)
                engine.RUN_CONF.write_text(text)
                proc = subprocess.Popen([pipewire, "-c", str(engine.RUN_CONF)])
            last = text
            STATE_FILE.write_text(json.dumps({
                "device": device.name if device else None,
                "device_description": device.description if device else None,
                "chain": name, "bypass": bool(settings.get("asr_mic_bypass")),
                "noise_reduction": settings.get("noise_reduction", "off"),
                "skipped": skipped, "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
            }, indent=2) + "\n")
        time.sleep(POLL_S)
