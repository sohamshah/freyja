"""Slack manifest is on the Agent experience, not the Assistant one.

Migrating a Slack app from `assistant_view` to `agent_view` is one-way on Slack's side. Freyja's manifest is copy-paste (the wizard writes a file and copies it; it never POSTs apps.manifest.update), so a regression here would not revert the app on its own: it would sit latent until someone re-ran `freyja setup slack` and pasted an Assistant-era manifest over an upgraded app. These assertions make that regression fail in CI instead.
"""

from __future__ import annotations

from bridge.gateway.platforms.slack_manifest import BOT_EVENTS, BOT_SCOPES, build_manifest

# Events Slack stops sending once an app moves to the agent experience.
_ASSISTANT_ERA_EVENTS = {"assistant_thread_started", "assistant_thread_context_changed"}


def test_manifest_is_on_the_agent_experience():
    """agent_view with a description under Slack's 300-character cap and no assistant-era leftovers. Suggested prompts stay empty because runtime setSuggestedPrompts fails with static_prompts_configured when the manifest declares any. Code channels declare no URL: Socket Mode apps have none and Slack routes agent slash commands over the socket."""
    manifest = build_manifest()
    features = manifest["features"]
    assert "agent_view" in features and "assistant_view" not in features
    agent_view = features["agent_view"]
    assert "assistant_description" not in agent_view
    description = agent_view["agent_description"]
    assert isinstance(description, str) and 0 < len(description) <= 300
    assert agent_view["suggested_prompts"] == []
    assert not (_ASSISTANT_ERA_EVENTS & set(BOT_EVENTS))
    subscribed = set(manifest["settings"]["event_subscriptions"]["bot_events"])
    assert not (_ASSISTANT_ERA_EVENTS & subscribed)
    code_channels = features["code_channels"]
    assert code_channels["enabled"] is True and "slash_command_url" not in code_channels


def test_events_and_scopes_the_agent_surface_needs_are_declared():
    """message.im is the agent surface's input and app_context_changed is what attaches app_context to it. Without agent_session_stopped Slack warns on every setStatus and renders no Stop control. assistant:write backs the status indicator and the agents.sessions.* methods also need chat:write; reactions, customize and code channels each need their own scope."""
    assert {
        "message.im", "app_context_changed",
        "app_mention", "message.channels", "message.groups",
        "agent_session_stopped", "agent_session_title_changed",
        "app_home_opened", "reaction_added", "message.session",
    } <= set(BOT_EVENTS)
    assert {
        "assistant:write", "chat:write", "reactions:read",
        "chat:write.customize", "code_channels:manage",
    } <= set(BOT_SCOPES)
