"""Profile sync (v2.8): mirror the per-mic profiles and the saved chains to a
folder, and optionally commit and push that folder with Git. See docs/profiles.md.

Settings (settings.json):
    sync_folder   ""            path of the profile folder; "" = sync off
    sync_git      False         the folder is a Git clone: pull before, commit and push after
    sync_auto     True          sync at start, a few seconds after a profile changes, and hourly

The folder:
    <folder>/profiles/<mic key>.json   the same files as ~/.config/asr-mic-tuner/profiles/
    <folder>/chains/<name>.json        the same files as ~/.config/asr-mic-tuner/chains/
    <folder>/README.md                 written once

Three-way per file, against what each side held at the last sync
(~/.config/asr-mic-tuner/sync-state.json, sha256 per relative path):
    changed on one side only  -> copied to the other (a deletion too)
    changed on both           -> the newer wins (profile "updated", else mtime);
                                 the other is kept as <name>.conflict-<host>-<time>.json in the folder
Machine-local state (curve cache, history, settings) never goes in the folder.
"""

import hashlib
import json
import os
import shutil
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from .core import CONFIG_DIR

STATE_FILE = CONFIG_DIR / "sync-state.json"
SUBDIRS = ("profiles", "chains")
DEFAULT_REPO_NAME = "asr-mic-tuner-profiles"

README = """# ASR Mic Tuner profiles

Synced by [ASR Mic Tuner](https://github.com/danielrosehill/ASR-Mic-Tuner): one file per
microphone in `profiles/` (its identity, gain profiles per place, chain per port) and the
saved effects chains in `chains/`. The app reads and writes these; edit them only when it
is closed. Format: `docs/profiles.md` in the app's repository.

Keep this repository **private**: the files hold microphone serials and machine models.
"""


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _when(path):
    """Modification time for the newer-wins rule: a profile's own `updated`,
    else the file's mtime (Git checkouts reset mtimes, so `updated` matters)."""
    try:
        updated = json.loads(path.read_text()).get("updated")
        if updated:
            return datetime.fromisoformat(updated).timestamp()
    except (OSError, ValueError, AttributeError):
        pass
    return path.stat().st_mtime


def _files(base):
    out = {}
    for sub in SUBDIRS:
        d = base / sub
        if d.is_dir():
            for p in d.glob("*.json"):
                if ".conflict-" not in p.name:
                    out[f"{sub}/{p.name}"] = p
    return out


def _merge_profile(rel, a, b):
    """Merge two edited copies of one mic's profile file (format 2): the folder's
    copy, with this computer's buckets taken from the local copy; shared fields
    (identity, chain_bindings, about) from whichever is newer; benchmarks
    from both. None if not mergeable."""
    from .identity import this_machine
    from .profiles import FORMAT, PER_MACHINE
    if not rel.startswith("profiles/"):
        return None
    try:
        mine, theirs = json.loads(a.read_text()), json.loads(b.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    for side in (mine, theirs):
        if int(side.get("format", 1)) < FORMAT:
            if any(side.get(f) for f in PER_MACHINE):
                return None             # old flat profiles: whose computer is unknown
            side["format"] = FORMAT     # only shared fields (a chain binding): nothing to place
    me = this_machine()["id"]
    out = dict(theirs)
    for f in PER_MACHINE + ("machines",):
        bucket = dict(theirs.get(f) or {})
        if (mine.get(f) or {}).get(me) is not None:
            bucket[me] = mine[f][me]
        else:
            bucket.pop(me, None)
        out[f] = bucket
    newer = mine if _when(a) >= _when(b) else theirs
    for f in ("identity", "chain_bindings", "about"):
        if newer.get(f) is not None:
            out[f] = newer[f]
        else:
            out.pop(f, None)
    # Benchmarks (v3.0) are a log: keep both sides' entries, oldest first.
    seen, bench = set(), []
    for e in sorted((mine.get("benchmarks") or []) + (theirs.get("benchmarks") or []), key=lambda e: e.get("at", "")):
        tag = (e.get("at"), e.get("computer_id"))
        if tag not in seen:
            seen.add(tag)
            bench.append(e)
    if bench:
        from .mics import BENCH_KEEP
        out["benchmarks"] = bench[-BENCH_KEEP:]
    out["updated"] = max(mine.get("updated", ""), theirs.get("updated", ""))
    return json.dumps(out, indent=2, sort_keys=True) + "\n"


def _load_state():
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _git(folder, *args, timeout=60):
    proc = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def remote_visibility(folder):
    """'PRIVATE' / 'PUBLIC' / 'INTERNAL' for a GitHub origin (via gh), else None."""
    if not shutil.which("gh"):
        return None
    rc, url = _git(folder, "remote", "get-url", "origin")
    if rc or "github.com" not in url:
        return None
    proc = subprocess.run(["gh", "repo", "view", url, "--json", "visibility", "-q", ".visibility"],
                          capture_output=True, text=True, timeout=30)
    return proc.stdout.strip().upper() or None if proc.returncode == 0 else None


def sync(settings, local=CONFIG_DIR):
    """One sync at a time per machine: the tray's automatic sync and
    `--sync` would otherwise race on the same clone (seen 2026-11-21: the
    second push was rejected with "cannot lock ref")."""
    import fcntl
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_DIR / "sync.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _sync(settings, local)


def _sync(settings, local=CONFIG_DIR):
    """Run one sync. Returns a JSON-able report:
    {"ok", "folder", "pulled": [...], "pushed": [...], "deleted": [...], "conflicts": [...],
     "git": "...", "error": "..."}. Never raises for expected failures."""
    folder = Path(os.path.expanduser(settings.get("sync_folder") or ""))
    report = {"ok": False, "folder": str(folder), "pulled": [], "pushed": [], "deleted": [],
              "conflicts": [], "merged": [], "git": "", "error": "",
              "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if not settings.get("sync_folder"):
        report["error"] = "No profile folder set."
        return report
    use_git = bool(settings.get("sync_git"))
    try:
        folder.mkdir(parents=True, exist_ok=True)
        if use_git:
            if not (folder / ".git").exists():
                report["error"] = f"{folder} is not a Git clone."
                return report
            vis = remote_visibility(folder)
            if vis == "PUBLIC":
                report["error"] = "The profile repository is public. Make it private before syncing."
                return report
            rc, heads = _git(folder, "ls-remote", "--heads", "origin")
            if rc:
                report["error"] = f"cannot reach the repository: {heads[-300:]}"
                return report
            if heads:                       # a brand-new empty repository has nothing to pull
                branch = _git(folder, "rev-parse", "--abbrev-ref", "HEAD")[1]
                remote_branch = branch if f"refs/heads/{branch}" in heads else heads.split("refs/heads/")[1].split()[0]
                rc, out = _git(folder, "pull", "--rebase", "--autostash", "origin", remote_branch)
                if rc:
                    report["error"] = f"git pull failed: {out[-300:]}"
                    return report
        if not (folder / "README.md").exists():
            (folder / "README.md").write_text(README)

        state = _load_state().get(str(folder), {})
        mine, theirs = _files(local), _files(folder)
        new_state = {}
        host = socket.gethostname()
        for rel in sorted(set(mine) | set(theirs) | set(state)):
            a, b = mine.get(rel), theirs.get(rel)
            ha = _sha(a) if a else None
            hb = _sha(b) if b else None
            last = state.get(rel)
            if ha == hb:
                if ha:
                    new_state[rel] = ha
                continue
            a_changed, b_changed = ha != last, hb != last
            if a_changed and not b_changed:
                winner = "local"
            elif b_changed and not a_changed:
                winner = "folder"
            else:                               # both changed (or no history yet)
                merged = _merge_profile(rel, a, b) if a and b else None
                if merged is not None:
                    # Each computer only edits its own bucket (v2.9), so a profile
                    # edited on two computers merges cleanly: no conflict copy.
                    for dest in (folder / rel, local / rel):
                        dest.write_text(merged)
                    report["merged"].append(rel)
                    new_state[rel] = hashlib.sha256(merged.encode()).hexdigest()
                    continue
                if a and b:
                    winner = "local" if _when(a) >= _when(b) else "folder"
                    loser = b if winner == "local" else a
                    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                    keep = folder / rel.replace(".json", f".conflict-{host}-{stamp}.json")
                    shutil.copy2(loser, keep)
                    report["conflicts"].append({"file": rel, "kept": winner, "other_saved_as": str(keep)})
                else:
                    winner = "local" if a else "folder"   # a new file on one side
            if winner == "local":
                dest = folder / rel
                if a:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(a, dest)
                    report["pushed"].append(rel)
                    new_state[rel] = ha
                else:
                    dest.unlink(missing_ok=True)
                    report["deleted"].append(f"folder:{rel}")
            else:
                dest = local / rel
                if b:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(b, dest)
                    report["pulled"].append(rel)
                    new_state[rel] = hb
                else:
                    dest.unlink(missing_ok=True)
                    report["deleted"].append(f"local:{rel}")

        if use_git:
            _git(folder, "add", "-A")
            rc, _ = _git(folder, "diff", "--cached", "--quiet")
            if rc:
                changed = (len(report["pushed"]) + len(report["deleted"]) + len(report["conflicts"])
                           + len(report["merged"]))
                _git(folder, "commit", "-q", "-m", f"Sync from {host}: {changed} file(s)")
            rc, out = _git(folder, "push", "-q", "-u", "origin", "HEAD", timeout=90)
            report["git"] = "pushed" if rc == 0 else f"push failed: {out[-300:]}"
            if rc:
                report["error"] = report["git"]

        all_state = _load_state()
        all_state[str(folder)] = new_state
        STATE_FILE.write_text(json.dumps(all_state, indent=2) + "\n")
        report["ok"] = not report["error"]
    except (OSError, subprocess.SubprocessError) as exc:
        report["error"] = str(exc)
    return report


def create_github_repo(name=DEFAULT_REPO_NAME, folder=None):
    """Create a private GitHub repository with gh and clone it to `folder`
    (default ~/.local/share/asr-mic-tuner/profile-repo). Returns (ok, folder or error)."""
    from .store import DATA_DIR
    if not shutil.which("gh"):
        return False, "The GitHub CLI (gh) is not installed. Install it and run `gh auth login`."
    target = Path(os.path.expanduser(folder)) if folder else DATA_DIR / "profile-repo"
    if target.exists() and any(target.iterdir()):
        return False, f"{target} already exists and is not empty."
    target.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["gh", "repo", "create", name, "--private",
                           "--description", "ASR Mic Tuner microphone profiles (private)"],
                          capture_output=True, text=True, timeout=60)
    if proc.returncode != 0 and "already exists" not in proc.stderr:
        return False, (proc.stderr or proc.stdout).strip()[-300:]
    proc = subprocess.run(["gh", "repo", "clone", name, str(target)], capture_output=True, text=True, timeout=90)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()[-300:]
    return True, str(target)
