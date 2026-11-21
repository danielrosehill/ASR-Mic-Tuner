# Quick tune and mic profiles (v2.6, 2026-11-21)

Quick tune is the one-click version of what had been done by hand: listen to the
speaker for about 30 s, measure, set the gain, keep the result for that mic. Code:
`asrmictuner/tune.py` (`plan_gain` is pure and shared by the GUI and the CLI),
`Session.save_profile` / `apply_preset` / `follow_default` in `session.py`, the
follow timer in `app.py`, the buttons in `tray.py` (pop-up) and `measure.py`.

## What it does

1. Records `tune_seconds` (default 30) of whatever you say on the selected mic. No
   passage: talk the way you dictate, at your usual distance.
2. `analysis.analyse_voice` measures it (room reference: the active room profile,
   else the pauses in the sample).
3. `plan_gain`:
   - needs ≥ 3 s of *active* speech blocks. The analyser gates hard (blocks ≥ noise
     + 12 dB): 28 s of real talk on the internal mic on 2026-11-21 gave 3.7 s of
     active blocks, so a 6 s minimum (the first draft) rejected real tunes;
   - target: speech peak p95 at `target_peak_db(settings)`, −10 dBFS
     (`CALIB_TARGET_PEAK_DB`), −18 with Conservative gain;
   - any clipped run, or a max peak ≥ −1 dBFS: 3 dB lower target;
   - < 1.5 dB off: leave the gain; > 6 dB off: apply, then listen 15 s more and
     correct again (the first estimate was made far from the target).
4. Saves a **profile**: `devices.json` → `presets[<device>][<place>]` with `index`,
   `db`, `control`, `port`, `noise_reduction`, and `tuned` (speech peak and RMS,
   noise, SNR, score, time). It also becomes the remembered gain (login restore) and
   the mic's **active profile**, `active_profile["<device>:<port>"]`. The place is
   the Place box (defaults to the window's location, else `Default`). Each pass is
   also logged to History as a voice measurement, notes `Quick tune pass N (<place>)`.

Headless: `asr-mic-tuner --tune [--place NAME] [--seconds 30] [--device SRC] [--dry-run]`
prints JSON with every pass. It needs a hardware gain control with a curve.

## Following the default mic

Every 3 s (`app.py` `_follow`, setting `follow_default_mic`, on by default) the tuner
checks the hardware behind the system default input. When that *changes* (USB mic
plugged in, a headset picked in Plasma) it selects the new mic and applies its
active profile, else its newest tuned profile on the same port, else its remembered
gain, and says which in a notification. It acts only on a change of the default,
so a mic picked by hand in the window stays picked. Paused during a tune.

A profile carries the noise reduction mode (`off` / `light` / `strong`), which is a
global setting feeding ASR Mic, so applying a profile can switch it. The rest of the
ASR chain stays bound per mic and port in ASR Effects (`chain_bindings`); folding the
chain into the profile is the next step.

## Why (what was wrong before)

On 2026-11-21 Daniel found dictation accuracy hurt by the internal mic
(`alsa_input.pci-0000_04_00.6.analog-stereo`, Realtek ALC257) sitting too low:

- ASR auto gain capped the analog gain at **+15 dB** (`asr_autogain_max_db`,
  = `ZONE_CAUTION_HI`). The internal mic needs about +18 to +24 dB for speech
  peaks near −10 dBFS at dictating distance. That cap is what kept it low; v2.5
  had already turned auto gain off by default for this reason.
- Its dead band was the whole −18…−10 band ±2 dB, so it did nothing between −20
  and −8 dBFS, and it aimed at −12.
- Nothing followed a mic switch, and nothing was learned per mic.

Measured the same day, by hand, after Daniel had raised the gain himself to
Capture 55/63 (+24 dB, Mic Boost 0, digital 0 dB): speech RMS −18 dBFS, peak p95
−4 dBFS, noise in pauses −33 dBFS, SNR ≈ 14 dB, and one sample with 60 clipped
samples. So it had become slightly hot. `plan_gain` on that sample: −6 dB, to +18 dB
(index 47).

v2.6 changes:
- the target is −10 dBFS (ASR loses more to quiet input than to hot input that
  does not clip; clipping is guarded separately);
- the auto gain ceiling defaults to +30 dB, and a saved 15 is migrated to 30 once
  (`settings_rev` 26 in `store.load_settings`);
- the auto gain dead band is ±3 dB around the target, and steps go up to 6 dB
  when more than 8 dB off;
- auto gain stays **off by default** (Daniel's v2.5 decision). Quick tune is the
  default way to set the gain.

## Format of the processed stream (decided 2026-11-21)

Asked whether ASR wants a particular rate or format from the virtual source: no.
Parakeet and Whisper run at 16 kHz mono and every dictation app resamples to that
itself, so publishing 16 kHz would gain nothing, and RNNoise and DeepFilterNet need
48 kHz. Bit depth beyond 16 bits is irrelevant. What helps is the **mono fold** of
the laptop's two mic channels (2 to 4 dB imbalance measured), which ASR Mic already
does. ASR Mic (`docs/asr-mic.md`) already is the "ASR stream": processed and
separate, with the raw mic left as the default.
