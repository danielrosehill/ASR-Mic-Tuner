---
name: benchmark-microphone
description: Benchmark one or more microphones for speech recognition with ASR Mic Tuner, from a new 30 s recording or saved reference clips, and compare them. Use on "benchmark this mic", "which of my mics is best for dictation", "compare the headset and the laptop mic", "re-score the reference clips".
---

# Benchmark microphones for ASR (ASR Mic Tuner)

Scores how well a mic's signal suits speech recognition (0–100 readiness, plus
findings) and keeps every result with the mic. Reference: `docs/mics.md` and
`docs/analysis.md` in the ASR-Mic-Tuner repo. CLI: `asr-mic-tuner`, or
`python3 -m asrmictuner` from a source checkout (system Python). JSON on stdout.

## 1. What is there already

```bash
asr-mic-tuner --mics --json                      # mics, latest benchmark, clip count
asr-mic-tuner --references --device <mic>        # saved clips for one mic (omit --device for all)
```

If a mic already has a reference clip at the gain in question, re-score it. Don't ask
the user to speak again:

```bash
asr-mic-tuner --benchmark --clip <reference id>
```

## 2. New recording

Tell the user first: "Talk normally for 30 seconds at your dictation distance, starting
now." Then:

```bash
asr-mic-tuner --benchmark --device <mic key | prefix | node name> --place "<place>" --name "<label>"
```

`--seconds` changes the length. Under ~20 s, `level_spread_db` is not measured. To
compare mics fairly, record them in the same room, at each one's normal distance, with
similar speech. One take per mic, then compare. If there are several mics, run them
one after another and tell the user which mic to talk into before each.

## 3. Read the scorecard

`scorecard.score` / `grade` / `deductions`, `findings` and `asr_checks` (each
`{level: pass|warn|fail, text}`), and `metrics`. Split the findings for the user:

- **Fixed by gain** (re-tune with the `tune-microphone` skill, or
  `asr-mic-tuner --tune --clip <id>`): speech peak outside −18…−10 dBFS, clipping.
- **Not fixed by gain**: `snr_db` < 20 (distance or room), `plosive_pops` ≥ 5 (boom
  position, pop filter), `sibilance_vs_body_db` > −3, `dropouts` > 0 (a gate or VAD
  upstream), narrow `bandwidth_hz` < 7000 (Bluetooth HFP), `channel_imbalance_db` ≥ 6.

`bandwidth_hz` of 24000 just means the noise floor sits within 50 dB of the peak. It
is not a fidelity score.

## 4. Compare

To compare mics, tabulate per mic: name (from `--mics`), gain at recording, score,
speech peak, SNR, plosives, and the main unfixable issue. Rank by what gain can't fix
(SNR first), because a level problem on a good mic is one `--tune` away. Each benchmark
is also stored in the mic's profile file (`benchmarks`, with computer and place) and
shows in the GUI under Profiles → About this microphone.
