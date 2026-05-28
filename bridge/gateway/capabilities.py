"""Capability sets for gateway-routed sessions.

When a session is created by an inbound platform message (vs the
desktop renderer), it runs unattended — the operator can't approve a
per-tool permission prompt, and the session may run anywhere the
gateway daemon is hosted. We restrict the tool surface to safe,
read-mostly capabilities by default.

The default set lives here as code. v2 will read it from
``~/.freyja/gateway.yaml`` with per-workflow overrides.
"""

from __future__ import annotations

from typing import Any


# Tools the gateway-routed agent is allowed to call. Everything else
# is stripped from the registry at session-init time (see
# ``_BridgeSession.initialize`` in ``bridge/freyja_bridge.py``).
#
# Three buckets:
#
#   1. READ — safe reads of the local workspace. The agent can
#      reference code, docs, and previously-produced artifacts.
#   2. RESEARCH — internet access for context. No persistent
#      side effects on the user's machine.
#   3. WRITE_SCOPED — limited writes confined to project output
#      dir. Lets the agent persist artifacts (notes, generated
#      assets) without touching the user's source tree.
#   4. SYNTHESIS — sub-agent spawn so the agent can fan out to
#      explore-fast / verify / code profiles. Sub-agents inherit
#      this same capability set when they themselves are spawned
#      from a gateway session.
#
# Notably ABSENT:
#   · bash — too broad; even read-only intent gets misused
#   · computer / computer_use / click / move_mouse / type_text
#     / press_key / screenshot — no operator at keyboard to veto
#   · browser_execute_js / browser_screenshot — same reason
#   · generate_image / analyze_video — fine in principle but not
#     part of the demo surface; reintroduce after operator opts in
#   · memory mutators (record_user_preference) — agent shouldn't
#     mutate the operator's memory store unattended
#   · session_memory — same reason
#
# Future: this becomes per-platform / per-workspace / per-workflow.
SLACK_TOOL_ALLOWLIST: frozenset[str] = frozenset({
    # READ
    "read_file",
    "list_directory",
    "glob",
    "grep",
    "artifacts",
    # RESEARCH
    "web_search",
    "web_fetch",
    "web_research",
    # WRITE (scoped)
    "write_file",
    "edit_file",
    "edit_json",
    # SYNTHESIS / coordination
    "sub_agent",
    "subagents",
    "summarize_context",
    "tool_search",
    # KNOWLEDGE (read-only)
    "list_skills",
    "search_skills",
    "load_skill",
    "memory",
    # GATEWAY meta tools (added in a later phase — e.g. /goal
    # implementation may want goal_control). Listed here so future
    # additions are explicit.
})


# Tools the gateway adds back on top of the allowlist for specific
# platforms. Keeps the default allowlist small but allows per-platform
# customization.
_PLATFORM_EXTRAS: dict[str, frozenset[str]] = {
    # No extras yet; reserved for things like Slack-specific tools
    # (e.g. a future `slack_react` for emoji reactions).
}


def tools_allowed_for_gateway(platform: Any) -> set[str]:
    """Return the set of tool names a gateway-routed session may call,
    for the given platform. Unknown platform → the slack defaults
    (the most-restrictive baseline)."""
    base = set(SLACK_TOOL_ALLOWLIST)
    if platform is not None:
        platform_name = getattr(platform, "value", None) or str(platform)
        extras = _PLATFORM_EXTRAS.get(platform_name)
        if extras:
            base |= set(extras)
    return base


def tools_denied_for_gateway(platform: Any, all_tools: set[str]) -> set[str]:
    """For audit / display: list of tools that would be stripped from
    ``all_tools`` if we ran them through ``tools_allowed_for_gateway``."""
    allowed = tools_allowed_for_gateway(platform)
    return {t for t in all_tools if t not in allowed}


def gateway_filter_enabled(platform: Any) -> bool:
    """Return True if the gateway should restrict the tool surface for
    sessions on this platform.

    Defaults to False — the operator is presumed to be the only user
    of their own install, so the agent gets the same tool surface
    over Slack that it has on the desktop (bash, computer-use,
    browser, image generation, memory mutations, etc.). Flip on per
    workspace via ``slack.enable_tool_filter: true`` in
    ``~/.freyja/gateway.yaml`` when a shared workspace has other
    users who can DM the bot.

    Read once per session init (no long-running cache). If the
    operator edits gateway.yaml, the change takes effect at the
    next session creation.
    """
    if platform is None:
        return False
    try:
        from bridge.gateway.config import GatewayConfig
        cfg = GatewayConfig.load()
    except Exception:  # noqa: BLE001
        # Config malformed / missing — fail open (no filter) rather
        # than silently strip the agent's tools.
        return False
    platform_name = getattr(platform, "value", None) or str(platform)
    if platform_name == "slack":
        return cfg.slack.enable_tool_filter
    # Unknown platform: no filter by default. Add per-platform
    # toggles to GatewayConfig as new platforms ship.
    return False
