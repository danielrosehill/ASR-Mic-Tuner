# Planning

Public on purpose. The maintainer works from several machines, and this is where they
keep ideas, explorations and sprint plans. Nothing in here is shipped behaviour. What
has shipped is in `docs/` (start with `docs/spec.md`).

Rules for this folder (it is public):
- Write about hardware generically ("a USB condenser mic", "the laptop's internal
  mic"). No hostnames, serial numbers, MAC addresses, account names or personal paths.
- Date entries absolutely (YYYY-MM-DD). Mark each claim as **confirmed** (checked on a
  machine or in code) or **assumed** (expected, not yet verified).
- When an idea ships, move its outcome to `docs/` in the release commit and mark the
  sprint item done here with the version number.

| File | What |
|---|---|
| `sprints.md` | Ordered sprints: goal, items, done-when |
| `ideas.md` | Backlog of ideas not yet in a sprint, plus ones parked or rejected, with the reason |
| `explorations/mic-identity.md` | Identifying a mic reliably across ports, reboots and machines (vendor/product IDs, codec, machine) |
| `explorations/profile-store.md` | Where profiles live: local store now, private Git repo next, cloud sync later |
| `explorations/asr-mic-gain.md` | Should ASR Mic have its own gain, separate from the hardware gain? |
| `explorations/asr-format.md` | Sample rate, channels and bit depth for local ASR |
| `explorations/preset-sources.md` | Published mic/voice chains and papers on enhancement and Whisper accuracy: what to borrow as presets |
| `explorations/effects-for-asr.md` | Effects worth adding for recognition accuracy, and how to prove they help |

Started 2026-11-21, at v2.6.
