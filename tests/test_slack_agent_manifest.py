"""Slack manifest is on the Agent experience, not the Assistant one.

Migrating a Slack app from `assistant_view` to `agent_view` is one-way on
Slack's side. Freyja's manifest is copy-paste (the wizard writes a file
and copies it; it never POSTs apps.manifest.update), so a regression here
would not revert the app on its own — it would sit latent until someone
re-ran `freyja setup slack` and pasted an Assistant-era manifest over an
upgraded app. These assertions make that regression fail in CI instead.
"""

from __future__ import annotations

from bridge.gateway.platforms.slack_manifest import (
    BOT_EVENTS,
    BOT_SCOPES,
    build_manifest,
)

# Events Slack stops sending once an app moves to the agent experience.
_ASSISTANT_ERA_EVENTS = {
    "assistant_thread_started",
    "assistant_thread_context_changed",
}


def test_manifest_uses_agent_view_not_assistant_view():
    features = build_manifest()["features"]
    assert "agent_view" in features
    assert "assistant_view" not in features, (
        "assistant_view is deprecated and unavailable to new apps; pasting "
        "it over an upgraded app is the one way to regress the migration"
    )


def test_agent_view_shape_matches_slack_schema():
    agent_view = build_manifest()["features"]["agent_view"]
    # agent_description is required; the old key was assistant_description.
    assert "agent_description" in agent_view
    assert "assistant_description" not in agent_view
    description = agent_view["agent_description"]
    assert isinstance(description, str) and description
    # Slack caps the description at 300 characters.
    assert len(description) <= 300, len(description)


def test_agent_description_is_overridable():
    custom = "Freyja, but with a different blurb."
    manifest = build_manifest(agent_description=custom)
    assert manifest["features"]["agent_view"]["agent_description"] == custom


def test_assistant_era_events_are_not_subscribed():
    """Dead subscriptions: Slack no longer delivers these, so keeping them
    only misleads the next reader into thinking something listens."""
    stale = _ASSISTANT_ERA_EVENTS & set(BOT_EVENTS)
    assert not stale, stale
    manifest_events = set(
        build_manifest()["settings"]["event_subscriptions"]["bot_events"]
    )
    assert not (_ASSISTANT_ERA_EVENTS & manifest_events)


def test_events_needed_for_the_agent_surface_are_subscribed():
    events = set(BOT_EVENTS)
    # message.im is the agent surface's primary input; app_context_changed
    # is what makes Slack attach app_context to those message.im events.
    assert "message.im" in events
    assert "app_context_changed" in events
    # Channel behaviour is unaffected by the migration and must survive it.
    assert {"app_mention", "message.channels", "message.groups"} <= events


def test_scopes_cover_the_agent_session_methods():
    """assistant:write backs the status indicator; the agents.sessions.*
    methods additionally require chat:write."""
    assert "assistant:write" in BOT_SCOPES
    assert "chat:write" in BOT_SCOPES
