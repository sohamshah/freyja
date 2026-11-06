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

from engine.openai_provider import OpenAIConfig, OpenAIProvider
from engine.types import ToolCall


def test_a_replayed_computer_call_never_carries_pending_safety_checks(monkeypatch) -> None:
    """Empty, non-empty (a malicious-instructions warning) or absent, the field is stripped; a ToolCall from before the shape was wired, with empty provider_data, gets status "completed" instead of a KeyError."""
    # The constructor checks for the key; no request is made.
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-not-used")
    provider = OpenAIProvider(OpenAIConfig(model="gpt-5.5"))
    warning = [{"id": "sc_1", "code": "malicious_instructions", "message": "x"}]
    for provider_data in (
        {"id": "cu_xyz", "status": "completed", "pending_safety_checks": []},
        {"id": "cu_def", "status": "completed", "pending_safety_checks": warning},
        {},
    ):
        call = ToolCall(
            id="call_abc",
            name="computer",
            arguments={"action": {"type": "screenshot"}},
            provider_kind="openai.computer_call",
            provider_data=provider_data,
        )
        item = provider._computer_call_input_item(call)
        assert "pending_safety_checks" not in item, provider_data
        assert item["type"] == "computer_call" and item["call_id"] == "call_abc"
        assert item["action"] == {"type": "screenshot"} and item["status"] == "completed"
