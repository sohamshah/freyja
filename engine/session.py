"""
Session and transcript management.

Provides:
- TranscriptEntry: Individual entries in the conversation history
- TranscriptManager: Manages conversation history with branching support
- Session: Complete session state including transcript and tools
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from engine.constants import (
    KEEP_RECENT_COMPUTER_IMAGES,
    KEEP_RECENT_TOOL_RESULTS,
    MAX_SESSION_EVENTS,
    MAX_REQUEST_IMAGES_SAFETY,
    MESSAGE_TOKEN_OVERHEAD,
    MIN_CONTENT_LENGTH_FOR_TRUNCATION,
)
from engine.tools import Tool
from engine.types import (
    ContentBlock,
    ImageBlock,
    Message,
    RedactedThinkingBlock,
    TextBlock,
    ThinkingBlock,
    ToolCall,
)
from engine.tokenizer import count_tokens

logger = logging.getLogger(__name__)

COMPUTER_IMAGE_TOOL_NAMES: frozenset[str] = frozenset({
    "screenshot",
    "click",
    "move_mouse",
    "type_text",
    "press_key",
    "key_down",
    "key_up",
    "scroll",
    "wait",
    "inspect_region",
})


@dataclass
class ImagePruneResult:
    """Summary of request-history image pruning."""

    images_before: int
    images_after: int
    tool_result_images_before: int
    tool_result_images_after: int
    omitted_images: int = 0
    modified_messages: int = 0
    kept_recent: int = KEEP_RECENT_COMPUTER_IMAGES
    hard_limit: int = MAX_REQUEST_IMAGES_SAFETY

    @property
    def changed(self) -> bool:
        return self.omitted_images > 0

    def to_details(self) -> dict[str, Any]:
        return {
            "images_before": self.images_before,
            "images_after": self.images_after,
            "tool_result_images_before": self.tool_result_images_before,
            "tool_result_images_after": self.tool_result_images_after,
            "omitted_images": self.omitted_images,
            "modified_messages": self.modified_messages,
            "kept_recent": self.kept_recent,
            "hard_limit": self.hard_limit,
        }


# ============================================================================
# Transcript Entry
# ============================================================================


@dataclass
class TranscriptEntry:
    """
    An entry in the conversation transcript.

    Supports both messages and compaction events for tracking
    conversation history through context compaction.
    """

    id: str
    """Unique identifier for this entry."""

    message: Message | None
    """The message, if this is a message entry."""

    is_compaction: bool = False
    """True if this entry represents a compaction event."""

    compaction_summary: str | None = None
    """Summary text if this is a compaction entry."""

    tokens_before: int | None = None
    """Token count before compaction."""

    tokens_after: int | None = None
    """Token count after compaction."""

    timestamp: float = field(default_factory=time.time)
    """When this entry was created."""

    parent_id: str | None = None
    """ID of the parent entry (for branching support)."""

    def to_dict(self) -> dict[str, Any]:
        """Serialize for transcript persistence."""
        d: dict[str, Any] = {
            "id": self.id,
            "is_compaction": self.is_compaction,
            "timestamp": self.timestamp,
        }
        if self.message is not None:
            d["message"] = self.message.to_dict()
        if self.compaction_summary is not None:
            d["compaction_summary"] = self.compaction_summary
        if self.tokens_before is not None:
            d["tokens_before"] = self.tokens_before
        if self.tokens_after is not None:
            d["tokens_after"] = self.tokens_after
        if self.parent_id is not None:
            d["parent_id"] = self.parent_id
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TranscriptEntry":
        """Deserialize from a dict produced by to_dict()."""
        message = None
        if "message" in d and d["message"] is not None:
            message = Message.from_dict(d["message"])
        return cls(
            id=d["id"],
            message=message,
            is_compaction=d.get("is_compaction", False),
            compaction_summary=d.get("compaction_summary"),
            tokens_before=d.get("tokens_before"),
            tokens_after=d.get("tokens_after"),
            timestamp=d.get("timestamp", 0.0),
            parent_id=d.get("parent_id"),
        )


# ============================================================================
# Transcript Manager
# ============================================================================


class TranscriptManager:
    """
    Manages conversation history with branching support.

    The transcript is a linear sequence of entries (messages and
    compaction events). Branching is supported for compaction retry
    scenarios where we need to try different compaction strategies.
    """

    def __init__(
        self,
        max_entries: int = MAX_SESSION_EVENTS,
        on_append: Callable[[Message], None] | None = None,
    ):
        """
        Initialize the transcript manager.

        Args:
            max_entries: Maximum entries to keep (oldest are dropped)
            on_append: Optional sync callback invoked after each message
                is appended.
        """
        self._entries: list[TranscriptEntry] = []
        self._max_entries = max_entries
        self._head_id: str | None = None
        self.on_append = on_append

    @property
    def entries(self) -> list[TranscriptEntry]:
        """Get all entries (read-only view)."""
        return list(self._entries)

    @property
    def head_id(self) -> str | None:
        """Get the current head entry ID."""
        return self._head_id

    def append_message(self, message: Message) -> str:
        """
        Append a message to the transcript.

        Args:
            message: The message to append

        Returns:
            The entry ID
        """
        entry_id = str(uuid.uuid4())
        entry = TranscriptEntry(
            id=entry_id,
            message=message,
            parent_id=self._head_id,
        )
        self._entries.append(entry)
        self._head_id = entry_id

        # Trim if over max
        if len(self._entries) > self._max_entries:
            removed = len(self._entries) - self._max_entries
            self._entries = self._entries[removed:]
            logger.debug(f"Trimmed {removed} old transcript entries")

        # Notify listener
        if self.on_append is not None:
            self.on_append(message)

        return entry_id

    def append_compaction(
        self,
        summary: str,
        first_kept_id: str,
        tokens_before: int,
        tokens_after: int | None = None,
    ) -> str:
        """
        Record a compaction event and remove summarized entries.

        This replaces old entries (before first_kept_id) with a compaction
        summary, keeping only recent entries.
        """
        keep_from_index = len(self._entries)
        if first_kept_id:
            for i, entry in enumerate(self._entries):
                if entry.id == first_kept_id:
                    keep_from_index = i
                    break

        entries_removed = keep_from_index
        kept_entries = self._entries[keep_from_index:]

        entry_id = str(uuid.uuid4())
        self._entries = kept_entries
        if tokens_after is None:
            summary_tokens = len(summary) // 4
            tokens_after = self.estimate_tokens() + summary_tokens

        compaction_entry = TranscriptEntry(
            id=entry_id,
            message=None,
            is_compaction=True,
            compaction_summary=summary,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            parent_id=None,
        )

        self._entries = [compaction_entry] + kept_entries
        self._head_id = self._entries[-1].id if self._entries else entry_id

        logger.info(
            f"Compaction complete: {tokens_before} -> {tokens_after} tokens, "
            f"{entries_removed} entries summarized, {len(kept_entries)} entries kept"
        )

        return entry_id

    def get_messages(self) -> list[Message]:
        """Get current transcript as a message list."""
        messages: list[Message] = []
        last_compaction_summary: str | None = None

        for entry in self._entries:
            if entry.is_compaction:
                last_compaction_summary = entry.compaction_summary
            elif entry.message is not None:
                if last_compaction_summary:
                    messages.append(
                        Message(
                            role="system",
                            content=f"[Previous conversation summary]\n{last_compaction_summary}",
                        )
                    )
                    last_compaction_summary = None
                messages.append(entry.message)

        return messages

    def get_entry(self, entry_id: str) -> TranscriptEntry | None:
        """Get an entry by ID."""
        for entry in self._entries:
            if entry.id == entry_id:
                return entry
        return None

    def branch_from(self, entry_id: str) -> bool:
        """Branch from a specific entry (for compaction retry)."""
        for i, entry in enumerate(self._entries):
            if entry.id == entry_id:
                self._entries = self._entries[: i + 1]
                self._head_id = entry_id
                logger.debug(f"Branched transcript from entry {entry_id}")
                return True
        return False

    def estimate_tokens(self) -> int:
        """Estimate token count for current transcript using tiktoken."""
        total_tokens = 0
        message_overhead = MESSAGE_TOKEN_OVERHEAD

        for entry in self._entries:
            if entry.message is not None:
                total_tokens += message_overhead
                content = entry.message.content
                if isinstance(content, str):
                    total_tokens += count_tokens(content)
                elif isinstance(content, list):
                    for block in content:
                        if hasattr(block, "text"):
                            total_tokens += count_tokens(block.text)
            elif entry.compaction_summary:
                total_tokens += count_tokens(entry.compaction_summary)

        return total_tokens

    def prune_old_tool_results(
        self,
        keep_recent: int = KEEP_RECENT_TOOL_RESULTS,
    ) -> int:
        """
        Halve old tool results to reduce context size.

        Keeps the most recent tool results intact, halves older ones.
        """
        truncation_marker = "Request specific sections if needed.]"
        min_content_length = MIN_CONTENT_LENGTH_FOR_TRUNCATION

        tool_result_indices = []
        for i, entry in enumerate(self._entries):
            if entry.message is not None and entry.message.role == "tool_result":
                tool_result_indices.append(i)

        if len(tool_result_indices) <= keep_recent:
            return 0

        indices_to_prune = tool_result_indices[:-keep_recent]
        pruned_count = 0

        for idx in indices_to_prune:
            entry = self._entries[idx]
            if entry.message is None:
                continue

            content = entry.message.content
            if not isinstance(content, str):
                continue

            if content.rstrip().endswith(truncation_marker):
                continue
            if len(content) <= min_content_length:
                continue

            half_point = len(content) // 2
            head_chars = int(half_point * 0.7)
            tail_chars = int(half_point * 0.25)

            head = content[:head_chars]
            last_newline = head.rfind("\n")
            if last_newline > head_chars * 0.7:
                head = head[:last_newline]

            tail = content[-tail_chars:]
            first_newline = tail.find("\n")
            if first_newline > 0 and first_newline < tail_chars * 0.3:
                tail = tail[first_newline + 1 :]

            truncated = (
                head + f"\n\n[Content truncated - {len(content):,} total chars, middle removed. "
                "Request specific sections if needed.]\n\n" + tail
            )

            entry.message.content = truncated
            pruned_count += 1

        if pruned_count > 0:
            logger.info(f"Halved {pruned_count} old tool results")

        return pruned_count

    def prune_old_tool_result_images(
        self,
        keep_recent: int = KEEP_RECENT_COMPUTER_IMAGES,
        hard_limit: int = MAX_REQUEST_IMAGES_SAFETY,
    ) -> ImagePruneResult:
        """
        Remove old computer-use screenshots from model history.

        Computer-control tools can capture hundreds of frames in a long
        session. The UI and frame dump still retain those observations, but
        provider requests should only carry the latest few images. Older
        image blocks are replaced by a text marker inside the same tool_result
        message so tool-use/result adjacency stays valid.
        """
        keep_recent = max(0, int(keep_recent))
        hard_limit = max(1, int(hard_limit))

        tool_name_by_id: dict[str, str] = {}
        for entry in self._entries:
            msg = entry.message
            if msg is None or not msg.tool_calls:
                continue
            for call in msg.tool_calls:
                tool_name_by_id[call.id] = call.name

        image_refs: list[tuple[int, int]] = []
        prunable_refs: list[tuple[int, int]] = []
        for entry_index, entry in enumerate(self._entries):
            msg = entry.message
            if msg is None or isinstance(msg.content, str):
                continue
            tool_name = tool_name_by_id.get(msg.tool_call_id or "")
            for block_index, block in enumerate(msg.content):
                if not isinstance(block, ImageBlock):
                    continue
                image_refs.append((entry_index, block_index))
                if (
                    msg.role == "tool_result"
                    and (tool_name in COMPUTER_IMAGE_TOOL_NAMES or tool_name is None)
                ):
                    prunable_refs.append((entry_index, block_index))

        images_before = len(image_refs)
        tool_images_before = len(prunable_refs)
        if tool_images_before == 0:
            return ImagePruneResult(
                images_before=images_before,
                images_after=images_before,
                tool_result_images_before=0,
                tool_result_images_after=0,
                kept_recent=keep_recent,
                hard_limit=hard_limit,
            )

        non_prunable_images = images_before - tool_images_before
        allowed_by_hard_limit = max(0, hard_limit - non_prunable_images)
        actual_keep = min(keep_recent, allowed_by_hard_limit, tool_images_before)

        if tool_images_before <= actual_keep and images_before <= hard_limit:
            return ImagePruneResult(
                images_before=images_before,
                images_after=images_before,
                tool_result_images_before=tool_images_before,
                tool_result_images_after=tool_images_before,
                kept_recent=actual_keep,
                hard_limit=hard_limit,
            )

        refs_to_keep = set(prunable_refs[-actual_keep:]) if actual_keep > 0 else set()
        refs_to_prune = set(prunable_refs) - refs_to_keep
        if not refs_to_prune:
            return ImagePruneResult(
                images_before=images_before,
                images_after=images_before,
                tool_result_images_before=tool_images_before,
                tool_result_images_after=tool_images_before,
                kept_recent=actual_keep,
                hard_limit=hard_limit,
            )

        omitted_images = 0
        modified_messages = 0
        affected_entries = {entry_index for entry_index, _ in refs_to_prune}
        for entry_index in sorted(affected_entries):
            entry = self._entries[entry_index]
            msg = entry.message
            if msg is None or isinstance(msg.content, str):
                continue
            new_blocks: list[ContentBlock] = []
            removed_here = 0
            for block_index, block in enumerate(msg.content):
                if (entry_index, block_index) in refs_to_prune and isinstance(block, ImageBlock):
                    removed_here += 1
                    continue
                new_blocks.append(block)
            if removed_here == 0:
                continue
            omitted_images += removed_here
            modified_messages += 1
            new_blocks.append(TextBlock(text=(
                f"\n\n[{removed_here} older screenshot image"
                f"{'s' if removed_here != 1 else ''} omitted from model history. "
                f"The latest {actual_keep} screenshot image"
                f"{'s' if actual_keep != 1 else ''} remain visible to the model; "
                "take a fresh screenshot if visual state has changed.]"
            )))
            msg.content = new_blocks

        images_after = images_before - omitted_images
        tool_images_after = tool_images_before - omitted_images
        if omitted_images > 0:
            logger.info(
                "Pruned %d old screenshot image block(s): %d -> %d images",
                omitted_images,
                images_before,
                images_after,
            )

        return ImagePruneResult(
            images_before=images_before,
            images_after=images_after,
            tool_result_images_before=tool_images_before,
            tool_result_images_after=tool_images_after,
            omitted_images=omitted_images,
            modified_messages=modified_messages,
            kept_recent=actual_keep,
            hard_limit=hard_limit,
        )

    def clear(self) -> None:
        """Clear all entries."""
        self._entries.clear()
        self._head_id = None

    def __len__(self) -> int:
        return len(self._entries)

    def to_dict(self) -> dict[str, Any]:
        """Serialize transcript state for persistence."""
        return {
            "entries": [e.to_dict() for e in self._entries],
            "head_id": self._head_id,
        }

    @classmethod
    def from_dict(
        cls,
        d: dict[str, Any],
        max_entries: int = MAX_SESSION_EVENTS,
    ) -> "TranscriptManager":
        """Restore transcript from a dict produced by to_dict()."""
        tm = cls(max_entries=max_entries)
        for entry_d in d.get("entries", []):
            tm._entries.append(TranscriptEntry.from_dict(entry_d))
        tm._head_id = d.get("head_id")
        return tm


# ============================================================================
# Session
# ============================================================================


@dataclass
class Session:
    """
    Complete session state.

    A session represents a single conversation with an agent,
    including the transcript, system prompt, available tools,
    and metadata.
    """

    id: str
    """Unique session identifier."""

    transcript: TranscriptManager
    """Conversation history."""

    system_prompt: str
    """System prompt for this session."""

    tools: list[Tool] = field(default_factory=list)
    """Available tools for this session."""

    created_at: float = field(default_factory=time.time)
    """When this session was created."""

    last_activity: float = field(default_factory=time.time)
    """Last activity timestamp."""

    compaction_count: int = 0
    """Number of times this session has been compacted."""

    tool_tokens: int = 0
    """Estimated tokens used by tool definitions in the API payload."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Arbitrary session metadata."""

    @classmethod
    def create(
        cls,
        system_prompt: str = "",
        tools: list[Tool] | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Session:
        """Create a new session."""
        return cls(
            id=session_id or str(uuid.uuid4()),
            transcript=TranscriptManager(),
            system_prompt=system_prompt,
            tools=tools or [],
            metadata=metadata or {},
        )

    def touch(self) -> None:
        """Update last activity timestamp."""
        self.last_activity = time.time()

    def add_user_message(self, content: str | list[ContentBlock]) -> str:
        """Add a user message to the transcript."""
        self.touch()
        return self.transcript.append_message(Message(role="user", content=content))

    def add_assistant_message(
        self,
        content: str,
        tool_calls: list[ToolCall] | None = None,
        thinking_blocks: list[ThinkingBlock | RedactedThinkingBlock] | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> str:
        """Add an assistant message to the transcript."""
        self.touch()
        return self.transcript.append_message(
            Message(
                role="assistant",
                content=content,
                tool_calls=tool_calls,
                thinking_blocks=thinking_blocks,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
            )
        )

    def add_tool_result(
        self,
        call_id: str,
        content: str | list[ContentBlock],
        is_error: bool = False,
    ) -> str:
        """Add a tool result to the transcript."""
        self.touch()
        return self.transcript.append_message(
            Message(
                role="tool_result",
                content=content,
                tool_call_id=call_id,
            )
        )

    def get_messages(self) -> list[Message]:
        """Get the conversation history as messages."""
        return self.transcript.get_messages()

    def estimate_tokens(self) -> int:
        """Estimate total token cost for the session."""
        system_tokens = count_tokens(self.system_prompt)
        return system_tokens + self.transcript.estimate_tokens() + self.tool_tokens

    # ── Transcript persistence ───────────────────────────────────────

    def serialize_transcript(self) -> dict[str, Any]:
        """Serialize the transcript and session metadata for disk persistence.

        Returns a provider-agnostic dict that can be JSON-encoded. Does NOT
        include tools (those are reconstructed from the registry on resume)
        or the system prompt (regenerated from the workspace/model config).
        """
        return {
            "version": 1,
            "session_id": self.id,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "compaction_count": self.compaction_count,
            "tool_tokens": self.tool_tokens,
            "metadata": self.metadata,
            "transcript": self.transcript.to_dict(),
        }

    def restore_transcript(self, data: dict[str, Any]) -> None:
        """Restore transcript state from a dict produced by serialize_transcript().

        Replaces the current (empty) transcript with the persisted one.
        The session must already have its system_prompt and tools set
        (those are NOT part of the serialized transcript).
        """
        if data.get("version") != 1:
            raise ValueError(f"Unsupported transcript version: {data.get('version')}")
        self.created_at = data.get("created_at", self.created_at)
        self.last_activity = data.get("last_activity", self.last_activity)
        self.compaction_count = data.get("compaction_count", 0)
        self.tool_tokens = data.get("tool_tokens", self.tool_tokens)
        self.metadata = data.get("metadata", {})
        transcript_d = data.get("transcript")
        if transcript_d:
            self.transcript = TranscriptManager.from_dict(transcript_d)

    def strip_thinking_blocks(self) -> int:
        """Remove all thinking/redacted_thinking blocks from the transcript.

        Used when resuming a session with a different provider — thinking
        signatures are provider-specific and won't validate cross-provider.

        Returns the number of entries modified.
        """
        modified = 0
        for entry in self.transcript._entries:
            if entry.message and entry.message.thinking_blocks:
                entry.message.thinking_blocks = None
                modified += 1
        return modified
