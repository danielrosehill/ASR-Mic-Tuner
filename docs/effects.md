# Effects reference (ASR Effects tab)

Every effect runs inside PipeWire as part of ASR Mic's filter-chain
(`asrmictuner/engine.py`). Chains are saved as JSON in
`~/.config/asr-mic-tuner/chains/<name>.json`:

```json
{"input_mix": "Mix all channels",
 "effects": [{"kind": "highpass", "bypass": false, "settings": {"frequency": 80, "steep": true}},
             {"kind": "autogain", "bypass": false, "settings": {"target": -20, "history": 30, "max_boost": 12, "max_cut": 12}}]}
```

Bindings live in `settings.json` under `chain_bindings`, keyed `"<device>:<port description>"`
(e.g. `alsa_input.pci-0000_04_00.6.analog-stereo:Internal Microphone`).

| Effect | Implementation | Needs |
|---|---|---|
| Mono fold (always first) | builtin `mixer`, equal gains, or a single channel | nothing |
| High-pass, low-pass | builtin `bq_highpass` / `bq_lowpass`; "steep" = two stages, Q 0.5412 and 1.3066 (4th-order Butterworth) | nothing |
| Hum notch | builtin `bq_notch` at 50 or 60 Hz × harmonics, Q 30 | nothing |
| EQ band | builtin `bq_peaking` | nothing |
| Auto gain | `ebur128` (gated loudness, `max-history` N s) → `lufs2gain` (target) → builtin `clamp` (±max dB) → builtin `linear` `Mult` | `libspa-0.2-modules-extra` |
| Noise gate | swh `gate` (`gate_1410`), keyed on 100 Hz–6 kHz; `Range` is the reduction, so it lowers rather than mutes | `swh-plugins` |
| Compressor | swh `sc4m` | `swh-plugins` |
| Limiter | swh `fastLookaheadLimiter` (stereo plugin, mono use on `Input 1`) | `swh-plugins` |
| RNNoise | `librnnoise_ladspa.so`, label `noise_suppressor_mono`; `VAD Threshold (%)` 0 = no gating | download |
| Voice activity gate | RNNoise with `VAD Threshold (%)` > 0 (with a caution: see `docs/asr-mic.md`) | download |
| DeepFilterNet | `libdeep_filter_ladspa.so`, label `deep_filter_mono`, `Attenuation Limit (dB)` | download |

## Quick noise reduction

The **Noise reduction** switch (tray menu, or ASR Effects → ASR Mic) lays a denoiser
over whatever chain is bound, without editing it. Setting `noise_reduction`:

| Mode | Adds | When | CPU (Ryzen 7 5700U) |
|---|---|---|---|
| `off` | nothing; the chain as it is | most of the time | — |
| `light` | RNNoise, no VAD | steady noise: fans, hum, traffic | about 5 % of one core |
| `strong` | DeepFilterNet, 30 dB cap | loud, changing noise: a crying baby, voices, dishes | about 34 % of one core |

Any denoiser already in the chain is replaced, so two never run in series; a VAD gate
is kept. The new one goes after the leading filters and before the gate and gain stages
(`engine.with_noise_reduction`). It needs the plugin downloaded (ASR Effects →
Get); if it is missing it is skipped and marked like any other effect.

A denoiser is not free for ASR: Whisper-family models are trained on noisy audio and
cope with mild noise better than with denoiser artefacts. Leave it off in a quiet room.

A missing backend never breaks ASR Mic. The effect is skipped, the tab marks it
"plugin missing: skipped", and the supervisor records it in `asr-mic-state.json`.

## Downloads (from the maintainers)

Fetched only when you press **Get**. Each one is checked against a pinned SHA-256
before it is installed into `~/.local/share/asr-mic-tuner/plugins/`:

| Plugin | Release asset | SHA-256 |
|---|---|---|
| RNNoise | werman/noise-suppression-for-voice v1.10 `linux-rnnoise.zip` → `linux-rnnoise/ladspa/librnnoise_ladspa.so` | zip `811390b6…160e99`, .so `ba2b420c…01f97` |
| DeepFilterNet | Rikorose/DeepFilterNet v0.5.6 `libdeep_filter_ladspa-0.5.6-x86_64-unknown-linux-gnu.so` | `2ca3205c…2e2236` |

Full hashes are in `engine.PLUGINS`. LADSPA labels and port names were read from
each binary's own descriptor (ctypes, 2026-10-27).

## Notes from testing (PipeWire 1.6.2, 2026-10-27)

- The builtin `noisegate` never opens. Its `Level` default is NaN, and the
  filter-graph clamps it to 0. That is why the gate uses swh.
- Auto gain on pure room noise: gated loudness counts anything above −70 LUFS,
  so after more than N seconds with no speech the gain drifts up to `max_boost`.
  It comes back within a few seconds of speech. The analog **ASR auto gain**
  (Settings) does not have this behaviour, because it only acts on speech.
- CPU on the reference laptop: DeepFilterNet chain about 50 % of one core (one
  underrun at start), RNNoise chain about 10 %.
- Test method: play tones into a temporary null sink and point the chain at it
  with `target.object=<sink>` and `stream.capture.sink=true`. Always check that
  `asr-mic-tuner.asr-mic` exists before recording from it.
