# micscope

> *See what your microphone is really doing.*

A single window showing the **true analog gain** of a capture device in dB, its
**signal level** in dBFS with peak hold, and **whether it is clipping** —
counted in samples, not guessed from an indicator light. Set the gain in dB
instead of as a percentage of an opaque curve.

**Status: specified, not yet implemented.** See [`docs/spec.md`](docs/spec.md).

## Why

Every microphone GUI on Ubuntu misreports gain, because PipeWire/PulseAudio
volume is not the analog preamp gain and the mapping between them is neither
linear nor lossless. On the reference machine the two readings, taken at the
same instant, differ by **60 dB**:

| Reported by | Value |
|---|---|
| `pactl` / pavucontrol / GNOME Sound | 12% / −55.25 dB |
| ALSA `Capture` control (the real preamp) | index 29/63 / **+4.50 dB** |

Worse, the whole usable analog range is squeezed into `pactl` 5%–35%, and
anything above 35% is pinned at maximum boost. A slider sitting a third of the
way along looks conservative and is in fact flat out — room noise reaches
−11 dBFS and speech saturates the ADC. That damage is analog, so **no filter,
AGC or noise suppressor downstream can repair it**; speech recognition accuracy
collapses while the microphone still appears to work fine.

The full measured curve, the derivation, and the effect on noise floor are in
[`docs/gain-mapping.md`](docs/gain-mapping.md).

## What it does not do

micscope inserts **no** processing — no filter, no virtual source, no AGC, no
noise suppression — and installs nothing into `/etc`. The audio graph is
byte-identical before and after running it. It observes, and it sets gain.

The sibling project [`dictamic`](../dictamic) took the other approach, adding a
WebRTC AGC/noise-suppression chain as a virtual source. Measurement showed raw
audio at *correct* analog gain had a **9 dB lower noise floor than that entire
chain achieved** while fighting bad gain. Correct gain beats processing around
bad gain, so micscope makes correct gain easy to see and set.

## Planned features

- Gain slider bound to the ALSA control index, labelled in dB, with safe /
  caution / danger zones derived from the device's own curve
- Per-channel dBFS meters with peak hold and a −18…−10 dBFS target band
- Clipped-sample and clipped-**run** counters (runs, not isolated peaks,
  indicate real saturation)
- Calibration assistant: measure silence, measure speech, recommend and apply
  a gain that puts peaks at −12 dBFS with verified headroom
- Detection of processed/virtual sources, which have no analog gain at all
- Drift detection, because wireplumber will overwrite your setting

## Install

```bash
sudo apt install ./dist/micscope_*.deb
```

## Build

```bash
./build-deb.sh 0.1.0        # → dist/micscope_0.1.0_all.deb
```

## Meanwhile, without this tool

```bash
alsamixer -c 1              # F4 for capture view — honest gain in dB
amixer -c 1 sget Capture    # same, scriptable
```

Use Audacity's recording meter for dBFS and clipping. Avoid pavucontrol and
GNOME Sound for *setting* mic gain, for the reason above.
