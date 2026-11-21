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
      in `docs/mic-identity.md`. USB done 2026-11-21 (Boya BY-CM1, Samson Q2U); Bluetooth still assumed.

Done when (not yet verified: needs a USB mic): a USB mic's profile is found after replugging it into a different port and
after its card profile changes from `analog-stereo` to `mono-fallback`.

## Sprint 2: profile sync through a private Git repo (v2.8, shipped 2026-11-21)

- [x] Settings: `sync_folder`, `sync_git`, `sync_auto`; Settings → Profile sync (any folder, or a Git clone).
- [x] "Create a private GitHub repo…" (via `gh`), then sync.
- [x] Pull, three-way merge per file, commit, push; at start, after changes, hourly, Sync now;
      failures and conflicts go to the tray only.
- [x] Refuses to sync to a public GitHub repo.
- [x] Docs: `docs/profiles.md`.
- [x] Done when: the laptop and the desktop share profiles through one private repo
      (2026-11-21, see `docs/profiles.md` → Tested).
- [ ] A USB mic tuned on one machine applied automatically on the other: needs the mic moved.

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
