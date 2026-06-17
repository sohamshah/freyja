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
# After the FIRST rebuild with this script, grant TCC permissions in
# System Settings → Privacy & Security. The closing message of this
# script walks you through what each one does and why you want it.
# Subsequent rebuilds keep all grants because the signing identity
# stays the same.
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
  · Run: npm run setup-signing-cert
  · Or export FREYJA_SIGN_IDENTITY to point at an existing identity:
        security find-identity -v -p codesigning
    will list the candidates.
EOF
  exit 1
fi

# ───── pre-flight: node + python deps ────────────────────────────────
# Single-command guarantee: if a fresh clone runs `npm run rebuild`,
# everything below should self-heal. We don't try to be clever about
# skip-if-unchanged — `npm install` and `uv sync` are both near-instant
# no-ops when nothing's changed, and the cost of getting freshness
# wrong (silently shipping a stale Python bundle without slack-sdk)
# is too high.

if [ ! -d node_modules ]; then
  echo "→ Installing npm deps (first run)…"
  npm install --no-audit --no-fund
fi

if ! command -v uv >/dev/null 2>&1; then
  cat >&2 <<EOF
ERROR: \`uv\` not on PATH. Install with:
  curl -LsSf https://astral.sh/uv/install.sh | sh
Then re-run \`npm run rebuild\`.
EOF
  exit 1
fi

echo "→ Syncing Python deps via uv (no-op if .venv is current)…"
uv sync --quiet

# ───── refresh the python bundle ─────────────────────────────────────
# electron-builder copies python-bundle/ verbatim into Resources/. If
# the bundle on disk is stale (e.g., new deps added to pyproject since
# the last bundle), the packaged app silently ships without them. Always
# regenerate from the freshly-synced .venv so the bundle matches code.
echo "→ Refreshing python-bundle/ from .venv…"
bash scripts/bundle-python.sh

# ───── quit running app so we can replace the bundle ─────────────────
# `osascript … to quit` is a graceful quit. Falls back to a hard
# pkill if AppleScript fails (app not running, AppleScript disabled).
echo "→ Quitting running Freyja (if any)…"
osascript -e 'tell application "Freyja" to quit' >/dev/null 2>&1 || true
sleep 1
pkill -f "Freyja.app/Contents/MacOS/Freyja" >/dev/null 2>&1 || true
sleep 0.5

# ───── build ────────────────────────────────────────────────────────
echo "→ Building Electron bundle with identity \"$IDENTITY\"…"
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

# ───── restart the gateway daemon (if installed) ────────────────────
# launchd holds the OLD daemon in memory across rebuilds because the
# Python code only loads at process start. Without an explicit
# restart the daemon keeps running pre-rebuild bridge/gateway/* code
# even though python-bundle/ on disk has the new version.
#
# IMPORTANT — what was here before silently failed:
#
#   launchctl stop co.freyja.gateway      # async: sends SIGTERM, returns
#   sleep 1                                # daemon needs ~3-5s to drain
#   launchctl start co.freyja.gateway      # no-op: service still "active"
#                                          # Then daemon exits 0,
#                                          # KeepAlive.SuccessfulExit=false
#                                          # → no auto-restart → stale dead.
#
# Two layers of correctness now:
#   1. Wait for the OLD daemon's PID to actually disappear before
#      issuing start. Poll the pid file + ``kill -0`` against the pid.
#   2. After start, poll for a NEW PID to appear with a different
#      value than the old one. If it doesn't show within 15s, fail
#      the rebuild loudly with the daemon's stderr so the operator
#      sees what broke rather than thinking the rebuild succeeded.
if [ -f ~/Library/LaunchAgents/co.freyja.gateway.plist ]; then
  echo "→ Restarting gateway daemon to pick up new Python code…"
  PID_FILE="$HOME/.freyja/.gateway.pid"
  OLD_PID=""
  if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null || echo "")
  fi
  echo "  · old PID: ${OLD_PID:-<none>}"

  # 1. Send the stop. Don't fail if it errors — service may not
  #    currently be active.
  launchctl stop co.freyja.gateway 2>/dev/null || true

  # 2. Wait up to 15s for the old PID to actually die. Polls ``kill
  #    -0`` (signal 0 = liveness check, no actual signal sent) which
  #    works whether or not we own the process.
  if [ -n "$OLD_PID" ]; then
    for i in $(seq 1 30); do
      kill -0 "$OLD_PID" 2>/dev/null || break
      sleep 0.5
    done
    if kill -0 "$OLD_PID" 2>/dev/null; then
      echo "  · WARN: old daemon (pid $OLD_PID) didn't exit in 15s, sending SIGKILL"
      kill -KILL "$OLD_PID" 2>/dev/null || true
      sleep 1
    fi
  fi

  # 3. Start the new one + wait for a fresh PID to register.
  launchctl start co.freyja.gateway 2>/dev/null || true
  NEW_PID=""
  for i in $(seq 1 30); do
    if [ -f "$PID_FILE" ]; then
      CANDIDATE=$(cat "$PID_FILE" 2>/dev/null || echo "")
      if [ -n "$CANDIDATE" ] && [ "$CANDIDATE" != "$OLD_PID" ]; then
        if kill -0 "$CANDIDATE" 2>/dev/null; then
          NEW_PID="$CANDIDATE"
          break
        fi
      fi
    fi
    sleep 0.5
  done

  if [ -n "$NEW_PID" ]; then
    echo "  · new daemon up: pid $NEW_PID"
  else
    echo ""
    echo "  · ERROR: gateway daemon did not come up within 15s after restart."
    echo "  · Last 20 lines of ~/.freyja/logs/gateway.err:"
    tail -20 ~/.freyja/logs/gateway.err 2>/dev/null | sed 's/^/      /'
    echo ""
    echo "  · Last 20 lines of ~/.freyja/logs/gateway.log:"
    tail -20 ~/.freyja/logs/gateway.log 2>/dev/null | sed 's/^/      /'
    echo ""
    echo "  · The .app rebuild succeeded but the daemon is dead. Fix the"
    echo "    error above, then run \`launchctl start co.freyja.gateway\`"
    echo "    or re-run npm run rebuild after the fix."
    exit 1
  fi
fi

# ───── restart the scheduler daemon (if installed) ──────────────────
# The scheduler runs as its own LaunchAgent (com.freyja.scheduler) that
# fires jobs headless — including the morning briefer. Like the gateway,
# launchd keeps the OLD process alive across a rebuild, so it keeps
# executing pre-rebuild bridge code until kicked. (This is exactly how a
# briefer rebrief silently ran without the new recency block: the desktop
# app updated but this daemon didn't.) kickstart -k does stop+start in
# one call; -k is harmless if it happens to be stopped.
if [ -f ~/Library/LaunchAgents/com.freyja.scheduler.plist ]; then
  echo "→ Restarting scheduler daemon to pick up new Python code…"
  # kickstart -k is a synchronous kill+restart (unlike the gateway's
  # async stop/start dance above), so no PID polling is needed. We DON'T
  # silence the failure path: a kickstart that fails here means the
  # daemon keeps running stale code — exactly the bug this block exists
  # to prevent — so surface it (non-fatal; the app itself still works).
  if ! launchctl kickstart -k "gui/$(id -u)/com.freyja.scheduler"; then
    echo "  · WARN: scheduler daemon restart failed (it may not be loaded)."
    echo "    If briefings look stale, run:"
    echo "      launchctl kickstart -k gui/\$(id -u)/com.freyja.scheduler"
  fi
fi

# ───── launch ───────────────────────────────────────────────────────
if [ "$OPEN_APP" -eq 1 ]; then
  echo "→ Launching"
  open "$DST"
fi

echo
echo "✓ Done."
cat <<'PERMS_BLOCK'

  First-rebuild permission grants
  ───────────────────────────────
  If this is the first rebuild with the "Freyja Dev" identity, you
  need to grant four permissions ONCE in System Settings → Privacy &
  Security. After this, every subsequent `npm run rebuild` keeps them.

  · Screen Recording
      Required. Lets Freyja capture your screen so agents can see
      what's on it. Without this, the agent can't see anything
      outside Freyja's own window — computer-use is dead.

  · Accessibility
      Required. Lets Freyja read and control other apps' UI: find a
      button by its label, click a specific element, scroll a
      specific pane, read window layouts. Without this, the agent
      can see pixels but can't reliably interact with any app.

  · Input Monitoring
      Required. Lets Freyja move the mouse, click, and type into
      other apps on your behalf. Without this, the agent can look
      and read but can't actually drive anything.

  · Full Disk Access
      Optional. Lets Freyja (and any bash command it runs) read and
      write files outside your home directory, plus reach protected
      directories — ~/Library, ~/Documents, ~/Desktop, ~/Downloads
      on macOS 15+. Skip this if you only want the agent operating
      inside your project tree under ~/.

  For each one, toggle Freyja on. If you see duplicate Freyja rows
  (from older unsigned builds), remove the unchecked ones with `-`
  and keep only the row tied to /Applications/Freyja.app.

  Quit + relaunch Freyja after granting the last one so all four
  are active in the same running instance.
PERMS_BLOCK
