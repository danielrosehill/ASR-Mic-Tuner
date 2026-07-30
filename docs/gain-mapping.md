# How PipeWire volume maps onto ALSA analog capture gain

Verified 2026-07-30 on `daniellaptop`, Ubuntu (kernel 7.0.0-28-generic),
PipeWire + wireplumber, Realtek ALC257 (`HD-Audio Generic`, card 1),
device `alsa_input.pci-0000_04_00.6.analog-stereo`.

Everything below is **measured**, not inferred, except where marked.

## The headline

`pactl` / pavucontrol / GNOME Sound report a dB figure for microphone volume
that is **not** the analog preamp gain, and on this hardware the two differ by
60 dB. Both readings taken at the same instant:

| Reported by | Value |
|---|---|
| `pactl list sources` → `Volume:` | 12% / **−55.25 dB** |
| `amixer -c 1 sget Capture` | index 29/63 / **+4.50 dB** |

Setting mic gain from any PulseAudio-facing GUI on this machine is therefore
guesswork. Use `amixer`/`alsamixer`.

## The measured curve

Obtained by stepping `pactl set-source-volume` and reading the hardware control
after each step:

| pactl volume | analog gain | ALSA index |
|---|---|---|
| 5% | −17.25 dB | 0/63 |
| 8% | −6.00 dB | 15/63 |
| 10% | 0.00 dB | 23/63 |
| 12% | +4.50 dB | 29/63 |
| 15% | +10.50 dB | 37/63 |
| 18% | +15.00 dB | 43/63 |
| 20% | +18.00 dB | 47/63 |
| 25% | +23.25 dB | 54/63 |
| 30% | +28.50 dB | 61/63 |
| 35% | +30.00 dB | 63/63 |
| 44%, 68%, 80%, 100% | +30.00 dB (pinned) | 63/63 |

The entire usable analog range lives between **pactl 5% and 35%**. Above 35%
the slider moves and nothing happens — the preamp is already at maximum.

In GNOME's sound panel this means anything past roughly a third of the way
along the input slider is maximum analog boost.

## Deriving the curve without touching the device

`amixer cget` prints the dB-scale TLV, so the whole curve is available with no
writes at all:

```
$ amixer -c 1 cget name='Capture Volume'
numid=6,iface=MIXER,name='Capture Volume'
  ; type=INTEGER,access=rw---R--,values=2,min=0,max=63,step=0
  : values=29,29
  | dBscale-min=-17.25dB,step=0.75dB,mute=0
```

```
dB(index) = dBscale_min + step_dB × index
          = −17.25 + 0.75 × index
```

Checks against the table above: index 23 → 0.00, index 29 → +4.50,
index 63 → +30.00. Exact at every point.

**Inferred, not verified:** that all HDA codecs expose this TLV. Cards that do
not will need min/mid/max probing instead.

## Audible consequences

Four seconds of *ambient room noise*, no speech, same room, same moment:

| Configuration | noise floor (RMS) | ambient peak |
|---|---|---|
| raw device, +30 dB analog | −27.1 dBFS | **−11.2 dBFS** |
| WebRTC AGC + noise suppression on top of +30 dB | −46.9 dBFS | −26.0 dBFS |
| raw device, +4.5 dB analog, no processing | **−56.4 dBFS** | −43.7 dBFS |

Room noise peaking at −11 dBFS leaves nowhere for speech to go: it saturates
the ADC. Because that happens in the analog domain, **no downstream filter,
AGC or noise suppressor can undo it.**

Note the middle row: a full WebRTC processing chain fighting bad analog gain
still ends up 9 dB noisier than plain audio at correct analog gain. Fixing the
gain is strictly better than processing around it.

## Gain does not stay where you put it

Two independent mechanisms move it:

1. **wireplumber restores its saved per-route volume** when a device becomes
   the default source, or when routes change. Observed: setting 12%, then
   making the device default, restored it to 44%; a `systemctl --user restart
   wireplumber` later left it at 68% (= +30 dB). The saved value lives in
   `~/.local/state/wireplumber/default-routes`, as linear (not cubic)
   `channelVolumes` — e.g. `0.085185` ⇒ 0.085185^(1/3) ≈ 44%.
2. **The user**, via a desktop slider whose non-linearity makes a small nudge
   a 25 dB change.

Any tool that sets capture gain must therefore expect to be overridden, and
should detect drift rather than assume its setting held.

## Practical recipe

```bash
# read the truth
amixer -c 1 sget Capture
alsamixer -c 1          # then F4 for capture view

# set 0 dB, then +4.5 dB
amixer -c 1 cset numid=6 23,23
amixer -c 1 cset numid=6 29,29
```

Target for dictation: speech peaks **−18 to −10 dBFS**, noise floor below
**−50 dBFS**, zero clipped runs. On this hardware that is roughly index 23–29
(0 to +4.5 dB) at normal speaking distance.

Also check `Mic Boost` and `Internal Mic Boost` (both 0–3 on this codec, both
currently 0). They stack on top of `Capture` and are a second route to
clipping.
