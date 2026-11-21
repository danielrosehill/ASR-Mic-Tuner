# Identifying a microphone reliably

Status: shipped in v2.7 (2026-11-21). The shipped design and test results are in
`docs/mic-identity.md`; this file keeps the reasoning.

## Today (v2.6, confirmed in code)

Everything is keyed by the PipeWire **node name**, plus the port description for
chain bindings and profiles:

- `devices.json` → `devices[<node name>]`, `presets[<node name>][<place>]` (`core.py`)
- `settings.json` → `chain_bindings["<node name>:<port>"]`, `active_profile[...]` (`store.py`, `engine.py`)
- `history.db` → `measurements.device`, `profiles.device`

A node name looks like `alsa_input.pci-0000_04_00.6.analog-stereo` (internal) or
`alsa_input.usb-<Vendor>_<Product>_<serial>-00.mono-fallback` (USB, assumed from the
PipeWire naming pattern). The problems:

1. **Internal mics embed a PCI bus address.** Two different laptops can produce the
   same name, and one laptop's name can change after a firmware or kernel update. The
   name says nothing about which machine it is.
2. **The profile suffix changes.** `analog-stereo` vs `mono-fallback` vs `pro-input-0`
   depends on the card profile chosen, so the same mic can appear under two names.
3. **USB names depend on the USB descriptor strings.** Mics with no serial look the
   same as their twins.
4. **Bluetooth** headsets are keyed by MAC in the node name; the A2DP/HFP profile
   switch changes the name.

## What PipeWire already gives us (confirmed on an AMD laptop, 2026-11-21)

`pactl -f json list sources` → `properties` on the internal mic include:

| Property | Example | Note |
|---|---|---|
| `device.vendor.id` | `0x1022` | the **audio controller** vendor (AMD), not the mic |
| `device.product.id` | `0x15e3` | the controller, not the mic |
| `device.bus` | `pci` | `usb`, `pci`, `bluetooth` |
| `device.bus_path` | `pci-0000:04:00.6` | not portable |
| `sysfs.path` | `/devices/pci0000:00/...` | not portable |
| `alsa.card_name` | `HD-Audio Generic` | generic |

For an internal mic the vendor/product pair names the controller, which is shared by
every laptop with that chipset. The part that actually shapes the sound is the
**codec** plus the **machine** (the mic capsules and their placement):

- Codec: `/proc/asound/card<N>/codec#0` → `Codec: Realtek ALC257`, `Vendor Id: 0x10ec0257`,
  `Subsystem Id: ...` (confirmed the first two exist; the Subsystem Id usually names the
  laptop's OEM board, assumed).
- Machine: `/sys/class/dmi/id/sys_vendor`, `product_name`, `product_version`,
  `board_name` (confirmed readable without root for vendor and product name).

For USB mics (assumed, verify with a USB mic plugged in): `device.vendor.id`,
`device.product.id`, `device.serial` (from udev `ID_SERIAL`), and `device.bus=usb`.

For Bluetooth (assumed): `api.bluez5.address`, `device.vendor.id`/`product.id` when
the headset reports them.

## Proposal: a mic fingerprint

Store a `mic_id` object with every profile, measurement and binding, alongside the
node name (kept for the live lookup):

```json
{
  "mic_key": "usb:0d8c:0014:SN1234",
  "bus": "usb",
  "vendor_id": "0x0d8c", "product_id": "0x0014",
  "vendor_name": "...", "product_name": "...",
  "serial": "SN1234",
  "codec": null,
  "machine": null,
  "port": "Microphone",
  "node_name_seen": "alsa_input.usb-..."
}
```

Key derivation (`mic_key`), most specific first:

| Bus | Key | Portable across machines? |
|---|---|---|
| usb, has serial | `usb:<vid>:<pid>:<serial>` | yes, follows the physical mic |
| usb, no serial | `usb:<vid>:<pid>` | yes, but twins collide (acceptable: twins sound alike) |
| bluetooth | `bt:<address>` | yes |
| pci / internal | `internal:<dmi vendor>/<dmi product>:<codec vendor id>` | no, and should not be: it is that machine's mic |

Port stays a separate field, as today. A machine ID is never put in a public file;
the profile store is the user's own (see `profile-store.md`).

## Migration

`migrate.py` already exists. On first run of the new version: for every node name in
`devices.json` / `settings.json` / `history.db` that is currently present, compute
its `mic_key` and write both. Names not present stay keyed by name until the mic is
seen again; lookups fall back from `mic_key` to node name.

## Open questions

- Is `device.serial` stable for USB mics with no descriptor serial (does udev invent
  one from the port path)? Test with two USB mics.
- Is the HDA `Subsystem Id` a better machine identifier than DMI? Compare on two laptops.
- Should the user be able to say "these two keys are the same mic" (manual merge)?
