# ASR Mic: the processed microphone as its own input

**ASR Mic** is an input that carries your microphone through its ASR effects chain.
Your raw microphone stays the system default and is never altered. Point only your
dictation or transcription app at "ASR Mic". Calls, recorders and everything else
keep getting the raw signal.

```
hardware mic ──► [mono fold → high-pass → … → limiter] ──► ASR Mic (mono, 48 kHz) ──► dictation app
     │
     └──► default input: calls, recorders, everything else (raw, unprocessed)
```

Turn it on in **ASR Effects → ASR Mic → Create ASR Mic**, or let **Quick setup**
turn it on. Code: `asrmictuner/asrmic.py` (supervisor and unit) and
`asrmictuner/engine.py` (the chain). The effects themselves are described in
`docs/effects.md`.

## How it works

- A systemd **user** unit, `~/.config/systemd/user/asr-mic-tuner-asr-mic.service`
  (`WantedBy=pipewire.service`), runs `asr-mic-tuner --asr-mic`.
- Every 3 s that supervisor picks the microphone that feeds ASR Mic. That is the one
  chosen under *Fed by*, or else the hardware behind the default input. It then
  loads the chain bound to that mic and port and renders a PipeWire filter-chain
  config to `~/.config/asr-mic-tuner/asr-mic-chain.conf`. It keeps exactly one
  `pipewire -c <that file>` process running, and restarts it when the config text
  changes: a different mic, chain, setting, bypass, or a newly installed plugin.
  A restart takes about a second.
- Capture node `asr-mic-tuner.asr-mic-capture`: `target.object=<mic>`,
  `node.passive=true`, `node.dont-fallback=true`. Being passive, it does not keep the
  mic open, so Plasma's in-use indicator lights only when something actually
  records from ASR Mic. With `dont-fallback`, an unplugged mic never turns into
  another mic.
- Output node `asr-mic-tuner.asr-mic`, "ASR Mic": `media.class=Audio/Source`, mono,
  48 kHz (RNNoise and DeepFilterNet both require 48 kHz).
- The supervisor writes what it is running to
  `~/.config/asr-mic-tuner/asr-mic-state.json`: the device, the chain, the bypass
  state, and any effects skipped because a plugin is missing. The tab shows this.

## Turning it off for a while

- **Pause** (ASR Effects) or **ASR Mic running** (tray menu) stops the unit for now,
  for a call for example. It stays enabled, so it starts again at the next login,
  or when you resume it.
- **Bypass effects** keeps ASR Mic but passes the plain mic (folded to mono), for
  A/B comparison.
- Unticking **Create ASR Mic** disables and deletes the unit. Nothing else is touched.

## Names

Node names start with `asr-mic-tuner.` deliberately:
- The quiet-mic alert and the auto gain recognise ASR Mic's own passive capture and
  do not treat it as "someone is dictating".
- An app recording from ASR Mic is attributed to the mic that feeds it
  (`core.recording_clients` walks `pw-link` back from the capture node).
- The older sibling project `dictamic` creates a source named `asr_mic`. These
  names never collide with it.

Keep the raw microphone as the system default and choose ASR Mic per app.

## Two VADs are worse than one

The chain can include a voice-activity gate (ASR Effects → "⚠ Voice activity gate").
Most transcription apps already run their own VAD. Two in series clip word starts and
drop quiet words. Add one only if your app has none, then run a mic test: **dropouts**
in the result mean the gate is cutting speech.
