"""Slack app manifest generator.

Hermes pattern: rather than provision the Slack app via API (OAuth +
admin install + scope grants — complex), we generate a JSON manifest
the operator pastes into Slack's "Create app from manifest" UI. That
flow takes one minute and leaves the operator with a fully-configured
app: scopes, event subscriptions, slash commands, Socket Mode, all
declared at once.

The manifest is regenerated on demand (e.g. when we add new slash
commands) and written to ``~/.freyja/slack-manifest.json``. The setup
wizard copies it to the clipboard too.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bridge.gateway.pid import freyja_home


# The slash commands Freyja exposes through Slack. Each becomes a
# native Slack slash command via the manifest. Keep the descriptions
# short — Slack truncates them in its UI.
#
# When adding a new slash command:
#   1. Add it here
#   2. Implement the handler in bridge/gateway/platforms/slack.py
#      (or route to a shared handler)
#   3. Run `freyja slack manifest --write` to regenerate the manifest
#   4. Re-paste it in Slack's app manifest UI (or run `slack manifest
#      update` if you have slack-cli installed)
SLASH_COMMANDS: list[dict[str, str]] = [
    {
        "command": "/freyja",
        "description": "Show what Freyja can do",
        "usage_hint": "[help|status]",
    },
    {
        "command": "/goal",
        "description": "Arm a goal loop with an autonomous judge",
        "usage_hint": "<objective>",
    },
    {
        "command": "/mode",
        "description": "Switch coordination strategy",
        "usage_hint": "bus|goal|kanban|isolated",
    },
    {
        "command": "/model",
        "description": "Switch the agent model for this session",
        "usage_hint": "<model-id>",
    },
    {
        "command": "/stop",
        "description": "Interrupt the current turn",
    },
    {
        "command": "/reset",
        "description": "Start a fresh conversation in this thread/DM",
    },
    {
        "command": "/status",
        "description": "Show session info (model, mode, spend, turns)",
    },
    {
        "command": "/perms",
        "description": "Show the agent's tool permissions for this session",
    },
]


# Bot OAuth scopes required for the adapter to function.
# See: https://api.slack.com/scopes
BOT_SCOPES: list[str] = [
    "app_mentions:read",       # receive app_mention events
    "assistant:write",         # use Slack AI Assistant typing/status indicators
    "channels:history",        # read public channel message history
    "channels:read",           # list channels the bot is in
    "chat:write",              # send messages
    "commands",                # register + handle slash commands
    "files:read",              # download user-attached files
    "files:write",             # upload files (images, voice, docs)
    "groups:history",          # read private channel message history
    "groups:read",             # list private channels
    "im:history",              # read DM message history
    "im:read",                 # list DMs
    "im:write",                # send DMs (sometimes implied by chat:write)
    "users:read",              # resolve user IDs to display names
]


# Bot events the adapter subscribes to via Socket Mode.
# See: https://api.slack.com/events
BOT_EVENTS: list[str] = [
    "app_mention",                          # @bot in any channel
    "assistant_thread_started",             # Slack AI Assistant lifecycle
    "assistant_thread_context_changed",
    "message.channels",                     # public channel messages
    "message.groups",                       # private channel messages
    "message.im",                           # DM messages
]


def build_manifest(
    *,
    app_name: str = "Freyja",
    description: str = "Your Freyja agent on Slack",
    bot_display_name: str = "Freyja",
    background_color: str = "#0a0a0f",
    assistant_description: str = (
        "Chat with Freyja in DMs or @mention me in channels."
    ),
) -> dict[str, Any]:
    """Build the full Slack app manifest as a Python dict.

    Pass through ``json.dumps(..., indent=2)`` for the operator to
    paste, or ``write_manifest()`` to persist + render together.
    """
    return {
        "_metadata": {
            "major_version": 1,
            "minor_version": 1,
        },
        "display_information": {
            "name": app_name,
            "description": description,
            "background_color": background_color,
        },
        "features": {
            "app_home": {
                "home_tab_enabled": False,
                "messages_tab_enabled": True,
                "messages_tab_read_only_enabled": False,
            },
            "bot_user": {
                "display_name": bot_display_name,
                "always_online": True,
            },
            "slash_commands": list(SLASH_COMMANDS),
            "assistant_view": {
                "assistant_description": assistant_description,
            },
        },
        "oauth_config": {
            "scopes": {
                "bot": list(BOT_SCOPES),
            }
        },
        "settings": {
            "event_subscriptions": {
                "bot_events": list(BOT_EVENTS),
            },
            "interactivity": {
                "is_enabled": True,
            },
            "org_deploy_enabled": False,
            "socket_mode_enabled": True,
            "token_rotation_enabled": False,
        },
    }


def manifest_path() -> Path:
    return freyja_home() / "slack-manifest.json"


def write_manifest(manifest: dict[str, Any] | None = None) -> Path:
    """Write the manifest to ``~/.freyja/slack-manifest.json`` atomically."""
    path = manifest_path()
    data = manifest if manifest is not None else build_manifest()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def manifest_json(manifest: dict[str, Any] | None = None) -> str:
    """Return the manifest serialized as a pretty JSON string."""
    return json.dumps(manifest if manifest is not None else build_manifest(), indent=2)


def known_slash_command_names() -> set[str]:
    """Set of slash command names (with leading slash) the adapter
    should route. Used by the Slack adapter to register a regex match
    of just these names so commands the user typed by mistake fall
    through to message handling."""
    return {cmd["command"] for cmd in SLASH_COMMANDS}
