"""Microphone identity: a key that follows the physical mic (docs/mic-identity.md).

PipeWire node names (`alsa_input.pci-0000_04_00.6.analog-stereo`) embed a bus
address and the card profile, so the same mic can appear under two names, and
two laptops can produce the same name. Everything remembered about a mic is
keyed by `mic_key(identity)` instead; the node name is kept only for the live
lookup.

    usb:<vid>:<pid>:<serial>     a USB mic with a serial (portable)
    usb:<vid>:<pid>              a USB mic without one (twins share it)
    bt:<address>                 a Bluetooth headset (portable)
    internal:<machine>:<codec>   a built-in mic: that machine's mic only
    node:<node name>             anything else (virtual sources, network)
"""

import functools
import re
from pathlib import Path

DMI = Path("/sys/class/dmi/id")


def _read(path):
    try:
        return Path(path).read_text(errors="replace").strip()
    except OSError:
        return ""


@functools.lru_cache(maxsize=1)
def machine():
    """DMI vendor and product, e.g. 'LENOVO 20XX...'. Readable without root."""
    vendor = _read(DMI / "sys_vendor")
    product = _read(DMI / "product_name")
    version = _read(DMI / "product_version")
    return {"vendor": vendor, "product": product, "version": version}


@functools.lru_cache(maxsize=16)
def codec(card):
    """The first HDA codec on ALSA card `card` (name, vendor id, subsystem id),
    from /proc/asound/card<N>/codec#*. None for non-HDA cards (USB, ACP DMIC)."""
    if card is None:
        return None
    for path in sorted(Path(f"/proc/asound/card{card}").glob("codec#*")):
        text = _read(path)
        out = {}
        for field, key in (("Codec", "name"), ("Vendor Id", "vendor_id"), ("Subsystem Id", "subsystem_id")):
            m = re.search(rf"^{field}:\s*(.+)$", text, re.M)
            if m:
                out[key] = m.group(1).strip()
        if out.get("vendor_id"):
            return out
    return None


def _hex(value):
    text = str(value or "").strip().lower()
    return text[2:] if text.startswith("0x") else text


def _slug(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(text)).strip("_")


def identity(name, props, card=None):
    """Everything known about the mic behind a PipeWire source, as plain JSON."""
    bus = props.get("device.bus") or ""
    api = props.get("device.api") or ""
    ident = {
        "bus": bus or ("bluetooth" if api == "bluez5" else ""),
        "vendor_id": props.get("device.vendor.id") or "",
        "product_id": props.get("device.product.id") or "",
        "vendor_name": props.get("device.vendor.name") or "",
        "product_name": props.get("device.product.name") or "",
        "serial": props.get("device.serial") or "",
        "bt_address": props.get("api.bluez5.address") or "",
        "codec": None,
        "machine": None,
        "node_name_seen": name,
    }
    if ident["bus"] == "pci" or (api == "alsa" and ident["bus"] not in ("usb", "bluetooth")):
        ident["codec"] = codec(card)
        ident["machine"] = machine()
    ident["key"] = mic_key(ident)
    return ident


def mic_key(ident):
    bus = ident.get("bus")
    vid, pid = _hex(ident.get("vendor_id")), _hex(ident.get("product_id"))
    if bus == "usb" and vid and pid:
        serial = _slug(ident.get("serial"))
        return f"usb:{vid}:{pid}:{serial}" if serial else f"usb:{vid}:{pid}"
    if bus == "bluetooth" and ident.get("bt_address"):
        return f"bt:{ident['bt_address'].upper()}"
    m = ident.get("machine")
    if m and (m.get("vendor") or m.get("product")):
        place = _slug(f"{m.get('vendor', '')}_{m.get('product', '')}")
        c = ident.get("codec") or {}
        # Controller id as well as codec: a laptop can have an HDA codec and a
        # separate digital-mic controller, each its own capture device.
        parts = [p for p in (f"{vid}-{pid}" if vid and pid else "", _hex(c.get("vendor_id"))) if p]
        return f"internal:{place}:{'-'.join(parts) or 'unknown'}"
    return f"node:{ident.get('node_name_seen', '')}"


def filename(key):
    """A profile file name for `key` (profiles/<this>.json)."""
    return _slug(key.replace(":", "__"))


def describe(ident):
    """Plain-text lines for a tooltip or report."""
    lines = [f"Mic key: {ident.get('key', '')}"]
    if ident.get("vendor_id") or ident.get("product_id"):
        lines.append(f"Vendor/product: {ident.get('vendor_id', '?')}:{ident.get('product_id', '?')}  "
                     f"{ident.get('vendor_name', '')} {ident.get('product_name', '')}".rstrip())
    if ident.get("serial"):
        lines.append(f"Serial: {ident['serial']}")
    if ident.get("bt_address"):
        lines.append(f"Bluetooth: {ident['bt_address']}")
    c = ident.get("codec") or {}
    if c:
        lines.append(f"Codec: {c.get('name', '')} {c.get('vendor_id', '')}".rstrip())
    m = ident.get("machine") or {}
    if m:
        lines.append(f"Machine: {m.get('vendor', '')} {m.get('product', '')} {m.get('version', '')}".rstrip())
    lines.append(f"Node: {ident.get('node_name_seen', '')}")
    return "\n".join(lines)


# -- the computer (v2.9): a profile is a mic, on a computer, in a place --

_CHASSIS = {
    **{n: "desktop" for n in (3, 4, 5, 6, 7, 13, 15, 16, 24, 35, 36)},
    **{n: "laptop" for n in (8, 9, 10, 14, 31, 32)},
    **{n: "tablet" for n in (11, 30)},
    **{n: "server" for n in (17, 23, 25, 28, 29)},
    **{n: "mini PC" for n in (34,)},
}


def _os_name():
    text = _read("/etc/os-release")
    m = re.search(r'^PRETTY_NAME="?([^"\n]+)"?', text, re.M)
    return m.group(1) if m else "Linux"


@functools.lru_cache(maxsize=1)
def this_machine():
    """{"id", "hostname", "kind", "os", "vendor", "product", "version"} for this computer.

    `id` is the first 12 hex digits of sha256(/etc/machine-id): stable across
    renames and reinstalls of the app, and not the raw machine-id itself.
    """
    import hashlib
    import socket
    raw = _read("/etc/machine-id") or _read("/var/lib/dbus/machine-id") or socket.gethostname()
    try:
        chassis = _CHASSIS.get(int(_read(DMI / "chassis_type") or 0), "computer")
    except ValueError:
        chassis = "computer"
    m = machine()
    return {
        "id": hashlib.sha256(raw.encode()).hexdigest()[:12],
        "hostname": socket.gethostname(),
        "kind": chassis,
        "os": _os_name(),
        "vendor": m.get("vendor", ""),
        "product": m.get("product", ""),
        "version": m.get("version", ""),
    }


def machine_label(info):
    """'daniellaptop (Linux laptop)'-style text for a this_machine() dict."""
    if not info:
        return "unknown computer"
    os_word = "Linux" if "linux" in info.get("os", "").lower() or info.get("os", "").startswith("Ubuntu") else info.get("os", "")
    return f"{info.get('hostname', '?')} ({os_word} {info.get('kind', 'computer')})".replace("( ", "(")


def is_this_machines_internal(key):
    """True for an internal:<machine>:… key of this computer."""
    m = machine()
    place = _slug(f"{m.get('vendor', '')}_{m.get('product', '')}")
    return key.startswith(f"internal:{place}:")
