"""The operator-facing text for a runner give-up, in one place.

When ``engine.runner`` gives up after consecutive ProviderError retries it
returns ``AgentResult(success=False, error=...)`` rather than raising.
Previously the bridge ignored ``result.success`` and emitted
``turn_complete(success=True)`` regardless — leaving the operator on
Slack staring at "Catching up — back to you shortly" forever while the
real failure (overloaded_error from Anthropic) lived only in the bridge
logs.

``_format_user_facing_runner_failure`` encodes these product decisions:

* After streamed prose a provider error stays quiet (the operator has an
  answer; a "(provider error)" tail is noise), but a refusal and the step
  ceiling still show — they are what explains a reply cut off mid-word or
  an announced action that never happened.
* Each cause names itself, carries the engine's detail (so operators can
  grep the logs) and ends with the next step the operator can take.
* An org with no fast-mode quota is told so, not "overloaded".
"""

from __future__ import annotations

import pytest

from bridge.freyja_bridge import _format_user_facing_runner_failure

_NO_QUOTA = (
    "This request would exceed your organization's rate limit of 0 fast mode "
    "input tokens per minute (org: 00000000-0000-4000-8000-000000000000, "
    "model: claude-opus-4-8)"
)
_REFUSAL_DETAIL = (
    "category=cyber; explanation: Request touched restricted cybersecurity "
    "content; 1625 chars of partial output were cut off mid-stream and "
    "discarded from the model's context"
)
EMPTY = object()


@pytest.mark.parametrize(
    "kwargs,prefix,contains,excludes",
    [
        pytest.param(
            dict(reason="rate_limit", message="overloaded_error", already_streamed=True),
            None, EMPTY, (),
            id="provider-error-after-prose-stays-quiet",
        ),
        pytest.param(
            dict(reason="rate_limit", message="overloaded_error", already_streamed=False),
            None, ("overloaded", "overloaded_error", ("retry", "again")), (),
            id="rate-limit-names-cause-detail-and-next-step",
        ),
        pytest.param(
            dict(reason="auth", message="invalid x-api-key", already_streamed=False),
            None, (("auth", "key"), "invalid x-api-key"), (),
            id="auth-points-at-the-key",
        ),
        pytest.param(
            dict(
                reason="context",
                message="prompt is too long: 350000 tokens",
                already_streamed=False,
            ),
            None, (("/reset", "longer"),), (),
            id="context-overflow-offers-reset-or-a-longer-model",
        ),
        pytest.param(
            dict(
                reason="some_new_reason",
                message="weird internal failure",
                already_streamed=False,
            ),
            None, ("some_new_reason",), (),
            id="unknown-reason-is-never-silent-and-carries-its-tag",
        ),
        pytest.param(
            dict(reason="rate_limit", message=_NO_QUOTA, already_streamed=False),
            None, ("Fast mode is not enabled", "fast-mode quota"), ("overloaded",),
            id="no-fast-quota-is-not-overload",
        ),
        pytest.param(
            dict(
                reason="unknown",
                message="Reached maximum iterations (100)",
                already_streamed=True,
                code="max_iterations",
            ),
            "\n\n---\n", ("100 steps", "did NOT"), (),
            id="step-limit-shows-after-prose-and-negates-the-announced-action",
        ),
        pytest.param(
            dict(
                reason="unknown",
                message="Reached maximum iterations (100)",
                already_streamed=False,
                code="max_iterations",
            ),
            "⚠️", ("100 steps",), (),
            id="step-limit-without-prose-has-no-separator",
        ),
        pytest.param(
            dict(
                reason="unknown",
                message="Reached maximum iterations",
                already_streamed=False,
                code="max_iterations",
            ),
            None, ("step limit",), ("()",),
            id="step-limit-survives-an-unparseable-count",
        ),
        pytest.param(
            dict(reason="refusal", message=_REFUSAL_DETAIL, already_streamed=True),
            None,
            ("refusal", "category=cyber", "Request touched restricted cybersecurity content"),
            (),
            id="refusal-shows-after-prose-with-the-full-reason",
        ),
        pytest.param(
            dict(
                reason="refusal",
                message="the API returned no stop_details for this refusal",
                already_streamed=False,
            ),
            None, ("refusal", "no stop_details", "claude-opus-4-8"), (),
            id="refusal-without-details-says-so-and-names-a-way-out",
        ),
    ],
)
def test_failure_text_policy(kwargs, prefix, contains, excludes):
    out = _format_user_facing_runner_failure(**kwargs)

    if contains is EMPTY:
        assert out == ""
        return
    assert out
    if prefix is not None:
        assert out.startswith(prefix), out[:20]
    for needle in contains:
        if isinstance(needle, tuple):
            assert any(n in out for n in needle), (needle, out)
        else:
            assert needle in out, (needle, out)
    for needle in excludes:
        assert needle not in out.lower(), (needle, out)


def test_long_detail_is_truncated_so_the_chat_bubble_stays_readable():
    """Provider error blobs can be huge (JSON dumps, full URLs). The full
    detail still lives in the bridge log; the bubble gets a marked cut."""
    out = _format_user_facing_runner_failure(
        reason="rate_limit", message="x" * 5000, already_streamed=False
    )
    assert len(out) < 700
    assert "…" in out
