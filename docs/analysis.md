# Analysis: metrics, thresholds, score

Source: `asrmictuner/analysis.py`. Input is raw int16 audio, shape
`(frames, channels)`, at the device's native rate (48 kHz on the reference
laptop). Output is plain JSON, stored as `measurements.metrics` in
`history.db` and sent as-is (with band tables compacted) to the AI review.

Levels and clipping are always **per channel**. Spectral shape and pitch use a
channel mean, which is harmless for those purposes.

## Shared

| Field | Meaning |
|---|---|
| `channels[].rms_dbfs / peak_dbfs` | whole-sample level per channel |
| `channels[].dc_offset` | mean as a fraction of full scale; warning at ≥ 0.01 |
| `channels[].clipped_samples / clipped_runs / longest_clip_run` | \|s\| ≥ 32764; a run is ≥ 3 consecutive samples, which is real saturation rather than an isolated full-scale sample |
| `channel_imbalance_db` | max − min channel RMS; warning at ≥ 6 dB |
| `bands` | 1/3-octave band power, 31.5 Hz–16 kHz, dBFS (Welch, Hann, nfft 8192) |

## Room tone (`kind: room`)

| Field | Meaning / threshold |
|---|---|
| `noise_floor_p10_dbfs` | 10th percentile of 30 ms block RMS. ≤ −60 is quiet, ≤ −50 acceptable, above −50 fails |
| `noise_rms_dbfs`, `noise_p90_dbfs` | overall and 90th-percentile block level |
| `noise_a_weighted_dbfs` | PSD × A-weighting, summed; closer to perceived loudness |
| `stationarity_db` | std dev of block RMS; > 4 dB means "fluctuating" (noise reduction trained on it works less well) |
| `transients` | blocks > median + 10 dB |
| `low_rumble_dbfs` (20–150 Hz), `speech_band_dbfs` (300–3400), `hiss_dbfs` (4–16 kHz) | energy per region; the largest names `character` |
| `hum` | 50 and 60 Hz families, harmonics 1–6; prominence = line minus the median of the ±6–25-bin neighbourhood. `present` when ≥ 15 dB |
| `spectral_flatness` | geometric over arithmetic mean of PSD, 100 Hz–8 kHz; near 1 is white, near 0 is tonal |

A room sample also gets a sox noise profile (`.noiseprof`) next to its FLAC.
Clean up later with `sox in.wav out.wav noisered <file>.noiseprof 0.21`.

## Voice (`kind: voice`)

Noise reference: the **active room profile's** `noise_floor_p10_dbfs` if one is
set for the device, otherwise the 10th percentile of the sample's own blocks
(the pauses). Recorded in `noise_reference_source`.

Speech activity: blocks ≥ max(noise + 12 dB, 30th percentile, −55 dBFS).

| Field | Meaning / threshold |
|---|---|
| `speech_peak_p95_dbfs` | 95th percentile of per-block worst-channel peak over active blocks. Target −18…−10, aim −12 |
| `speech_rms_dbfs` | RMS over active blocks |
| `snr_db` | speech RMS − noise reference. ≥ 30 excellent, ≥ 20 OK, < 20 fails |
| `crest_factor_db`, `dynamic_range_db` | p95 peak − RMS; p95 − p10 of active block RMS |
| `f0_median_hz`, `f0_p10_hz`, `f0_p90_hz`, `voiced_blocks` | normalised autocorrelation on a 16 kHz decimation, 40 ms windows, 60–400 Hz, voiced if peak correlation ≥ 0.5 |
| `spectral_centroid_hz`, `spectral_tilt_db_per_octave` | brightness, and the slope of band levels 300 Hz–6 kHz |
| `bandwidth_hz` | highest frequency within 50 dB of the smoothed spectral peak; < 7 kHz warns (Bluetooth HFP, heavy filtering) |
| `low_vs_body_db` | 20–80 Hz vs 150–1000 Hz; > −6 dB warns of rumble or proximity effect. 80 Hz sits below almost any speaker's F0, so voice itself doesn't trip it (an earlier 150 Hz edge flagged a 140 Hz synthetic voice) |
| `presence_vs_body_db` (2–5 kHz), `sibilance_vs_body_db` (5–10 kHz; > −3 dB warns) | tonal balance |
| `plosive_pops` | active blocks whose < 80 Hz energy is > median + 15 dB; ≥ 5 warns |
| `speech_ratio` | share of active blocks; < 0.3 warns that the sample is unreliable |

### ASR readiness score

Start at 100 and subtract named deductions; anything under 1 point is ignored.

| Deduction | Points |
|---|---|
| no speech detected | 60 |
| speech peak > −3 dBFS | 35 |
| peak above −10 | min(25, 3 × dB over) |
| peak below −18 | min(30, 2 × dB under) |
| clipping | min(40, 15 + 2 × runs) |
| SNR below 30 | min(35, 1.5 × dB short) |
| bandwidth < 7 kHz | 15 |
| noise reference above −50 | min(20, 2 × dB over) |
| ≥ 5 plosive pops | 5 |

Grades: ≥ 85 excellent, ≥ 70 good, ≥ 50 fair, else poor.

## Checked against synthetic signals (2026-10-22)

| Input | Result |
|---|---|
| White noise at −60 dBFS + 50 Hz sine at −50 dBFS | hum detected at 50 Hz, prominence 40 dB; floor −53.2 dBFS (the hum dominates the RMS); character "mains hum at 50 Hz, … steady" |
| 140 Hz harmonic "voice", gated 2.5 Hz, peaks ≈ −17 dBFS, over that room | F0 140.04 Hz; peak p95 −16.8 dBFS; SNR 29.9 dB against the room profile |
| Real room on the reference laptop at +24.75 dB analog gain | floor −32.4 dBFS (fails), channel imbalance 11.8 dB, no hum: the gain is far too high, as `docs/gain-mapping.md` predicts |

Not yet validated against real voice recordings. The thresholds are design
targets for ASR, not tuned on data.
