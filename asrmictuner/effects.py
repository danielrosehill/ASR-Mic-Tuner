"""EasyEffects input presets: the speech-relevant subset, chain templates,
and per-microphone autoload bindings.

ASR Mic Tuner still processes no audio itself. This module reads and writes
EasyEffects' own files and asks EasyEffects (over its command line) to load
a preset; the processing is EasyEffects'. Verified against EasyEffects 8.3.0
(Flatpak), source tag v8.3.0:

- presets: `<data>/input/<name>.json`, `{"input": {"blocklist": [],
  "plugins_order": ["rnnoise#0", ...], "rnnoise#0": {...}}}`. A key left out
  of a plugin's section loads as that plugin's default (presets_macros.hpp,
  UPDATE_PROPERTY), so presets written here carry only the settings shown.
- enum-like settings (filter type, slope, autogain reference) are stored as
  their label strings, not indices.
- autoload: `<data>/autoload/input/<device>:<port description>.json` with
  `device`, `device-description`, `device-profile` (the port description,
  "/" replaced by "_" in the file name) and `preset-name`
  (presets_autoload_manager.cpp).
- `<data>` is `~/.var/app/com.github.wwmm.easyeffects/data/easyeffects` for
  the Flatpak and `$XDG_DATA_HOME/easyeffects` for a native install.
- `easyeffects -l NAME` loads the input preset of that name if one exists,
  else the output one; it needs a running instance.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

from .core import run

FLATPAK_ID = "com.github.wwmm.easyeffects"
_HOME = Path.home()
_DATA = Path(os.environ.get("XDG_DATA_HOME") or _HOME / ".local/share")
_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME") or _HOME / ".config")


# ---------------------------------------------------------------------------
# the effect library
# ---------------------------------------------------------------------------
# A parameter: (key, label, kind, low, high, EasyEffects default, unit).
# `kind` is "float", "int", "bool" or a list of enum labels. Dotted keys are
# nested sections ("vad.enable"). The default is EasyEffects' own, shown when
# a preset leaves the key out; `start` holds the values a newly added effect
# gets here, chosen for speech going into recognition.

FILTER_TYPES = ["High-pass", "Low-pass", "Low-shelf", "High-shelf", "Bell", "Notch"]
SLOPES = ["x1", "x2", "x3", "x4"]
AUTOGAIN_REFS = ["Momentary", "Shortterm", "Integrated", "Geometric Mean (MSI)",
                 "Geometric Mean (MS)", "Geometric Mean (MI)", "Geometric Mean (SI)"]
STEREO_MODES = ["LR > LR (Stereo Default)", "LR > LL (Mono Left Channel)",
                "LR > RR (Mono Right Channel)", "LR > L+R (Mono Sum L+R)"]

EFFECTS = {
    "filter": {
        "name": "High-pass filter",
        "blurb": "Cuts rumble, desk thumps and HVAC below the voice. Cheap and nearly always worth having first.",
        "params": [
            ("type", "Type", FILTER_TYPES, None, None, "Low-pass", ""),
            ("frequency", "Frequency", "float", 20, 500, 2000, "Hz"),
            ("slope", "Slope", SLOPES, None, None, "x1", ""),
        ],
        "start": {"type": "High-pass", "frequency": 80.0, "slope": "x2"},
    },
    "stereo_tools": {
        "name": "Mono (stereo tools)",
        "blurb": "Folds a two-channel mic to mono, or keeps the better channel. Laptop arrays often differ per side.",
        "params": [("mode", "Mode", STEREO_MODES, None, None, "LR > LR (Stereo Default)", "")],
        "start": {"mode": "LR > L+R (Mono Sum L+R)"},
    },
    "gate": {
        "name": "Noise gate",
        "blurb": "Turns the room down between phrases. Keep the reduction gentle: a hard gate clips word onsets.",
        "params": [
            ("curve-threshold", "Threshold", "float", -60, 0, -24, "dB"),
            ("reduction", "Reduction", "float", -72, 0, -24, "dB"),
            ("attack", "Attack", "float", 0, 2000, 20, "ms"),
            ("release", "Release", "float", 0, 5000, 100, "ms"),
        ],
        "start": {"curve-threshold": -50.0, "reduction": -15.0, "attack": 5.0, "release": 250.0},
    },
    "rnnoise": {
        "name": "Noise reduction (RNNoise)",
        "blurb": "Light neural denoiser for steady background noise. Low CPU, few artefacts.",
        "params": [
            ("enable-vad", "Voice activity gate", "bool", None, None, False, ""),
            ("vad-thres", "VAD threshold", "float", 0, 100, 50, "%"),
            ("wet", "Wet level", "float", -100, 20, 0, "dB"),
            ("release", "Release", "float", 0, 20000, 20, "ms"),
        ],
        "start": {"enable-vad": False},
    },
    "deepfilternet": {
        "name": "Noise reduction (DeepFilterNet)",
        "blurb": "Stronger neural denoiser for fans, traffic, keyboards. Cap the attenuation: full strength smears consonants.",
        "params": [
            ("attenuation-limit", "Attenuation limit", "float", 0, 100, 100, "dB"),
            ("post-filter-beta", "Post-filter beta", "float", 0, 0.05, 0.02, ""),
        ],
        "start": {"attenuation-limit": 30.0},
    },
    "speex": {
        "name": "Speex (denoise, AGC, dereverb)",
        "blurb": "Classic all-in-one voice processor. Lighter than the neural options; its AGC is basic.",
        "params": [
            ("enable-denoise", "Denoise", "bool", None, None, True, ""),
            ("noise-suppression", "Suppression", "int", -100, -1, -70, "dB"),
            ("enable-agc", "Automatic gain", "bool", None, None, False, ""),
            ("enable-dereverb", "Dereverb", "bool", None, None, False, ""),
            ("vad.enable", "Voice activity gate", "bool", None, None, False, ""),
        ],
        "start": {"enable-denoise": True, "noise-suppression": -30, "enable-agc": False,
                  "enable-dereverb": False, "vad": {"enable": False}},
    },
    "autogain": {
        "name": "Auto gain",
        "blurb": "Evens out loudness towards a target (LUFS). Fix the analog gain first: this cannot undo clipping.",
        "params": [
            ("target", "Target", "float", -100, 0, -23, "LUFS"),
            ("silence-threshold", "Silence threshold", "float", -100, 0, -70, "dB"),
            ("maximum-history", "History", "int", 6, 3600, 15, "s"),
            ("reference", "Reference", AUTOGAIN_REFS, None, None, "Geometric Mean (MSI)", ""),
        ],
        "start": {"target": -20.0, "silence-threshold": -60.0, "maximum-history": 15},
    },
    "compressor": {
        "name": "Compressor",
        "blurb": "Narrows the gap between loud and soft syllables. Moderate settings only.",
        "params": [
            ("threshold", "Threshold", "float", -60, 0, -12, "dB"),
            ("ratio", "Ratio", "float", 1, 100, 4, ":1"),
            ("attack", "Attack", "float", 0, 2000, 20, "ms"),
            ("release", "Release", "float", 0, 5000, 100, "ms"),
            ("makeup", "Makeup", "float", -60, 60, 0, "dB"),
        ],
        "start": {"threshold": -24.0, "ratio": 3.0, "attack": 10.0, "release": 150.0},
    },
    "deesser": {
        "name": "De-esser",
        "blurb": "Tames sharp S and SH sounds that some recognisers mishear.",
        "params": [
            ("threshold", "Threshold", "float", -60, 0, -18, "dB"),
            ("ratio", "Ratio", "float", 1, 20, 3, ":1"),
        ],
        "start": {"threshold": -24.0, "ratio": 3.0},
    },
    "limiter": {
        "name": "Limiter",
        "blurb": "A ceiling after the gain stages so nothing digital clips. Belongs last.",
        "params": [
            ("threshold", "Threshold", "float", -48, 0, 0, "dB"),
            ("lookahead", "Lookahead", "float", 0.1, 20, 5, "ms"),
            ("release", "Release", "float", 0.25, 20, 5, "ms"),
        ],
        "start": {"threshold": -3.0},
    },
}

TEMPLATES = [
    ("Light clean-up", "High-pass and RNNoise: quiet room, decent mic.",
     [("filter", {}), ("rnnoise", {})]),
    ("Dictation standard", "High-pass, DeepFilterNet, auto gain, limiter: the usual chain.",
     [("filter", {}), ("deepfilternet", {}), ("autogain", {}), ("limiter", {})]),
    ("Noisy room", "Adds a gentle gate and stronger denoising for fans, traffic or open offices.",
     [("filter", {"frequency": 100.0}), ("gate", {}), ("deepfilternet", {"attenuation-limit": 50.0}),
      ("autogain", {}), ("limiter", {})]),
    ("Laptop array to mono", "Sums a two-channel internal mic to mono, then high-pass and RNNoise.",
     [("stereo_tools", {}), ("filter", {}), ("rnnoise", {})]),
    ("Speex all-in-one", "One lightweight block: denoise and automatic gain.",
     [("filter", {}), ("speex", {"enable-agc": True})]),
    ("Level only", "No denoising: auto gain and a limiter.",
     [("autogain", {}), ("limiter", {})]),
]


def kind_of(instance_id):
    return instance_id.split("#", 1)[0]


def describe(entry):
    """Short label for a chain entry: "High-pass filter 80 Hz"."""
    kind = entry["kind"]
    spec = EFFECTS.get(kind)
    if not spec:
        return f"{kind} (edit in EasyEffects)"
    key, _label, ptype, _lo, _hi, default, unit = spec["params"][0]
    value = get_value(entry["settings"], key, default)
    if kind == "filter":
        return f"{value} {get_value(entry['settings'], 'frequency', 2000):g} Hz"
    if ptype == "bool" or isinstance(ptype, list):
        return spec["name"]
    return f"{spec['name']} {value:g} {unit}".strip()


def get_value(settings, key, default=None):
    node = settings
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def set_value(settings, key, value):
    parts = key.split(".")
    node = settings
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def new_entry(kind, chain, overrides=None):
    taken = {e["id"] for e in chain}
    n = 0
    while f"{kind}#{n}" in taken:
        n += 1
    settings = json.loads(json.dumps(EFFECTS[kind]["start"]))
    settings["bypass"] = False
    for key, value in (overrides or {}).items():
        set_value(settings, key, value)
    return {"id": f"{kind}#{n}", "kind": kind, "settings": settings}


def from_template(name):
    chain = []
    for _n, _b, steps in (t for t in TEMPLATES if t[0] == name):
        for kind, overrides in steps:
            chain.append(new_entry(kind, chain, overrides))
    return chain


# ---------------------------------------------------------------------------
# the installation
# ---------------------------------------------------------------------------

class EasyEffects:
    def __init__(self):
        self.flatpak_dir = _HOME / ".var/app" / FLATPAK_ID
        native = shutil.which("easyeffects")
        if native:
            self.kind, self.command = "native", [native]
            self.root = self._pick(_DATA / "easyeffects", _CONFIG / "easyeffects")
        elif shutil.which("flatpak") and (self.flatpak_dir.exists() or self._flatpak_installed()):
            self.kind, self.command = "flatpak", ["flatpak", "run", FLATPAK_ID]
            self.root = self._pick(self.flatpak_dir / "data/easyeffects", self.flatpak_dir / "config/easyeffects")
        else:
            self.kind, self.command, self.root = None, None, None

    @staticmethod
    def _flatpak_installed():
        return subprocess.run(["flatpak", "info", FLATPAK_ID], capture_output=True).returncode == 0

    @staticmethod
    def _pick(data_dir, config_dir):
        """EasyEffects 8 keeps presets under the data dir; 7.x used config."""
        if (data_dir / "input").exists() or not (config_dir / "input").exists():
            return data_dir
        return config_dir

    @property
    def installed(self):
        return self.command is not None

    @property
    def presets_dir(self):
        return self.root / "input"

    @property
    def autoload_dir(self):
        return self.root / "autoload" / "input"

    def running(self):
        return bool(run(["pgrep", "-x", "easyeffects"]).strip())

    # -- command line --
    def _cli(self, *args, timeout=15):
        """(ok, output). Answers come on stdout; stderr carries EasyEffects'
        own log lines ("easyeffects: presets_directory_manager.cpp:140 ..."),
        which are returned only when the call fails."""
        try:
            proc = subprocess.run(self.command + list(args), capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        if proc.returncode == 0:
            return True, proc.stdout.strip()
        lines = [ln for ln in proc.stderr.splitlines() if not ln.startswith("QThreadStorage")]
        return False, (lines[-1] if lines else f"exit status {proc.returncode}")

    def start(self):
        """Start the background service with its window hidden."""
        subprocess.Popen(self.command + ["--service-mode", "-w"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)

    def open_window(self):
        subprocess.Popen(self.command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)

    def load(self, name):
        return self._cli("-l", name)

    def last_loaded(self):
        ok, out = self._cli("-a", "input")
        return out.splitlines()[-1].strip() if ok and out else ""

    def bypassed(self):
        ok, out = self._cli("-b", "3")
        return ok and bool(out) and out.splitlines()[-1].strip() == "1"

    def set_bypass(self, on):
        return self._cli("-b", "1" if on else "2")

    # -- presets --
    def presets(self):
        if not self.root or not self.presets_dir.exists():
            return []
        return sorted(p.stem for p in self.presets_dir.glob("*.json"))

    def read_preset(self, name):
        """(chain, raw): the chain as [{id, kind, settings}] in order, and the
        whole file so effects not edited here survive a save."""
        raw = json.loads((self.presets_dir / f"{name}.json").read_text())
        section = raw.get("input", {})
        chain = [{"id": pid, "kind": kind_of(pid), "settings": section.get(pid, {})}
                 for pid in section.get("plugins_order", [])]
        return chain, raw

    def write_preset(self, name, chain, raw=None):
        raw = json.loads(json.dumps(raw)) if raw else {}
        section = raw.setdefault("input", {})
        section.setdefault("blocklist", [])
        for key in [k for k in section if "#" in k]:
            del section[key]
        section["plugins_order"] = [e["id"] for e in chain]
        for entry in chain:
            section[entry["id"]] = entry["settings"]
        self.presets_dir.mkdir(parents=True, exist_ok=True)
        path = self.presets_dir / f"{name}.json"
        path.write_text(json.dumps(raw, indent=4) + "\n")
        return path

    # -- per-microphone autoload --
    def bindings(self):
        if not self.root or not self.autoload_dir.exists():
            return []
        out = []
        for path in sorted(self.autoload_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, json.JSONDecodeError):
                continue
            data["path"] = str(path)
            out.append(data)
        return out

    def bind(self, device, preset):
        route = device.port or ""
        path = self.autoload_dir / f"{device.name}:{route.replace('/', '_')}.json"
        self.autoload_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "device": device.name,
            "device-description": device.description,
            "device-profile": route,
            "preset-name": preset,
        }, indent=4) + "\n")
        return path

    def unbind(self, path):
        path = Path(path)
        if path.parent == self.autoload_dir:
            path.unlink(missing_ok=True)
