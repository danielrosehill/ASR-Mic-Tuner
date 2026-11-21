# Microphone catalogue: notes, benchmarks, reference clips (v3.0, 2026-11-21)

Code: `asrmictuner/mics.py` (data), `asrmictuner/micpage.py` (Profiles tab boxes),
`asrmictuner/cli.py` (`--mics`, `--annotate`, `--benchmark`, `--references`,
`--add-reference`, `--delete-reference`, `--tune --clip`). Everything is keyed by the
mic key (`docs/mic-identity.md`), never the PipeWire node name.

## About (human notes)

A USB descriptor often names the chipset vendor, not the brand on the box. Example,
confirmed 2026-11-21: an **Acer OHW325** USB-C headset enumerates as
`DSEA A/S` / `SC230` (USB `1395:0026`) with udev serial
`DeSheng_Electronics_Inc._OHW325_OHW325_20260123`. `about` records what a person knows:

```json
"about": {"maker": "Acer", "model": "OHW325", "type": "headset", "connection": "usb-c",
          "notes": "free text", "updated": "2026-11-21T22:47:37+02:00"}
```

- Stored in the mic's profile file (`~/.config/asr-mic-tuner/profiles/<key>.json`),
  **shared across computers** (it describes the physical mic) and synced like
  `chain_bindings`: on a two-sided edit the newer file's `about` wins.
- `type`: `headset`, `earbuds`, `usb-desk`, `lavalier`, `studio-condenser`,
  `studio-dynamic`, `shotgun`, `conference`, `webcam`, `laptop-internal`, `other`.
- `connection`: `usb-c`, `usb-a`, `bluetooth`, `wireless-dongle`, `3.5mm`, `xlr`,
  `internal`, `other`.
- The GUI (Profiles → About this microphone) pre-fills unsaved fields from the identity
  (`mics.guess_about`) and says so. Guesses are never saved on their own.
- Display name everywhere in `--mics`: `maker model` if set, else the PipeWire description.
- A mic with only `about` (no gain profile) still keeps its file: `about` and
  `benchmarks` count as "something remembered" in `profiles.update`.

CLI: `asr-mic-tuner --annotate --device <node name | mic key | key prefix | name>
--maker … --model … --type … --connection … --mic-notes …`. `''` clears a field;
omitted fields are kept. `--device` also matches mics that are not plugged in, through
their profile files.

## Benchmark (ASR scorecard)

`mics.scorecard(metrics, rate, channels)` wraps `analysis.analyse_voice` (all metrics in
`docs/analysis.md`) into what an ASR user needs:

- `score` / `grade` / `deductions`: the existing readiness score (0–100).
- `findings`: the voice findings (level, clipping, SNR, plosives, sibilance, dropouts, …).
- `asr_checks`: native rate ≥ 16 kHz; bandwidth ≥ 7 kHz (what a 16 kHz model hears);
  channel imbalance ≥ 6 dB (ASR downmixes).
- `metrics`: the 16 numbers worth comparing between mics.

`--benchmark` records `--seconds` (default 30) from `--device` (default: the system
default input, resolved through virtual sources to the hardware mic), **keeps the
recording as a reference clip**, logs it to History as a voice measurement, appends a
summary to the mic's `benchmarks` list and prints JSON. `--benchmark --clip <id>`
re-scores a saved clip and records nothing new.

`benchmarks` (in the profile file, shared, newest last, at most 50): `at`, `computer`,
`computer_id`, `place`, `port`, `gain_db`, `soft_percent`, `score`, `grade`, `snr_db`,
`speech_peak_p95_dbfs`, `noise_dbfs`, `bandwidth_hz`, `clipped_runs`, `reference`.
Re-scoring the same clip replaces its entry. Sync merges both sides' lists by
(`at`, `computer_id`).

The score is measured **at the gain the clip was recorded at**. A mic recorded too hot
scores low for level, which a gain change fixes. SNR, bandwidth, plosives and dropouts
are properties of the mic, the distance and the room, which gain does not fix.

## Reference clips

Recordings kept so the mic can be re-analysed, benchmarked or tuned again without
anyone speaking. Machine-local, **not synced** (audio is large):

```
~/.local/share/asr-mic-tuner/references/<mic file name>/<id>.flac
~/.local/share/asr-mic-tuner/references/<mic file name>/<id>.json
```

`<id>` is `YYYYMMDD-HHMMSS-<first 6 hex of sha1(mic key)>`; commands accept a unique
prefix. The JSON holds `mic_key`, `device_label`, `audio`, `rate`, `channels`,
`seconds`, `port`, `gain_control`, `gain_index`, `gain_db`, `soft_percent`, `computer`,
`place`, `name`, `notes` and `recorded`. **The gain fields are the gain at recording
time.** That is what makes `--tune --clip` possible.

Ways in:
- `--benchmark` (CLI), always.
- Measure tab → mic test → **Keep as reference clip** (copies the sample).
- `--add-reference <audio> --device <mic>` files an existing recording. The gain is read
  from the device **now**, so use it only if the gain has not moved since recording.

Ways to use one:
- Profiles → Reference clips: Play, Benchmark, Delete.
- `--benchmark --clip <id>`: re-score it (after an analysis change, say).
- `--tune --clip <id> [--place …] [--dry-run]`: Quick tune from the clip. The gain is
  planned relative to the clip's recorded `gain_index` with `tune.plan_gain`, applied to
  the mic (which must be connected) and saved as the profile for `--place`. There is no
  verification pass. Entry notes: `Quick tune from reference clip <id>`.
- Any external tool: the FLAC is plain 48 kHz int16, at the device's native channels.

## Tested (2026-11-21, ThinkPad E15 Gen 3, PipeWire 1.6)

| Mic | Clip gain | Score | Notes |
|---|---|---|---|
| Acer OHW325 USB-C headset | +3.39 dB (index 3427) | 92, excellent | peaks −16.4 dBFS, SNR 27.9 dB, 15 plosive pops (boom in front of mouth) |
| ThinkPad internal (ALC257) | +25.5 dB (index 57) | 38, poor | peaks −3.4 dBFS (too hot), SNR 15 dB, little speech detected |

`--tune --clip` on the internal clip planned −9.8 dB (target −13 dBFS: 3 dB extra
headroom because the clip came within 1 dB of full scale), set +15.75 dB (index 44) and
saved it as the internal mic's profile for "Home living room". The GUI boxes were
checked offscreen (`QT_QPA_PLATFORM=offscreen`): notes load, the clip lists, Benchmark
shows the scorecard.

Bandwidth reads 24000 Hz on both mics: the 50 dB-below-peak rule in
`analysis.bandwidth_hz` reaches Nyquist when the noise floor is within 50 dB of the
speech peak. So the 7 kHz check only catches true narrowband sources (Bluetooth HFP).
It is not a fidelity measure.

## Agent skills

`skills/` in this repo holds agent skills (SKILL.md, usable by Claude Code and similar
agents) that drive the CLI above: `tune-microphone` (identify, annotate, record a
reference, benchmark, tune, save the profile) and `benchmark-microphone` (score and
compare mics from new or saved clips).
