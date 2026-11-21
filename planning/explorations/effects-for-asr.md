# Effects for recognition accuracy

Status: exploration, 2026-11-21.

Already shipped (v2.6, `docs/effects.md`): mono fold, high/low-pass, hum notch, EQ
band, auto gain, noise gate, compressor, limiter, RNNoise, RNNoise VAD gate,
DeepFilterNet, the quick noise reduction switch.

The rule for adding anything: **it must improve word error rate, measured**, not
just sound cleaner. Whisper-family models are trained on noisy audio and often do
worse on denoised or heavily processed audio. So the first item is a way to measure.

## 1. Accuracy harness (prerequisite)

- Record each read passage (`passages.py` already has reading passages with known
  text) once, raw, per mic and place.
- Render each candidate chain offline over the same recording (`pw-cat` through a
  filter-chain, or the same LADSPA plugins in Python).
- Transcribe each render and score WER against the passage text. Transcription runs
  on a hosted API or a remote GPU, not on the laptop (heavy jobs stay off it). Small
  passages keep the cost to cents.
- Output: a JSON table per mic, chain → WER, shown in the Measure tab as "best chain
  for this mic here".

This also answers the open questions in `asr-mic-gain.md` and `asr-format.md`.

## 2. Candidate effects

| Effect | Helps when | Backend (in scope per CLAUDE.md) | Confidence it helps ASR |
|---|---|---|---|
| Echo cancellation | the computer plays audio (a video, a call) while you dictate | PipeWire `module-echo-cancel` with the WebRTC backend (ships with PipeWire) | high, when there is playback |
| WebRTC noise suppression + high-pass | steady noise, lighter on CPU than DeepFilterNet | same module (`webrtc.noise_suppression`) | medium |
| Dereverberation | a bare room, mic far from the mouth (laptop internal mic) | candidates to evaluate: WebRTC has none; DeepFilterNet 3 models do some; a dedicated LADSPA/LV2 dereverb plugin needs research | medium-high for internal mics, if a usable plugin exists |
| Mic correction EQ from measurement | a mic with a strong tilt or proximity-effect boom | builtin `bq_*` filters, curve derived from the Measure tab's spectrum | low-medium |
| De-click (keyboard) | typing while dictating | research needed; RNNoise partly covers it | unknown |
| Soft clipper ahead of the limiter | occasional shouts | builtin or swh | low (clipping is mostly a gain-setting problem, already handled) |

Rejected for now: de-esser, exciter, reverb, pitch or "voice enhancement" effects.
They make speech sound nicer to a person and give a recogniser nothing.

## 3. Presets by situation

Once the harness exists, ship a small set of tested chains: "Quiet room", "Laptop
mic, echoey room", "Noisy household", "Playback while dictating" (echo cancel). Each
records the WER evidence it was chosen on in `docs/effects.md`.
