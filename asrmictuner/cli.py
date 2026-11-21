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
        entry["mic_key"] = d.key
        entry["identity"] = d.ident
        entry["remembered"] = config["devices"].get(d.key)
        entry["presets"] = config["presets"].get(d.key, {})
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
        pid = settings.get("active_room_profile", {}).get(dev.key)
        prof = db.profile(pid) if pid else None
        room = prof.get("metrics") if prof else None
    metrics = (analysis.analyse_room(audio, dev.rate) if kind == "room"
               else analysis.analyse_voice(audio, dev.rate, room=room))
    path = store.save_sample(audio, dev.rate, kind, dev.name)
    prof_path = store.make_noise_profile(path) if kind == "room" else None
    gain = pick_gain_control(read_controls(dev.card)) if dev.is_hardware else None
    mid = db.add_measurement(
        kind=kind, device=dev.name, mic_key=dev.key, device_label=dev.description,
        gain_control=gain.name if gain else None, gain_index=gain.index if gain else None,
        gain_db=gain.db if gain else None, soft_percent=dev.pw_percent,
        location=location, notes=notes, duration_s=len(audio) / dev.rate,
        metrics=metrics, sample_path=str(path), noiseprof=str(prof_path) if prof_path else None,
    )
    print(json.dumps({"id": mid, "sample": str(path), "metrics": metrics}, indent=2))
    return 0


def _tune(seconds, device_name, place, apply=True, clip=None):
    """Headless quick tune: listen, set the gain, save the profile (tune.py).
    With `clip` (a reference clip id, v3.0) the listen is the saved clip: the gain
    is planned from the gain it was recorded at, so nobody has to talk again."""
    from datetime import datetime
    from . import analysis, store, tune
    from .capture import Analyser
    from .identity import machine_label, this_machine
    from .core import (Curve, curve_cache_key, default_source, fmt_gain, list_devices, load_config,
                       note_identity, pick_gain_control, read_controls, resolve_hardware_for,
                       save_config)

    devices = list_devices()
    by_name = {d.name: d for d in devices}
    ref = None
    if clip:
        from . import mics
        ref = mics.reference(clip)
        if ref is None:
            print(f"no such reference clip: {clip!r} (see --references)", file=sys.stderr)
            return 1
        live = next((d for d in devices if d.key == ref["mic_key"]), None)
        if live is None:
            print(f"the clip's mic ({ref['mic_key']}) is not connected, so its gain can't be set", file=sys.stderr)
            return 1
        if ref.get("gain_index") is None:
            print("the clip has no recorded gain index", file=sys.stderr)
            return 1
        device_name = live.name
    name = device_name or default_source()
    if name in by_name and not by_name[name].is_hardware:
        name = resolve_hardware_for(name) or name
    dev = by_name.get(name) or next((d for d in devices if d.is_hardware), None)
    if dev is None:
        print("no capture device", file=sys.stderr)
        return 1
    config, settings = load_config(), store.load_settings()
    gain = pick_gain_control(read_controls(dev.card)) if dev.is_hardware else None
    curve = None
    if gain is not None:
        curve = gain.curve or Curve.from_json(config["curves"].get(curve_cache_key(dev, gain), {}))
    if gain is None or curve is None:
        print("no analog gain control with a known curve on this device", file=sys.stderr)
        return 1
    passes, plan = [], None
    if ref is not None:
        audio, rate = mics.read_audio(ref["audio"])
        metrics = analysis.analyse_voice(audio, rate)
        before = fmt_gain(curve.db(ref["gain_index"]))
        plan = tune.plan_gain(metrics, settings, curve, ref["gain_index"])
        passes.append({"pass": 1, "clip": ref["id"], "gain_before": before, "plan": plan,
                       "summary": tune.summary(metrics)})
        print(tune.describe(plan, before), file=sys.stderr)
        if not plan["ok"]:
            print(json.dumps({"ok": False, "passes": passes}, indent=2))
            return 2
        if apply:
            gain.set_index(plan["index"])
    for n, secs in (() if ref is not None else ((1, seconds), (2, tune.VERIFY_SECONDS))):
        analyser = Analyser(dev.name, dev.channels, dev.rate)
        analyser.start()
        time.sleep(0.4)
        analyser.begin_record()
        print(f"Pass {n}: talk normally for {secs:g} s on {dev.description} "
              f"(gain {fmt_gain(curve.db(gain.index))})…", file=sys.stderr)
        time.sleep(secs)
        audio = analyser.end_record()
        analyser.stop()
        metrics = analysis.analyse_voice(audio, dev.rate)
        before = fmt_gain(curve.db(gain.index))
        plan = tune.plan_gain(metrics, settings, curve, gain.index)
        passes.append({"pass": n, "gain_before": before, "plan": plan, "summary": tune.summary(metrics)})
        print(tune.describe(plan, before), file=sys.stderr)
        if not plan["ok"]:
            print(json.dumps({"ok": False, "passes": passes}, indent=2))
            return 2
        if apply and not plan["converged"]:
            gain.set_index(plan["index"])         # updates gain.values, hence gain.index
        if not (apply and plan["verify"]) or n == 2:
            break
    label = (place or settings.get("location") or "Default").strip()
    if apply:
        entry = {"notes": f"Quick tune from reference clip {ref['id']}" if ref else "Quick tune", "created": datetime.now().astimezone().isoformat(timespec="seconds"),
                 "index": gain.index, "db": curve.db(gain.index), "control": gain.name, "port": dev.port,
                 "place": label, "computer": machine_label(this_machine()),
                 "noise_reduction": settings.get("noise_reduction", "off"),
                 "tuned": dict(passes[-1]["summary"], at=datetime.now().astimezone().isoformat(timespec="seconds"))}
        note_identity(config, dev)
        config["presets"].setdefault(dev.key, {})[label] = entry
        config["devices"][dev.key] = {"card_name": dev.card_name, "control": gain.name, "numid": gain.numid,
                                       "index": gain.index, "db": curve.db(gain.index),
                                       "saved": entry["created"]}
        config.setdefault("active_profile", {}).setdefault(dev.key, {})[dev.port] = label
        save_config(config)
    print(json.dumps({"ok": True, "device": dev.name, "port": dev.port, "profile": label if apply else None,
                      "gain_db": curve.db(gain.index), "passes": passes}, indent=2))
    return 0


# -- mic catalogue, benchmarks, reference clips (v3.0, mics.py, docs/mics.md) --

def _pick_device(spec):
    """A Device by node name, mic key or unique key prefix; default: the
    system default input (resolved to its hardware mic)."""
    from .core import default_source, list_devices, resolve_hardware_for
    devices = list_devices()
    if spec:
        hits = ([d for d in devices if spec in (d.name, d.key)]
                or [d for d in devices if d.key.startswith(spec) or spec.lower() in d.description.lower()])
        return hits[0] if len(hits) == 1 else None
    name = default_source()
    by_name = {d.name: d for d in devices}
    if name in by_name and not by_name[name].is_hardware:
        name = resolve_hardware_for(name) or name
    return by_name.get(name) or next((d for d in devices if d.is_hardware), None)


def _known_key(spec):
    """A remembered mic's key (connected or not) by key, unique key prefix or name."""
    from . import mics
    if not spec:
        return None
    cat = mics.catalogue()
    hits = ([m for m in cat if m["mic_key"] == spec]
            or [m for m in cat if m["mic_key"].startswith(spec) or spec.lower() in m["name"].lower()])
    return hits[0]["mic_key"] if len(hits) == 1 else None


def _gain_state(dev):
    from .core import pick_gain_control, read_controls
    gain = pick_gain_control(read_controls(dev.card)) if dev.is_hardware else None
    return {"gain_control": gain.name if gain else None, "gain_index": gain.index if gain else None,
            "gain_db": gain.db if gain else None, "soft_percent": dev.pw_percent}


def _annotate(spec, fields):
    from . import mics
    from .identity import filename
    from .profiles import is_mic_key
    dev = _pick_device(spec)
    key = dev.key if dev else _known_key(spec)
    if not key or not is_mic_key(key):
        print(f"no such microphone: {spec!r} (see --mics)", file=sys.stderr)
        return 1
    try:
        about = mics.set_about(key, ident=dev.ident if dev else None,
                               **{k: v for k, v in fields.items() if v is not None})
    except (KeyError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(json.dumps({"mic_key": key, "about": about, "file": f"profiles/{filename(key)}.json"}, indent=2))
    return 0


def _benchmark(spec, seconds, clip, place, name, notes):
    """Score a mic for ASR from a fresh recording (kept as a reference clip) or a saved clip."""
    from . import analysis, mics, store
    from .core import list_devices
    settings = store.load_settings()
    place = (place or settings.get("location") or "").strip()
    if clip:
        ref = mics.reference(clip)
        if ref is None and not __import__("os").path.exists(clip):
            print(f"no such reference clip: {clip!r} (see --references)", file=sys.stderr)
            return 1
        path = ref["audio"] if ref else clip
        audio, rate = mics.read_audio(path)
        key = ref["mic_key"] if ref else None
        dev = next((d for d in list_devices() if d.key == key), None) if key else _pick_device(spec)
        key = key or (dev.key if dev else None)
        state = {k: ref.get(k) for k in ("gain_control", "gain_index", "gain_db", "soft_percent")} if ref else {}
        port = ref.get("port") if ref else None
        place = place or (ref or {}).get("place", "")
        source = {"reference": ref["id"] if ref else None, "audio": path, "recorded_now": False}
    else:
        from .capture import Analyser
        dev = _pick_device(spec)
        if dev is None:
            print("no capture device", file=sys.stderr)
            return 1
        key, port, state = dev.key, dev.port, _gain_state(dev)
        analyser = Analyser(dev.name, dev.channels, dev.rate)
        analyser.start()
        time.sleep(0.4)
        analyser.begin_record()
        print(f"Benchmark: talk normally for {seconds:g} s on {dev.description}…", file=sys.stderr, flush=True)
        time.sleep(seconds)
        audio = analyser.end_record()
        analyser.stop()
        rate = dev.rate
        if len(audio) < rate * 5:
            print(analyser.error or "too little audio captured", file=sys.stderr)
            return 1
        tmp = store.save_sample(audio, rate, "reference", dev.name)
        ref = mics.add_reference(tmp, key, device_label=dev.description, port=port, rate=rate,
                                 channels=audio.shape[1], seconds=round(len(audio) / rate, 2), place=place,
                                 name=name, notes=notes, move=True, **state)
        source = {"reference": ref["id"], "audio": ref["audio"], "recorded_now": True}
    room = None
    if key:
        pid = settings.get("active_room_profile", {}).get(key)
        prof = store.Store().profile(pid) if pid else None
        room = prof.get("metrics") if prof else None
    metrics = analysis.analyse_voice(audio, rate, room=room)
    card = mics.scorecard(metrics, rate, audio.shape[1])
    entry = None
    if key:
        entry = mics.record_benchmark(key, card, place=place, port=port, gain_db=state.get("gain_db"),
                                      soft_percent=state.get("soft_percent"), reference_id=source["reference"],
                                      ident=dev.ident if dev else None)
    if source["recorded_now"]:
        store.Store().add_measurement(
            kind="voice", device=dev.name, mic_key=key, device_label=dev.description,
            gain_control=state["gain_control"], gain_index=state["gain_index"], gain_db=state["gain_db"],
            soft_percent=state["soft_percent"], location=place, notes=notes or "Benchmark",
            duration_s=len(audio) / rate, metrics=metrics, sample_path=source["audio"])
    print(json.dumps({"mic_key": key, "name": mics.display_name(key) if key else None, "place": place,
                      "gain": state, "source": source, "scorecard": card, "logged": entry}, indent=2, default=str))
    return 0


def _add_reference(path, spec, place, name, notes):
    import os
    from . import mics
    dev = _pick_device(spec)
    if dev is None or not os.path.exists(path):
        print("need an existing audio file and a connected --device (gain is read from it now)", file=sys.stderr)
        return 1
    audio, rate = mics.read_audio(path)
    ref = mics.add_reference(path, dev.key, device_label=dev.description, port=dev.port, rate=rate,
                             channels=audio.shape[1], seconds=round(len(audio) / rate, 2), place=place,
                             name=name, notes=notes, **_gain_state(dev))
    print(json.dumps(ref, indent=2))
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
    parser.add_argument("--seconds", type=float, default=None,
                        help="duration for --measure (default 15) or --tune (default 30)")
    parser.add_argument("--tune", action="store_true",
                        help="quick tune: listen while you talk, set the gain for ASR, save it as a profile "
                             "for this mic and --place; prints JSON")
    parser.add_argument("--place", help="profile name for --tune (default: the current location, else Default)")
    parser.add_argument("--dry-run", action="store_true", help="with --tune: measure and plan only")
    parser.add_argument("--device", help="source name for --measure (default: first hardware device)")
    parser.add_argument("--location", help="location tag for --measure")
    parser.add_argument("--notes", help="notes for --measure")
    mic = parser.add_argument_group("microphone catalogue (v3.0, docs/mics.md)")
    mic.add_argument("--mics", action="store_true",
                     help="list every known mic (connected or remembered) with its notes, benchmarks and clips")
    mic.add_argument("--annotate", action="store_true",
                     help="save human notes for --device (node name, mic key or key prefix; default: the "
                          "default input) from --maker/--model/--type/--connection/--mic-notes; '' clears one")
    mic.add_argument("--maker")
    mic.add_argument("--model")
    mic.add_argument("--type", dest="mic_type", help="headset, earbuds, usb-desk, lavalier, studio-condenser, "
                     "studio-dynamic, shotgun, conference, webcam, laptop-internal, other")
    mic.add_argument("--connection", help="usb-c, usb-a, bluetooth, wireless-dongle, 3.5mm, xlr, internal, other")
    mic.add_argument("--mic-notes")
    mic.add_argument("--benchmark", action="store_true",
                     help="score --device for ASR: record --seconds (default 30), keep it as a reference clip, "
                          "log it; or re-score a saved clip with --clip. Prints JSON")
    mic.add_argument("--clip", help="with --benchmark or --tune: a reference clip id (or prefix) or an audio file path")
    mic.add_argument("--name", help="name for the reference clip --benchmark records")
    mic.add_argument("--references", action="store_true", help="list reference clips (JSON); --device filters")
    mic.add_argument("--add-reference", metavar="AUDIO",
                     help="file an existing recording as a reference clip of --device (records its gain now)")
    mic.add_argument("--delete-reference", metavar="ID")
    parser.add_argument("--sync", action="store_true",
                        help="sync profiles and chains with the profile folder (Settings → Profile sync), "
                             "print a JSON report, and exit")
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
        return _measure(args.measure, args.seconds or 15.0, args.device, args.location, args.notes)
    if args.tune:
        return _tune(args.seconds or 30.0, args.device, args.place, apply=not args.dry_run, clip=args.clip)
    if args.mics:
        from . import mics
        data = mics.catalogue()
        if args.json:
            print(json.dumps(data, indent=2))
        else:
            for m in data:
                flags = ("connected" if m["connected"] else "not connected") + (", default" if m["system_default"] else "")
                b = m["latest_benchmark"]
                print(f"{m['name']}  [{m['about'].get('type') or '?'}, {m['about'].get('connection') or '?'}]  ({flags})")
                print(f"  key {m['mic_key']}")
                if b:
                    print(f"  last benchmark {b['at'][:16]}: {b['score']} ({b['grade']}), SNR {b['snr_db']} dB")
                print(f"  {m['benchmarks']} benchmarks, {m['reference_clips']} reference clips")
        return 0
    if args.annotate:
        return _annotate(args.device, {"maker": args.maker, "model": args.model, "type": args.mic_type,
                                       "connection": args.connection, "notes": args.mic_notes})
    if args.benchmark:
        return _benchmark(args.device, args.seconds or 30.0, args.clip, args.place, args.name, args.notes)
    if args.references:
        from . import mics
        dev = _pick_device(args.device) if args.device else None
        key = dev.key if dev else _known_key(args.device)
        if args.device and not key:
            print(f"no such microphone: {args.device!r} (see --mics)", file=sys.stderr)
            return 1
        print(json.dumps(mics.references(key), indent=2))
        return 0
    if args.add_reference:
        return _add_reference(args.add_reference, args.device, args.place, args.name, args.notes)
    if args.delete_reference:
        from . import mics
        ref = mics.reference(args.delete_reference)
        if ref is None:
            print("no such reference clip", file=sys.stderr)
            return 1
        mics.delete_reference(ref)
        print(json.dumps({"deleted": ref["id"]}))
        return 0
    if args.sync:
        from . import store, sync
        report = sync.sync(store.load_settings())
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
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
