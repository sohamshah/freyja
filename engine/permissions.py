"""
Permission types for human-in-the-loop interactions.

Pure data types extracted from the CLI human interaction system.
No UI dependencies (Rich, prompt_toolkit, etc.).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol, runtime_checkable


class PermissionLevel(Enum):
    """Permission levels for actions."""

    INFO = "info"
    """Just requesting information, no permission needed."""

    LOW = "low"
    """Low-risk action, auto-approve by default."""

    MEDIUM = "medium"
    """Medium-risk, prompt user but allow quick approval."""

    HIGH = "high"
    """High-risk, require explicit confirmation."""

    DANGEROUS = "dangerous"
    """Dangerous action, require explicit y/n confirmation."""


@dataclass
class HumanResponse:
    """Response from a human interaction."""

    approved: bool
    """Whether the action was approved (for permission requests)."""

    response: str
    """The human's text response."""

    timestamp: datetime = field(default_factory=datetime.now)
    """When the response was given."""


@dataclass
class PermissionRequest:
    """Describes a permission check to present to the user.

    Attributes
    ----------
    prompt : str
        Human-readable description of the action, e.g.
        ``"Delete resource 'foo' (id: abc)"``.
    level : PermissionLevel
        Risk level -- determines how the handler presents the prompt.
    details : str or None
        Optional extra context shown below the main prompt.
    """

    prompt: str
    level: PermissionLevel
    details: str | None = None


@runtime_checkable
class HumanInteractionHandler(Protocol):
    """Protocol for handling human-in-the-loop interactions.

    Implementations provide the actual UI (CLI, HTTP, etc.).
    This protocol defines only the interface contract.
    """

    def request_permission(
        self,
        action: str,
        level: PermissionLevel = PermissionLevel.MEDIUM,
        details: str | None = None,
    ) -> HumanResponse:
        """Request permission from a human for an action.

        Args:
            action: Description of the action to perform
            level: Permission level required
            details: Additional technical details

        Returns:
            HumanResponse with approval status
        """
        ...
