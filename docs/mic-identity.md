# Mic identity and per-mic profile files (v2.7, 2026-11-21)

Everything remembered about a microphone (remembered gain, profiles, the active
profile per port, the chain bound per port) is keyed by a **mic key** that follows
the physical mic, and stored in one file per mic. Code: `asrmictuner/identity.py`
(the key), `asrmictuner/profiles.py` (the files), `core.load_config` / `save_config`
and `store.load_settings` / `save_settings` (read and write them).

## Why not the node name

PipeWire node names embed a bus address and the card profile:
`alsa_input.pci-0000_04_00.6.analog-stereo`. The same mic changes name when the card
profile changes (`analog-stereo` → `mono-fallback` → `pro-input-0`), two laptops can
produce the same name, and a USB mic's name depends on its descriptor strings. Up to
v2.6 everything was keyed by that name.

## The key

| Bus | Key | Follows the mic to another machine |
|---|---|---|
| USB with a serial | `usb:<vid>:<pid>:<serial>` | yes |
| USB without one | `usb:<vid>:<pid>` | yes (identical twins share it) |
| Bluetooth | `bt:<address>` | yes |
| Built-in (PCI / other ALSA) | `internal:<DMI vendor>_<DMI product>:<controller vid>-<pid>-<codec vendor id>` | no, by design: it is that machine's mic |
| Anything else (virtual, network) | `node:<node name>` | no |

`Device.ident` holds the full identity, `Device.key` the key. The picker in the
window header shows both in its tooltip, and `asr-mic-tuner --list --json` prints
`mic_key` and `identity` per device.

Sources:
- PipeWire source properties (`pactl -f json list sources` → `properties`):
  `device.bus`, `device.api`, `device.vendor.id`, `device.product.id`,
  `device.vendor.name`, `device.product.name`, `device.serial`, `api.bluez5.address`.
- HDA codec: `/proc/asound/card<N>/codec#*` → `Codec:`, `Vendor Id:`, `Subsystem Id:`.
- Machine: `/sys/class/dmi/id/sys_vendor`, `product_name`, `product_version` (readable without root).

**Confirmed** (2026-11-21, AMD laptop, Realtek ALC257 internal mic, PipeWire 1.6):
the internal source carries `device.bus=pci`, `device.vendor.id=0x1022`,
`device.product.id=0x15e3` (the AMD audio controller, not the mic), and no serial. The
codec file gives `Realtek ALC257` / `0x10ec0257` plus a subsystem id. DMI is readable. So
for built-in mics the vendor/product pair identifies the controller chip only. The
controller, the codec and the machine model together identify the mic.

**Assumed, not yet tested:** that USB mics carry `device.serial` (from udev `ID_SERIAL`,
which may be `Vendor_Product` with no real serial), and that Bluetooth sources carry
`api.bluez5.address`. Check with a real USB mic and headset (`--list --json`) and
update this section.

## Files

`~/.config/asr-mic-tuner/profiles/<key with ":" as "__">.json`:

```json
{"mic_key": "usb:0d8c:0014:SN1234",
 "identity": {"bus": "usb", "vendor_id": "0x0d8c", "product_id": "0x0014", "serial": "SN1234", "...": "..."},
 "remembered": {"control": "Mic Capture Volume", "numid": 4, "index": 20, "db": 12.0, "saved": "..."},
 "presets": {"Office": {"index": 20, "db": 12.0, "port": "Microphone", "noise_reduction": "off", "tuned": {}}},
 "active_profile": {"Microphone": "Office"},
 "chain_bindings": {"Microphone": "Desk chain"},
 "updated": "2026-11-21T18:00:00+00:00"}
```

Machine-local state stays out of these files: the gain-curve probe cache
(`devices.json` → `curves`), room and voice profiles and measurements (`history.db`,
which gained a `mic_key` column), and app settings (`settings.json`, where
`active_room_profile` is now keyed by mic key).

In memory the rest of the app sees the old dict shapes, keyed by mic key:
`config["devices"][key]`, `config["presets"][key][place]`,
`config["active_profile"][key][port]`, `config["identities"][key]`,
`settings["chain_bindings"][key][port]`. `save_config` and `save_settings` write only
the mics whose data changed since load, so a file updated by a sync or another process
for a different mic is never overwritten with a stale copy.

## Migration from v2.6

On first load, `devices.json` (`rev` < 27) moves its node-name keyed `devices`,
`presets` and `active_profile` into `legacy`, and `settings.json` (`settings_rev` < 27)
does the same with `chain_bindings` and `active_room_profile`. Entries for mics that are
present move to their key at once. The rest wait in `legacy` and move the first time
the mic is seen (`Session.rescan`). History rows get `mic_key` filled in the same way
(`Store.backfill_keys`); rows from mics never seen again keep a NULL key and still show
under "all devices". Nothing is deleted.

Tested 2026-11-21 on a copy of a v2.6 config. One internal mic with one profile migrated
to its key. The GUI loads, binding and unbinding a chain round-trips through the profile
file, and the ASR Mic supervisor's `plan()` resolves the binding.

## Open

- Verify USB serial and Bluetooth address handling with real devices (above).
- Is the HDA `Subsystem Id` a better machine identifier than DMI? It is recorded in
  `identity.codec.subsystem_id`, so switching the key later needs no new probing.
- Manual merge ("these two keys are the same mic") if a key ever changes.
