"""KWin window rule that places the tray pop-up at the panel corner.

On Plasma Wayland a client cannot position its own top-level window, and a
StatusNotifierItem does not tell the app where its icon is. A KWin rule
matching the pop-up's app id and title is the native way to place it; the
rule is written with kwriteconfig6 into ~/.config/kwinrulesrc under its own
group and removed cleanly when the option is turned off. KWin is told to
reload over D-Bus.
"""

import shutil
import subprocess

RULE_GROUP = "asr-mic-tuner-tray-popup"
POPUP_TITLE = "ASR Mic Tuner — tray"
MARGIN = 8


def _read(group, key):
    out = subprocess.run(["kreadconfig6", "--file", "kwinrulesrc", "--group", group, "--key", key],
                         capture_output=True, text=True)
    return out.stdout.strip()


def _write(group, key, value, kind=None):
    cmd = ["kwriteconfig6", "--file", "kwinrulesrc", "--group", group, "--key", key]
    if kind:
        cmd += ["--type", kind]
    subprocess.run(cmd + [str(value)], capture_output=True)


def _delete(group, key):
    subprocess.run(["kwriteconfig6", "--file", "kwinrulesrc", "--group", group, "--key", key,
                    "--delete"], capture_output=True)


def _reconfigure():
    for tool in ("qdbus6", "qdbus"):
        if shutil.which(tool):
            subprocess.run([tool, "org.kde.KWin", "/KWin", "reconfigure"], capture_output=True)
            return


def available():
    return bool(shutil.which("kwriteconfig6") and shutil.which("kreadconfig6"))


def rule_installed():
    if not available():
        return False
    return RULE_GROUP in [r for r in _read("General", "rules").split(",") if r]


def install_rule(width=400, height=360):
    """Write (or refresh) the rule for the current primary screen geometry."""
    if not available():
        return False, "kwriteconfig6 not found — not a Plasma session?"
    from PyQt6.QtGui import QGuiApplication
    screen = QGuiApplication.primaryScreen()
    geo = screen.availableGeometry() if screen else None
    if geo is None:
        return False, "No screen geometry available."
    x = geo.x() + geo.width() - width - MARGIN
    y = geo.y() + geo.height() - height - MARGIN
    g = RULE_GROUP
    _write(g, "Description", "ASR Mic Tuner tray pop-up")
    _write(g, "wmclass", "asr-mic-tuner")
    _write(g, "wmclassmatch", 1)
    _write(g, "wmclasscomplete", "false", "bool")
    _write(g, "title", POPUP_TITLE)
    _write(g, "titlematch", 1)
    _write(g, "position", f"{x},{y}")
    _write(g, "positionrule", 3)            # apply initially: still movable by hand
    _write(g, "above", "true", "bool")
    _write(g, "aboverule", 2)
    for key in ("skiptaskbar", "skippager", "skipswitcher"):
        _write(g, key, "true", "bool")
        _write(g, f"{key}rule", 2)
    rules = [r for r in _read("General", "rules").split(",") if r]
    if g not in rules:
        rules.append(g)
    _write("General", "rules", ",".join(rules))
    _write("General", "count", len(rules))
    _reconfigure()
    return True, f"KWin rule installed: pop-up opens at {x},{y}."


def remove_rule(group=RULE_GROUP):
    if not available():
        return True, "No KWin rule to remove."
    rules = [r for r in _read("General", "rules").split(",") if r and r != group]
    _write("General", "rules", ",".join(rules))
    _write("General", "count", len(rules))
    _delete(group, "Description")
    # kwriteconfig6 cannot drop a whole group; clear every key we wrote.
    for key in ("wmclass", "wmclassmatch", "wmclasscomplete", "title", "titlematch",
                "position", "positionrule", "above", "aboverule", "skiptaskbar",
                "skiptaskbarrule", "skippager", "skippagerrule", "skipswitcher",
                "skipswitcherrule"):
        _delete(group, key)
    _reconfigure()
    return True, "KWin rule removed."
