# Should ASR Mic have its own gain?

Status: exploration, 2026-11-21. Recommendation: **yes**.

## Background (confirmed, v2.6)

The virtual source already exists: **ASR Mic** (`docs/asr-mic.md`). The raw mic stays
the system default, untouched, and only the dictation app records from ASR Mic. So
the "raw mic for other apps, processed mic for dictation" design is in place.

What is not separate yet is gain. There is one analog gain, on the hardware, and it
feeds both the raw mic and ASR Mic. Auto gain inside the chain exists (`autogain`
effect) but is off by default since v2.5.

## Proposal: two gain stages, each with one job

| Stage | Where | Job | Set by |
|---|---|---|---|
| Hardware gain | ALSA mixer (as today) | best signal-to-noise without clipping: loud enough to clear the converter's noise, with headroom for laughs and plosives | Quick tune / Measure, per mic and port |
| ASR Mic gain | a fixed `linear` gain at the end of the chain, before the limiter | bring speech to the level the recogniser likes, without touching what calls hear | per binding (mic + port), saved in the profile |

Why split them: the hardware gain that suits a call (calls apps run their own AGC)
and the level that suits Whisper are different targets. With only one knob, raising
it for dictation also raises it for calls and recordings. A digital stage after the
hardware cannot add signal-to-noise, so the hardware stage still has to be right;
the ASR Mic stage only shifts level.

Default ASR Mic gain: 0 dB. Quick tune would set the hardware gain for peaks around
-10 dBFS (its v2.6 target) and then set the ASR Mic gain so speech loudness lands at
a recogniser target (start at about -20 LUFS, to be checked by the accuracy harness
in `effects-for-asr.md`).

## Open questions

- Does Whisper care about absolute level at all? Its log-mel front end normalises
  per clip, so level mostly matters for VAD and for quiet trailing words. Measure it
  before making this a headline feature.
- Fixed gain vs the existing auto gain: keep both. Fixed is the default, auto stays opt-in.
