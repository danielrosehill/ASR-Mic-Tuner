# Sprints

Planned 2026-11-21 from v2.6. Each sprint is one small release (`v2.7`, `v2.8`, …).
Order matters: identity first, because profiles, sync and the harness all key on it.

## Sprint 1: mic identity and portable profiles (v2.7, shipped 2026-11-21 except the device tests)

- [x] `core.Device` reads `device.vendor.id`, `device.product.id`, `device.serial`,
      `device.bus`, `api.bluez5.address`; for PCI cards also the HDA codec
      (`/proc/asound/card<N>/codec#0`) and DMI machine strings.
- [x] `mic_key` derivation per `explorations/mic-identity.md`; shown in the device picker tooltip.
- [x] Profiles, bindings and measurements store `mic_key` alongside the node name;
      lookups try `mic_key` first.
- [x] Keys backfilled for mics that are present (in `load_config`, `load_settings`, `Session.rescan`, `Store.backfill_keys`, not `migrate.py`).
- [x] Per-mic profile files under `~/.config/asr-mic-tuner/profiles/<mic_key>.json`.
- [x] Tested with the internal mic (`docs/mic-identity.md`).
- [ ] Test with one USB mic and one Bluetooth headset; record the real property values
      in `docs/mic-identity.md` (USB serial and BT address are still assumed).

Done when (not yet verified: needs a USB mic): a USB mic's profile is found after replugging it into a different port and
after its card profile changes from `analog-stereo` to `mono-fallback`.

## Sprint 2: profile sync through a private Git repo (target v2.8)

- [ ] Settings: `profile_repo` path; "Use a Git repo for profiles" in the app.
- [ ] Commit on save, pull on start, push in the background; failures go to the tray only.
- [ ] Refuse or warn on a public repo (checked with `gh` when available).
- [ ] Docs: `docs/profiles.md` (format, what syncs, what stays local).

Done when: a mic tuned on machine A gets its gain applied automatically when plugged
into machine B.

## Sprint 3: ASR Mic gain and format (target v2.9)

- [ ] Fixed ASR Mic gain stage per binding, saved in the profile; slider in ASR Effects.
- [ ] Quick tune sets hardware gain (peaks) then ASR Mic gain (speech loudness).
- [ ] Setting "Match ASR format (16 kHz)" on the ASR Mic node; default off.
- [ ] Verify with `pw-top` that the dictation app receives 16 kHz.

Done when: raising ASR Mic gain changes what the dictation app hears and nothing else.

## Sprint 4: accuracy harness (target v3.0)

- [ ] Record a passage raw; render candidate chains offline; transcribe off-laptop; score WER.
- [ ] Cost estimate and approval before any run beyond a pilot.
- [ ] Results per mic in the Measure tab; evidence written into `docs/effects.md`.
- [ ] Use it to settle: does level matter to Whisper, does 16 kHz matter, does each denoiser help.

## Sprint 5: new effects, chosen by the harness (target v3.1)

- [ ] Echo cancellation via PipeWire's WebRTC module.
- [ ] Dereverberation, if Sprint 4 research finds a backend that fits the rules in `CLAUDE.md`.
- [ ] Situation presets with their WER evidence, starting from the candidates in
      `explorations/preset-sources.md` (ASR Clean, Quiet room gated, Noisy room, Strong, Laptop mic).
