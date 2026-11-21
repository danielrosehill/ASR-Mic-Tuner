---
name: tune-microphone
description: Measure a connected microphone for speech recognition, record a reference clip, set its analog gain and save it as a profile in ASR Mic Tuner, with human notes (maker, model, type, connection). Use on "tune this mic", "measure this microphone and set a profile", "set up my new headset for dictation", "save a profile for this mic".
---

# Tune a microphone for ASR (ASR Mic Tuner)

Drives the `asr-mic-tuner` CLI (Linux, PipeWire). Everything it saves is keyed by the
mic's identity (USB vendor/product/serial, Bluetooth address, or codec + machine), so
it follows the mic, and the GUI shows the same data under Profiles. Reference:
`docs/mics.md`, `docs/quick-tune.md`, `docs/mic-identity.md` in the ASR-Mic-Tuner repo.

Run the CLI as `asr-mic-tuner` (the installed .deb). From a source checkout, run
`python3 -m asrmictuner` with the system Python, which has numpy and PyQt6. Every mode
prints JSON on stdout and progress on stderr.

## 1. Find the mic

```bash
asr-mic-tuner --mics --json      # every known mic: name, about, connected, default, benchmarks, clips
asr-mic-tuner --list --json      # live devices: node name, mic_key, identity, gain control
```

If the user names a mic ("the Acer headset"), match it to a connected entry by
`usb_descriptor`, `pipewire_description` or `about`. USB descriptors often show the
chipset vendor, not the brand (an Acer OHW325 reports `DSEA A/S SC230`). If two
candidates fit, ask which one. Note the `mic_key`: pass it, or a unique prefix of it, as
`--device` from here on.

## 2. Record the human notes

Ask only for what the user hasn't said and the descriptor can't tell you. Then:

```bash
asr-mic-tuner --annotate --device <mic_key> --maker "<maker>" --model "<model>" \
  --type <headset|earbuds|usb-desk|lavalier|studio-condenser|studio-dynamic|shotgun|conference|webcam|laptop-internal|other> \
  --connection <usb-c|usb-a|bluetooth|wireless-dongle|3.5mm|xlr|internal|other> \
  --mic-notes "<what the descriptor says vs the real name; anything unconfirmed marked as such>"
```

## 3. Record a reference clip (the user talks once)

Tell the user **before** starting: "Talk normally for 30 seconds at your dictation
distance, starting now." Then run (blocks ~31 s):

```bash
asr-mic-tuner --benchmark --device <mic_key> --place "<place>" --name "<short label>"
```

It records, keeps the audio as a reference clip, logs it to History, adds a benchmark
entry to the mic and prints the scorecard JSON (`source.reference` is the clip id).
`--place` defaults to the app's current location. Use the user's room name if they
gave one.

## 4. Set the gain and save the profile from that clip

```bash
asr-mic-tuner --tune --clip <reference id> --place "<place>" --dry-run   # show the plan
asr-mic-tuner --tune --clip <reference id> --place "<place>"             # apply and save
```

This plans the analog gain from the gain the clip was recorded at (speech peaks to
−10 dBFS, 3 dB lower if the clip clipped or came within 1 dB of full scale), sets it,
and saves the profile for that mic, port, computer and place. The profile is then
active, and it is reapplied when the mic becomes the default. The mic must be plugged
in. Exit code 2 with `ok: false` means too little speech: ask for another take.

For a mic with no analog gain control (Bluetooth, virtual), `--tune` cannot set a
hardware gain. Report the benchmark and say so.

For a live tune with a verification pass instead (talk 30 s plus 15 s more if the
change is large), use `asr-mic-tuner --tune --device <mic> --place "<place>"`.

## 5. Report

Give the user, briefly: the mic as annotated, the score and grade before tuning, the
gain change (from → to), and any findings gain can't fix (SNR, plosives, bandwidth,
dropouts) with the one-line fix for each. If the change was large, offer a new 30 s
benchmark at the new gain to confirm. Don't run one unasked.

## Notes

- Never edit `~/.config/asr-mic-tuner/profiles/*.json` by hand while the app runs. Use
  the CLI, which writes only the changed mic's file.
- Reference clips stay on this computer (`~/.local/share/asr-mic-tuner/references/`). An
  agent can re-analyse them (`--benchmark --clip`) or read the FLAC directly, without
  asking the user to speak again.
- No system-wide changes: everything is user config.
