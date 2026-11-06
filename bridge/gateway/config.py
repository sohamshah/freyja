"""Gateway-level configuration.

Reads ``~/.freyja/gateway.yaml`` if present, otherwise falls back to
hard-coded defaults. Hot-reloads on disk change are NOT yet wired —
v1 reads once at daemon start.

Schema (all keys optional; missing → default):

  defaults:
    model: claude-opus-5-5             # default model for new sessions (high thinking)
    reasoning_level: xhigh             # optional default reasoning effort (low/medium/high/xhigh/max)
    coordination_strategy: bus         # default strategy
  slack:
    allowed_user_ids:                  # per-workspace allowlist of
      T012345: [U001, U002]            #   Slack user ids
      T067890: ["*"]                   # "*" → every member of that
                                       #   workspace, with full tools
                                       #   (not Slack Connect users
                                       #   from other workspaces)
                                       # empty list → nobody
                                       # workspace not present at all
                                       #   → DENY-ALL for unknown
                                       #   workspaces (safer default)
    enforce_workspace_allowlist: true  # no longer opens anything when
                                       # false; kept so old files load
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

    # Map of team_id → list of allowed user_ids. Whoever is let in gets
    # the full toolset unless ``enable_tool_filter`` is on, so opening a
    # workspace is explicit: "*" in its list lets in every member. An
    # empty list lets in no one, and a workspace not in this map at all
    # is denied.
    allowed_user_ids: dict[str, list[str]] = field(default_factory=dict)
    # False used to let in any user of any workspace. An open gateway
    # now takes "*", so this changes nothing; it stays so older files
    # and the desktop app's setup still load and save it.
    enforce_workspace_allowlist: bool = True
    mention_required_in_channels: bool = True
    reply_in_thread: bool = True
    # When true: gateway sessions get only SLACK_TOOL_ALLOWLIST
    # (capabilities.py — no bash, computer-use, browser, etc.). Default
    # False: the operator is presumed to be the only user of their
    # own install, so the agent gets the full toolset over Slack
    # too. Flip to True in shared workspaces where other people DM
    # the bot.
    enable_tool_filter: bool = False

    def user_allowed(
        self, team_id: str, user_id: str, user_team: str | None = None
    ) -> bool:
        """Decide whether a user from a workspace should be heard.

        ``user_team`` is the user's own workspace when Slack gives it, or
        "" when it does not in a channel shared with another organization.
        "*" lets in members of ``team_id`` only, never a Slack Connect user
        from another workspace, so it needs ``user_team`` to be ``team_id``
        (None: nothing suggests another workspace). A listed user id passes
        either way."""
        if not team_id or not user_id:
            return False
        ids = self.allowed_user_ids.get(team_id) or []
        if user_id in ids:
            return True
        return "*" in ids and (user_team is None or user_team == team_id)


@dataclass
class GatewayConfig:
    """Top-level gateway config."""

    # Chat-gateway (Slack/Telegram) default. This used to be
    # kimi-k3-fast on the theory that gateway turns are latency-sensitive
    # (someone is watching a thread) and so should get a fast model while
    # the desktop sat on a slower one. Moved to Opus 5.5 on 2026-09-22:
    # it is also *cheaper* than kimi-k3-fast ($4/$20 vs $4.50/$22.50),
    # and the desktop moved to the same model, so the split no longer
    # buys anything. Latency is "moderate" rather than fast — if thread
    # responsiveness regresses, the options are `claude-opus-5-5-fast`
    # (~2.5x output tokens/sec, $8/$40, needs the fast-mode allowlist) or
    # dropping reasoning_level to `low`/`medium` in the yaml.
    # Resolution order is FREYJA_MODEL env → ~/.freyja/gateway.yaml →
    # this constant, so this only bites a fresh install with no yaml.
    default_model: str = "claude-opus-5-5"
    default_reasoning_level: str | None = None
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
        allowed_raw = slack_raw.get("allowed_user_ids") or {}
        if not isinstance(allowed_raw, dict):
            # A flat list names no workspace. Fail closed, but start, so
            # the log says what to fix instead of launchd restarting a
            # gateway that crashes here.
            logger.error(
                "%s: slack.allowed_user_ids must map each workspace id to a "
                "list of user ids, like `T012345: [U001]`, or `T012345: "
                "[\"*\"]` for every member; it is a %s, so no one is let in",
                path, type(allowed_raw).__name__,
            )
            allowed_raw = {}
        slack = SlackConfig(
            allowed_user_ids={
                str(team): [str(u) for u in (ids or [])]
                for team, ids in allowed_raw.items()
            },
            enforce_workspace_allowlist=bool(
                slack_raw.get("enforce_workspace_allowlist", True)
            ),
            mention_required_in_channels=bool(
                slack_raw.get("mention_required_in_channels", True)
            ),
            reply_in_thread=bool(slack_raw.get("reply_in_thread", True)),
            enable_tool_filter=bool(
                slack_raw.get("enable_tool_filter", False)
            ),
        )
        if not slack.enforce_workspace_allowlist:
            logger.warning(
                "%s: slack.enforce_workspace_allowlist: false no longer lets "
                "everyone in; list \"*\" under a workspace in allowed_user_ids "
                "to open it", path,
            )
        for team, ids in slack.allowed_user_ids.items():
            if not ids:
                logger.warning(
                    "%s: slack.allowed_user_ids.%s is empty, so no one there "
                    "is let in; list \"*\" to let in every member", path, team,
                )
        return cls(
            default_model=str(
                defaults.get("model") or cls.default_model
            ),
            default_reasoning_level=(
                str(defaults["reasoning_level"])
                if defaults.get("reasoning_level")
                else None
            ),
            default_strategy=str(
                defaults.get("coordination_strategy") or "bus"
            ),
            slack=slack,
        )

    def to_yaml(self) -> str:
        """Serialize the config back to YAML for write_config."""
        defaults_payload: dict[str, Any] = {"model": self.default_model}
        if self.default_reasoning_level:
            defaults_payload["reasoning_level"] = self.default_reasoning_level
        defaults_payload["coordination_strategy"] = self.default_strategy
        payload: dict[str, Any] = {
            "defaults": defaults_payload,
            "slack": {
                "allowed_user_ids": {
                    team: list(ids)
                    for team, ids in self.slack.allowed_user_ids.items()
                },
                "enforce_workspace_allowlist": self.slack.enforce_workspace_allowlist,
                "mention_required_in_channels": self.slack.mention_required_in_channels,
                "reply_in_thread": self.slack.reply_in_thread,
                "enable_tool_filter": self.slack.enable_tool_filter,
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
