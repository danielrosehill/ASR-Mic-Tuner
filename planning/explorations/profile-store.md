# Where profiles live

Status: exploration, 2026-11-21.

Goal: tune a mic once, and have that tuning follow the mic to any of the user's
machines. Today a profile is a row in local `devices.json` / `settings.json` keyed by
node name (see `mic-identity.md`), so it never leaves the machine.

## Stages

1. **Local store, portable format (next).** One JSON file per mic under
   `~/.config/asr-mic-tuner/profiles/<mic_key>.json`: identity, per-port gain profiles
   per place, chain bindings, noise reduction mode, last measurement summary. One file
   per mic means a sync never has to merge two mics' edits. `devices.json` keeps only
   machine-local things (probe curve cache, the remembered gain for restore).
2. **Private Git repo (opt-in).** Settings field `profile_repo` (a local clone path).
   The app commits on every profile save and does `git pull --rebase` / `push` on
   start and on save, quietly; a failure shows in the tray and never blocks dictation.
   The repo must be private: it holds serials and machine model names. The app checks
   `gh repo view --json visibility` when `gh` is present and warns if it is public
   (assumed workable; `gh` may not be installed, then skip the check).
3. **Cloud sync (later).** Same per-mic files, any backend that syncs a folder (a
   synced directory is enough, so no service code of our own is needed first).

## What syncs and what does not

| Syncs | Stays local |
|---|---|
| USB/Bluetooth mic profiles (gain index per port and place, chain, noise mode) | Gain curve probe cache (it is per card, cheap to re-probe) |
| Chains (`chains/*.json`) | Measurement audio samples |
| Internal-mic profiles, keyed to their machine (harmless elsewhere, never matched) | The OpenRouter key and other settings |

Analog gain is a mixer index, and the same USB mic on another machine has the same
mixer, so the index carries over. **Assumed**: verify that the same USB mic exposes the
same ALSA control range on two machines (kernel quirks could differ).

## Conflicts

Last write wins per file, with the losing version kept in Git history. Two machines
editing the same mic at the same minute is unlikely for one user.
