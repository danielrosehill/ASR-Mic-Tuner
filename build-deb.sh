#!/usr/bin/env bash
# Build the micscope .deb
#
# Output goes to dist/ rather than build/ because build/ is excluded by the
# global gitignore, which would silently prevent committing the artefact.
set -euo pipefail
cd "$(dirname "$0")"

VERSION="${1:-0.1.0}"
PKG=micscope
ROOT="dist/${PKG}_${VERSION}_all"

if [ ! -f micscope.py ]; then
    echo "error: micscope.py not present — the app is specified but not yet" >&2
    echo "       implemented. See docs/spec.md." >&2
    exit 1
fi

rm -rf "$ROOT"
mkdir -p "$ROOT/DEBIAN" \
         "$ROOT/usr/bin" \
         "$ROOT/usr/share/applications" \
         "$ROOT/usr/share/doc/$PKG"

install -m 755 micscope.py    "$ROOT/usr/bin/$PKG"
cp micscope.desktop           "$ROOT/usr/share/applications/"
cp README.md                  "$ROOT/usr/share/doc/$PKG/"
cp docs/spec.md               "$ROOT/usr/share/doc/$PKG/"
cp docs/gain-mapping.md       "$ROOT/usr/share/doc/$PKG/"

cat > "$ROOT/DEBIAN/control" <<CONTROL
Package: $PKG
Version: $VERSION
Section: sound
Priority: optional
Architecture: all
Depends: python3, python3-gi, gir1.2-gtk-3.0, pulseaudio-utils, alsa-utils
Maintainer: Daniel Rosehill <public@danielrosehill.com>
Homepage: https://github.com/danielrosehill/micscope
Description: Microphone gain, level and clipping meter showing true analog gain
 micscope shows the real analog gain of a capture device in dB, read from the
 ALSA hardware control, alongside per-channel dBFS level meters with peak hold
 and a clipped-sample counter.
 .
 PipeWire and PulseAudio report a microphone volume that is not the analog
 preamp gain; on some HDA codecs the two differ by as much as 60 dB, and the
 whole usable analog range is compressed into the bottom third of the desktop
 slider. Gain set from those tools therefore drives the ADC into clipping while
 appearing moderate, which destroys speech-recognition accuracy irrecoverably.
 .
 micscope installs no system configuration and inserts no audio processing: it
 observes the device and sets its gain, leaving the audio graph unchanged.
CONTROL

dpkg-deb --build --root-owner-group "$ROOT" "dist/${PKG}_${VERSION}_all.deb"
echo "Built: dist/${PKG}_${VERSION}_all.deb"
