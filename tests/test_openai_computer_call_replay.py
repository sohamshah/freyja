"""Regression: OpenAI Responses API rejects ``pending_safety_checks`` on
computer_call input items with::

    400 - 'pending_safety_checks' is not supported for the "computer" tool.

Once a session ever invoked the computer tool, the prior turn's
computer_call was replayed back into the input list, the provider
emitted ``pending_safety_checks: []`` on every replay, and the API
rejected every follow-up turn forever. Symptom: "Non-retryable error"
on every message in an affected session, no way to recover without
either deleting the session or stripping the field at serialization
time. We chose the latter.

This test pins the contract: regardless of what's sitting in
``tool_call.provider_data``, the serialized computer_call input item
must NOT carry ``pending_safety_checks``.
"""
from __future__ import annotations

import os

from engine.openai_provider import OpenAIConfig, OpenAIProvider
from engine.types import ToolCall


def _make_provider() -> OpenAIProvider:
    # Provider constructor requires an API key; tests don't make real
    # API calls, but the env-var check fires in __init__.
    os.environ.setdefault("OPENAI_API_KEY", "test-key-not-used")
    return OpenAIProvider(OpenAIConfig(model="gpt-5.5"))


def test_input_item_omits_pending_safety_checks_when_empty() -> None:
    provider = _make_provider()
    call = ToolCall(
        id="call_abc",
        name="computer",
        arguments={"action": {"type": "screenshot"}},
        provider_kind="openai.computer_call",
        provider_data={
            "id": "cu_xyz",
            "status": "completed",
            "pending_safety_checks": [],
        },
    )
    item = provider._computer_call_input_item(call)
    assert "pending_safety_checks" not in item, (
        "empty pending_safety_checks must NOT appear in the replay item — "
        "OpenAI's current Responses API rejects the field on computer_call"
    )
    assert item["type"] == "computer_call"
    assert item["call_id"] == "call_abc"
    assert item["action"] == {"type": "screenshot"}


def test_input_item_omits_pending_safety_checks_when_nonempty() -> None:
    """Even when the prior response carried a non-empty list of pending
    safety checks (e.g. malicious-instructions warning), we must NOT
    echo them back on replay. The API rejects the field outright on the
    new ``{"type": "computer"}`` tool, regardless of payload."""
    provider = _make_provider()
    call = ToolCall(
        id="call_def",
        name="computer",
        arguments={"action": {"type": "click", "x": 10, "y": 20}},
        provider_kind="openai.computer_call",
        provider_data={
            "id": "cu_def",
            "status": "completed",
            "pending_safety_checks": [
                {"id": "sc_1", "code": "malicious_instructions", "message": "x"},
            ],
        },
    )
    item = provider._computer_call_input_item(call)
    assert "pending_safety_checks" not in item


def test_input_item_handles_missing_provider_data() -> None:
    """Sessions created before the computer_call shape was fully wired
    can have ToolCalls with empty provider_data. Must not KeyError."""
    provider = _make_provider()
    call = ToolCall(
        id="call_ghi",
        name="computer",
        arguments={"action": {"type": "screenshot"}},
        provider_kind="openai.computer_call",
        provider_data={},
    )
    item = provider._computer_call_input_item(call)
    assert "pending_safety_checks" not in item
    assert item["call_id"] == "call_ghi"
    # Status falls through to "completed" when provider_data is empty.
    assert item["status"] == "completed"


if __name__ == "__main__":
    test_input_item_omits_pending_safety_checks_when_empty()
    test_input_item_omits_pending_safety_checks_when_nonempty()
    test_input_item_handles_missing_provider_data()
    print("OK")
