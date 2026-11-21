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

**Confirmed for USB** (2026-11-21, a desktop with two USB mics):

| Mic | `device.vendor.id`:`product.id` | `device.serial` | Key |
|---|---|---|---|
| Boya BY-CM1 | `0xff01`:`0x000b` | `BY-CM1_BY-CM1_20180508` | `usb:ff01:000b:BY-CM1_BY-CM1_20180508` |
| Samson Q2U | `0x17a0`:`0x0304` | `Samson_Technologies_Samson_Q2U_Microphone` | `usb:17a0:0304:Samson_Technologies_Samson_Q2U_Microphone` |

`device.serial` is udev's `ID_SERIAL`, which is `<vendor>_<product>[_<serial>]`. Neither mic
has a per-unit serial. The Boya's `20180508` looks like a firmware date that every unit
shares (assumed). So in practice the key identifies the **model**, and two identical mics
share a profile, which is acceptable because they behave alike. The key does not depend
on the USB port or the card profile. A v2.6 chain binding on the Boya migrated to its key,
and ASR Mic kept running that chain after the upgrade.

**Assumed, not yet tested:** that Bluetooth sources carry `api.bluez5.address`.

## A profile is a mic, on a computer, in a place (v2.9)

Since v2.9 each mic's file holds one bucket per **computer**, so a USB headset that moves
between a laptop and a desktop has separate gain profiles on each. The same mic draws a
different gain on a different USB port controller, and sits in a different room.

- Computer: `identity.this_machine()`. `id` is the first 12 hex of sha256(`/etc/machine-id`).
  It also records hostname, kind (from DMI `chassis_type`: laptop, desktop, …), OS and DMI
  vendor/product. The raw machine-id is never stored.
- Place: the profile's label (Quick tune's Place box, else the window's location). Each
  entry also carries `place`, `port` and `computer` (for example
  `"daniellaptop (Linux laptop)"`), so a synced file reads on its own.
- `remembered`, `presets` and `active_profile` are `{<computer id>: …}` in the file, and
  `machines` describes each computer id. `identity` and `chain_bindings` stay shared:
  a chain suits the mic wherever it is plugged in. `"format": 2` marks the layout.
- In memory (`load_config`) the app sees only **this computer's** bucket, in the same dict
  shapes as before. `profiles.save_machine_view` writes it back and leaves other computers'
  buckets alone.
- A v2.7–2.8 file (format 1, flat) belongs to the computer that first loads it, except
  another computer's built-in mic, whose file is left untouched until its own computer
  converts it.
- Sync (`docs/profiles.md`): when two computers changed the same mic file, it merges. The
  folder's copy is taken with this computer's buckets from the local copy, and the shared
  fields from the newer copy. There is no conflict copy.

## Files

`~/.config/asr-mic-tuner/profiles/<key with ":" as "__">.json`:

Format 1 (v2.7–2.8) is shown below. Format 2 wraps `remembered`, `presets` and
`active_profile` in `{<computer id>: …}` and adds `machines` and `"format": 2` (above).

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

- Verify the Bluetooth address with a real headset (above).
- Is the HDA `Subsystem Id` a better machine identifier than DMI? It is recorded in
  `identity.codec.subsystem_id`, so switching the key later needs no new probing.
- Manual merge ("these two keys are the same mic") if a key ever changes.
