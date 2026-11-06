"""The publishing policy in the system prompt: a standing authorization is honoured without re-asking, stays scoped to what it names, and anything else shows a draft and asks first."""

from bridge.freyja_bridge import _EXECUTING_ACTIONS_BLOCK


def test_publishing_policy_honours_scoped_standing_authorization_and_asks_otherwise():
    policy = _EXECUTING_ACTIONS_BLOCK

    # A durable standing instruction covers later PR reviews without re-asking.
    assert "durable standing instruction" in policy
    assert "Honor matching standing authorization without asking again" in policy
    assert "review findings and inline comments when later asked to review PRs" in policy
    assert "unless they asked you to in this turn" not in policy

    # The authorization stays scoped to the action, destination and scope named.
    assert "covers the action, destination and scope" in policy
    assert "but not merges, pushes or unrelated messages" in policy
    assert "Use only the review event types their instructions authorize" in policy

    # No matching authorization, or an unknown posting identity: show the draft and ask.
    assert "does not by itself authorize publication" in policy
    assert "If no matching authorization exists, show the draft and ask" in policy
    assert "cannot tell which identity a tool posts as, ask first" in policy
