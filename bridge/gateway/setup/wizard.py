"""Interactive `freyja setup slack` wizard.

Walks the operator through:
  1. Generating the Slack app manifest + copying to clipboard
  2. Prompting them to paste-create the app at api.slack.com
  3. Enabling Socket Mode + collecting the App-Level Token
  4. Installing the app + collecting the Bot Token
  5. Verifying the tokens via auth_test
  6. Showing default capability defaults (informational, v1)
  7. Offering to install + start the launchd service
  8. Walk-in: how to test the bot in Slack

Designed for `freyja setup` and `freyja setup slack` both.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Any

from bridge.gateway.platforms.slack_manifest import (
    build_manifest,
    manifest_json,
    manifest_path,
    write_manifest,
)
from bridge.gateway.pid import freyja_home, get_running_pid
from bridge.gateway.setup.env_writer import get_env_value, save_env_value

try:
    from bridge.gateway.setup import launchd as launchd_mod
    _LAUNCHD_AVAILABLE = sys.platform == "darwin"
except Exception:  # noqa: BLE001
    launchd_mod = None  # type: ignore
    _LAUNCHD_AVAILABLE = False


# ── ANSI styling helpers ─────────────────────────────────────────


def _is_tty() -> bool:
    return sys.stdout.isatty() and sys.stdin.isatty()


def _ansi(code: str, text: str) -> str:
    if not _is_tty():
        return text
    return f"\033[{code}m{text}\033[0m"


def _dim(t: str) -> str:    return _ansi("2", t)
def _bold(t: str) -> str:   return _ansi("1", t)
def _green(t: str) -> str:  return _ansi("32", t)
def _red(t: str) -> str:    return _ansi("31", t)
def _yellow(t: str) -> str: return _ansi("33", t)
def _cyan(t: str) -> str:   return _ansi("36", t)


def _hr() -> None:
    print(_dim("─" * 60))


def _step(n: int, total: int, title: str) -> None:
    print()
    print(_bold(f"Step {n}/{total} — {title}"))
    _hr()


def _info(msg: str) -> None:
    print(f"  {msg}")


def _ok(msg: str) -> None:
    print(_green(f"  ✓ {msg}"))


def _warn(msg: str) -> None:
    print(_yellow(f"  ⚠ {msg}"))


def _err(msg: str) -> None:
    print(_red(f"  ✗ {msg}"))


def _prompt(label: str, *, password: bool = False, default: str | None = None) -> str:
    suffix = ""
    if default is not None:
        suffix = _dim(f" [{default}]")
    line = f"  {label}{suffix}: "
    try:
        if password:
            import getpass
            raw = getpass.getpass(line)
        else:
            raw = input(line)
    except EOFError:
        return default or ""
    raw = (raw or "").strip()
    if not raw and default is not None:
        return default
    return raw


def _yes_no(label: str, *, default: bool = True) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        ans = _prompt(f"{label} {suffix}", default="Y" if default else "N")
        ans = ans.lower()
        if ans in {"y", "yes"}:
            return True
        if ans in {"n", "no"}:
            return False


def _wait_for_enter(msg: str = "Press Enter to continue") -> None:
    try:
        input(_dim(f"  ⏎ {msg}"))
    except EOFError:
        pass


# ── clipboard helpers ────────────────────────────────────────────


def _copy_to_clipboard(text: str) -> bool:
    """Copy ``text`` to the system clipboard. Returns True on success."""
    if sys.platform == "darwin":
        proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        proc.communicate(text.encode("utf-8"))
        return proc.returncode == 0
    if sys.platform.startswith("linux"):
        for cmd in (["xclip", "-selection", "clipboard"], ["wl-copy"]):
            if shutil.which(cmd[0]):
                proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
                proc.communicate(text.encode("utf-8"))
                return proc.returncode == 0
    return False


# ── token validation ────────────────────────────────────────────


async def _verify_tokens(bot_token: str, app_token: str) -> tuple[bool, dict[str, Any]]:
    """Call Slack's ``auth.test`` to verify the bot token. Returns
    (ok, dict_with_team_name/bot_user_id_or_error)."""
    try:
        from slack_sdk.web.async_client import AsyncWebClient
    except ImportError:
        return False, {"error": "slack-sdk not installed"}
    client = AsyncWebClient(token=bot_token)
    try:
        result = await client.auth_test()
    except Exception as exc:  # noqa: BLE001
        return False, {"error": str(exc)}
    if not result.get("ok"):
        return False, {"error": result.get("error", "auth_test rejected")}
    return True, {
        "bot_user_id": result.get("user_id", ""),
        "bot_name": result.get("user", "?"),
        "team_id": result.get("team_id", ""),
        "team_name": result.get("team", "?"),
    }


# ── the wizard ──────────────────────────────────────────────────


def run_slack_setup() -> int:
    """Top-level Slack setup wizard. Returns shell exit code."""
    print()
    print(_bold(_cyan("Freyja → Slack setup")))
    print(_dim("This will help you provision a Slack app and configure Freyja"))
    print(_dim("to talk on it. Takes ~5 minutes."))
    print()

    # Detect existing config and offer to skip.
    existing_bot = get_env_value("SLACK_BOT_TOKEN")
    existing_app = get_env_value("SLACK_APP_TOKEN")
    if existing_bot and existing_app:
        _info(_dim(
            f"Existing tokens detected (bot={existing_bot[:6]}…, "
            f"app={existing_app[:6]}…)"
        ))
        if not _yes_no("Reconfigure Slack?", default=False):
            print()
            _info("Skipping setup. To regenerate just the manifest, run:")
            print(_dim("    freyja slack manifest --write"))
            return 0

    TOTAL = 7

    # ── Step 1: Generate manifest ──
    _step(1, TOTAL, "Generate Slack app manifest")
    manifest = build_manifest()
    manifest_text = manifest_json(manifest)
    path = write_manifest(manifest)
    _ok(f"Manifest written to {path}")
    copied = _copy_to_clipboard(manifest_text)
    if copied:
        _ok("Copied to clipboard")
    else:
        _warn("Could not access clipboard — open the file above to copy manually")

    # ── Step 2: Create app from manifest ──
    _step(2, TOTAL, "Create your Slack app")
    _info("Open this URL in your browser:")
    print(_cyan("    https://api.slack.com/apps?new_app=1"))
    _info("Then:")
    _info("  1. Click " + _bold("From an app manifest"))
    _info("  2. Pick your workspace")
    _info("  3. Paste the manifest" + (" (already in your clipboard)" if copied else ""))
    _info("  4. Review the scopes + features Slack shows you, click " + _bold("Create"))
    print()
    _wait_for_enter("Press Enter once the app is created")

    # ── Step 3: App-Level Token (Socket Mode) ──
    _step(3, TOTAL, "Enable Socket Mode + collect App-Level Token")
    _info("In your new app:")
    _info("  1. Sidebar → " + _bold("Settings → Socket Mode"))
    _info("  2. Toggle " + _bold("Enable Socket Mode") + " to ON")
    _info("  3. Click " + _bold("Generate an app-level token"))
    _info("  4. Token name: anything (e.g. 'freyja-socket')")
    _info("  5. Scope: " + _bold("connections:write"))
    _info("  6. Click " + _bold("Generate") + " — copy the token (starts with " + _bold("xapp-") + ")")
    print()
    app_token = ""
    while not app_token:
        candidate = _prompt("Paste your App Token (xapp-...)", password=True)
        if not candidate:
            if not _yes_no("App Token is required. Try again?", default=True):
                return 0
            continue
        if not candidate.startswith("xapp-"):
            _err("Token doesn't look right — Slack App Tokens start with 'xapp-'")
            if not _yes_no("Try again?", default=True):
                return 0
            continue
        app_token = candidate
    save_env_value("SLACK_APP_TOKEN", app_token)
    _ok("App Token saved")

    # ── Step 4: Bot Token (Install) ──
    _step(4, TOTAL, "Install the app to your workspace + collect Bot Token")
    _info("In your app:")
    _info("  1. Sidebar → " + _bold("Settings → Install App"))
    _info("  2. Click " + _bold("Install to <Your Workspace>"))
    _info("  3. Authorize the requested scopes (one-tap)")
    _info("  4. Copy the " + _bold("Bot User OAuth Token") + " (starts with " + _bold("xoxb-") + ")")
    print()
    bot_token = ""
    while not bot_token:
        candidate = _prompt("Paste your Bot Token (xoxb-...)", password=True)
        if not candidate:
            if not _yes_no("Bot Token is required. Try again?", default=True):
                return 0
            continue
        if not candidate.startswith("xoxb-"):
            _err("Token doesn't look right — Slack Bot Tokens start with 'xoxb-'")
            if not _yes_no("Try again?", default=True):
                return 0
            continue
        bot_token = candidate
    save_env_value("SLACK_BOT_TOKEN", bot_token)
    _ok("Bot Token saved")

    # ── Step 5: Verify ──
    _step(5, TOTAL, "Verify the connection")
    print(_dim("  Calling Slack's auth.test ..."))
    ok, info = asyncio.run(_verify_tokens(bot_token, app_token))
    if not ok:
        _err(f"auth.test failed: {info.get('error')}")
        _info("Common causes:")
        _info("  · Wrong token pasted (bot vs app)")
        _info("  · App not installed to workspace yet")
        _info("  · Token revoked")
        return 1
    _ok(
        f"Authenticated as @{info['bot_name']} in workspace "
        f"{info['team_name']} (team {info['team_id']})"
    )

    # ── Step 6a: Per-workspace allowlist ──
    _step(6, TOTAL, "Lock down who can talk to your bot")
    _info(
        "Without an allowlist, anyone in the workspace can DM your bot or "
        "@mention it. For a demo deployment we recommend allowlisting just "
        "yourself + a small handful of trusted users."
    )
    _info(
        f"Your user_id in workspace {info['team_name']}: " + _bold(info["bot_user_id"][:0] or "?")
    )
    _info(_dim("(That's the bot — find your own user_id in Slack: profile → ⋮ → 'Copy member ID')"))
    print()
    allowlist_raw = _prompt(
        "Allowed user_ids (comma-separated, or 'any' for no restriction)",
        default="",
    )
    from bridge.gateway.config import GatewayConfig, SlackConfig, write_config
    cfg = GatewayConfig.load()
    if allowlist_raw.strip().lower() == "any":
        cfg.slack.enforce_workspace_allowlist = False
        cfg.slack.allowed_user_ids = {}
        _warn(
            "Allowlist disabled — every user in every workspace can talk "
            "to the bot. NOT recommended outside dev."
        )
    elif allowlist_raw.strip():
        ids = [u.strip() for u in allowlist_raw.split(",") if u.strip()]
        cfg.slack.enforce_workspace_allowlist = True
        cfg.slack.allowed_user_ids[info["team_id"]] = ids
        _ok(
            f"Allowlist for {info['team_name']}: " + ", ".join(ids)
        )
    else:
        cfg.slack.enforce_workspace_allowlist = True
        cfg.slack.allowed_user_ids[info["team_id"]] = []  # empty = allow any user in this workspace
        _ok(
            f"Allowlist scope set to workspace {info['team_name']} "
            "(any user in this workspace allowed; other workspaces denied)"
        )
    write_config(cfg)

    # ── Step 6b: Capability defaults (informational) ──
    print()
    _info("Tool surface for Slack-routed sessions:")
    _info(_green("  ✓ read_file, list_directory, glob, grep   ") + _dim("(read-only filesystem)"))
    _info(_green("  ✓ web_search, web_fetch                   ") + _dim("(internet access)"))
    _info(_green("  ✓ sub_agent                               ") + _dim("(spawn specialized helpers)"))
    _info(_red("  ✗ bash                                    ") + _dim("(no shell over Slack)"))
    _info(_red("  ✗ computer / browser / typing tools       ") + _dim("(no UI automation)"))
    _info(_red("  ✗ write_file (outside project dir)        ") + _dim("(no destructive writes)"))
    print()
    _info(_dim("You can change these per-session from the Freyja desktop app,"))
    _info(_dim("or globally via ~/.freyja/gateway.yaml (v2)."))

    # ── Step 7: launchd install ──
    _step(7, TOTAL, "Install the gateway as a background service")
    if not _LAUNCHD_AVAILABLE:
        _warn(
            "Auto-install only supported on macOS. Run the gateway manually with:"
        )
        print(_cyan("    freyja gateway run"))
    else:
        _info("Installs ~/Library/LaunchAgents/co.freyja.gateway.plist.")
        _info("The gateway will start at login + survive Electron quit.")
        if _yes_no("Install + start now?", default=True):
            try:
                assert launchd_mod is not None
                path = launchd_mod.install()
                _ok(f"Installed plist at {path}")
                # Wait briefly for the daemon to register its PID.
                for _ in range(30):  # up to ~3s
                    pid = get_running_pid()
                    if pid:
                        _ok(f"Gateway running (pid {pid})")
                        break
                    time.sleep(0.1)
                else:
                    _warn(
                        "Gateway didn't write its PID file within 3s — "
                        "check `freyja gateway logs`"
                    )
            except subprocess.CalledProcessError as exc:
                _err(f"launchctl load failed: {exc}")
            except Exception as exc:  # noqa: BLE001
                _err(f"install failed: {exc}")
        else:
            _info("To start it manually later:")
            print(_cyan("    freyja gateway run         ") + _dim("# foreground"))
            print(_cyan("    freyja gateway install     ") + _dim("# launchd"))

    # ── Walk-in ──
    print()
    _hr()
    print(_bold(_green("Setup complete!")))
    print()
    _info("Test it now:")
    _info(f"  1. Open Slack and find " + _bold(f"@{info['bot_name']}"))
    _info("     in your DMs (might be under 'Apps' in the sidebar)")
    _info("  2. Type a message: " + _dim("'hi, what can you do?'"))
    print()
    _info("Useful commands:")
    print(_cyan("    freyja gateway status   ") + _dim("# see what's running"))
    print(_cyan("    freyja gateway logs     ") + _dim("# tail the gateway log"))
    print(_cyan("    freyja gateway stop     ") + _dim("# stop the daemon"))
    print(_cyan("    freyja setup slack      ") + _dim("# reconfigure"))
    print()
    _info("Slash commands available in Slack:")
    _info("  /freyja          " + _dim("what this bot can do"))
    _info("  /goal <obj>      " + _dim("arm a goal loop"))
    _info("  /mode <strategy> " + _dim("switch coordination strategy"))
    _info("  /stop            " + _dim("interrupt the current turn"))
    _info("  /status          " + _dim("session info"))
    print()
    return 0


def run_full_setup() -> int:
    """Future: branches into different setup flows. For v1, just Slack."""
    return run_slack_setup()
