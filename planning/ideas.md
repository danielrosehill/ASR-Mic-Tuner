# Ideas backlog

Dated 2026-11-21 unless noted. Ideas move into `sprints.md` when they are scheduled.

## Open

- **Mic fingerprint** (vendor/product ID, serial, codec, machine) as the key for
  everything. See `explorations/mic-identity.md`. → Sprint 1.
- **Portable per-mic profile files**, then sync via a private Git repo, later any
  synced folder or cloud. See `explorations/profile-store.md`. → Sprints 1–2.
- **Separate ASR Mic gain** from the hardware gain. See `explorations/asr-mic-gain.md`. → Sprint 3.
- **16 kHz ASR Mic option** to match local recognisers. See `explorations/asr-format.md`. → Sprint 3.
- **Accuracy (WER) harness** to choose effects and chains on evidence. See
  `explorations/effects-for-asr.md`. → Sprint 4.
- **Starter presets** borrowed from published mic chains (values only, with attribution),
  validated by the harness. See `explorations/preset-sources.md`. → Sprint 5.
- **Echo cancellation** (PipeWire WebRTC) for dictating while audio plays. → Sprint 5.
- **Dereverberation** for internal laptop mics. Needs a backend search first.
- **Chain in the profile**: a profile carries its whole chain, not just the noise
  mode (named as the next step in `docs/quick-tune.md`).
- **Fleet view**: list every mic known across machines (from the profile repo), with
  when and where each was last tuned.
- **Per-app hint**: detect which app is recording from the raw mic while a dictation
  app is known, and suggest pointing it at ASR Mic instead.

## Parked

- Cloud service of our own for sync: a synced folder or Git repo covers one user; revisit if others ask.

## Rejected

- Output/playback processing, music EQ: out of scope (see `CLAUDE.md`).
- "Nicer sounding" effects (de-esser, exciter, reverb): no ASR benefit.
