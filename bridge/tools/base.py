"""
Type shim for Freyja bridge tools.

This is the ONE file that currently imports from engine. Every other
tool in this package imports its types from here, so turning the app
into a standalone repo is a one-file change: replace the bodies below
with self-contained copies from engine/tools.py and engine/types.py.

Keeping the shim in place today guarantees that the tools registered here are
duck-type compatible with `engine.runner.AsyncAgentRunner`'s
`ToolRegistry`, which is what our freyja_bridge.py still uses.
"""

from __future__ import annotations

# NOTE: The only remaining coupling. Swap the block below for vendored
# definitions when the desktop app is split into its own repo.
from engine.tools import (  # noqa: F401
    ToolDefinition,
    ToolRegistry,
    ToolResult,
    ToolTier,
)
from engine.types import (  # noqa: F401
    ContentBlock,
    DocumentBlock,
    ImageBlock,
    TextBlock,
    ToolCall,
    ToolUseBlock,
)

try:
    # human_tools pulls in Rich / prompt_toolkit under the hood. Import lazily
    # so we can fall back if those modules are missing in a minimal env.
    from engine.permissions import (  # noqa: F401
        PermissionLevel,
        PermissionRequest,
    )
except Exception:  # pragma: no cover - best effort
    from dataclasses import dataclass
    from enum import Enum

    class PermissionLevel(str, Enum):  # type: ignore[no-redef]
        LOW = "low"
        MEDIUM = "medium"
        HIGH = "high"
        DANGEROUS = "dangerous"

    @dataclass
    class PermissionRequest:  # type: ignore[no-redef]
        prompt: str
        level: PermissionLevel
        details: str = ""


__all__ = [
    "ToolDefinition",
    "ToolRegistry",
    "ToolResult",
    "ToolTier",
    "ToolCall",
    "ToolUseBlock",
    "ContentBlock",
    "DocumentBlock",
    "ImageBlock",
    "TextBlock",
    "PermissionLevel",
    "PermissionRequest",
]
