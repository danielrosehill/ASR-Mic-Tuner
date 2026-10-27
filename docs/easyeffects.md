# EasyEffects integration (ASR Effects tab)

The ASR Effects tab edits **EasyEffects' own files** and calls **its command
line**. ASR Mic Tuner still creates no node and processes no audio
(`docs/spec.md` §3, as amended in §11). Code: `asrmictuner/effects.py`
(files and CLI) and `asrmictuner/fxpage.py` (the tab).

Everything below was read from the EasyEffects source at tag **v8.3.0**
(`github.com/wwmm/easyeffects`) and checked against the Flatpak
`com.github.wwmm.easyeffects` 8.3.0 on the reference laptop on 2026-10-27.
Older 7.x releases differ (presets lived under the config dir; the code falls
back to it when the data dir has no `input/`), and 7.x was not tested.

## Where things live

| Install | Root (`<data>`) |
|---|---|
| Flatpak | `~/.var/app/com.github.wwmm.easyeffects/data/easyeffects` |
| Native | `$XDG_DATA_HOME/easyeffects` (`~/.local/share/easyeffects`) |

EasyEffects resolves this with `QStandardPaths::AppDataLocation`
(`src/presets_directory_manager.cpp`). On start, 8.x tries to migrate the
old `~/.config/easyeffects/{input,output,irs,rnnoise,autoload}` dirs and logs
"Could not copy any file. Aborting migration" when they are empty. That is
harmless noise.

## Preset files

`<data>/input/<name>.json`:

```json
{
    "input": {
        "blocklist": [],
        "plugins_order": ["filter#0", "deepfilternet#0", "autogain#0", "limiter#0"],
        "filter#0": {"bypass": false, "type": "High-pass", "frequency": 80.0, "slope": "x2"},
        "deepfilternet#0": {"bypass": false, "attenuation-limit": 30.0},
        "autogain#0": {"bypass": false, "target": -20.0, "silence-threshold": -60.0, "maximum-history": 15},
        "limiter#0": {"bypass": false, "threshold": -3.0}
    }
}
```

- **Missing keys load as the plugin's default.** `UPDATE_PROPERTY` in
  `src/presets_macros.hpp` calls `.value(key, default##Property##Value())`.
  So presets written here carry only the settings the tab shows, and a full
  preset saved from EasyEffects keeps all its keys when re-saved here: the
  tab edits the existing section in place.
- **Enum-like settings are label strings**, not indices
  (`UPDATE_ENUM_LIKE_PROPERTY`): filter `type` (`"High-pass"`), `slope`
  (`"x1"`…`"x16"`), autogain `reference` (`"Geometric Mean (MSI)"`),
  stereo tools `mode` (`"LR > L+R (Mono Sum L+R)"`). An unknown label is
  ignored silently, and the previous value stays.
- Speex keeps its VAD in a subsection: `"vad": {"enable": …,
  "probability-start": …, "probability-continue": …}`.
- Key names, ranges and defaults per plugin are in
  `src/<plugin>_preset.cpp` and `src/contents/kcfg/easyeffects_db_<plugin>.kcfg`.
  The `EFFECTS` table in `effects.py` copies the ones the tab exposes.
- Plugins the tab does not know (an equaliser, say) are kept, shown as
  "edit in EasyEffects", and written back unchanged.

## Autoload (microphone bindings)

`<data>/autoload/input/<device>:<port description>.json`, with any `/` in the
port description written as `_` in the file name
(`src/presets_autoload_manager.cpp`):

```json
{
    "device": "alsa_input.pci-0000_04_00.6.analog-stereo",
    "device-description": "Ryzen HD Audio Controller Analog Stereo",
    "device-profile": "Internal Microphone",
    "preset-name": "Dictation"
}
```

`device-profile` is the **active port's description** from `pactl list
sources` (`Internal Microphone`), not its name (`analog-input-internal-mic`).
A file from an older EasyEffects using the port name was found in the Trash on
the reference laptop. `Device.port` in `core.py` holds the description.

A binding whose preset no longer exists loads nothing. The tab marks it
"missing". On 2026-10-27 the reference laptop had exactly that: an
`Internal Microphone` binding to `Voice`, a preset that had been moved to the Trash.

## Command line

The Flatpak runs as `flatpak run com.github.wwmm.easyeffects …`. Each call
spawns a sandbox (about 1 s), so the tab runs them off the GUI thread.

| Call | Behaviour (8.3.0) |
|---|---|
| `-l NAME` | Loads the **input** preset `NAME` if one exists, else the output one (`command_line_parser.cpp`). Needs a running instance. |
| `-a input` | Prints the last loaded input preset on stdout. |
| `-b 1` / `-b 2` / `-b 3` | Global bypass on / off / print state. |
| `--service-mode -w` | Starts the background service with no window. |
| `-p` | Lists presets (not used: the tab reads the directory). |

**Answers go to stdout and logs go to stderr.** Every call prints the
migration log lines above to stderr, so `effects.py` parses stdout only.

Verified on 2026-10-27: a preset written by `effects.py` (the "Noisy room"
template) loaded with `-l`. The log showed `Successfully loaded the preset`,
and `pw-link -l` showed the chain
`alsa_input… → ee_sie_filter → ee_sie_gate → ee_sie_deepfilternet →
ee_sie_autogain → ee_sie_limiter → … → easyeffects_source`.

DeepFilterNet logged two `Underrun detected (RTF: 1.64)` warnings when the
chain first started on the reference laptop (Ryzen, ALC257), then none in the
following ~30 s. Whether it keeps up during long dictation was not tested. If
it doesn't, the RNNoise templates are the lighter option.

## How the quiet-mic alert sees EasyEffects

EasyEffects' filter nodes do **not** show up as PulseAudio source-outputs, so
they never count as "someone is recording". A dictation app recording from
`easyeffects_source` does count. `resolve_hardware_for()` walks `pw-link -l`
upstream through the `ee_sie_*` nodes to the `alsa_input.*` behind them. This
walk was verified with EasyEffects running: a test `parec` on
`easyeffects_source` was attributed to the internal mic and not to a
different mic.
