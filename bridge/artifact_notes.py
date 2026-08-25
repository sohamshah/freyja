"""Operator comments on artifacts, rendered for the agent that produced them.

The desktop's artifact browser lets the operator highlight a slice of any file
any session ever wrote and attach a comment. That comment is routed back to the
session that most recently touched the file — the whole point being that the
agent which built the thing is the one asked to change it, without the operator
having to remember which of a thousand sessions that was.

This module owns the agent-facing wording. It is deliberately free of session
machinery so the phrasing can be pinned by a test — see
``tests/test_artifact_notes.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NoteAnchor:
    """The highlighted region, as it was when the operator wrote the note."""

    start_line: int
    end_line: int
    quote: str

    @classmethod
    def from_payload(cls, payload: Any) -> "NoteAnchor | None":
        if not isinstance(payload, dict):
            return None
        try:
            start = int(payload.get("startLine") or 0)
            end = int(payload.get("endLine") or 0)
        except (TypeError, ValueError):
            return None
        quote = str(payload.get("quote") or "")
        if start <= 0 or end <= 0 or not quote.strip():
            return None
        return cls(start_line=start, end_line=max(start, end), quote=quote)

    @property
    def label(self) -> str:
        if self.start_line == self.end_line:
            return f"line {self.start_line}"
        return f"lines {self.start_line}-{self.end_line}"


#: How much of a quoted region to carry into the message. A note on a large
#: selection should still read as a comment, not as a paste of the file.
MAX_QUOTE_CHARS = 2000


def render_note_message(
    *,
    artifact_path: str,
    body: str,
    anchor: NoteAnchor | None = None,
    filename: str = "",
) -> str:
    """The text the receiving agent sees as a user turn.

    Three jobs, in order: say which file, show what was highlighted, and give
    the comment. The closing paragraph exists because the note may be days or
    months old — the agent must re-read before editing rather than trusting the
    quoted snippet, and a comment phrased as a question deserves an answer
    rather than an edit.
    """
    body = (body or "").strip()
    name = filename or artifact_path.rsplit("/", 1)[-1]

    lines: list[str] = []
    lines.append(
        f"The operator left a comment on `{name}`, an artifact produced in this "
        f"session."
    )
    lines.append("")
    lines.append(f"Full path: `{artifact_path}`")

    if anchor is not None:
        quote = anchor.quote
        if len(quote) > MAX_QUOTE_CHARS:
            quote = quote[:MAX_QUOTE_CHARS] + "\n…[truncated]"
        lines.append("")
        lines.append(f"They highlighted {anchor.label}:")
        lines.append("")
        lines.append("```")
        lines.append(quote)
        lines.append("```")

    lines.append("")
    lines.append("Their comment:")
    lines.append("")
    lines.append(body or "(empty)")
    lines.append("")
    lines.append(
        "Read the file before you change anything — the comment may be older "
        "than the file's current contents, and the quoted snippet may have "
        "moved. Then make the requested change and say briefly what you "
        "changed. If the comment is a question rather than a request, answer "
        "it instead of editing."
    )
    return "\n".join(lines)


def note_label(filename: str) -> str:
    """Sender label shown on the inbox message."""
    return f"Operator (comment on {filename})" if filename else "Operator"
