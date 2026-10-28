#!/usr/bin/env bash
#
# scripts/build-glass.sh — build the island's backdrop-blur shim.
#
# native/island-glass/glass.m puts a masked NSVisualEffectView behind the
# island's transparent window, so the island can be real frosted glass in
# any shape (Electron's own vibrancy frosts the whole window rectangle).
# It is an N-API module, so it works across Electron versions without a
# rebuild, and it needs only clang and the Node headers that ship with
# every Node install.
#
# Best effort by design: when anything is missing the build prints why and
# exits 0, and the island falls back to CSS glass without the blur.
set -uo pipefail
cd "$(dirname "$0")/.."

SRC=native/island-glass/glass.m
OUT=dist-native/island-glass.node

if [ "$(uname -s)" != "Darwin" ]; then
  echo "[glass] not macOS, skipping"
  exit 0
fi
if [ -f "$OUT" ] && [ "$OUT" -nt "$SRC" ] && [ "$OUT" -nt "$0" ]; then
  echo "[glass] $OUT is up to date"
  exit 0
fi
INC="$(node -p "require('path').join(process.execPath, '..', '..', 'include', 'node')" 2>/dev/null || true)"
if [ -z "$INC" ] || [ ! -f "$INC/node_api.h" ]; then
  echo "[glass] Node headers not found (looked in '$INC'); the island will use CSS glass" >&2
  exit 0
fi
if ! command -v clang >/dev/null 2>&1; then
  echo "[glass] clang not found (install the Xcode command line tools); the island will use CSS glass" >&2
  exit 0
fi
mkdir -p dist-native
if clang -x objective-c -fobjc-arc -O2 -Wall -mmacosx-version-min=12.0 -arch "$(uname -m)" \
    -shared -undefined dynamic_lookup -framework Cocoa -I"$INC" -o "$OUT" "$SRC"; then
  echo "[glass] built $OUT"
else
  echo "[glass] build failed; the island will use CSS glass" >&2
  rm -f "$OUT"
fi
exit 0
