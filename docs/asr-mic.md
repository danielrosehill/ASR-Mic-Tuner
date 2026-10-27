# ASR Mic: the processed microphone as its own input

**ASR Mic** is a virtual source carrying the output of your EasyEffects input
chain. Your raw microphone stays the system default and untouched. Point only your
dictation or transcription app at "ASR Mic". Calls, recorders and everything else
keep getting the raw signal.

```
hardware mic ──► EasyEffects input chain ──► easyeffects_source ──► ASR Mic ──► dictation app
     │
     └──► default input: calls, recorders, everything else (raw, unprocessed)
```

Turn it on in **ASR Effects → ASR Mic → Create ASR Mic**. Code: `asrmictuner/asrmic.py`.

## How it works

- A systemd **user** unit, `~/.config/systemd/user/asr-mic-tuner-asr-mic.service`
  (`WantedBy=pipewire.service`), runs `asr-mic-tuner --asr-mic`.
- That supervisor checks every 3 s whether `easyeffects_source` exists. While it does,
  it runs one `pw-loopback`:
  - capture side `asr-mic-tuner.asr-mic-capture`, with `target.object=easyeffects_source`,
    `node.passive=true` and `node.dont-fallback=true`;
  - playback side `asr-mic-tuner.asr-mic`, with `media.class=Audio/Source` and
    `node.description="ASR Mic"`.
- **When EasyEffects is not running, ASR Mic does not exist.** It never falls back to
  the raw mic, so a broken chain shows up as a missing input instead of silently
  unprocessed audio. What your app does when its chosen input disappears (usually it
  falls back to the default) is up to the app.
- Removing it (untick the box) disables and deletes the unit. Nothing else is touched.

Why a supervisor: `pw-loopback` exits with status 0 when its target disappears, and
exits at once if started without one (because of `node.dont-fallback`). So a plain
`Restart=` policy either never brings it back or restarts it in a loop. Verified on
2026-10-27 with PipeWire 1.6.2 and EasyEffects 8.3.0 (Flatpak): ASR Mic appeared
within one poll of EasyEffects starting, carried audio, disappeared when EasyEffects
quit, and came back when it restarted.

Node names start with `asr-mic-tuner.` deliberately:
- The quiet-mic alert recognises the loopback's own capture and does not treat it as
  "someone is dictating".
- An app recording from ASR Mic is attributed to the hardware mic behind EasyEffects
  (`core.recording_clients`).
- The older sibling project `dictamic` creates a source named `asr_mic`. These names
  never collide with it.

## Keep the raw mic as the default

EasyEffects follows the default input unless it is pinned to a device. If you made
ASR Mic (or `easyeffects_source`) the default, EasyEffects would try to process its
own output. Leave the hardware mic as the default, and choose ASR Mic per app.

## Two VADs are worse than one

The chain can include a voice-activity gate (ASR Effects → "⚠ Voice activity gate").
Most transcription apps already run their own VAD. Two in series clip word starts and
drop quiet words. Add one only if your app has none, then run a mic test: **dropouts**
in the result mean the gate is cutting speech.
