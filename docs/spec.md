# ASR Mic Tuner — specification

Status: **v0.2.0 implemented** (2026-10-22). Seeded 2026-07-30; v0.1 was a
GTK3 single window. v0.2 is a Qt 6 rewrite for KDE Plasma with a tray, sample
measurement, history, profiles and AI review. §1–§6 remain the core
contract; §10 covers what v0.2 added. Installed from a `.deb`.

---

## 1. Purpose

One window that tells the truth about a capture device:

- the **real analog gain** in dB, read from the ALSA hardware control;
- the **signal level** in dBFS, with peak hold and a target band;
- whether the device is **clipping**, counted in samples rather than guessed
  from a peak indicator.

And it lets you set the gain **in dB**, not as a percentage of an opaque curve.

## 2. Why this needs to exist

Every existing GUI on Ubuntu misreports microphone gain, because
PipeWire/PulseAudio volume is not the analog gain and the mapping between them
is neither linear nor lossless.

Measured 2026-07-30 on `daniellaptop` (Realtek ALC257, `alsa_input.pci-0000_04_00.6.analog-stereo`),
both readings taken at the same instant:

| Source | Reading |
|---|---|
| `pactl` / pavucontrol / GNOME Sound | **−55.25 dB** |
| ALSA `Capture` control (the actual preamp) | **+4.50 dB** |

A 60 dB discrepancy. The full useful analog range is compressed into
`pactl` 5%–35%, and everything above ~35% is pinned at maximum:

| pactl volume | analog gain | index |
|---|---|---|
| 5% | −17.25 dB | 0/63 |
| 8% | −6.00 dB | 15/63 |
| 10% | 0.00 dB | 23/63 |
| 12% | +4.50 dB | 29/63 |
| 15% | +10.50 dB | 37/63 |
| 20% | +18.00 dB | 47/63 |
| 30% | +28.50 dB | 61/63 |
| 35% and above | +30.00 dB (pinned) | 63/63 |

The consequence is that a slider sitting a third of the way along — which
looks conservative — is in fact maximum analog boost. Room noise then reaches
−11 dBFS peak and speech saturates the ADC. **Clipping happens before the
digital domain, so no filter, AGC or noise suppressor downstream can repair
it**; recognition accuracy collapses while the mic still appears to work.

This is not hypothetical: it is the failure mode that made dictation unusable
on the reference machine, and it went undiagnosed for months precisely because
the available meters looked fine.

## 3. Non-goals

- **The raw microphone is never processed.** ASR Mic Tuner observes it and
  sets its gain. Since v2.0 it can also publish a *separate* input, ASR Mic,
  that carries the mic through an input chain (§14). That is opt-in, and the
  raw mic stays the default and stays untouched. With ASR Mic off, the
  audio graph is byte-identical before and after running the app.
- Not an equaliser and not a routing tool (`helvum`/`qpwgraph` already do
  routing). It records **only on explicit request** (Measure tab, `--measure`),
  for analysis and for noise profiles; it is not a general recorder.
- No system configuration files installed. Nothing in `/etc`.

The sibling project `dictamic` took the opposite approach — inserting a WebRTC
AGC/noise-suppression/AEC chain as a virtual source — and measurement showed
raw audio at correct analog gain had a **9 dB lower noise floor than the full
processing chain achieved** (−56.4 vs −46.9 dBFS). The processing was solving
a problem that correct gain solves better. ASR Mic Tuner exists to make correct
gain easy to see and set, which is the part that actually works.

## 4. Core mechanism

### 4.1 Reading the gain curve (non-invasive)

```
$ amixer -c 1 cget name='Capture Volume'
numid=6,iface=MIXER,name='Capture Volume'
  ; type=INTEGER,access=rw---R--,values=2,min=0,max=63,step=0
  : values=29,29
  | dBscale-min=-17.25dB,step=0.75dB,mute=0
```

From this single command, with no writes:

```
dB(index) = dBscale_min + step_dB × index
```

Verified against measurement: index 23 → 0.00 dB, index 29 → +4.50 dB,
index 63 → +30.00 dB. All three match `amixer sget` exactly.

**Requirement: derive the curve per device at runtime. Never hard-code a
percentage or a dB figure.** Hard-coding "35% is the sweet spot" from one
laptop is exactly the bug this project exists to correct, and the same number
is wrong on the very machine it was measured on.

Not all cards expose a `dBscale` TLV. When absent, fall back to probing
min/mid/max (set, read, restore original) once at first run, cache the result
in `~/.config/asr-mic-tuner/devices.json`, and label the curve as *probed* rather
than *declared* in the UI.

### 4.2 Setting the gain

```
amixer -c <card> cset numid=<numid> <index>,<index>
```

Set by index; display in dB. Never route gain changes through `pactl`, whose
mapping is the problem being solved.

### 4.3 Capturing audio for the meter

```
parec --device=<source> --format=s16le --rate=48000 --channels=<native>
```

- Read the device's **native channel count** and analyse channels
  **independently**. Do not downmix: averaging two channels hides clipping
  that affects only one, and on this hardware the two internal-mic channels do
  not behave identically.
- `parec` subprocess rather than a Python audio binding, to keep the
  dependency set to what is already required for a GTK app.
- Capture from the **hardware** device by default, not from the default
  source, since the default source may be a processed virtual node.

## 5. UI

v0.1 had a single window with four panes. In v0.2 the four panes are the
**Monitor** tab, which still shows all four facts at once; the other tabs are
in §10.

### 5.1 Gain pane

- Slider bound to the ALSA control **index**, labelled in dB.
- Current value large and unambiguous: `+4.50 dB (index 29 / 63)`.
- Curve endpoints shown: `range −17.25 … +30.00 dB, 0.75 dB per step`.
- Zones shaded on the slider track:
  - **safe**: 0 dB to +6 dB
  - **caution**: +6 dB to +15 dB
  - **danger**: above +15 dB
  These are derived from the measured curve, not fixed percentages.
- `Mic Boost` / `Internal Mic Boost` controls shown read-only if present, with
  a warning if either is non-zero — they stack on top of `Capture` and are a
  second, independent way to reach clipping.
- Alongside, deliberately: **"PipeWire reports: 12% / −55.25 dB"**, explicitly
  labelled *not the analog gain*. Showing the discrepancy teaches the trap
  instead of hiding it, and lets the user correlate what ASR Mic Tuner says with
  what every other tool says.

### 5.2 Level pane

- Vertical or horizontal dBFS meter, log scale, −60 → 0 dBFS.
- Instantaneous RMS bar plus **peak-hold marker**, decay ≈ 1.5 s.
- Target band shaded: **peaks −18 to −10 dBFS**.
- Red above −3 dBFS.
- One meter **per channel**, side by side.
- Numeric readout: current RMS, held peak, rolling noise-floor estimate
  (10th percentile of RMS over the last 10 s).

### 5.3 Clipping pane

A peak indicator lighting up is a threshold check and misses brief saturation
that still wrecks recognition. Count it instead:

- **Clipped samples**: `|s| >= 32764` for s16 (small margin absorbs resampler
  rounding). Per channel.
- **Clipped runs**: ≥ 3 consecutive clipped samples. Isolated full-scale
  samples occur naturally; runs mean genuine saturation. Report count and
  longest run.
- Time since last clip; total monitoring duration; reset button.

### 5.4 Calibration assistant

The feature that makes this worth building — it automates what currently takes
a session of manual `parecord` and `sox` work:

1. **"Stay quiet"**, 3 s → measure noise floor (RMS and peak).
2. **"Speak normally, at your usual distance"**, 8 s → measure speech RMS and peak.
3. Compute the gain-index delta that would place speech peaks at **−12 dBFS**
   (**−18 dBFS** with Conservative gain on, §15).
4. Validate the result: require noise floor ≤ −50 dBFS **and** headroom ≥ 12 dB.
   If speech and noise are too close, the problem is the room or the mic, not
   the gain — say so rather than recommending a number.
5. Show recommendation as before/after (dB, index, predicted peak), apply on
   one click, and re-measure to confirm.

### 5.5 Device selection

- Enumerate capture sources; mark hardware (`alsa_input.*`) distinctly from
  virtual/processed nodes.
- Analog gain exists **only** on hardware devices. If the user selects a
  virtual source (e.g. a noise-suppression node or ASR Mic), show
  *"processed stream — no analog gain"*, resolve the underlying hardware
  device by walking `pw-link` and offer to switch to it.
- Warn when the **system default source is a virtual node** while the user is
  tuning a hardware device, since their dictation app may not be recording
  what they are looking at.

## 6. Persistence

Gain does not reliably stay put: wireplumber restores its own saved per-route
volume when a device becomes default or a route changes, which was observed
resetting the analog gain to maximum twice in one session on the reference
machine.

- Offer an explicit **"remember this gain"** that writes the chosen index to
  `~/.config/asr-mic-tuner/devices.json`.
- Optional user-level systemd unit (`asr-mic-tuner-restore.service`,
  `WantedBy=default.target`) that reapplies the saved index after
  wireplumber settles. Off by default; the GUI enables it.
- Detect and surface drift: if the current index differs from the remembered
  one, say so in the gain pane rather than silently correcting.

## 7. Packaging

House convention, matching `dictamic`: a self-contained `build-deb.sh` that
assembles a tree and calls `dpkg-deb --build --root-owner-group`.

```
./build-deb.sh 0.1.0     # → dist/asr-mic-tuner_0.1.0_all.deb
```

Output goes to **`dist/`, not `build/`** — `build/` is excluded by the global
gitignore, so artefacts placed there cannot be committed.

| Path | Content |
|---|---|
| `/usr/bin/asr-mic-tuner` | the app (mode 755) |
| `/usr/share/applications/asr-mic-tuner.desktop` | menu entry |
| `/usr/share/doc/ASR Mic Tuner/` | README, spec |

- `Architecture: all`, `Section: sound`
- `Depends: python3, python3-gi, gir1.2-gtk-3.0, pulseaudio-utils, alsa-utils`
- **No `conffiles`** and no `postinst` logic — ASR Mic Tuner installs nothing into
  `/etc` and requires no PipeWire restart. Deliberate contrast with dictamic.

v0.2 layout: the Python package goes in `/usr/lib/asr-mic-tuner/asrmictuner/`, with a
wrapper at `/usr/bin/asr-mic-tuner`. `Depends: python3, python3-pyqt6, python3-numpy,
pulseaudio-utils, alsa-utils`. `Recommends: sox, ffmpeg, pipewire-bin,
libkf6config-bin, qdbus-qt6`. Still no conffiles and no postinst. Qt 6 replaced
GTK3 because the target desktop is Plasma (`docs/kde-integration.md`).

## 8. Acceptance criteria

1. Reported analog gain matches `amixer -c <card> sget Capture` exactly, at
   every index, on the reference hardware.
2. The gain curve is read from the device, and a second machine with a
   different codec produces a different, correct curve with no code change.
3. With gain forced to maximum and normal speech, the clipping pane reports
   non-zero clipped runs. At the calibrated gain, it reports zero over 60 s.
4. Calibration on the reference laptop recommends an index in the 23–29 range
   (0 to +4.5 dB), consistent with the manual result of 2026-07-30.
5. **Zero effect on the audio graph**: `pw-link -l` and `pactl list short sources`
   produce identical output before launch and after quit. No node created, no
   default changed unless the user explicitly asks.
6. Meter latency under ~100 ms, and no audible or measurable effect on a
   concurrent recording by another application.

## 9. Open questions

- Should ASR Mic Tuner offer to *disable* competing processing it detects
  (other noise-suppression nodes in the graph), or only report it? Leaning
  report-only, per the no-side-effects goal.
- `python3-alsaaudio` (packaged, 0.10.0) would replace `amixer` parsing with a
  real binding. Cleaner, but adds a dependency and the `amixer` commands above
  are already proven. Defer to implementation.
- ~~Is a tray/indicator mode worth it?~~ Resolved in v0.2: yes, and it never
  opens the mic unless asked to (see `docs/kde-integration.md`). An idle tray
  polls `amixer` only, and is useful mainly for drift notifications.


## 10. v0.2 additions

All of these keep §3. No processing is inserted, and capture runs only while
something on screen needs it.

- **Gain stepping**: − / + buttons move one hardware index. On devices with
  no analog control (Bluetooth, virtual nodes), the same control drives
  PipeWire software volume in 1% steps, labelled as digital. Software volume
  is never offered for a device that has an ALSA capture control.
- **Measure**: room tone and voice samples, analysed per `docs/analysis.md`.
  They're saved as FLAC under `~/.local/share/asr-mic-tuner/samples/`, with a sox
  noise profile for each room sample.
- **History**: SQLite (`measurements`, `profiles`). Every calibration,
  recording and logged reading is kept, with device, gain, location tag and
  notes.
- **Profiles**: named gain presets per device in `devices.json` under
  `presets`. The remembered gain (`devices`) is the ★ default. Room and voice
  profiles point at measurements. The active room profile per device is the
  SNR reference and the spectrum overlay.
- **AI review**: OpenRouter chat completions with the measured JSON, and
  optionally a 16 kHz mono MP3 of the sample (off by default). Default model
  `google/gemini-3.8-flash`, which handles both text and audio. The reply is
  structured JSON stored with the measurement. Verified 2026-10-22: one room
  review cost $0.0056.
- **Tray**: zone-coloured icon, drift notification (optional auto-restore),
  and a left-click pop-up placed by a KWin rule. Optional start-in-tray at
  login through XDG autostart.
- **CLI for agents**: `--list --json`, `--measure room|voice`,
  `--history --json`.

## 11. v0.5 additions (2026-10-27)

- **Start at login** is now also a checkable item in the tray menu, next to
  the Settings switch. Both write the same XDG autostart entry,
  `~/.config/autostart/asr-mic-tuner.desktop` (`Exec=asr-mic-tuner --tray`).
- **Quiet-mic alert** (`asrmictuner/watch.py`, on by default). Every 3 s it
  asks `pactl` whether an application other than a level meter is recording
  from the selected mic, directly or through a virtual source fed by it
  (`core.recording_clients`). Only while one is does the watcher hold its own
  capture. This is in memory only, nothing is written, and Plasma's in-use
  indicator is already lit by the other app. It gates out non-speech blocks
  (noise floor + 12 dB, floor −60 dBFS) and judges the last *N* minutes of
  **recording time** (default 3), which accumulate across dictation bursts
  and are discarded after a 30-minute gap. It needs ≥ 20 s of speech in that
  window. The level is the 95th-percentile block peak, the live verdict's
  statistic. If that is ≥ *margin* dB (default 6) under the −18 dBFS band
  edge, one notification goes out, the window restarts, and the next alert
  waits at least 30 min. A gain change or device switch restarts the window.
  This amends §3's "records only on explicit request": the watcher analyses
  but never stores.
- **ASR Effects tab**: first version. Superseded by the engine in §14.

## 12. v1.0 additions (2026-10-27)

- **Scope statement**: input tuning for ASR only (`CLAUDE.md`). Every
  recommendation is tied to a device.
- **Mic test** (Measure tab): a passage library (`passages.py`) of 30 s,
  1, 2 and 3 min texts, written to exercise plosives, sibilants, soft
  endings, numbers and names. Recording runs up to 1.5× the nominal length and
  stops on *Done reading*. New voice metrics: `dropouts`/`dropout_s` (runs of
  ≥ 90 ms below −90 dBFS between speech; a live mic never reaches that, so a
  gate, VAD or underrun is muting audio), `level_spread_db` (p90−p10 of speech
  RMS per 5 s window) and `level_drift_db` (last third against first). Findings
  and readiness deductions in `analysis.py`; thresholds `DROPOUT_*`,
  `LEVEL_*`. *Apply recommended gain* is computed from the gain the sample was
  taken at. It sets the gain **and remembers it** for the device, or sets
  software volume on devices with no analog control. The "This microphone" panel
  shows that device's last test, remembered gain and bound ASR chain.
- **ASR Effects** gets *Quick setup*, a collapsed *Customise* editor, a
  "None" template, low-CPU RNNoise versions of the DeepFilterNet chains, and a
  **VAD** entry that carries a caution wherever it is on: two VADs in series
  (chain and transcription app) clip words. **ASR Mic** first appears.
  Both were reimplemented in v2.0 (§14).

## 13. v1.1 additions (2026-10-27)

- **ASR auto gain** (`watch.py`, setting `asr_autogain`, default on; ceiling
  `asr_autogain_max_db`, default 15 dB = `ZONE_CAUTION_HI`). It runs on the same
  speech-gated blocks as the quiet alert, only while another app records. It needs
  30 s of recording and ≥ 8 s of speech per decision, uses the p95 block peak, has a
  ±2 dB dead band around −18…−10 dBFS, steps ≤ 3 dB toward −12 dBFS, and acts only
  when the last 300 ms are below the speech gate. A new clipped run means −3 dB at
  the next pause. Steps go through `Session.set_index(deliberate=True)`, so the
  remembered gain follows and drift detection stays quiet. Software-volume devices
  step their percentage (cubic, ceiling 100 %). Simulated 2026-10-27: a −32 dBFS
  speaker went up 3 dB per 30 s to +12 dB; −14 and −19 dBFS speakers were left
  alone; a −45 dBFS speaker stopped at the +15 dB ceiling and the quiet alert
  then fired; −6 dBFS stepped down twice; one clip stepped down once.
  It has not yet been checked during real dictation.
- **ASR Mic pause**: `asrmic.set_running()` starts or stops the unit and leaves
  it enabled. It is in the tray menu ("ASR Mic running") and in ASR Effects ("Pause").
- **Speaker profiles and personal chains**: voice profiles are now called
  speaker profiles (one person on one microphone). `effects.personal_chain()`
  builds a plain-DSP chain from a mic test (and the active room profile if
  any). The high-pass is at 0.7 × F0 p10, clamped to 50–150 Hz and rounded to 5 Hz.
  A low-pass at 8 kHz is added when the mic's bandwidth reaches 8.5 kHz. A gate at
  min(noise + 12, speech RMS − 6) dB with −12 dB reduction is added only if
  SNR ≥ 20 dB. Auto gain uses −20 LUFS, a 30 s history and a silence threshold of
  noise + 6 dB, clamped to −80…−30. A −3 dB limiter goes last. Each decision comes
  with a sentence of reasoning, shown in ASR Effects, where the chain opens for
  review. Nothing is saved until the user saves it. In v2.0 it builds
  engine effects (§14) and adds a hum notch when the room profile found mains hum.

## 14. v2.0: our own effects engine (2026-10-27)

ASR Mic Tuner now runs input chains itself, inside PipeWire, and depends on
no other effects application. Code: `engine.py` (catalogue, graph generator,
chains, bindings, plugin downloads, personal chain), `asrmic.py` (supervisor
and user unit), `fxpage.py` (the tab). Reference: `docs/effects.md`.

- **How it runs.** The user unit `asr-mic-tuner-asr-mic.service` runs
  `asr-mic-tuner --asr-mic`. Every 3 s it picks the feeding mic (the one chosen
  in the tab, else the hardware behind the default input), loads the chain
  bound to that mic and port, and renders a PipeWire `filter-chain` config to
  `~/.config/asr-mic-tuner/asr-mic-chain.conf`. It keeps one
  `pipewire -c <that file>` process running, and restarts it when the text
  changes. The capture side is passive and targets the mic with
  `node.dont-fallback`. The output is "ASR Mic" (`asr-mic-tuner.asr-mic`),
  mono, 48 kHz.
- **Effects and where they come from.** High-pass, low-pass, hum notch and EQ
  are PipeWire builtin biquads. Mono fold is the builtin `mixer`. Auto gain is
  PipeWire's `ebur128` plugin: gated loudness over N s → `lufs2gain` → `clamp`
  (±max) → `linear`. Gate, compressor and limiter are swh-plugins LADSPA
  (`gate_1410`, `sc4m_1916`, `fast_lookahead_limiter_1913`). RNNoise and
  DeepFilterNet are LADSPA builds downloaded on request from
  werman/noise-suppression-for-voice v1.10 and Rikorose/DeepFilterNet v0.5.6,
  pinned by SHA-256.
- **PipeWire 1.6.2 builtin `noisegate` is unusable**: its "Level" control
  defaults to NaN (meaning "measure the input"), but the filter-graph clamps
  every default into [min, max] = [0, 0], so the gate sees level 0 and never
  opens. Verified with a −36 dBFS tone (output silent). The swh gate is used
  instead.
- **Verified with test tones through a temporary null sink, 2026-10-27**
  (target `<sink>` + `stream.capture.sink=true`; a `.monitor` name does not
  resolve as `target.object`):
  - auto gain (target −20 LUFS, ±12 dB): −46.5 LUFS in → +12.0 dB;
    −26.4 → +5.8 (expected about +6.4); −12.5 → −8.2 (about −7.5);
  - gate (−50 dBFS, −12 dB): a −66 dBFS tone → −12.0 dB; a −36 dBFS tone → +0.1 dB;
  - limiter (−3 dB): a tone pushed to about +3 dBFS → −3.0 dBFS peak;
  - high-pass 100 Hz: −3.1 dB at 100 Hz; low-pass 8 kHz: −3 dB at 8 kHz, −19 dB at 12 kHz.
  Lesson: check that ASR Mic exists before recording from it, because
  `parec --device=<missing>` silently records the default mic.
- **CPU on the reference laptop (Ryzen, 16 threads)**: "Dictation standard"
  (DeepFilterNet) used about 50 % of one core and logged one underrun at
  start; the RNNoise version used about 10 %.
- **Supervisor checks, 2026-10-27**: it ran the bound chain, rebound to
  another chain within one poll, attributed a recorder on ASR Mic to the
  feeding mic (`core.recording_clients` walks `pw-link` back from the capture
  node), and left nothing running after it stopped.
- **Breaking change**: chains from the v1.x ASR Effects tab do not carry over.

## 15. v2.1 additions (2026-10-27)

- **Conservative gain** (setting `conservative_gain`, default false; Settings →
  While you dictate, and a checkable tray menu item). Asked for because quiet
  dictation tuned to −12 dBFS peaks leaves 12 dB of headroom, and a crying baby
  or a shout in the room blows straight through it. When on,
  `core.target_peak_db(settings)` returns `CONSERVATIVE_TARGET_PEAK_DB`
  (= `TARGET_LO_DB`, −18 dBFS) instead of `CALIB_TARGET_PEAK_DB` (−12). Every
  recommendation reads its target from there: Measure's *Apply recommended
  gain* (`analysis.suggest_index(..., target)` and the software-volume branch),
  calibration (`monitor.py`), the live verdict's "needs ±x dB" fix, auto gain
  and the quiet-mic alert text (`watch.py`). Auto gain also caps raises at
  `CONSERVATIVE_RAISE_STEP_DB` (1.5 dB) instead of 3; lowering and the
  immediate step down on clipping are unchanged. The target band itself
  (−18…−10) and the readiness score do not change, so −18 still reads "Good
  level". Findings text already stored with older measurements still quotes −12.
- **Tray icon redrawn** (`widgets.tray_icon`): the round status dot read as a
  recording indicator and looked like another tray app's icon. It is now a
  microphone beside a vertical fader; the knob carries the zone colour and sits
  at a height per state (`TRAY_KNOB_POS`), or follows the live level when one
  is passed. Idle is a grey knob, so the icon is never just a bare mic. Drift
  and clip add a white bar across the knob in place of the old "!". Rendered
  offscreen at 64 and 24 px and checked by eye before release.
