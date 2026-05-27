#!/usr/bin/env bash
#
# scripts/rebuild.sh — one-command rebuild + install that preserves
# macOS TCC permissions (Screen Recording, Accessibility, Automation,
# Input Monitoring, Full Disk Access) across builds.
#
# How it works
# ────────────
# macOS keys TCC grants by the app's code-signing "Designated
# Requirement". Ad-hoc signatures (the default `codesign --sign -`)
# have no stable identity, so macOS treats every rebuild as a fresh
# app and forgets every grant. The fix is to sign every build with
# the SAME self-signed certificate. TCC then recognises the rebuild
# as the same app and keeps the grants.
#
# One-time setup
# ──────────────
# Easiest:
#   npm run setup-signing-cert
# That runs scripts/setup-signing-cert.sh which creates a 10-year
# self-signed Code Signing cert named "Freyja Dev" via openssl,
# imports it into your login keychain, and trusts it for code
# signing (sudo prompt once).
#
# Manual (Keychain Access GUI):
#   1. Open Keychain Access.
#   2. Menu: Keychain Access → Certificate Assistant → Create a
#      Certificate…  (if the menu doesn't show Certificate
#      Assistant, just use the CLI script above)
#   3. Set:
#        Name:              Freyja Dev
#        Identity Type:     Self Signed Root
#        Certificate Type:  Code Signing
#        Let me override defaults: ✓ (so you can extend validity)
#   4. Click through defaults. After creation, right-click the cert
#      → Get Info → Trust → set Code Signing to "Always Trust".
#
# Either way, verify:
#   security find-identity -v -p codesigning | grep "Freyja Dev"
# Should print one line with a hex hash.
#
# After the FIRST rebuild with this script, grant the relevant TCC
# permissions as usual via System Settings → Privacy & Security:
#
#   · Screen Recording   (computer-use screenshots, agent screen
#                         observation)
#   · Accessibility       (UI inspection, click / type / scroll, AX
#                         tree reads)
#   · Input Monitoring    (synthesizing key events the agent fires)
#   · Full Disk Access    (read/write outside the home directory, plus
#                         protected dirs like ~/Library, ~/Documents,
#                         and ~/Desktop on macOS 15+. Skip if you only
#                         want the agent to touch your project tree.)
#
# Subsequent rebuilds will not require re-granting because the signing
# identity stays the same.
#
# Usage
# ─────
#   ./scripts/rebuild.sh            # build, copy to /Applications, launch
#   ./scripts/rebuild.sh --no-open  # build + install, don't launch
#   FREYJA_SIGN_IDENTITY="Other Name" ./scripts/rebuild.sh
#                                    # use a different keychain identity
#
# Reset path (when you DO want a clean TCC slate)
# ────────────────────────────────────────────────
#   tccutil reset All co.freyja.desktop
# Then the next launch will re-prompt for permissions.

set -euo pipefail

# Resolve repo root regardless of where the script is invoked from.
cd "$(dirname "$0")/.."

IDENTITY="${FREYJA_SIGN_IDENTITY:-Freyja Dev}"
OPEN_APP=1
for arg in "$@"; do
  case "$arg" in
    --no-open) OPEN_APP=0 ;;
    --help|-h)
      sed -n '1,60p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
  esac
done

# ───── pre-flight: verify the signing identity exists ────────────────
if ! security find-identity -v -p codesigning | grep -F "\"$IDENTITY\"" > /dev/null; then
  cat >&2 <<EOF
ERROR: code-signing identity "$IDENTITY" not found in keychain.

Either:
  · Create one via Keychain Access (see the comment at the top of
    this script for the one-time setup steps), or
  · Export FREYJA_SIGN_IDENTITY to point at an existing identity:
        security find-identity -v -p codesigning
    will list the candidates.
EOF
  exit 1
fi

# ───── quit running app so we can replace the bundle ─────────────────
# `osascript … to quit` is a graceful quit. Falls back to a hard
# pkill if AppleScript fails (app not running, AppleScript disabled).
echo "→ Quitting running Freyja (if any)…"
osascript -e 'tell application "Freyja" to quit' >/dev/null 2>&1 || true
sleep 1
pkill -f "Freyja.app/Contents/MacOS/Freyja" >/dev/null 2>&1 || true
sleep 0.5

# ───── build ────────────────────────────────────────────────────────
echo "→ Building bundle with identity \"$IDENTITY\"…"
FREYJA_SIGN_IDENTITY="$IDENTITY" npm run dist

# ───── install ──────────────────────────────────────────────────────
SRC="out/mac-arm64/Freyja.app"
DST="/Applications/Freyja.app"

if [ ! -d "$SRC" ]; then
  echo "ERROR: build output $SRC not found — did npm run dist fail?" >&2
  exit 1
fi

echo "→ Installing to $DST"
# Remove old bundle wholesale. Partial replacement leaves stale
# resources that can cause weird half-state on launch.
sudo rm -rf "$DST" 2>/dev/null || rm -rf "$DST"
cp -R "$SRC" "$DST"

# Verify signature on the installed copy. If the cdhash on the
# copy differs from the build output, something stripped the
# signature during copy (rare but possible with sudo / network
# volumes).
echo "→ Verifying signature on installed bundle…"
if ! codesign --verify --deep --strict "$DST" 2>/dev/null; then
  echo "  WARN: codesign verification of $DST failed. Re-signing in place…"
  FREYJA_SIGN_IDENTITY="$IDENTITY" \
    codesign --force --deep --sign "$IDENTITY" --timestamp=none "$DST"
fi

# Print the Designated Requirement so you can confirm it's stable
# across rebuilds. Same DR = TCC will remember grants.
echo "→ Designated Requirement (should be identical across rebuilds):"
codesign --display --requirements - "$DST" 2>&1 \
  | sed -n 's/^designated => //p' \
  | sed 's/^/    /'

# ───── launch ───────────────────────────────────────────────────────
if [ "$OPEN_APP" -eq 1 ]; then
  echo "→ Launching"
  open "$DST"
fi

echo
echo "✓ Done."
echo "  If this is the first rebuild with the \"$IDENTITY\" identity,"
echo "  you'll need to grant TCC permissions once in System Settings"
echo "  → Privacy & Security:"
echo "    · Screen Recording  (computer-use screenshots)"
echo "    · Accessibility      (click / type / scroll, AX tree reads)"
echo "    · Input Monitoring   (synthesized key events)"
echo "    · Full Disk Access   (reach beyond ~/ — protected dirs,"
echo "                          ~/Library, ~/Documents on macOS 15+,"
echo "                          arbitrary paths in bash tools)"
echo "  After that, subsequent ./scripts/rebuild.sh runs keep all grants."
