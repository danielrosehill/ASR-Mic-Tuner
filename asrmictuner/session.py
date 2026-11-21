"""Shared state between the main window and the tray: the selected device,
its gain control and curve, saved presets, and one reference-counted
capture stream.

The capture stream (`parec`) is only open while something on screen needs
it — the main window, the tray pop-up, or the opt-in live tray meter —
because an open capture keeps the source running and lights Plasma's
microphone-in-use indicator.
"""

from datetime import datetime, timezone

from PyQt6.QtCore import QObject, pyqtSignal

from .capture import Analyser
from .core import (
    Curve, _adopt_legacy, curve_cache_key, default_source, list_devices, load_config, note_identity,
    pick_boost_controls, pick_gain_control, probe_curve, read_controls,
    resolve_hardware_for, save_config, set_source_percent,
)
from .store import Store, _adopt_legacy_settings, load_settings, save_settings


class Session(QObject):
    device_changed = pyqtSignal()
    gain_changed = pyqtSignal()          # index moved (by us or behind our back)
    presets_changed = pyqtSignal()
    history_changed = pyqtSignal()
    profiles_changed = pyqtSignal()
    settings_changed = pyqtSignal()
    status = pyqtSignal(str)
    open_chain = pyqtSignal(object, str, object)   # chain, preset name, notes: open in ASR Effects

    def __init__(self):
        super().__init__()
        self.config = load_config()
        self.settings = load_settings()
        self.store = Store()
        self.devices = []
        self.device = None
        self.controls = []
        self.gain = None
        self.curve = None
        self.boosts = []
        self.analyser = None
        self._users = set()

    # -- devices --
    def rescan(self):
        self.devices = list_devices()
        known = getattr(self, "_known_keys", set())
        if {d.key for d in self.devices} - known:
            # A mic seen for the first time this run: give its older history
            # rows and any legacy (node-name keyed) settings its key.
            self._known_keys = known | {d.key for d in self.devices}
            self.store.backfill_keys(self.devices)
            if self.config.get("legacy"):
                _adopt_legacy(self.config, self.devices)
            if self.settings.get("legacy"):
                _adopt_legacy_settings(self.settings, self.devices)
        return self.devices

    def preferred_device(self):
        """The hardware behind the system default source, else the first
        hardware device, else anything."""
        default = default_source()
        by_name = {d.name: d for d in self.devices}
        dev = by_name.get(default)
        if dev and not dev.is_hardware:
            hw = resolve_hardware_for(dev.name)
            if hw and hw in by_name:
                return by_name[hw]
        if dev:
            return dev
        return next((d for d in self.devices if d.is_hardware),
                    self.devices[0] if self.devices else None)

    def select_device(self, device):
        users = set(self._users)
        self._stop_analyser()
        self.device = device
        self._load_mixer()
        if users:
            self._start_analyser()
        self.device_changed.emit()

    def _load_mixer(self):
        self.controls, self.gain, self.curve, self.boosts = [], None, None, []
        if not self.device or not self.device.is_hardware:
            return
        self.controls = read_controls(self.device.card)
        self.gain = pick_gain_control(self.controls)
        self.boosts = pick_boost_controls(self.controls)
        if self.gain is None:
            return
        self.curve = self.gain.curve
        if self.curve is not None:
            return
        key = curve_cache_key(self.device, self.gain)
        cached = self.config["curves"].get(key)
        if cached:
            self.curve = Curve.from_json(cached)
        if self.curve is None:
            self.status.emit("Probing the gain curve (device declares none)…")
            self.curve = probe_curve(self.gain)
            if self.curve is not None:
                self.config["curves"][key] = self.curve.to_json()
                save_config(self.config)

    @property
    def soft_only(self):
        """No analog control: PipeWire's software volume is the only knob."""
        return self.device is not None and self.gain is None

    # -- capture, reference counted --
    def acquire(self, who):
        self._users.add(who)
        if self.analyser is None or not self.analyser.alive:
            self._start_analyser()

    def release(self, who):
        self._users.discard(who)
        if not self._users:
            self._stop_analyser()

    def ensure_capture(self):
        """Restart a capture that died (device unplugged and back)."""
        if self._users and self.device and (self.analyser is None or not self.analyser.alive):
            self._stop_analyser()
            self._start_analyser()

    def _start_analyser(self):
        if not self.device:
            return
        self.analyser = Analyser(self.device.name, self.device.channels, self.device.rate)
        self.analyser.start()

    def _stop_analyser(self):
        if self.analyser:
            self.analyser.stop()
            self.analyser = None

    # -- gain --
    def gain_db(self, index=None):
        if self.gain is None or self.curve is None:
            return None
        return self.curve.db(self.gain.index if index is None else index)

    def refresh_controls(self):
        """Re-read the live mixer values; True if the gain index moved."""
        if not (self.device and self.device.is_hardware and self.gain is not None):
            self._refresh_soft()
            return False
        fresh = {c.numid: c for c in read_controls(self.device.card)}
        before = self.gain.index
        for control in [self.gain] + self.boosts:
            updated = fresh.get(control.numid)
            if updated is not None:
                control.values = updated.values
        self._refresh_soft()
        if self.gain.index != before:
            self.gain_changed.emit()
            return True
        return False

    def _refresh_soft(self):
        if not self.device:
            return
        for d in list_devices():
            if d.name == self.device.name:
                self.device.pw_percent = d.pw_percent
                self.device.pw_db = d.pw_db

    def set_index(self, index, deliberate=True):
        if self.gain is None:
            return False
        if not self.gain.set_index(index):
            self.status.emit("amixer refused the gain change")
            return False
        # A change made in ASR Mic Tuner is deliberate, so it becomes the
        # remembered value; drift detection is for changes behind our back.
        if deliberate and self.remembered() is not None:
            self.remember_current()
        self.gain_changed.emit()
        return True

    def step(self, delta):
        if self.gain is not None:
            return self.set_index(self.gain.index + delta)
        if self.device and self.device.pw_percent is not None:
            return self.set_soft(self.device.pw_percent + delta)
        return False

    def set_soft(self, percent):
        if not self.device:
            return False
        if set_source_percent(self.device.name, percent):
            self.device.pw_percent = max(0, min(150, int(percent)))
            self.gain_changed.emit()
            return True
        return False

    # -- remembered gain (what --restore and the login unit reapply) --
    def remembered(self):
        return self.config["devices"].get(self.device.key) if self.device else None

    def remember_current(self):
        if not self.device or self.gain is None:
            return
        note_identity(self.config, self.device)
        self.config["devices"][self.device.key] = {
            "card_name": self.device.card_name,
            "control": self.gain.name,
            "numid": self.gain.numid,
            "index": self.gain.index,
            "db": self.gain_db(),
            "saved": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        save_config(self.config)
        self.presets_changed.emit()

    def forget(self):
        if self.device:
            self.config["devices"].pop(self.device.key, None)
            save_config(self.config)
            self.presets_changed.emit()

    def drift(self):
        """(remembered index, current index) if they differ, else None."""
        saved = self.remembered()
        if not saved or self.gain is None or saved.get("index") == self.gain.index:
            return None
        return saved["index"], self.gain.index

    def restore_remembered(self):
        saved = self.remembered()
        if saved and self.gain is not None:
            return self.set_index(saved["index"], deliberate=False)
        return False

    # -- named presets --
    def presets(self, device=None):
        key = device or (self.device.key if self.device else None)
        return self.config["presets"].get(key, {}) if key else {}

    def save_preset(self, label, notes=""):
        if not self.device:
            return
        entry = {"notes": notes, "created": datetime.now().astimezone().isoformat(timespec="seconds")}
        if self.gain is not None:
            entry.update(index=self.gain.index, db=self.gain_db(), control=self.gain.name)
        else:
            entry.update(soft_percent=self.device.pw_percent)
        note_identity(self.config, self.device)
        self.config["presets"].setdefault(self.device.key, {})[label] = entry
        save_config(self.config)
        self.presets_changed.emit()

    def apply_preset(self, label):
        entry = self.presets().get(label)
        if not entry:
            return False
        ok = False
        if "index" in entry and self.gain is not None:
            ok = self.set_index(entry["index"])
        elif "soft_percent" in entry and entry["soft_percent"] is not None:
            ok = self.set_soft(entry["soft_percent"])
        # A profile (v2.6) also carries the noise reduction mode it was tuned with.
        nr = entry.get("noise_reduction")
        if nr and nr != self.settings.get("noise_reduction"):
            self.settings["noise_reduction"] = nr
            self.save_settings()
        if ok or nr:
            self._set_active_profile(label)
        return ok

    # -- profiles (v2.6): a preset per mic, port and place, made by quick tune --
    def _device_key(self, device=None):
        d = device or self.device
        return (d.key, d.port) if d else None

    def _set_active_profile(self, label):
        if self.device:
            note_identity(self.config, self.device)
            self.config.setdefault("active_profile", {}).setdefault(self.device.key, {})[self.device.port] = label
            save_config(self.config)

    def active_profile(self):
        d = self.device
        label = self.config.get("active_profile", {}).get(d.key, {}).get(d.port) if d else None
        return label if label in self.presets() else None

    def save_profile(self, label, tuned=None):
        """Save the current gain, port and noise reduction as profile `label`,
        make it the remembered gain and the active profile for this mic."""
        self.save_preset(label, notes="Quick tune" if tuned else "")
        entry = self.config["presets"][self.device.key][label]
        entry["port"] = self.device.port
        entry["noise_reduction"] = self.settings.get("noise_reduction", "off")
        if tuned:
            entry["tuned"] = dict(tuned, at=datetime.now().astimezone().isoformat(timespec="seconds"))
        save_config(self.config)
        if self.gain is not None:
            self.remember_current()
        self._set_active_profile(label)
        self.presets_changed.emit()

    def follow_default(self):
        """Switch to the hardware behind the default input if it changed (a USB
        mic plugged in, a headset picked in Plasma), and apply that mic's active
        profile. Returns the status text, or None if nothing changed."""
        # Acts only when the default itself changes, so a mic picked by hand in
        # the window stays picked until the system default moves.
        self.rescan()
        dev = self.preferred_device()
        key = self._device_key(dev) if dev else None
        first = not hasattr(self, "_last_default")
        changed = not first and key != self._last_default
        self._last_default = key
        if dev is None or not changed or (self.device and self._device_key() == key):
            return None
        self.select_device(dev)
        label = self.active_profile()
        if label is None:
            # The newest tuned profile for this port, if any.
            tuned = [(e.get("tuned", {}).get("at", ""), l) for l, e in self.presets().items()
                     if e.get("tuned") and e.get("port", dev.port) == dev.port]
            label = max(tuned)[1] if tuned else None
        if label and self.apply_preset(label):
            text = f"{dev.description}: profile “{label}” applied."
        elif self.restore_remembered():
            text = f"{dev.description}: remembered gain applied."
        else:
            text = f"{dev.description}: no profile yet. Run Quick tune to make one."
        self.status.emit(text)
        return text

    def make_preset_default(self, label):
        """Apply a preset and make it the remembered gain."""
        if self.apply_preset(label):
            self.remember_current()
            return True
        return False

    def delete_preset(self, label):
        presets = self.config["presets"].get(self.device.key, {}) if self.device else {}
        if label in presets:
            del presets[label]
            save_config(self.config)
            self.presets_changed.emit()

    def default_preset_label(self):
        saved = self.remembered()
        if not saved:
            return None
        for label, entry in self.presets().items():
            if entry.get("index") == saved.get("index"):
                return label
        return None

    def reload_profiles(self):
        """Re-read the per-mic profile files (after a sync brought changes in)."""
        self.config = load_config()
        fresh = load_settings()
        self.settings["chain_bindings"] = fresh["chain_bindings"]
        self.settings["_bindings_snapshot"] = fresh["_bindings_snapshot"]
        self.presets_changed.emit()
        self.settings_changed.emit()

    # -- settings --
    def save_settings(self):
        save_settings(self.settings)
        self.settings_changed.emit()

    def location(self):
        return self.settings.get("location") or ""

    def set_location(self, text):
        text = text.strip()
        self.settings["location"] = text
        if text and text not in self.settings["locations"]:
            self.settings["locations"].insert(0, text)
            self.settings["locations"] = self.settings["locations"][:30]
        save_settings(self.settings)

    def active_room_profile(self):
        if not self.device:
            return None
        pid = self.settings.get("active_room_profile", {}).get(self.device.key)
        return self.store.profile(pid) if pid else None

    def set_active_room_profile(self, pid):
        if not self.device:
            return
        self.settings.setdefault("active_room_profile", {})
        if pid is None:
            self.settings["active_room_profile"].pop(self.device.key, None)
        else:
            self.settings["active_room_profile"][self.device.key] = pid
        self.save_settings()
        self.profiles_changed.emit()

    # -- history --
    def device_fields(self):
        if not self.device:
            return {}
        return {
            "device": self.device.name,
            "mic_key": self.device.key,
            "device_label": self.device.description,
            "gain_control": self.gain.name if self.gain else None,
            "gain_index": self.gain.index if self.gain else None,
            "gain_db": self.gain_db(),
            "soft_percent": self.device.pw_percent,
            "location": self.location() or None,
        }

    def log(self, kind, metrics, **extra):
        fields = self.device_fields()
        fields.update(kind=kind, metrics=metrics, **extra)
        mid = self.store.add_measurement(**fields)
        self.history_changed.emit()
        return mid

    def ai_context(self, measurement):
        """Facts for the AI review: device, curve, the measurement, the
        active room profile, and a few recent readings on the same device."""
        context = {
            "device": {
                "name": measurement.get("device"),
                "label": measurement.get("device_label"),
                "gain_control": measurement.get("gain_control"),
                "gain_index": measurement.get("gain_index"),
                "gain_db": measurement.get("gain_db"),
                "software_volume_percent": measurement.get("soft_percent"),
            },
            "location": measurement.get("location"),
            "notes": measurement.get("notes"),
            "measurement_kind": measurement.get("kind"),
            "measurement": measurement.get("metrics"),
        }
        if self.device and self.device.name == measurement.get("device") and self.curve:
            context["device"]["gain_curve"] = self.curve.describe()
            context["device"]["gain_index_range"] = [self.curve.imin, self.curve.imax]
        room = self.active_room_profile()
        if room and measurement.get("kind") != "room":
            context["room_profile"] = room.get("metrics")
            context["room_profile_name"] = room.get("name")
        recent = []
        for m in self.store.measurements(device=measurement.get("device"), mic_key=measurement.get("mic_key"),
                                         limit=8):
            if m["id"] == measurement.get("id"):
                continue
            metrics = m.get("metrics") or {}
            recent.append({
                "created": m["created"], "kind": m["kind"], "gain_db": m["gain_db"],
                "location": m["location"],
                "noise_floor": metrics.get("noise_floor_p10_dbfs") or metrics.get("noise_dbfs"),
                "speech_peak": metrics.get("speech_peak_p95_dbfs") or metrics.get("speech_peak_dbfs"),
                "score": (metrics.get("readiness") or {}).get("score"),
            })
        context["recent_history"] = recent[:6]
        return context
