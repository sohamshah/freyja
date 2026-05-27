"""Gateway-level configuration.

Reads ``~/.freyja/gateway.yaml`` if present, otherwise falls back to
hard-coded defaults. Hot-reloads on disk change are NOT yet wired —
v1 reads once at daemon start.

Schema (all keys optional; missing → default):

  defaults:
    model: claude-sonnet-4-6           # default model for new sessions
    coordination_strategy: bus         # default strategy
  slack:
    allowed_user_ids:                  # per-workspace allowlist of
      T012345: [U001, U002]            #   Slack user ids
      T067890: [U003]
                                       # empty list / missing → allow
                                       #   any user in that workspace
                                       # workspace not present at all
                                       #   → DENY-ALL for unknown
                                       #   workspaces (safer default)
    enforce_workspace_allowlist: true  # set false to allow any user
                                       # in any workspace
    mention_required_in_channels: true
    reply_in_thread: true
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from bridge.gateway.pid import freyja_home

logger = logging.getLogger(__name__)


def config_path() -> Path:
    return freyja_home() / "gateway.yaml"


@dataclass
class SlackConfig:
    """Slack-specific gateway config."""

    # Map of team_id → list of allowed user_ids. An entry with an
    # empty list = allow any user in that workspace. Workspace not in
    # this map at all → behavior depends on
    # ``enforce_workspace_allowlist``.
    allowed_user_ids: dict[str, list[str]] = field(default_factory=dict)
    # When true (default): a workspace not in ``allowed_user_ids`` is
    # treated as DENY-ALL. When false: any user in any workspace is
    # allowed (legacy / dev mode).
    enforce_workspace_allowlist: bool = True
    mention_required_in_channels: bool = True
    reply_in_thread: bool = True

    def user_allowed(self, team_id: str, user_id: str) -> bool:
        """Decide whether a user from a workspace should be heard."""
        if not self.enforce_workspace_allowlist:
            return True
        if not team_id or not user_id:
            return False
        if team_id not in self.allowed_user_ids:
            return False
        ids = self.allowed_user_ids[team_id]
        # Empty list = allow any user in this workspace (explicit
        # opt-in to the workspace, but no per-user restriction).
        if not ids:
            return True
        return user_id in ids


@dataclass
class GatewayConfig:
    """Top-level gateway config."""

    default_model: str = "claude-sonnet-4-6"
    default_strategy: str = "bus"
    slack: SlackConfig = field(default_factory=SlackConfig)

    @classmethod
    def load(cls) -> "GatewayConfig":
        """Read ``~/.freyja/gateway.yaml``; return defaults on missing
        / parse error."""
        path = config_path()
        if not path.exists():
            return cls()
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not parse %s: %s — using defaults", path, exc)
            return cls()
        defaults = raw.get("defaults") or {}
        slack_raw = raw.get("slack") or {}
        slack = SlackConfig(
            allowed_user_ids={
                str(team): [str(u) for u in (ids or [])]
                for team, ids in (slack_raw.get("allowed_user_ids") or {}).items()
            },
            enforce_workspace_allowlist=bool(
                slack_raw.get("enforce_workspace_allowlist", True)
            ),
            mention_required_in_channels=bool(
                slack_raw.get("mention_required_in_channels", True)
            ),
            reply_in_thread=bool(slack_raw.get("reply_in_thread", True)),
        )
        return cls(
            default_model=str(
                defaults.get("model") or "claude-sonnet-4-6"
            ),
            default_strategy=str(
                defaults.get("coordination_strategy") or "bus"
            ),
            slack=slack,
        )

    def to_yaml(self) -> str:
        """Serialize the config back to YAML for write_config."""
        payload: dict[str, Any] = {
            "defaults": {
                "model": self.default_model,
                "coordination_strategy": self.default_strategy,
            },
            "slack": {
                "allowed_user_ids": {
                    team: list(ids)
                    for team, ids in self.slack.allowed_user_ids.items()
                },
                "enforce_workspace_allowlist": self.slack.enforce_workspace_allowlist,
                "mention_required_in_channels": self.slack.mention_required_in_channels,
                "reply_in_thread": self.slack.reply_in_thread,
            },
        }
        return yaml.safe_dump(payload, sort_keys=False)


def write_config(config: GatewayConfig) -> Path:
    """Persist a GatewayConfig back to disk atomically."""
    path = config_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(config.to_yaml(), encoding="utf-8")
    tmp.replace(path)
    return path
