# Profile sync (v2.8, 2026-11-21)

Your microphone profiles and saved chains can live in a folder that other machines
see too: a synced folder (Syncthing, Nextcloud, Dropbox…) or a clone of a **private**
Git repository that the app commits to and pushes for you. Code: `asrmictuner/sync.py`
(the sync), `asrmictuner/syncui.py` (runs it in the app), Settings → **Profile sync**
(`pages.SettingsPage._sync_box`), `asr-mic-tuner --sync` (headless, prints JSON).

## Set up

- **A folder:** Settings → Profile sync → Choose… Any folder works.
- **GitHub, created for you:** "Create a private GitHub repo…" runs
  `gh repo create <name> --private` and clones it to
  `~/.local/share/asr-mic-tuner/profile-repo`. This needs the GitHub CLI signed in
  (`gh auth login`). Then it turns on "Commit and push with Git" and syncs.
- **GitHub, a repo you already have:** clone it anywhere, choose that folder and tick
  "Commit and push with Git". The app pushes with your normal Git credentials.

On a second machine: clone the same repo (or point at the same synced folder), choose
it, and press Sync now. The mic profiles arrive and apply the next time that mic is the
default. A USB mic's profile carries over to any machine because its key is its
hardware (`docs/mic-identity.md`). A built-in mic's profile is tied to its machine, so
it syncs but only matches there.

## What is in the folder

```
<folder>/profiles/<mic key>.json   one per mic, the same file as ~/.config/asr-mic-tuner/profiles/
<folder>/chains/<name>.json        saved ASR chains, the same as ~/.config/asr-mic-tuner/chains/
<folder>/README.md                 written once
```

Never synced, because it is machine-local: the gain-curve probe cache (`devices.json`),
measurements, recordings and room profiles (`history.db`, `samples/`), and settings
(`settings.json`, which holds the OpenRouter key). The profile files do contain mic serials
and machine model names, which is why a GitHub repo must be private. The sync **refuses to
run** if `gh repo view` reports the origin as public.

## How a sync decides

The sync is three-way per file. It compares each side against its content at the last sync
(sha256 per path in `~/.config/asr-mic-tuner/sync-state.json`, recorded per folder):

| Local | Folder | Result |
|---|---|---|
| changed | unchanged | local copied to the folder (a deletion is copied too) |
| unchanged | changed | folder copied to local (a deletion too), and the app reloads its profiles |
| changed | changed (a mic's profile file, v2.9) | merged: each computer's own bucket from that computer's copy, shared fields from the newer copy (`sync._merge_profile`) |
| changed | changed (anything else) | the newer one wins. Profiles are compared by their `updated` field, chains by file time. The other copy is kept in the folder as `<name>.conflict-<host>-<time>.json` |

With Git, each sync runs these steps:
1. `git ls-remote --heads origin`. A brand-new empty repo is fine.
2. `git pull --rebase --autostash origin <branch>`.
3. The file merge above.
4. `git add -A`, then a commit "Sync from <host>: N file(s)" if anything changed.
5. `git push -u origin HEAD`.

When it runs (setting `sync_auto`, on by default): 3 s after the app starts, 8 s after a
profile, gain or binding changes (debounced), every hour, and on **Sync now**.

Failures and conflicts show as a tray notification. The Settings tab shows the last result.
A failed sync never touches the mic or ASR Mic.

## Tested (2026-11-21)

- Two sandboxed configs (`XDG_CONFIG_HOME`) shared a plain folder:
  - The first sync copies everything out and the second copies it in.
  - An edit and a chain deletion on one side reached the other.
  - Edits on both sides kept the newer one and saved the other as a conflict file.
- The same configs synced through a local bare Git repo, starting empty.
- The app, run offscreen, synced at start and showed "Synced …, pushed".
- The laptop and the desktop then synced through a real private GitHub repo.
- 2026-11-21, v2.9: the tray's start-up sync and `--sync` ran at once on one clone, and the second push was rejected ("cannot lock ref"). Fixed with an exclusive lock, `~/.config/asr-mic-tuner/sync.lock` (`sync.sync`).
