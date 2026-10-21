"""Both of Slack's "that stream is gone" errors must disable card emission.

Slack rejects an append to a finished stream two different ways:

  · ``message_not_in_streaming_state`` — the message exists, but stopStream
    already sealed it.
  · ``message_not_found`` — the message is gone entirely.

Only the first was ever matched. A stream that died the second way left
``cards_disabled`` False, so every subsequent event retried the append and
logged a full traceback. A Slack turn on 2026-09-09 produced 44 identical
failures this way while the progress card sat frozen on "Searching".
"""

from __future__ import annotations

import inspect

import pytest

from bridge.gateway.stream_consumer import SlackStreamConsumer, _stream_is_dead


@pytest.mark.parametrize(
    "error",
    [
        "message_not_in_streaming_state",
        "message_not_found",
        "The request to the Slack API failed. {'ok': False, 'error': 'message_not_found'}",
        "{'ok': False, 'error': 'message_not_in_streaming_state'}",
        "MESSAGE_NOT_FOUND",
    ],
)
def test_dead_stream_errors_are_recognized(error):
    assert _stream_is_dead(error) is True


@pytest.mark.parametrize(
    "error",
    [
        None,
        "",
        "ratelimited",
        "channel_not_found",
        "invalid_auth",
        "not connected",
    ],
)
def test_live_or_unrelated_errors_are_not_treated_as_dead(error):
    """Disabling cards on a transient error would silently drop progress
    output for the rest of the turn."""
    assert _stream_is_dead(error) is False


def test_no_call_site_still_matches_the_raw_string():
    """Every check must go through the predicate — a lingering literal is
    exactly the half-covered case this bug was."""
    src = inspect.getsource(SlackStreamConsumer)
    assert 'if "message_not_in_streaming_state" in' not in src


def test_all_dead_stream_checks_use_the_predicate():
    """Since 2026-10-20 every prose, thinking, tool card and widget append
    goes through ``_stream_append_locked``, which is where the predicate
    runs; the plan chunk in ``_ensure_phase_open`` has its own check. So
    the predicate appears at those two sites and the one helper carries
    the other four kinds of append."""
    src = inspect.getsource(SlackStreamConsumer)
    assert src.count("_stream_is_dead(") >= 2
    assert src.count("await self._stream_append_locked(") >= 4
    helper = inspect.getsource(SlackStreamConsumer._stream_append_locked)
    assert "_stream_is_dead(" in helper
