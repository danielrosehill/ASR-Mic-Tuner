"""The effects engine: input chains run by PipeWire itself, published as "ASR Mic".

A chain is an ordered list of effects saved as JSON under
`~/.config/asr-mic-tuner/chains/<name>.json` and bound to a microphone and
port. The supervisor (`asr-mic-tuner --asr-mic`, run by a systemd user unit,
see asrmic.py) turns the chain bound to the active microphone into a PipeWire
filter-chain config and runs it as its own process (`pipewire -c <file>`):

    hardware mic ──► [mono mix → effects…] ──► "ASR Mic" (mono, 48 kHz) ──► dictation app
         │
         └──► default input, untouched, for everything else

Where the effects come from (verified against PipeWire 1.6.2, 2026-10-27):

- **Built into PipeWire** (`builtin` filter-graph plugin, no extra packages):
  biquad high-pass, low-pass, notch and peaking EQ, the `mixer` that folds the
  mic to mono, `linear` gain and `clamp`.
- **PipeWire's EBU R128 plugin** (`ebur128`, package libspa-0.2-modules-extra):
  the loudness auto gain, `ebur128` → `lufs2gain` → `clamp` → `linear`.
- **swh-plugins** (Ubuntu archive, LADSPA): `gate`, `sc4m` compressor and
  `fastLookaheadLimiter`.
- **Downloaded from the maintainers' GitHub releases**, pinned by SHA-256
  (PLUGINS below), into `~/.local/share/asr-mic-tuner/plugins/`: RNNoise
  (werman/noise-suppression-for-voice) and DeepFilterNet (Rikorose/DeepFilterNet).

Port names and ranges were read from the PipeWire 1.6.2 source
(spa/plugins/filter-graph/plugin_builtin.c, plugin_ebur128.c) and from the
LADSPA descriptors of the plugin binaries themselves.
"""

import hashlib
import json
import math
import os
import shutil
import urllib.request
import zipfile
from pathlib import Path

from . import APP
from .core import ASR_MIC_NODE

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / APP
CHAINS_DIR = CONFIG_DIR / "chains"
PLUGIN_DIR = DATA_DIR / "plugins"
RUN_CONF = CONFIG_DIR / "asr-mic-chain.conf"     # what the supervisor last generated
CAPTURE_NODE = f"{ASR_MIC_NODE}-capture"
RATE = 48000                                     # RNNoise and DeepFilterNet both require it

FILTER_GRAPH_DIRS = [Path("/usr/lib/x86_64-linux-gnu/spa-0.2/filter-graph"),
                     Path("/usr/lib/spa-0.2/filter-graph"), Path("/usr/lib64/spa-0.2/filter-graph")]
LADSPA_DIRS = [Path(p) for p in (os.environ.get("LADSPA_PATH") or "").split(":") if p] + [
    Path("/usr/lib/ladspa"), Path("/usr/local/lib/ladspa"), Path("/usr/lib/x86_64-linux-gnu/ladspa"),
    Path("/usr/lib64/ladspa")]

# Upstream binaries, fetched only when the user asks. Hashes computed on
# 2026-10-27 from the release assets.
PLUGINS = {
    "rnnoise": {
        "title": "RNNoise (werman/noise-suppression-for-voice v1.10)",
        "url": "https://github.com/werman/noise-suppression-for-voice/releases/download/v1.10/linux-rnnoise.zip",
        "sha256": "811390b6eb6e28dde023c70590c74d26e74ebb2e595bcf4b95af2341db160e99",
        "member": "linux-rnnoise/ladspa/librnnoise_ladspa.so",
        "file": "librnnoise_ladspa.so",
        "file_sha256": "ba2b420c76cd2f1e7684430c96fef8c533041d5f67b94060f8eeda6af1701f97",
        "size_mb": 37,
    },
    "deepfilternet": {
        "title": "DeepFilterNet (Rikorose/DeepFilterNet v0.5.6)",
        "url": "https://github.com/Rikorose/DeepFilterNet/releases/download/v0.5.6/"
               "libdeep_filter_ladspa-0.5.6-x86_64-unknown-linux-gnu.so",
        "sha256": "2ca3205c2911d389604a826a240e745597d50252b5cab81c8248252b335e2236",
        "file": "libdeep_filter_ladspa.so",
        "file_sha256": "2ca3205c2911d389604a826a240e745597d50252b5cab81c8248252b335e2236",
        "size_mb": 0,
    },
}


# ---------------------------------------------------------------------------
# what is available on this machine
# ---------------------------------------------------------------------------

def _find(dirs, name):
    return next((d / name for d in dirs if (d / name).exists()), None)


def plugin_path(key):
    path = PLUGIN_DIR / PLUGINS[key]["file"]
    return path if path.exists() else None


def backend_ready(backend):
    """(ready, what is missing) for an effect's backend."""
    if backend == "builtin":
        return True, ""
    if backend == "ebur128":
        ok = _find(FILTER_GRAPH_DIRS, "libspa-filter-graph-plugin-ebur128.so") is not None
        return ok, "" if ok else "PipeWire's loudness plugin (package libspa-0.2-modules-extra)"
    if backend == "swh":
        ok = all(_find(LADSPA_DIRS, f) for f in ("sc4m_1916.so", "fast_lookahead_limiter_1913.so", "gate_1410.so"))
        return ok, "" if ok else "swh-plugins (sudo apt install swh-plugins)"
    if backend in PLUGINS:
        ok = plugin_path(backend) is not None
        return ok, "" if ok else f"{PLUGINS[backend]['title']}: press Get"
    return False, backend


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(key):
    """Fetch an upstream plugin, verify it, install it. Runs on a worker thread."""
    spec = PLUGINS[key]
    PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PLUGIN_DIR / (spec["file"] + ".download")
    with urllib.request.urlopen(spec["url"], timeout=120) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f)
    if _sha256(tmp) != spec["sha256"]:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"checksum mismatch for {spec['url']}; not installed")
    target = PLUGIN_DIR / spec["file"]
    if "member" in spec:
        with zipfile.ZipFile(tmp) as z, z.open(spec["member"]) as src, open(target, "wb") as dst:
            shutil.copyfileobj(src, dst)
        tmp.unlink()
    else:
        tmp.replace(target)
    if _sha256(target) != spec["file_sha256"]:
        target.unlink(missing_ok=True)
        raise RuntimeError("extracted plugin failed its checksum; not installed")
    target.chmod(0o644)
    return str(target)


# ---------------------------------------------------------------------------
# the effect catalogue
# ---------------------------------------------------------------------------
# A parameter: (key, label, kind, low, high, default, unit). `kind` is
# "float", "int", "bool" or a list of choices.

VAD_CAUTION = (
    "Use with caution. A VAD mutes everything it judges not to be speech. Most transcription apps "
    "(Whisper-based tools, cloud ASR, dictation apps) already run their own VAD, and two in series "
    "clip word starts and drop quiet words. Add one only if your app has none, then run a mic test: "
    "dropouts in the result mean it is cutting speech.")

EFFECTS = {
    "highpass": {
        "name": "High-pass filter", "backend": "builtin",
        "blurb": "Cuts rumble, desk thumps and HVAC below the voice. Cheap and nearly always worth having first.",
        "params": [("frequency", "Frequency", "float", 20, 300, 80, "Hz"),
                   ("steep", "Steep (24 dB/octave)", "bool", None, None, True, "")],
    },
    "lowpass": {
        "name": "Low-pass filter", "backend": "builtin",
        "blurb": "Cuts hiss above the voice. Most recognisers work at 16 kHz and discard everything above 8 kHz anyway.",
        "params": [("frequency", "Frequency", "float", 3000, 20000, 8000, "Hz"),
                   ("steep", "Steep (24 dB/octave)", "bool", None, None, True, "")],
    },
    "notch": {
        "name": "Hum notch", "backend": "builtin",
        "blurb": "Removes mains hum at 50 or 60 Hz and its first harmonics.",
        "params": [("frequency", "Mains frequency", ["50", "60"], None, None, "50", "Hz"),
                   ("harmonics", "Harmonics", "int", 1, 4, 3, "")],
    },
    "eq": {
        "name": "EQ band", "backend": "builtin",
        "blurb": "One peaking band: a little presence (2–4 kHz) helps consonants; a cut tames a boomy room.",
        "params": [("frequency", "Frequency", "float", 50, 12000, 3000, "Hz"),
                   ("gain", "Gain", "float", -12, 12, 3, "dB"),
                   ("q", "Q", "float", 0.3, 8, 1.0, "")],
    },
    "gate": {
        "name": "Noise gate", "backend": "swh",
        "blurb": "Turns the room down between phrases. It lowers rather than mutes, so word onsets survive.",
        "params": [("threshold", "Open above", "float", -70, -10, -50, "dBFS"),
                   ("reduction", "Reduction", "float", -40, -3, -12, "dB"),
                   ("attack", "Attack", "float", 1, 100, 5, "ms"),
                   ("hold", "Hold", "float", 0, 1000, 300, "ms"),
                   ("release", "Release", "float", 10, 1000, 150, "ms")],
    },
    "autogain": {
        "name": "Auto gain (loudness, 30 s)", "backend": "ebur128",
        "blurb": "Holds your loudness at a target. It measures gated loudness (EBU R128) over the last 30 s, "
                 "so silence and pauses don't count and the room is never pumped up. Bounded both ways.",
        "params": [("target", "Target", "float", -40, -10, -20, "LUFS"),
                   ("history", "Measure over", "int", 5, 120, 30, "s"),
                   ("max_boost", "Max boost", "float", 0, 24, 12, "dB"),
                   ("max_cut", "Max cut", "float", 0, 24, 12, "dB")],
    },
    "compressor": {
        "name": "Compressor", "backend": "swh",
        "blurb": "Narrows the gap between loud and soft syllables. Moderate settings only.",
        "params": [("threshold", "Threshold", "float", -30, 0, -20, "dB"),
                   ("ratio", "Ratio", "float", 1, 20, 3, ":1"),
                   ("attack", "Attack", "float", 1.5, 400, 10, "ms"),
                   ("release", "Release", "float", 2, 800, 150, "ms"),
                   ("makeup", "Makeup", "float", 0, 24, 0, "dB")],
    },
    "limiter": {
        "name": "Limiter", "backend": "swh",
        "blurb": "A ceiling after the gain stages so nothing clips. Belongs last.",
        "params": [("limit", "Ceiling", "float", -20, 0, -3, "dB"),
                   ("release", "Release", "float", 0.01, 2, 0.1, "s")],
    },
    "rnnoise": {
        "name": "Noise reduction (RNNoise)", "backend": "rnnoise",
        "blurb": "Light neural denoiser for steady background noise. Runs fine on a laptop CPU.",
        "params": [("vad", "Voice activity gate", "float", 0, 99, 0, "%")],
    },
    "deepfilternet": {
        "name": "Noise reduction (DeepFilterNet)", "backend": "deepfilternet",
        "blurb": "Strong neural denoiser for fans, traffic, keyboards, children. Heavy on CPU: fine on a "
                 "desktop, use RNNoise on a laptop. Cap the attenuation: full strength smears consonants.",
        "params": [("attenuation", "Attenuation limit", "float", 0, 100, 30, "dB")],
    },
}

# What the Effects list offers: (label, kind, starting settings, blurb).
LIBRARY = [(spec["name"], kind, {}, spec["blurb"]) for kind, spec in EFFECTS.items()]
LIBRARY.insert(next(i for i, item in enumerate(LIBRARY) if item[1] == "rnnoise") + 1,
               ("⚠ Voice activity gate (VAD)", "rnnoise", {"vad": 50.0},
                "RNNoise with its voice-activity gate on: it mutes anything under the threshold. " + VAD_CAUTION))

INPUT_MIX = ["Mix all channels", "Left channel only", "Right channel only"]

TEMPLATES = [
    ("Dictation standard", "High-pass, DeepFilterNet, auto gain, limiter. Best on a desktop CPU.",
     [("highpass", {}), ("deepfilternet", {}), ("autogain", {}), ("limiter", {})]),
    ("Dictation standard, low CPU", "The same chain on RNNoise: for laptops.",
     [("highpass", {}), ("rnnoise", {}), ("autogain", {}), ("limiter", {})]),
    ("Noisy room", "Adds a gentle gate and stronger DeepFilterNet. Desktop CPU.",
     [("highpass", {"frequency": 100.0}), ("gate", {}), ("deepfilternet", {"attenuation": 50.0}),
      ("autogain", {}), ("limiter", {})]),
    ("Noisy room, low CPU", "Gate and RNNoise instead of DeepFilterNet: for laptops.",
     [("highpass", {"frequency": 100.0}), ("gate", {}), ("rnnoise", {}), ("autogain", {}), ("limiter", {})]),
    ("Simple clean-up", "High-pass, low-pass and auto gain. Built into PipeWire, nothing to download.",
     [("highpass", {}), ("lowpass", {}), ("autogain", {}), ("limiter", {})]),
    ("Level only", "Auto gain and a limiter.",
     [("autogain", {}), ("limiter", {})]),
    ("None", "No effects: ASR Mic carries the mic as it is, folded to mono.", []),
]


def defaults(kind):
    return {key: default for key, _l, _k, _lo, _hi, default, _u in EFFECTS[kind]["params"]}


def new_entry(kind, overrides=None):
    settings = defaults(kind)
    settings.update(overrides or {})
    return {"kind": kind, "bypass": False, "settings": settings}


def from_template(name):
    for tname, _blurb, steps in TEMPLATES:
        if tname == name:
            return [new_entry(kind, overrides) for kind, overrides in steps]
    return []


def has_vad(entry):
    return entry["kind"] == "rnnoise" and float(entry["settings"].get("vad", 0)) > 0


def describe(entry):
    kind, s = entry["kind"], entry["settings"]
    spec = EFFECTS.get(kind)
    if spec is None:
        return f"{kind} (unknown)"
    text = {
        "highpass": lambda: f"High-pass {s['frequency']:g} Hz",
        "lowpass": lambda: f"Low-pass {s['frequency']:g} Hz",
        "notch": lambda: f"Hum notch {s['frequency']} Hz ×{s['harmonics']}",
        "eq": lambda: f"EQ {s['gain']:+g} dB at {s['frequency']:g} Hz",
        "gate": lambda: f"Gate {s['threshold']:g} dBFS, {s['reduction']:g} dB",
        "autogain": lambda: f"Auto gain {s['target']:g} LUFS / {s['history']} s",
        "compressor": lambda: f"Compressor {s['threshold']:g} dB {s['ratio']:g}:1",
        "limiter": lambda: f"Limiter {s['limit']:g} dB",
        "rnnoise": lambda: "RNNoise",
        "deepfilternet": lambda: f"DeepFilterNet {s['attenuation']:g} dB",
    }[kind]()
    if has_vad(entry):
        text += f" + VAD {float(s['vad']):g}% ⚠"
    return text


# ---------------------------------------------------------------------------
# saved chains and bindings
# ---------------------------------------------------------------------------

def chain_names():
    return sorted(p.stem for p in CHAINS_DIR.glob("*.json")) if CHAINS_DIR.exists() else []


def load_chain(name):
    data = json.loads((CHAINS_DIR / f"{name}.json").read_text())
    return data.get("effects", []), data.get("input_mix", INPUT_MIX[0])


def save_chain(name, effects, input_mix=INPUT_MIX[0]):
    if not name or "/" in name:
        raise ValueError("a chain name cannot be empty or contain '/'")
    CHAINS_DIR.mkdir(parents=True, exist_ok=True)
    path = CHAINS_DIR / f"{name}.json"
    path.write_text(json.dumps({"input_mix": input_mix, "effects": effects}, indent=2) + "\n")
    return path


def delete_chain(name):
    (CHAINS_DIR / f"{name}.json").unlink(missing_ok=True)


def binding_key(device_name, port):
    return f"{device_name}:{port or ''}"


# ---------------------------------------------------------------------------
# the filter graph
# ---------------------------------------------------------------------------

def _db(x):
    return 10 ** (x / 20.0)


def build_graph(effects, channels, input_mix=INPUT_MIX[0]):
    """PipeWire filter.graph for a chain, mono out. Returns (graph, skipped)
    where skipped lists effects left out because their backend is missing."""
    nodes, links, skipped = [], [], []
    channels = max(1, min(8, int(channels)))
    if input_mix == INPUT_MIX[1] or channels == 1:
        gains = [1.0] + [0.0] * (channels - 1)
    elif input_mix == INPUT_MIX[2] and channels >= 2:
        gains = [0.0, 1.0] + [0.0] * (channels - 2)
    else:
        gains = [1.0 / channels] * channels
    nodes.append({"type": "builtin", "name": "in", "label": "mixer",
                  "control": {f"Gain {i + 1}": g for i, g in enumerate(gains)}})
    tail = "in:Out"
    for i, entry in enumerate(effects):
        kind, s = entry["kind"], entry.get("settings", {})
        if entry.get("bypass") or kind not in EFFECTS:
            continue
        ready, _missing = backend_ready(EFFECTS[kind]["backend"])
        if not ready:
            skipped.append(kind)
            continue
        n = f"e{i}"

        def add(node, inp, out):
            nonlocal tail
            nodes.append(node)
            links.append({"output": tail, "input": f"{node['name']}:{inp}"})
            tail = f"{node['name']}:{out}"

        if kind in ("highpass", "lowpass"):
            label = "bq_highpass" if kind == "highpass" else "bq_lowpass"
            stages = 2 if s.get("steep", True) else 1
            for k in range(stages):
                # Two Butterworth-ish stages: Q 0.54 and 1.31 make a 4th-order Butterworth.
                q = (0.5412, 1.3066)[k] if stages == 2 else 0.7071
                add({"type": "builtin", "name": f"{n}_{k}", "label": label,
                     "control": {"Freq": float(s["frequency"]), "Q": q}}, "In", "Out")
        elif kind == "notch":
            base = float(s.get("frequency", 50))
            for k in range(int(s.get("harmonics", 3))):
                add({"type": "builtin", "name": f"{n}_{k}", "label": "bq_notch",
                     "control": {"Freq": base * (k + 1), "Q": 30.0}}, "In", "Out")
        elif kind == "eq":
            add({"type": "builtin", "name": n, "label": "bq_peaking",
                 "control": {"Freq": float(s["frequency"]), "Q": float(s["q"]), "Gain": float(s["gain"])}},
                "In", "Out")
        elif kind == "gate":
            # swh `gate`, keyed on the speech band so rumble and hiss don't
            # open it; Range is the reduction, so it lowers rather than mutes.
            # (PipeWire 1.6.2's builtin noisegate never opens: its "Level"
            # default is NaN, which the control setup clamps to 0.)
            add({"type": "ladspa", "name": n, "plugin": "gate_1410", "label": "gate",
                 "control": {"LF key filter (Hz)": 100.0, "HF key filter (Hz)": 6000.0,
                             "Threshold (dB)": float(s["threshold"]), "Attack (ms)": float(s["attack"]),
                             "Hold (ms)": max(2.0, float(s["hold"])), "Decay (ms)": float(s["release"]),
                             "Range (dB)": float(s["reduction"]), "Output select (-1 = key listen, 0 = gate, 1 = bypass)": 0.0}},
                "Input", "Output")
        elif kind == "autogain":
            # Loudness over the history window (gated, so silence doesn't count)
            # → gain to reach the target → bounded → applied.
            nodes += [
                {"type": "ebur128", "name": f"{n}_m", "label": "ebur128",
                 "config": {"max-history": float(s["history"])}},
                {"type": "ebur128", "name": f"{n}_c", "label": "lufs2gain",
                 "control": {"Target LUFS": float(s["target"])}},
                {"type": "builtin", "name": f"{n}_b", "label": "clamp",
                 "control": {"Min": _db(-float(s["max_cut"])), "Max": _db(float(s["max_boost"]))}},
                {"type": "builtin", "name": f"{n}_g", "label": "linear", "control": {"Mult": 1.0}},
            ]
            links += [{"output": tail, "input": f"{n}_m:In FC"},
                      {"output": f"{n}_m:Global LUFS", "input": f"{n}_c:LUFS"},
                      {"output": f"{n}_c:Gain", "input": f"{n}_b:Control"},
                      {"output": f"{n}_b:Notify", "input": f"{n}_g:Mult"},
                      {"output": f"{n}_m:Out FC", "input": f"{n}_g:In"}]
            tail = f"{n}_g:Out"
        elif kind == "compressor":
            add({"type": "ladspa", "name": n, "plugin": "sc4m_1916", "label": "sc4m",
                 "control": {"RMS/peak": 0.0, "Attack time (ms)": float(s["attack"]),
                             "Release time (ms)": float(s["release"]), "Threshold level (dB)": float(s["threshold"]),
                             "Ratio (1:n)": float(s["ratio"]), "Knee radius (dB)": 3.0,
                             "Makeup gain (dB)": float(s["makeup"])}}, "Input", "Output")
        elif kind == "limiter":
            add({"type": "ladspa", "name": n, "plugin": "fast_lookahead_limiter_1913", "label": "fastLookaheadLimiter",
                 "control": {"Input gain (dB)": 0.0, "Limit (dB)": float(s["limit"]),
                             "Release time (s)": float(s["release"])}}, "Input 1", "Output 1")
        elif kind == "rnnoise":
            add({"type": "ladspa", "name": n, "plugin": str(plugin_path("rnnoise")), "label": "noise_suppressor_mono",
                 "control": {"VAD Threshold (%)": float(s.get("vad", 0)), "VAD Grace Period (ms)": 200.0,
                             "Retroactive VAD Grace (ms)": 0.0}}, "Input", "Output")
        elif kind == "deepfilternet":
            add({"type": "ladspa", "name": n, "plugin": str(plugin_path("deepfilternet")), "label": "deep_filter_mono",
                 "control": {"Attenuation Limit (dB)": float(s["attenuation"])}}, "Audio In", "Audio Out")
    graph = {"nodes": nodes, "links": links,
             "inputs": [f"in:In {i + 1}" for i in range(channels)], "outputs": [tail]}
    return graph, skipped


def config_text(device, effects, input_mix=INPUT_MIX[0]):
    """A complete config for `pipewire -c`: one filter-chain module."""
    channels = max(1, min(8, device.channels))
    names = {"mono": "MONO", "front-left": "FL", "front-right": "FR", "front-center": "FC",
             "rear-left": "RL", "rear-right": "RR", "lfe": "LFE", "side-left": "SL", "side-right": "SR"}
    if len(device.channel_map) == channels:
        positions = [names.get(c, f"AUX{i}") for i, c in enumerate(device.channel_map)]
    else:
        positions = ["MONO"] if channels == 1 else (["FL", "FR"] + [f"AUX{i}" for i in range(2, channels)])[:channels]
    graph, skipped = build_graph(effects, channels, input_mix)
    module = {
        "name": "libpipewire-module-filter-chain",
        "args": {
            "node.description": "ASR Mic",
            "media.name": "ASR Mic",
            "filter.graph": graph,
            "capture.props": {
                "node.name": CAPTURE_NODE,
                "target.object": device.name,
                "node.passive": True,
                "node.dont-fallback": True,
                "audio.rate": RATE,
                "audio.channels": channels,
                "audio.position": positions,
            },
            "playback.props": {
                "node.name": ASR_MIC_NODE,
                "node.description": "ASR Mic",
                "media.class": "Audio/Source",
                "audio.rate": RATE,
                "audio.channels": 1,
                "audio.position": ["MONO"],
            },
        },
    }
    text = "\n".join([
        "# Generated by ASR Mic Tuner; rewritten whenever the chain or microphone changes.",
        'context.properties = { log.level = 0 }',
        "context.spa-libs = { audio.convert.* = audioconvert/libspa-audioconvert support.* = support/libspa-support }",
        "context.modules = [",
        '  { name = libpipewire-module-rt args = { } flags = [ ifexists nofail ] }',
        "  { name = libpipewire-module-protocol-native }",
        "  { name = libpipewire-module-client-node }",
        "  { name = libpipewire-module-adapter }",
        "  " + json.dumps(module),
        "]",
        "",
    ])
    return text, skipped


# ---------------------------------------------------------------------------
# a chain fitted to one speaker on one microphone
# ---------------------------------------------------------------------------

def personal_chain(v, room=None):
    """From a mic test's metrics (analysis.analyse_voice) and optionally a
    room profile's. All built into PipeWire, no neural denoiser: it trims what
    lies outside this voice and holds the level, which is what ASR benefits
    from most reliably. Returns (effects, notes), one note per decision."""
    chain, notes = [], []
    f0_low = v.get("f0_p10_hz")
    if f0_low:
        hp = int(max(50, min(150, 5 * round(f0_low * 0.7 / 5))))
        notes.append(f"High-pass {hp} Hz: 30% under the lowest pitch you used ({f0_low:.0f} Hz), "
                     "so nothing of your voice is cut, but rumble and desk thumps are.")
    else:
        hp = 80
        notes.append("High-pass 80 Hz: no reliable pitch in this sample, so the safe default.")
    if (v.get("low_vs_body_db") or -99) > -6:
        notes.append("Your sample had strong low-frequency energy (rumble or proximity), which the high-pass removes.")
    chain.append(new_entry("highpass", {"frequency": float(hp)}))

    bw = v.get("bandwidth_hz")
    if bw and bw >= 8500:
        chain.append(new_entry("lowpass", {"frequency": 8000.0}))
        notes.append("Low-pass 8 kHz: most recognisers work at 16 kHz and discard everything above 8 kHz; "
                     "cutting it here drops hiss before the auto gain can react to it.")
    elif bw:
        notes.append(f"No low-pass: this mic already stops near {bw / 1000:.1f} kHz.")

    hum = (room or {}).get("hum") or {}
    if hum.get("present") and hum.get("mains_hz") in (50.0, 60.0):
        mains = str(int(hum["mains_hz"]))
        chain.append(new_entry("notch", {"frequency": mains, "harmonics": 3}))
        notes.append(f"Hum notch at {mains} Hz and two harmonics: your room profile found mains hum.")

    snr = v.get("snr_db")
    noise = (room or {}).get("noise_floor_p10_dbfs", v.get("noise_reference_dbfs"))
    speech = v.get("speech_rms_dbfs")
    if snr is not None and noise is not None and speech is not None and snr >= 20:
        threshold = round(min(noise + 12, speech - 6))
        chain.append(new_entry("gate", {"threshold": float(threshold), "reduction": -12.0}))
        notes.append(f"Gentle gate opening above {threshold} dBFS (room {noise:.0f}, your speech {speech:.0f}): "
                     "turns the room down 12 dB between phrases, never to silence.")
    elif snr is not None:
        notes.append(f"No gate: signal-to-noise is only {snr:.0f} dB, and a gate would chop quiet words.")

    chain.append(new_entry("autogain"))
    notes.append("Auto gain to −20 LUFS, measured over 30 s of gated loudness so pauses don't count; "
                 "at most ±12 dB.")
    chain.append(new_entry("limiter"))
    notes.append("Limiter at −3 dB so nothing after the gain clips.")
    notes.append("No neural denoiser: add RNNoise or DeepFilterNet under Customise if the room needs it.")
    return chain, notes
