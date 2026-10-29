#!/usr/bin/env bash
#
# scripts/build-glass.sh — build the island's glass shim.
#
# native/island-glass/glass.m puts system glass behind the island's
# transparent window in exactly the island's shape: Liquid Glass
# (NSGlassEffectView) on macOS 26, a masked NSVisualEffectView blur before
# that (Electron's own vibrancy frosts the whole window rectangle). It is an
# N-API module that looks the glass class up at runtime, so it works across
# Electron and macOS versions without a rebuild, and it needs only clang and
# the Node headers that ship with every Node install.
#
# Best effort by design: when anything is missing the build prints why and
# exits 0, and the island falls back to a solid CSS pane.
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
  echo "[glass] Node headers not found (looked in '$INC'); the island will paint a solid pane" >&2
  exit 0
fi
if ! command -v clang >/dev/null 2>&1; then
  echo "[glass] clang not found (install the Xcode command line tools); the island will paint a solid pane" >&2
  exit 0
fi
mkdir -p dist-native
if clang -x objective-c -fobjc-arc -O2 -Wall -mmacosx-version-min=12.0 -arch "$(uname -m)" \
    -shared -undefined dynamic_lookup -framework Cocoa -I"$INC" -o "$OUT" "$SRC"; then
  echo "[glass] built $OUT"
else
  echo "[glass] build failed; the island will paint a solid pane" >&2
  rm -f "$OUT"
fi
exit 0
