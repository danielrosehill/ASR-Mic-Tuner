#!/usr/bin/env bash
# Build the asr-mic-tuner .deb
#
# Output goes to dist/ rather than build/ because build/ is excluded by the
# global gitignore. dist/ is gitignored too: the .deb ships as a GitHub
# release asset, not in the tree.
set -euo pipefail
cd "$(dirname "$0")"

VERSION="${1:-$(python3 -c 'import asrmictuner; print(asrmictuner.VERSION)')}"
PKG=asr-mic-tuner
ROOT="dist/${PKG}_${VERSION}_all"

rm -rf "$ROOT"
mkdir -p "$ROOT/DEBIAN" \
         "$ROOT/usr/bin" \
         "$ROOT/usr/lib/$PKG/asrmictuner" \
         "$ROOT/usr/share/applications" \
         "$ROOT/usr/share/doc/$PKG"

install -m 644 asrmictuner/*.py "$ROOT/usr/lib/$PKG/asrmictuner/"
cat > "$ROOT/usr/bin/$PKG" <<'WRAPPER'
#!/usr/bin/python3
import sys
sys.path.insert(0, "/usr/lib/asr-mic-tuner")
from asrmictuner.cli import main
sys.exit(main())
WRAPPER
chmod 755 "$ROOT/usr/bin/$PKG"
cp asr-mic-tuner.desktop           "$ROOT/usr/share/applications/"
cp README.md docs/*.md        "$ROOT/usr/share/doc/$PKG/"

cat > "$ROOT/DEBIAN/control" <<CONTROL
Package: $PKG
Version: $VERSION
Section: sound
Priority: optional
Architecture: all
Conflicts: micscope, mic-monitor
Replaces: micscope, mic-monitor
Provides: micscope, mic-monitor
Depends: python3, python3-pyqt6, python3-numpy, pulseaudio-utils, alsa-utils
Recommends: sox, ffmpeg, pipewire-bin, libkf6config-bin, qdbus-qt6
Maintainer: Daniel Rosehill <public@danielrosehill.com>
Homepage: https://github.com/danielrosehill/ASR-Mic-Tuner
Description: Microphone monitor for dictation: true analog gain, level, noise, voice
 ASR Mic Tuner shows the real analog gain of a capture device in dB, read from the
 ALSA hardware control, alongside per-channel dBFS level meters, a clipped-
 sample counter and a live spectrum, and lets you set the gain one hardware
 step at a time.
 .
 It records room tone and voice samples, measures noise floor, mains hum,
 SNR, pitch and bandwidth, scores readiness for speech recognition, keeps a
 history with locations and notes, stores per-microphone gain presets and
 per-room noise profiles, and can ask an OpenRouter model for a review.
 .
 A Qt 6 application for KDE Plasma with a system-tray pop-up. It installs no
 system configuration and inserts no audio processing.
CONTROL

dpkg-deb --build --root-owner-group "$ROOT" "dist/${PKG}_${VERSION}_all.deb"
echo "Built: dist/${PKG}_${VERSION}_all.deb"
