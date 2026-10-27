"""Command line: the GUI by default, plus headless modes for scripts and agents."""

import argparse
import json
import sys
import time

from . import APP, VERSION


def _devices_json():
    from .core import (
        Curve, curve_cache_key, default_source, list_devices, load_config,
        pick_boost_controls, pick_gain_control, read_controls, resolve_hardware_for,
    )
    config = load_config()
    default = default_source()
    out = []
    for d in list_devices():
        entry = {
            "name": d.name, "description": d.description, "hardware": d.is_hardware,
            "card": d.card, "channels": d.channels, "rate": d.rate,
            "system_default": d.name == default,
            "pipewire_volume_percent": d.pw_percent, "pipewire_volume_db": d.pw_db,
        }
        if not d.is_hardware:
            entry["fed_by"] = resolve_hardware_for(d.name)
        else:
            controls = read_controls(d.card)
            gain = pick_gain_control(controls)
            if gain:
                curve = gain.curve or Curve.from_json(config["curves"].get(curve_cache_key(d, gain), {}))
                entry["gain"] = {
                    "control": gain.name, "numid": gain.numid, "index": gain.index,
                    "min": gain.imin, "max": gain.imax,
                    "db": curve.db(gain.index) if curve else None,
                    "curve": curve.describe() if curve else None,
                }
            entry["boosts"] = [{"control": b.name, "index": b.index, "db": b.db}
                               for b in pick_boost_controls(controls)]
        entry["remembered"] = config["devices"].get(d.name)
        entry["presets"] = config["presets"].get(d.name, {})
        out.append(entry)
    return out


def _measure(kind, seconds, device_name, location, notes):
    """Headless room/voice measurement, logged to history like the GUI does."""
    from . import analysis, store
    from .capture import Analyser
    from .core import list_devices, pick_gain_control, read_controls

    devices = list_devices()
    dev = next((d for d in devices if d.name == device_name), None) if device_name else None
    if dev is None:
        dev = next((d for d in devices if d.is_hardware), devices[0] if devices else None)
    if dev is None:
        print("no capture device", file=sys.stderr)
        return 1
    analyser = Analyser(dev.name, dev.channels, dev.rate)
    analyser.start()
    time.sleep(0.4)
    analyser.begin_record()
    print(f"Recording {kind} on {dev.description} for {seconds:g} s…", file=sys.stderr)
    time.sleep(seconds)
    audio = analyser.end_record()
    analyser.stop()
    if analyser.error and len(audio) == 0:
        print(analyser.error, file=sys.stderr)
        return 1
    db = store.Store()
    room = None
    if kind == "voice":
        settings = store.load_settings()
        pid = settings.get("active_room_profile", {}).get(dev.name)
        prof = db.profile(pid) if pid else None
        room = prof.get("metrics") if prof else None
    metrics = (analysis.analyse_room(audio, dev.rate) if kind == "room"
               else analysis.analyse_voice(audio, dev.rate, room=room))
    path = store.save_sample(audio, dev.rate, kind, dev.name)
    prof_path = store.make_noise_profile(path) if kind == "room" else None
    gain = pick_gain_control(read_controls(dev.card)) if dev.is_hardware else None
    mid = db.add_measurement(
        kind=kind, device=dev.name, device_label=dev.description,
        gain_control=gain.name if gain else None, gain_index=gain.index if gain else None,
        gain_db=gain.db if gain else None, soft_percent=dev.pw_percent,
        location=location, notes=notes, duration_s=len(audio) / dev.rate,
        metrics=metrics, sample_path=str(path), noiseprof=str(prof_path) if prof_path else None,
    )
    print(json.dumps({"id": mid, "sample": str(path), "metrics": metrics}, indent=2))
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog=APP,
        description="Microphone gain, level, noise and voice analysis for speech recognition.",
    )
    parser.add_argument("--version", action="version", version=f"{APP} {VERSION}")
    parser.add_argument("--tray", action="store_true",
                        help="start in the system tray without opening the window")
    parser.add_argument("--list", action="store_true",
                        help="print every capture device with its real analog gain, and exit")
    parser.add_argument("--json", action="store_true", help="with --list or --history: JSON output")
    parser.add_argument("--history", action="store_true", help="print the measurement history and exit")
    parser.add_argument("--measure", choices=["room", "voice"],
                        help="record and analyse headlessly, log to history, print JSON")
    parser.add_argument("--seconds", type=float, default=15.0, help="duration for --measure")
    parser.add_argument("--device", help="source name for --measure (default: first hardware device)")
    parser.add_argument("--location", help="location tag for --measure")
    parser.add_argument("--notes", help="notes for --measure")
    parser.add_argument("--restore", action="store_true",
                        help="reapply saved gain settings and exit (used by the login unit)")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="seconds to wait before --restore, to let wireplumber settle")
    parser.add_argument("--asr-mic", action="store_true",
                        help="run the ASR Mic effects chain supervisor (used by its user unit)")
    args = parser.parse_args(argv)

    from .migrate import migrate
    for note in migrate():
        print(f"{APP}: migrated {note}", file=sys.stderr)

    if args.list:
        if args.json:
            print(json.dumps(_devices_json(), indent=2))
        else:
            from .core import device_report
            print(device_report())
        return 0
    if args.history:
        from .store import Store
        data = Store().export()
        if args.json:
            print(json.dumps(data, indent=2, default=str))
        else:
            from .qtutil import key_result
            for m in data["measurements"]:
                print(f"{m['created'][:16]}  {m['kind']:<11} {m.get('location') or '-':<16} "
                      f"{(m.get('device_label') or m['device'])[:32]:<32}  {key_result(m)}")
        return 0
    if args.measure:
        return _measure(args.measure, args.seconds, args.device, args.location, args.notes)
    if args.restore:
        from .core import restore_saved
        return restore_saved(args.delay)
    if args.asr_mic:
        from .asrmic import serve
        return serve()

    try:
        from .app import run
    except ImportError as exc:
        print(f"{APP}: PyQt6 not available ({exc}).", file=sys.stderr)
        print(f"Try `{APP} --list` for the same facts on the terminal.", file=sys.stderr)
        return 1
    return run(start_in_tray=args.tray)
