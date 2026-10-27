# ASR Mic Tuner

> *See what your microphone is really doing — tuned for dictation and speech recognition.*

**Write-up:** [The Linux mic slider lies: Mic Monitor shows the real gain](https://blog.danielrosehill.com/posts/linux-microphone-gain-dictation-mic-monitor-kaq5pj) (written before the rename to ASR Mic Tuner)

## Scope

ASR Mic Tuner does one thing: **tune your microphone input for speech recognition**,
whether the recogniser runs locally (Whisper.cpp, faster-whisper, Vosk) or in the cloud.
It's the one place to check your levels, test the mic by reading a passage, apply the
recommended gain, and put an input-side clean-up chain in front of your dictation app.
Each microphone is different, so every result, recommendation, remembered gain and
chain is tied to the specific microphone (and port) it was made for. Output audio, music EQ and
transcription itself are out of scope.

## Overview

A KDE Plasma (Qt 6) microphone monitor that lives in the system tray. It shows
the **true analog gain** of a capture device in dB, its **signal level** in
dBFS against the band speech recognition wants, and **whether it is clipping**,
counted in samples. You can step the gain one hardware index at a time. It also
records room tone and voice samples, measures them, keeps a history per
microphone and location, and can ask an AI model for a review.

![Monitor tab: analog gain in dB with zone colours, per-channel meters, live verdict, spectrum with the room profile overlaid](docs/screenshots/monitor.png)

## Why

Every microphone GUI on Linux misreports gain, because PipeWire/PulseAudio
volume is not the analog preamp gain, and the mapping between the two is
neither linear nor lossless. On the reference laptop (Realtek ALC257) the two
readings, taken at the same instant, differed by **60 dB**:

| Reported by | Value |
|---|---|
| `pactl` / pavucontrol / Plasma audio applet | 12% / −55.25 dB |
| ALSA `Capture` control (the real preamp) | index 29/63 / **+4.50 dB** |

The whole usable analog range is squeezed into `pactl` 5%–35%. Anything above
35% is pinned at maximum boost. Clipping at the preamp is analog damage, so **no
filter, AGC or noise suppressor downstream can repair it**. Full measurements
are in [`docs/gain-mapping.md`](docs/gain-mapping.md).

## What it does

| Tab | What's there |
|---|---|
| **Monitor** | The analog gain in dB, colour-zoned by the device's own curve, with − / + buttons that move one hardware step (0.75 dB on HDA). Per-channel meters with peak hold and the −18…−10 dBFS target band, and a live verdict ("too loud — needs −6 dB", with a **Fix it** button). It also counts clipped samples and runs, shows DC offset, channel balance and an estimated speech-to-noise ratio, and has a live spectrum with the active room profile overlaid. Calibration comes in three lengths, Quick, Standard and Thorough (3+8 s, 5+15 s, 10+30 s), and **Log reading** saves the current state to History. |
| **Measure** | **Mic test**: choose a text to read aloud (30 s, 1 min, 2 min or 3 min, several of each), read it at your normal dictation distance, and press *Done reading*. The result is an ASR readiness score, the problems that would hurt recognition (level, clipping, SNR, narrow bandwidth, rumble, plosives, sibilance, channel imbalance, **dropouts** from a gate or VAD muting speech, and **level spread and drift** over longer reads), and a one-click **Apply recommended gain**, which sets the gain and remembers it for that microphone. **Save as speaker profile** keeps the test as yours on that mic. **Build personal chain** turns it into an ASR Mic chain fitted to your voice: a high-pass 30% under your lowest pitch, a low-pass at 8 kHz (what 16 kHz recognisers keep anyway), a gentle gate set between your room and your speech (only when the SNR allows), auto gain that ignores the room, and a limiter. It adds a hum notch if your room profile found mains hum, uses plain DSP with no neural denoiser, opens in ASR Effects for review, and explains each choice. A "This microphone" panel shows that mic's last test, its remembered gain and its bound ASR chain. **Room tone** (5–120 s): noise floor (p10, RMS, A-weighted), stationarity, transients, rumble, speech-band and hiss energy, mains hum (50/60 Hz plus harmonics), spectral flatness, 1/3-octave bands, and a sox noise profile for later cleanup. The mic test measures speech peak and RMS, SNR against the active room profile, crest factor, dynamic range, pitch (F0) median and range, spectral centroid and tilt, bandwidth (it catches Bluetooth HFP narrowband), rumble, presence, sibilance, plosive pops and channel imbalance. All of that feeds an **ASR readiness score** from 0 to 100 with named deductions, plus a suggested gain. |
| **History** | Every measurement, with device, gain, location, notes, sample file and AI review. Filter by device or kind, play a sample, ask the AI, promote a measurement to a profile, or export everything as JSON. |
| **Profiles** | **Gain presets** per microphone ("office", "living room"); the ★ preset is the remembered gain that Restore, the tray and the login unit reapply. **Room noise profiles**: the active one per device is the SNR reference and the spectrum overlay, and each has a "copy sox cleanup command" action. **Voice profiles** as well. |
| **ASR Effects** | Input chains for dictation, run **inside PipeWire** by ASR Mic Tuner itself, and published as **ASR Mic**, a separate mono input. The raw mic stays the default and untouched ([`docs/asr-mic.md`](docs/asr-mic.md)). **Quick setup** is one button: pick a chain and it is saved, bound to this microphone, and ASR Mic is turned on. Templates: "Dictation standard" and "Noisy room" (DeepFilterNet, for desktop CPUs), a "low CPU" RNNoise version of each for laptops, "Simple clean-up" (nothing to download), "Level only" and "None". Under *Customise the chain*: high-pass, low-pass, hum notch, EQ band, gate, loudness auto gain (30 s, gated), compressor, limiter, RNNoise, DeepFilterNet, and an optional **voice activity gate** marked with a caution. A **Noise reduction** switch (Off / Light, RNNoise / Strong, DeepFilterNet), also in the tray menu, lays a denoiser over the bound chain for loud moments (a crying baby) and replaces any denoiser already in it. **Bypass** gives you A/B, **Pause** turns it off for a call, and each mic and port keeps its own chain. The neural denoisers are optional downloads from their maintainers' releases, checked against pinned checksums. Details: [`docs/effects.md`](docs/effects.md). |
| **Settings** | OpenRouter key and model, an option to send the audio as well as the numbers, tray behaviour, start at login, the quiet-mic alert, auto-restore on drift, and the data folder. |

| Tray pop-up | Room tone analysis |
|---|---|
| ![Tray pop-up](docs/screenshots/tray-popup.png) | ![Room tone result](docs/screenshots/room-tone.png) |

**Tray** (Plasma StatusNotifierItem):
- **Left-click** opens a pop-up with the device, a gain slider and ± buttons, a live meter, the verdict and preset choice. It listens only while it's open.
- **Middle-click** opens the main window. **Right-click** opens a menu with presets, Restore remembered gain and Quit.
- **Icon style** (tray menu → Icon style, or Settings): **Speech wave** (default), **Pop filter**, **Studio mic** or **Headset**. The status colour tints the wave, the pop filter mesh, the mic grille or the ear cups (grey idle, blue too quiet, green good, amber caution, red too loud); with the live meter on, it fills from the bottom with the level. There is deliberately no round dot, so it doesn't pass for a "recording" indicator. A warning triangle appears, and a notification is sent, when **something else moves your gain** (amber) or the mic clips (red). wireplumber does move it, and was seen resetting the preamp to maximum.
- **ASR auto gain** (on by default; tray menu and Settings): while you dictate, it moves the **analog** gain itself instead of processing audio. It judges 30 s of recording at a time, moves at most 3 dB, changes nothing while speech sits near the target band, makes changes only in pauses, steps down at once on clipping, and never goes above a ceiling (+15 dB by default). A DSP auto gain rides every word and turns the room up in every pause, which is why it often goes wrong for ASR. This one doesn't.
- **Conservative gain** (tray menu and Settings, off by default): for rooms where a sudden loud sound is likely (a crying baby, someone shouting). Every gain recommendation (Measure, calibration, the live verdict) and auto gain aim speech peaks at −18 dBFS, the bottom of the target band, instead of −12, which leaves 18 dB before clipping instead of 12. Auto gain also raises in 1.5 dB steps instead of 3. Lowering, and the step down on clipping, are unchanged.
- **Quiet-mic alert**: while another app (your dictation tool) is recording from the mic, ASR Mic Tuner listens alongside it. If your speech over the last 3 minutes of recording sits well under the target band (6 dB or more below −18 dBFS by default), you get one notification. It counts speech only, so pauses never trigger it, and it repeats at most every half hour. Window and margin are in Settings. With auto gain on, the alert waits until auto gain has hit its ceiling.
- **ASR Mic running** (tray menu): stop ASR Mic for now, before a call for example, without quitting the app or turning it off. It comes back at login or from the same menu.
- **Start at login** is a checkbox in the right-click menu and in Settings.

**Microphones with no analog control** (Bluetooth headsets, processed virtual sources) fall back to PipeWire software volume in 1% steps, clearly labelled as digital.

## Where processing happens

The raw microphone is never altered. ASR Mic Tuner observes it, sets its gain, and
records to disk only when you ask. The quiet-mic alert and the ASR auto gain analyse in
memory and store nothing. Nothing is installed into `/etc`.

Processing is opt-in and happens only on **ASR Mic**, a separate input that ASR Mic Tuner
runs inside PipeWire with the chain bound to your mic. The raw mic stays the default, so
only apps you point at ASR Mic get the chain. Fix the gain first, then add only what your
room needs. The sibling project [`dictamic`](https://github.com/danielrosehill/dictamic)
measured raw audio at *correct* gain with a **9 dB lower noise floor** than a full
processing chain, so a chain is not a substitute for correct gain.

## Install

From the signed APT repository at `apps.danielrosehill.com/apt` (updates arrive with `sudo apt upgrade`):

```bash
curl -fsSL https://apps.danielrosehill.com/apt/setup.sh | sudo sh -s -- asr-mic-tuner
```

If the repository is already set up on the machine:

```bash
sudo apt update && sudo apt install asr-mic-tuner
```

Or install the `.deb` from the [latest release](https://github.com/danielrosehill/ASR-Mic-Tuner/releases/latest):

```bash
sudo apt install ./asr-mic-tuner_*_all.deb
```

Then run `asr-mic-tuner`, or `asr-mic-tuner --tray` to start in the tray only (what "Start in the tray at login" runs).
Tested on Ubuntu 26.04; it needs only archive packages (PyQt6, numpy), so 24.04 and later should work. It's built for KDE Plasma and works on any desktop with a StatusNotifierItem tray.

Depends: `python3-pyqt6 python3-numpy pulseaudio-utils alsa-utils`.
Recommends: `sox` (FLAC samples and noise profiles), `ffmpeg` (audio for AI), `libnotify-bin` (quiet-mic alert without a tray), `swh-plugins` (gate, compressor, limiter) and `libspa-0.2-modules-extra` (auto gain) for ASR Effects,
`pipewire-bin` (playback), `libkf6config-bin` and `qdbus-qt6` (KWin pop-up placement).

**Formerly Mic Monitor (`mic-monitor`, v0.3) and `micscope` (v0.1–0.2).** The `asr-mic-tuner`
package replaces both, and on first run it moves `~/.config/<old name>` and
`~/.local/share/<old name>` to the new names and renames the KWin rule, autostart entry and login unit.
The GitHub repository was `danielrosehill/Mic-Monitor`; GitHub redirects the old URL.

## Command line (scripts and agents)

```bash
asr-mic-tuner --list [--json]                 # every source with real analog gain, presets, remembered gain
asr-mic-tuner --measure room --seconds 15 --location "living room"   # records, analyses, logs; prints JSON
asr-mic-tuner --measure voice --seconds 20
asr-mic-tuner --history [--json]              # the measurement log
asr-mic-tuner --restore [--delay 8]           # reapply remembered gain (used by the login unit)
asr-mic-tuner --asr-mic                       # run ASR Mic's chain (used by its user unit)
```

## Files

| Path | Contents |
|---|---|
| `~/.config/asr-mic-tuner/devices.json` | remembered gain per device, named presets, probed-curve cache |
| `~/.config/asr-mic-tuner/settings.json` | settings, including the OpenRouter key (mode 600). `OPENROUTER_API_KEY` in the environment also works. |
| `~/.local/share/asr-mic-tuner/history.db` | SQLite: `measurements`, `profiles` |
| `~/.local/share/asr-mic-tuner/samples/` | room and voice FLACs, plus `.noiseprof` for each room sample |
| `~/.config/systemd/user/asr-mic-tuner-asr-mic.service` | ASR Mic (only when you turn it on) |
| `~/.config/autostart/asr-mic-tuner.desktop` | start in the tray at login (Settings or tray menu) |
| `~/.config/asr-mic-tuner/chains/*.json` | saved ASR chains (bindings are in `settings.json`) |
| `~/.config/asr-mic-tuner/asr-mic-chain.conf`, `asr-mic-state.json` | the PipeWire config ASR Mic is running, and what it is running |
| `~/.local/share/asr-mic-tuner/plugins/` | RNNoise and DeepFilterNet, if you fetched them |
| `~/.config/kwinrulesrc` group `asr-mic-tuner-tray-popup` | KWin rule that places the pop-up (see [`docs/kde-integration.md`](docs/kde-integration.md)) |

## Build

```bash
./build-deb.sh            # version from asrmictuner/__init__.py → dist/asr-mic-tuner_<v>_all.deb
python3 -m asrmictuner       # run from the source tree
```

## Docs

- [`docs/spec.md`](docs/spec.md): design, constraints, acceptance criteria
- [`docs/gain-mapping.md`](docs/gain-mapping.md): the measured pactl ↔ ALSA mapping
- [`docs/analysis.md`](docs/analysis.md): what every metric means and the thresholds behind the findings and score
- [`docs/kde-integration.md`](docs/kde-integration.md): tray, pop-up placement on Wayland, and the microphone-in-use indicator
- [`docs/asr-mic.md`](docs/asr-mic.md): the ASR Mic virtual source, why the raw mic stays the default, and the VAD caution
- [`docs/effects.md`](docs/effects.md): every effect, what runs it, the plugin downloads and their checksums, and test results
