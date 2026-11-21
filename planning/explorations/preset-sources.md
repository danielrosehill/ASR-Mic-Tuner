# Preset sources: published chains and the evidence on ASR

Status: research, 2026-11-21. Gathered from web sources by a research pass. "Confirmed"
means the page was read; "assumed" means a search summary or an inference. No
licences were checked beyond what is stated.

## Short answer

No published effects library targets dictation or ASR. The published mic presets
are almost all for EasyEffects and are tuned for voice that people listen to: calls,
streaming, podcasts. Their values are a fair starting point for the safe stages
(high-pass, gentle EQ cut, compressor, limiter). Their "presence" boosts, de-essers
and stacked denoisers are aimed at listeners, and the evidence below says neural
denoising tends to hurt Whisper. So we borrow values, not whole chains, and
reimplement them as our own presets with attribution. None of the gists states a
licence, so their JSON is not copied.

## Sources

| Source | Licence | What it has | Use |
|---|---|---|---|
| [JackHack96/EasyEffects-Presets](https://github.com/JackHack96/EasyEffects-Presets) | MIT (confirmed) | output presets only | none |
| [EasyEffects wiki, community presets](https://github.com/wwmm/easyeffects/wiki/Community-presets) | per repo | one input preset listed (jtrv, below) | index only |
| [jtrv gist, "Masc NPR Voice + Noise Reduction"](https://gist.github.com/jtrv/47542c8be6345951802eebcf9dc7da31) | none stated | full chain with values (below) | values, with attribution |
| [MateusRodCosta gist](https://gist.github.com/MateusRodCosta/a10225eb132cdcb97d7c458526f93085) | none stated; its author marks it deprecated | gate → comp → HP 80 Hz → de-esser → RNNoise → limiter -1 dB | reference; its order is wrong for us (gate before denoise) |
| [Framework forum: built-in mic preset](https://community.frame.work/t/guide-preset-easyeffects-microphone-profile-for-built-in-mic-clear-voice-de-noised-no-tin-can-sound/84581) | none stated | HP 80 Hz 18 dB/oct → DeepFilterNet ~60 % → EQ +1 dB 160 Hz, -2.5 dB 650 Hz, +2 dB 2.6 kHz → comp → limiter -2.5 dB | the laptop-mic EQ idea |
| [PipeWire `source-rnnoise.conf`](https://gitlab.freedesktop.org/pipewire/pipewire/-/blob/master/src/daemon/filter-chain/source-rnnoise.conf) | PipeWire's (assumed MIT) | RNNoise filter-chain, VAD 50 % | already what our engine does |
| [dim-geo/micnoisereduction](https://github.com/dim-geo/micnoisereduction) | none stated | WebRTC echo-cancel (all webrtc processing off) → EQ → gate → de-esser → RNNoise; gate attack 3 ms, hold 500 ms, release 50 ms | gate timing values |

jtrv chain (confirmed): RNNoise → DeepFilterNet (atten. 100) → gate (-50 dB, attack
5 ms, release 250 ms, reduction -12 dB) → EQ (HP 80 Hz Q 0.7; -2 dB 220 Hz Q 0.7; -2 dB
350 Hz Q 1.2; +2 dB 3.5 kHz Q 0.9; +2 dB shelf 10 kHz) → compressor (-18 dB, 3:1,
15 / 200 ms, makeup +3 dB) → de-esser → limiter (-1.5 dB, 2 ms lookahead).

## Evidence: enhancement and Whisper accuracy

- [arXiv 2512.17562](https://arxiv.org/abs/2512.17562) (confirmed): MetricGAN+ enhancement
  before Whisper large-v3, Parakeet and others **raised** semantic WER in all 40 tested
  configurations, by 1.1 to 46.6 points. Even clean audio got 1.3 to 3.2 points worse.
- [arXiv 2603.04710](https://arxiv.org/abs/2603.04710) (assumed, summary): SAM-Audio
  enhancement raised PSNR but raised WER for every Whisper size, more for larger models.
- Neither study tested RNNoise or DeepFilterNet, so this is a strong prior, not proof
  for our denoisers. It supports what the app already does: noise reduction off by
  default, and a warning on it.
- No study found that isolates loudness normalisation or 16 kHz resampling for Whisper.
  The accuracy harness (`effects-for-asr.md`) has to answer those itself.
- WebRTC processing (PipeWire `module-echo-cancel`): no source recommends it for ASR.
  Echo cancellation is only worth having when audio plays while you dictate.
- Dereverberation: no open-source LADSPA/LV2 speech dereverb plugin was found. `nara_wpe`
  is offline Python only. Park it.

## Candidate presets (to validate with the harness before shipping)

All of them map onto effect kinds the engine already has (`docs/effects.md`).

1. **ASR Clean** (proposed default for Whisper-family): HP 80 Hz steep → bell -2 dB
   250 Hz Q 0.7 → compressor -18 dB 3:1 15/200 ms → limiter -1.5 dB. No denoiser.
2. **ASR Quiet room, gated**: 1 plus a gate before the compressor, threshold = measured
   noise floor + 8 dB, attack 3–5 ms, hold 500 ms, release 50–250 ms, range -12 dB (not
   a full mute, so word endings survive).
3. **ASR Noisy room**: HP 80 Hz → RNNoise (no VAD) → then as 1. Labelled "may lower
   Whisper accuracy; try it only when the noise is audible in the transcript".
4. **ASR Strong**: HP 80 Hz → DeepFilterNet (atten. limit 30–60 dB) → compressor → limiter.
   Last resort.
5. **Laptop internal mic**: 1 with the Framework EQ moves (-2.5 dB 650 Hz, +1 dB 160 Hz)
   to undo boxiness, as a test candidate.

Not borrowed: de-essers, the 3.5 kHz / 10 kHz boosts, stacked RNNoise + DeepFilterNet.
