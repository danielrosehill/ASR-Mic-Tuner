"""ASR Mic: the processed microphone as a source of its own.

EasyEffects publishes its processed input as `easyeffects_source` ("Easy
Effects Source"). ASR Mic is a `pw-loopback` from that node into a virtual
source named "ASR Mic", run by a systemd user unit so it survives logins:

    hardware mic ──► EasyEffects chain ──► easyeffects_source ──► ASR Mic ──► dictation app
         │
         └──► everything else (calls, recorders) gets the raw mic, still the default

The raw mic stays the system default and untouched; only apps pointed at
"ASR Mic" get the chain. If EasyEffects is not running, ASR Mic is silent
rather than quietly falling back to the raw mic (`node.dont-fallback`): the
source is simply absent until EasyEffects is back, so a broken chain is
noticed instead of masked.

Node names start with "asr-mic-tuner." so the quiet-mic watcher can tell the
loopback's own capture apart from a real recorder. The name `asr_mic` is
avoided on purpose: the older sibling project dictamic creates a source with
that name.
"""

import shutil
import signal
import subprocess
import sys
import time

from . import APP
from .core import ASR_MIC_NODE, UNIT_FILE, own_exec_path, pactl_json, run

UNIT_NAME = f"{APP}-asr-mic.service"
UNIT_PATH = UNIT_FILE.parent / UNIT_NAME
UPSTREAM = "easyeffects_source"

POLL_S = 3
LOOPBACK_ARGS = [
    "--name", f"{ASR_MIC_NODE}-loopback",
    f"--capture-props={{ node.name={ASR_MIC_NODE}-capture target.object={UPSTREAM} node.passive=true "
    "node.dont-fallback=true stream.dont-remix=true }",
    f'--playback-props={{ node.name={ASR_MIC_NODE} media.class=Audio/Source node.description="ASR Mic" }}',
]

# pw-loopback exits (status 0) when its target disappears, and exits at once
# if started without one (node.dont-fallback). So the unit runs a supervisor,
# `asr-mic-tuner --asr-mic` (serve() below), that keeps one loopback running
# exactly while easyeffects_source exists. Verified on 2026-10-27,
# PipeWire 1.6.2.
UNIT = """[Unit]
Description=ASR Mic: the EasyEffects-processed microphone as its own source (ASR Mic Tuner)
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


def serve():
    """Supervisor loop for the unit: loopback up while EasyEffects' output exists."""
    proc = None

    def stop(*_):
        if proc and proc.poll() is None:
            proc.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    loopback = shutil.which("pw-loopback")
    if not loopback:
        print("pw-loopback not found (package pipewire-bin)", file=sys.stderr)
        return 1
    while True:
        up = upstream_present()
        running = proc is not None and proc.poll() is None
        if up and not running:
            proc = subprocess.Popen([loopback, *LOOPBACK_ARGS])
        elif not up and running:
            proc.terminate()
            proc.wait(timeout=5)
        time.sleep(POLL_S)


def available():
    return bool(shutil.which("pw-loopback") and shutil.which("systemctl"))


def state():
    """One line for the UI."""
    if not enabled():
        return "off"
    if not running():
        return "paused"
    if present():
        return "active"
    return "waiting for EasyEffects" if not upstream_present() else "starting"


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


def upstream_present():
    return any(e.get("name") == UPSTREAM for e in pactl_json("list", "sources") or [])


def set_enabled(on):
    if not available():
        return False, "pw-loopback or systemctl is missing (package pipewire-bin)."
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
