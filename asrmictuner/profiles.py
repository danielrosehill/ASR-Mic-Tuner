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

FIELDS = ("identity", "remembered", "presets", "active_profile", "chain_bindings", "machines", "format")
PER_MACHINE = ("remembered", "presets", "active_profile")
FORMAT = 2      # v2.9: remembered / presets / active_profile are {machine id: ...}


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
    if not any(data.get(f) for f in FIELDS if f not in ("identity", "machines", "format")):
        # Nothing remembered about this mic any more.
        path.unlink(missing_ok=True)
        return True
    data["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)
    return True


# -- per-computer buckets (v2.9) --

def machine_buckets(key, prof):
    """{"remembered": {mid: ...}, "presets": {mid: ...}, "active_profile": {mid: ...}}
    for a profile file. A pre-v2.9 (format 1) file has one flat set: it belongs
    to this computer, unless it is another computer's built-in mic (None)."""
    from .identity import is_this_machines_internal, this_machine
    if int(prof.get("format", 1)) >= FORMAT:
        return {f: dict(prof.get(f) or {}) for f in PER_MACHINE}
    if key.startswith("internal:") and not is_this_machines_internal(key):
        return None
    me = this_machine()["id"]
    return {f: ({me: prof[f]} if prof.get(f) else {}) for f in PER_MACHINE}


def save_machine_view(key, fields):
    """Write this computer's view (`identity`, `remembered`, `presets`,
    `active_profile`) into the file of `key`, keeping other computers' buckets."""
    from .identity import this_machine
    path = profiles_dir() / f"{filename(key)}.json"
    try:
        prof = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        prof = {"mic_key": key}
    buckets = machine_buckets(key, prof) or {f: {} for f in PER_MACHINE}
    me = this_machine()
    for f in PER_MACHINE:
        if fields.get(f):
            buckets[f][me["id"]] = fields[f]
        else:
            buckets[f].pop(me["id"], None)
    machines = dict(prof.get("machines") or {})
    if any(buckets[f].get(me["id"]) for f in PER_MACHINE):
        machines[me["id"]] = {k: v for k, v in me.items() if k != "id"}
    return update(key, identity=fields.get("identity") or prof.get("identity"), machines=machines,
                  format=FORMAT, **{f: buckets[f] for f in PER_MACHINE})


# -- legacy (v2.6 and earlier: keyed by PipeWire node name) --

KEY_PREFIXES = ("usb:", "bt:", "internal:", "node:")


def is_mic_key(text):
    return str(text).startswith(KEY_PREFIXES)


def name_to_key(devices):
    """{node name: mic_key} for the devices present now."""
    return {d.name: d.key for d in devices}
