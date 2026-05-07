"""Provider-native tool protocol markers.

Most Freyja tools are normal JSON-schema function tools. Some providers also
have native tool protocols with different request/response shapes. Keep those
protocol identities here so providers, runners, and bridge adapters can agree
without hard-coding provider-specific strings throughout the codebase.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NativeToolProtocol:
    """A provider-native tool protocol Freyja can route through a hidden tool."""

    kind: str
    internal_tool_name: str
    shadowed_tool_names: frozenset[str]


OPENAI_COMPUTER_PROTOCOL = NativeToolProtocol(
    kind="openai.computer_call",
    internal_tool_name="computer",
    # Generic Freyja tools that overlap with OpenAI's native computer tool.
    # When active, these should not be advertised as plain function tools to
    # OpenAI models; their visual results cannot be returned as function output.
    shadowed_tool_names=frozenset(
        {
            "browser_screenshot",
            "click",
            "focus_window",
            "key_down",
            "key_up",
            "move_mouse",
            "press_key",
            "screenshot",
            "scroll",
            "type_text",
            "wait",
        }
    ),
)

OPENAI_COMPUTER_KIND = OPENAI_COMPUTER_PROTOCOL.kind
OPENAI_COMPUTER_TOOL_NAME = OPENAI_COMPUTER_PROTOCOL.internal_tool_name
OPENAI_NATIVE_COMPUTER_SHADOWED_TOOLS = OPENAI_COMPUTER_PROTOCOL.shadowed_tool_names


def is_native_tool_call(call: Any, protocol: NativeToolProtocol) -> bool:
    """Return True when a stored tool call belongs to a native protocol."""

    return (
        getattr(call, "provider_kind", None) == protocol.kind
        or getattr(call, "name", None) == protocol.internal_tool_name
    )


def is_openai_computer_call(call: Any) -> bool:
    """Return True when a stored tool call is an OpenAI native computer call."""

    return is_native_tool_call(call, OPENAI_COMPUTER_PROTOCOL)
