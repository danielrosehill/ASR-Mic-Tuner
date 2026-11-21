"""Per-microphone profile files (v2.7): the portable record of one mic.

    ~/.config/asr-mic-tuner/profiles/<identity.filename(key)>.json

    {"mic_key": "usb:0d8c:0014:SN1234",
     "identity": {...identity.identity()...},
     "remembered": {"control": ..., "numid": ..., "index": ..., "db": ..., "saved": ...},
     "presets": {"<place>": {"index": ..., "db": ..., "port": ..., "noise_reduction": ..., "tuned": {...}}},
     "active_profile": {"<port description>": "<place>"},
     "chain_bindings": {"<port description>": "<chain name>"},
     "updated": "<ISO time>"}

One file per mic, so syncing the folder (planning/sprints.md, sprint 2) never
has to merge two mics. Machine-local state stays out of it: the gain-curve
probe cache (devices.json), room profiles and measurements (history.db), and
app settings (settings.json).

core.load_config / save_config and store.load_settings / save_settings read
and write these; the rest of the app sees the usual dicts, keyed by mic_key.
"""

import json
from datetime import datetime, timezone

from .identity import filename

FIELDS = ("identity", "remembered", "presets", "active_profile", "chain_bindings")


def profiles_dir():
    from .core import CONFIG_DIR
    return CONFIG_DIR / "profiles"


def read_all():
    out = {}
    d = profiles_dir()
    if not d.is_dir():
        return out
    for path in sorted(d.glob("*.json")):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        key = data.get("mic_key")
        if key:
            out[key] = data
    return out


def update(key, **fields):
    """Merge `fields` into the profile of `key`; write only if something changed.
    A field set to None or {} is removed. Returns True if the file was written."""
    path = profiles_dir() / f"{filename(key)}.json"
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        data = {"mic_key": key}
    before = json.dumps(data, sort_keys=True)
    for name, value in fields.items():
        if name not in FIELDS:
            raise KeyError(name)
        if value in (None, {}):
            data.pop(name, None)
        else:
            data[name] = value
    if json.dumps(data, sort_keys=True) == before:
        return False
    if not any(data.get(f) for f in FIELDS if f != "identity"):
        # Nothing remembered about this mic any more.
        path.unlink(missing_ok=True)
        return True
    data["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)
    return True


# -- legacy (v2.6 and earlier: keyed by PipeWire node name) --

KEY_PREFIXES = ("usb:", "bt:", "internal:", "node:")


def is_mic_key(text):
    return str(text).startswith(KEY_PREFIXES)


def name_to_key(devices):
    """{node name: mic_key} for the devices present now."""
    return {d.name: d.key for d in devices}
