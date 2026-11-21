# Sample format for local ASR

Status: exploration, 2026-11-21.

Note on terms: uncompressed microphone audio has no "bitrate" setting as such. What
can be matched is **sample rate**, **channels** and **sample format** (bit depth).

## What recognisers want (assumed from their docs and source, verify per app)

| Recogniser | Wants | What happens otherwise |
|---|---|---|
| Whisper (whisper.cpp, faster-whisper, OpenAI API) | 16 kHz mono, float or s16 | the app or library resamples |
| Vosk / Kaldi models | usually 16 kHz mono s16 (some 8 kHz) | the app resamples, or recognition degrades if it doesn't |
| Cloud APIs (Google, Deepgram, Azure…) | accept 16–48 kHz; 16 kHz is the usual recommendation | server resamples |
| Parakeet / NeMo, Moonshine | 16 kHz mono | the app resamples |

## Today (confirmed, v2.6)

ASR Mic is mono, 48 kHz. 48 kHz is required inside the chain because RNNoise and
DeepFilterNet only run at 48 kHz.

## Options

1. **Leave 48 kHz and let the app resample** (today). Fine when the app uses a good
   resampler. Risk: some apps decimate naively, which aliases sibilants into the speech band.
2. **Offer a 16 kHz ASR Mic** (proposal). The chain stays at 48 kHz; the published
   source node gets `audio.rate = 16000`, so PipeWire's own resampler (good quality)
   does the conversion with a proper anti-alias filter, and the app receives exactly
   what the model eats. A low-pass at about 7.5 kHz before the resample is implied by
   the resampler; an explicit `lowpass` in the chain is not needed.
3. Bit depth: keep float32 end to end. s16 has more than enough dynamic range for
   speech, so there is nothing to gain by changing it; the app picks its format at
   negotiation anyway.

Mono is already done (mono fold is always first).

## How to verify

- `pw-top` while the dictation app records from ASR Mic: shows the negotiated rate
  and format per node.
- Compare WER with ASR Mic at 48 kHz vs 16 kHz on the same recorded passages (harness
  in `effects-for-asr.md`). Expected: no change with a good app, an improvement with
  an app that resamples badly. Ship option 2 as a setting ("Match ASR format: 16 kHz"),
  default off until the test says otherwise.
