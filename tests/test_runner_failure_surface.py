"""Tests for the bridge's runner-failure surfacing path.

When ``engine.runner`` gives up after consecutive ProviderError retries it
returns ``AgentResult(success=False, error=...)`` rather than raising.
Previously the bridge ignored ``result.success`` and emitted
``turn_complete(success=True)`` regardless — leaving the operator on
Slack staring at "Catching up — back to you shortly" forever while the
real failure (overloaded_error from Anthropic) lived only in the bridge
logs.

These tests pin the policy behavior of the user-facing message and
guard the no-double-message rule (we suppress the failure tail when
the agent already streamed partial prose).
"""

from __future__ import annotations

import pytest


def test_failure_message_omitted_when_already_streamed():
    """If the agent produced any prose this turn, the post-hoc failure
    line is suppressed — otherwise the operator sees their answer
    followed by an awkward "(provider error)" tail."""
    from bridge.freyja_bridge import _format_user_facing_runner_failure
    out = _format_user_facing_runner_failure(
        reason="rate_limit",
        message="overloaded_error",
        already_streamed=True,
    )
    assert out == ""


def test_rate_limit_returns_overloaded_message_with_retry_hint():
    """The most common case — Anthropic returns overloaded_error on every
    retry. Message must name the cause, surface the detail, and tell the
    operator the actionable next step (wait + retry or switch model)."""
    from bridge.freyja_bridge import _format_user_facing_runner_failure
    out = _format_user_facing_runner_failure(
        reason="rate_limit",
        message="overloaded_error",
        already_streamed=False,
    )
    assert "overloaded" in out.lower()
    assert "retry" in out.lower() or "again" in out.lower()
    # Must surface the engine's detail so operators can grep logs.
    assert "overloaded_error" in out


def test_auth_failure_surfaces_key_check_hint():
    from bridge.freyja_bridge import _format_user_facing_runner_failure
    out = _format_user_facing_runner_failure(
        reason="auth",
        message="invalid x-api-key",
        already_streamed=False,
    )
    assert "auth" in out.lower() or "key" in out.lower()


def test_context_overflow_returns_reset_hint():
    """When the engine gives up because the context exceeded the model
    limit even after compaction, the only operator-actionable advice is
    /reset or switching to a longer-context model."""
    from bridge.freyja_bridge import _format_user_facing_runner_failure
    out = _format_user_facing_runner_failure(
        reason="context",
        message="prompt is too long: 350000 tokens",
        already_streamed=False,
    )
    assert "/reset" in out or "longer" in out.lower()


def test_unknown_reason_falls_through_with_reason_tag():
    """Unknown reasons must still produce a non-empty message — silence
    is the bug we are fixing. The reason tag is surfaced so the operator
    can log-grep the cause class."""
    from bridge.freyja_bridge import _format_user_facing_runner_failure
    out = _format_user_facing_runner_failure(
        reason="some_new_reason",
        message="weird internal failure",
        already_streamed=False,
    )
    assert out  # non-empty
    assert "some_new_reason" in out


def test_long_detail_truncated_so_chat_bubble_stays_readable():
    """Provider error blobs can be huge (JSON dumps, full URLs, etc.).
    Truncate so the Slack message doesn't fill the screen. The full
    detail still lives in the bridge log."""
    from bridge.freyja_bridge import _format_user_facing_runner_failure
    huge = "x" * 5000
    out = _format_user_facing_runner_failure(
        reason="rate_limit",
        message=huge,
        already_streamed=False,
    )
    # Cap somewhere reasonable — implementation caps at 260 chars.
    assert len(out) < 700
    # Must include the truncation marker so the operator knows there
    # was more.
    assert "…" in out
