# KDE Plasma integration

Verified 2026-10-22 on Ubuntu 26.04, Plasma 6.6.6, Wayland, PyQt6 6.10.2.

## Why Qt, not GTK

v0.1 was GTK3 (seeded 2026-07-30). It was rewritten for Plasma in v0.2 because:

- `QSystemTrayIcon` speaks StatusNotifierItem natively, so left-click
  (`ActivationReason.Trigger`) can open a real pop-up. Ayatana AppIndicator
  under GTK only offers a menu on click.
- Breeze theming, Plasma notifications through `QSystemTrayIcon.showMessage`,
  and the Wayland app id all come for free.

The toolkit-independent core (`core.py`: ALSA curve and mixer parsing,
devices, restore; `capture.py`; `analysis.py`; `store.py`) was carried over
from v0.1, with `capture.py` moved to numpy.

## App id

`QGuiApplication.setDesktopFileName("asr-mic-tuner")` sets the Wayland `app_id`.
Plasma uses it to match `asr-mic-tuner.desktop` (icon, task grouping), and the KWin
rule below matches on it (`wmclass=asr-mic-tuner`).

## Placing the tray pop-up on Wayland

Two facts about Wayland combine here:

1. A client cannot position its own top-level window; `QWidget.move()` is
   ignored.
2. A StatusNotifierItem does not tell the app where its icon is.
   `QSystemTrayIcon.geometry()` is empty, and `QCursor.pos()` is stale over
   the panel.

So the pop-up is a frameless `Qt.Tool` window titled exactly `ASR Mic Tuner — tray`,
and a **KWin window rule** places it. `asrmictuner/kwin.py` writes the rule with
`kwriteconfig6` into `~/.config/kwinrulesrc`:

```ini
[General]
rules=asr-mic-tuner-tray-popup        # appended to any existing list; count kept in step

[asr-mic-tuner-tray-popup]
Description=ASR Mic Tuner tray pop-up
wmclass=asr-mic-tuner
wmclassmatch=1                   # exact
title=ASR Mic Tuner — tray
titlematch=1                     # exact
position=<x>,<y>                 # bottom-right of the primary screen's available area, 8 px margin
positionrule=3                   # "apply initially": reapplied each time the window maps; still draggable
above=true / aboverule=2         # force
skiptaskbar / skippager / skipswitcher = true, rule 2
```

It then runs `qdbus6 org.kde.KWin /KWin reconfigure`. The rule is installed on
first tray start (setting `pin_popup`, default true) and removed by unticking
it in Settings. `kwriteconfig6` cannot delete a whole group, so removal deletes
each key and takes the group out of `General/rules`.

**Assumed, not confirmed:** this assumes the panel is at the bottom. The
position comes from the primary screen's `availableGeometry()` at install time,
so after a resolution or panel change, toggle the setting off and on to refresh
it. Hiding and re-showing the same `QWidget` on Wayland creates a new surface,
which is why "apply initially" works every time.

**Not yet seen on screen:** the rule was written and the tray started cleanly
on 2026-10-22, but the pop-up's landing position hasn't been visually checked.
Testing was offscreen (`QT_QPA_PLATFORM=offscreen` plus `QWidget.grab()`), to
stay out of the user's workspace.

## The microphone-in-use indicator

Plasma shows a microphone indicator whenever any client has a capture stream
open. So ASR Mic Tuner opens `parec` only while something on screen needs it: the
main window, the tray pop-up, or the opt-in "live level in tray icon".
`Session.acquire/release` reference-counts those users. With the defaults, an
idle tray polls only `amixer` every 4 s, for the gain zone and drift, and
never shows ASR Mic Tuner as recording.

## Single instance

`app.py` listens on a `QLocalServer` named `asr-mic-tuner-<uid>`. A second launch
connects, sends `show` (or `tray`), and exits. That's how the autostart entry
(`~/.config/autostart/asr-mic-tuner.desktop`, `Exec=asr-mic-tuner --tray`) and a launcher
click can coexist.

## Testing without touching the desktop

```bash
QT_QPA_PLATFORM=offscreen XDG_CONFIG_HOME=/tmp/x XDG_DATA_HOME=/tmp/x python3 script.py
```

In the script, build `asrmictuner.app.Controller(app, False)` and save
`window.grab()` per tab. The offscreen platform reports no system tray, so
construct `TrayPopup` directly to render the pop-up. Capture still uses the real
microphone through `parec`.
