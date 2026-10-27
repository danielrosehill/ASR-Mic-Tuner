# ASR Mic Tuner

## Scope (read first)

**The only scope of this project is input tuning for speech recognition**: getting a
microphone's signal into the best shape for ASR, whether the recogniser is local
(Whisper.cpp, faster-whisper, Vosk…) or remote (cloud speech APIs). It is the one place
to check levels, measure the mic, set gain, and apply an input-side processing chain.

In scope:
- Analog gain (and software volume where no analog control exists), levels, clipping.
- Measuring a mic and the room on-device (Measure tab, `analysis.py`) and recommending
  gain changes, with a one-click apply that is remembered **per microphone**.
- Watching levels while another app dictates (quiet-mic alert, `watch.py`).
- Input-side processing chains run by our own engine inside PipeWire (ASR Effects
  tab, `engine.py`, `fxpage.py`), bound per microphone and port, published as the
  **ASR Mic** source (`asrmic.py`) while the raw mic stays the default. No external
  effects application is used or mentioned. Plugins come from PipeWire itself, the
  distro archive (swh-plugins), or the maintainers' releases (RNNoise, DeepFilterNet),
  pinned by SHA-256.

Out of scope (decline or point elsewhere): output/playback audio, speakers, music EQ,
transcription itself, routing beyond ASR Mic, recording as a general recorder, and any
system-wide change (nothing in `/etc`; user units and user config only).

Everything recommended or measured is tied to a device (`device` name, plus port for
chain bindings), because each microphone behaves differently.

## Releases

Release tier: small

Releases are tagged `v<major>.<one digit>` (`v1.0`, `v1.1` … `v1.9`, then `v2.0`); the
Debian version is the same number without the `v`. Releases up to `v0.4.0` predate the
scheme and are not retagged. Version lives in `asrmictuner/__init__.py`; `./build-deb.sh`
reads it.

## Docs

`docs/spec.md` (design and the processing boundary, §3 and §14), `docs/effects.md`
(every effect, its backend and test results), `docs/asr-mic.md` (the ASR Mic source),
`docs/analysis.md` (every metric and threshold).
