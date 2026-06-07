"""
Context compaction strategies.

Provides:
- CompactionStrategy protocol: Interface for compaction implementations
- SummaryCompaction: Compacts by summarizing old messages
- CompactionResult: Result of a compaction operation
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Protocol, runtime_checkable

from engine.constants import (
    COMPACTION_CONTENT_HEAD,
    COMPACTION_CONTENT_TAIL,
    COMPACTION_CONTENT_TRUNCATION_THRESHOLD,
    COMPACTION_END_BUDGET_RATIO,
    COMPACTION_RECENT_MESSAGES_FOR_FOOTER,
    COMPACTION_START_BUDGET_RATIO,
    KEEP_RECENT_MESSAGES,
    MAX_CHARS_TO_SUMMARIZE,
    MIN_CONTENT_LENGTH_FOR_TRUNCATION,
    MIN_MESSAGES_TO_COMPACT,
    MIN_TOKENS_TO_SUMMARIZE,
    SUMMARY_MAX_TOKENS,
)
from engine.session import TranscriptManager
from engine.types import ImageBlock, Message, ThinkingConfig

if TYPE_CHECKING:
    from engine.providers import ModelProvider, StructuredResponse

logger = logging.getLogger(__name__)


_SUMMARY_INJECT_MARKER = "[Previous conversation summary]"


def _is_summary_inject(msg: Message) -> bool:
    """True if ``msg`` is the synthetic sys_summary inserted by
    ``TranscriptManager.get_messages()`` to surface a prior compaction's
    summary to the model. We strip these from the iterative summarizer
    input so the previous summary isn't double-counted (the iterative
    prompt template already embeds the prior summary explicitly).
    """
    if getattr(msg, "role", "") != "system":
        return False
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content.lstrip().startswith(_SUMMARY_INJECT_MARKER)
    if isinstance(content, list):
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str) and text.lstrip().startswith(_SUMMARY_INJECT_MARKER):
                return True
    return False


_WM_TYPES = ("workstream", "decision", "finding", "open_thread", "artifact_note")


def _working_memory_schema() -> dict[str, Any]:
    """JSON schema for compaction's dedicated working-memory extraction
    call (Call B).

    Call B is a second, parallel LLM call made off the *same* conversation
    payload as the prose summary (Call A). Where Call A produces the
    human-facing narrative, Call B produces the structured, durable memory:
    a high-level summary, an explicit list of actions completed / work done,
    and the entity graph (workstreams, decisions, findings, open threads,
    artifact notes). Forcing structured output here removes the whole class
    of failures the old regex-parse path had — fenced JSON, dropped tags,
    optional emission. The provider refuses to return output that violates
    this schema.

    Like the drafter's schema, this targets the *subset* of JSON Schema that
    Anthropic's tool-call backend honors (no ``oneOf``/``if``/``then``), so
    per-type field requirements are expressed in prose in the property
    descriptions rather than via conditional ``required`` blocks. Each entity
    requires only ``type``; the model fills the fields that apply to that type.
    """
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["summary", "actions_completed", "entities"],
        "properties": {
            "summary": {
                "type": "string",
                "description": (
                    "A high-level summary of the session so far — the durable "
                    "narrative an agent resuming this session needs: the goal, "
                    "where things stand, and what is left. A few sentences to a "
                    "short paragraph. Faithful to what actually happened; never "
                    "invent progress."
                ),
            },
            "actions_completed": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "An explicit list of concrete actions taken / work "
                    "completed this session, each a short first-person bullet "
                    "(e.g. 'Created bridge/working_memory.py', 'Fixed the "
                    "context-window display bug'). Include every file "
                    "written/edited and state change you are confident "
                    "happened — especially any present in the confirmed-actions "
                    "ground truth. Empty array only if genuinely nothing was "
                    "done yet."
                ),
            },
            "entities": {
                "type": "array",
                "description": (
                    "The durable entity graph: the structured reasoning a file "
                    "list can't convey. Update and extend the CURRENT working "
                    "memory provided in the system prompt — reuse an existing "
                    "title/text to update that entity rather than duplicating "
                    "it. Empty array only if there is genuinely nothing "
                    "structural to record."
                ),
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["type"],
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": list(_WM_TYPES),
                            "description": (
                                "workstream: a goal/effort. decision: a choice "
                                "made + why. finding: something learned. "
                                "open_thread: an unresolved task. artifact_note: "
                                "a note about a file touched."
                            ),
                        },
                        "title": {
                            "type": "string",
                            "description": (
                                "Short title — for workstream and decision. "
                                "Reuse an existing title to update that entity."
                            ),
                        },
                        "request": {
                            "type": "string",
                            "description": "For workstream: the goal/ask.",
                        },
                        "rationale": {
                            "type": "string",
                            "description": "For decision: why this choice was made.",
                        },
                        "text": {
                            "type": "string",
                            "description": (
                                "For finding and open_thread: the substance."
                            ),
                        },
                        "source": {
                            "type": "string",
                            "description": (
                                "For finding: optional attribution (where it "
                                "came from)."
                            ),
                        },
                        "path": {
                            "type": "string",
                            "description": "For artifact_note: the file path.",
                        },
                        "note": {
                            "type": "string",
                            "description": "For artifact_note: the note about it.",
                        },
                        "workstream": {
                            "type": "string",
                            "description": (
                                "Parent workstream title for decision / finding "
                                "/ open_thread / artifact_note. Reference by the "
                                "workstream's title; an unknown title is "
                                "auto-created."
                            ),
                        },
                        "status": {
                            "type": "string",
                            "description": (
                                "Optional status for a workstream "
                                "(active/done) or open_thread (open/resolved)."
                            ),
                        },
                    },
                },
            },
        },
    }


def _count_images(messages: list[Message]) -> int:
    total = 0
    for msg in messages:
        if isinstance(msg.content, list):
            total += sum(1 for block in msg.content if isinstance(block, ImageBlock))
    return total


def _count_image_bytes_in_blocks(blocks: list) -> int:
    """Sum base64 bytes of any ImageBlocks in this block list, walking
    one level into ToolResultBlock content. Mirrors the walk used in
    `TranscriptManager.cumulative_image_bytes` but operates on a
    detached block list so the compaction gate can use it on
    pre-split slices without pulling in transcript internals.
    """
    from engine.types import ToolResultBlock  # local to avoid a hot import cycle

    total = 0
    for block in blocks:
        if isinstance(block, ImageBlock) and block.source_type == "base64" and block.data:
            total += len(block.data)
            continue
        if isinstance(block, ToolResultBlock) and isinstance(block.content, list):
            for sub in block.content:
                if (
                    isinstance(sub, ImageBlock)
                    and sub.source_type == "base64"
                    and sub.data
                ):
                    total += len(sub.data)
    return total


# ============================================================================
# Compaction Result
# ============================================================================

@dataclass
class CompactionResult:
    """Result of a compaction operation."""

    success: bool
    """Whether compaction succeeded."""

    summary: str | None
    """The generated summary, if successful."""

    tokens_before: int
    """Token count before compaction."""

    tokens_after: int
    """Token count after compaction."""

    error: str | None = None
    """Error message, if compaction failed."""

    entries_removed: int = 0
    """Number of transcript entries removed."""

    messages_before: int = 0
    """Number of transcript entries before compaction."""

    messages_after: int = 0
    """Number of transcript entries after compaction."""

    images_before: int = 0
    """Number of image blocks before compaction."""

    images_after: int = 0
    """Number of image blocks after compaction."""

    resumed_from_previous: bool = False
    """True if this compaction iteratively extended the prior summary
    rather than re-deriving from scratch (Gap 2 / iterative path)."""

    summary_tokens: int = 0
    """Estimated token count of the produced summary itself. Useful
    for forecasting how big future prompts' prefix will be (the
    summary lives at messages[0] post-compaction) and for sanity-
    checking the summarizer didn't go overboard."""

    summarizer_input_tokens: int = 0
    """Input tokens charged for the summarizer LLM call. Lets the
    dashboard surface compaction overhead distinct from main-loop
    spend (Gap 4 — without this the summarizer's cost is invisible)."""

    summarizer_output_tokens: int = 0
    """Output tokens charged for the summarizer LLM call."""

    summarizer_duration_ms: int = 0
    """Wall-clock duration of the summarizer LLM call."""

    summarizer_model: str | None = None
    """Model id that produced the summary (may differ from the main
    session model under fallback)."""

    summarizer_cost_usd: float | None = None
    """Estimated USD cost of the summarizer call. None when the model
    isn't in the pricing table."""


# ============================================================================
# Compaction Strategy Protocol
# ============================================================================

@runtime_checkable
class CompactionStrategy(Protocol):
    """Interface for context compaction strategies."""

    @abstractmethod
    def should_compact(
        self,
        transcript: TranscriptManager,
        context_window: int,
        threshold: float = 0.8,
    ) -> bool:
        """Check if compaction is needed."""
        ...

    @abstractmethod
    def compact(
        self,
        transcript: TranscriptManager,
        provider: "ModelProvider",
    ) -> CompactionResult:
        """Perform compaction on the transcript."""
        ...


# ============================================================================
# Summary-Based Compaction
# ============================================================================

class SummaryCompaction:
    """
    Compacts by summarizing old messages.

    Uses the model to generate a summary of older conversation
    history, replacing detailed exchanges with a condensed summary.
    """

    SUMMARY_UPDATE_PROMPT = """Your task is to UPDATE an existing summary by incorporating new conversation turns that have happened since it was written.

You will be given (a) the previous structured summary covering turns 1..K and (b) the new turns since (K+1..N). Your job is to extend the summary so it covers 1..N while preserving every fact in the prior summary that is still relevant.

CRITICAL RULES:
- PRESERVE every concrete fact from the previous summary (file paths, function signatures, error messages, decisions, artifact paths, user requests). Do not drop them, do not paraphrase them away.
- EXTEND the relevant sections — for example, append newly-completed actions to "Files and Code Sections", append new user requests to "All User Messages", update "Current Work" to reflect the latest state.
- REMOVE only material that has been superseded (e.g. an old "Current Work" task that is now complete moves to "Files and Code Sections"; a "Pending Task" that's now done moves into completed work).
- NEVER remove a completed write-action from the "Actions I performed" list. A file you created or edited, a command that changed state, a commit or PR — these stay listed verbatim in first person for the life of the session, even once the work is finished. They are not "superseded" by completion.
- KEEP the same 9-section structure as the previous summary.
- If the previous summary preserved a fact verbatim and that fact is still relevant, keep it verbatim.

PREVIOUS SUMMARY:
{previous_summary}

NEW TURNS TO INCORPORATE:
{conversation}

Respond with <analysis>...</analysis> followed by <summary>...</summary>. The <summary> block must contain the FULL updated summary (not just a diff)."""

    SUMMARY_PROMPT = """Your task is to create a detailed summary of the conversation so far, paying close attention to the user's explicit requests and your previous actions.

This summary should be thorough in capturing technical details, code patterns, file changes, and decisions that would be essential for continuing work without losing context.

Before providing your final summary, wrap your analysis in <analysis> tags to organize your thoughts. In your analysis:

1. Chronologically analyze each message. For each section identify:
   - The user's explicit requests and intents
   - Your approach to addressing those requests
   - Key decisions and technical concepts
   - Specific details like file names, code snippets, function signatures
   - Errors encountered and how they were fixed
   - User feedback, especially corrections or requests to do things differently

2. Double-check for technical accuracy and completeness.

Your summary MUST include these sections:

1. Primary Request and Intent:
   - Capture all of the user's explicit requests in detail
   - Note the overall goal and any sub-goals

2. Key Technical Concepts:
   - List important technical concepts, tools, and patterns discussed
   - Include any configuration or setup details

3. Files and Code Sections:
   - List specific files examined, modified, or created
   - Include relevant code snippets (especially recent changes)
   - Note why each file is important
   - Keep a distinct first-person list titled "Actions I performed" naming every
     file you CREATED or EDITED, every command that changed state, and every
     commit/PR — written as "I created X", "I edited Y". A completed action does
     not become irrelevant by being finished: never drop it or fold it away.
     This is what lets you later answer truthfully whether you made changes.

4. Sub-agent Artifact References:
   - CRITICAL: Preserve ALL file paths to sub-agent artifacts
     (paths matching ~/.freyja/sessions/*/artifacts/*.md)
   - These artifact files contain the ONLY copy of sub-agent research findings
   - For each artifact, list: the file path, the sub-agent label, and a one-line summary
   - Also preserve any other data file paths referenced in tool results

5. Errors and Fixes:
   - Document errors encountered and their solutions
   - Include user feedback on fixes

5. Problem Solving:
   - Document problems solved
   - Note any ongoing troubleshooting

6. All User Messages:
   - List ALL user messages (not tool results)
   - These are critical for understanding intent changes

7. Pending Tasks:
   - Outline any tasks explicitly requested but not yet completed

8. Current Work:
   - Describe precisely what was being worked on immediately before this summary
   - Focus on the most recent messages
   - Include file names and code snippets where applicable

9. Optional Next Step:
   - List the next step ONLY if it directly aligns with the user's most recent explicit request
   - Include direct quotes showing what task was in progress
   - Do not start tangential work without user confirmation

CONVERSATION TO SUMMARIZE:
{conversation}

Respond with <analysis>...</analysis> followed by <summary>...</summary>. Be thorough but focused on what's needed to continue the work."""

    WORKING_MEMORY_EXTRACTION_PROMPT = """You maintain the durable working memory for a long-running coding agent session. The conversation below is about to be compacted (older turns dropped from the live context), so your job is to capture everything that must survive into the agent's persistent memory.

You produce three things, returned as structured output:

1. summary — a high-level summary of the session: the goal, where things stand, and what remains. The durable narrative an agent resuming this session needs.

2. actions_completed — an explicit list of concrete actions taken / work completed: every file created or edited, every command that changed state, every commit/PR, every bug fixed. First-person, one short bullet each. This list is the answer to "what have I actually done this session?" — be complete and specific.

3. entities — the durable entity graph (workstreams, decisions, findings, open threads, artifact notes): the structured reasoning a file list can't convey.

RULES:
- Be FAITHFUL. Record only what actually happened. Never invent progress, files, or decisions.
- UPDATE and EXTEND the current working memory (shown below) rather than starting over. Reuse an existing entity's title/text to update it; only add genuinely new entities. Do not duplicate.
- Treat the confirmed-actions ground truth (when provided) as authoritative — every confirmed write-action must appear in actions_completed, attributed to yourself, even if the conversation slice that recorded it was truncated.
- Prefer durable, high-signal facts over transient chatter.
{current_working_memory}{ground_truth}
CONVERSATION TO EXTRACT FROM:
{conversation}"""

    _MAX_CHARS_TO_SUMMARIZE = MAX_CHARS_TO_SUMMARIZE

    def __init__(
        self,
        keep_recent_messages: int = KEEP_RECENT_MESSAGES,
        min_messages_to_compact: int = MIN_MESSAGES_TO_COMPACT,
        summary_max_tokens: int = SUMMARY_MAX_TOKENS,
    ):
        self.keep_recent = keep_recent_messages
        self.min_messages = min_messages_to_compact
        self.summary_max_tokens = summary_max_tokens

    def should_compact(
        self,
        transcript: TranscriptManager,
        context_window: int,
        threshold: float = 0.8,
    ) -> bool:
        """Check if compaction is needed based on token usage."""
        estimated_tokens = transcript.estimate_tokens()
        max_tokens = int(context_window * threshold)
        should = estimated_tokens > max_tokens and len(transcript) >= self.min_messages

        if should:
            logger.debug(
                f"Compaction needed: {estimated_tokens} tokens "
                f"(threshold: {max_tokens})"
            )

        return should

    def compact(
        self,
        transcript: TranscriptManager,
        provider: "ModelProvider",
        *,
        on_summarizer_call: "Callable[[dict[str, Any]], None] | None" = None,
        ground_truth: str | None = None,
        on_working_memory_upserts: "Callable[[dict[str, Any] | None], None] | None" = None,
        working_memory_state: str | None = None,
    ) -> CompactionResult:
        """Compact the transcript by summarizing old messages.

        Gating: rather than a strict ``len(messages) >= min_messages``
        floor (which mis-rejected short-but-heavy sessions — e.g. one
        huge tool_result image and a few replies — even when the user
        explicitly hit compact), we require either (a) enough messages
        to be worth summarizing, OR (b) enough *summarizable* tokens —
        content outside the ``keep_recent`` tail. Either path proves
        there is real material to compress.

        ``on_summarizer_call`` is an optional hook fired after the
        summarizer LLM call returns. The callback receives a payload
        with ``model, input_tokens, output_tokens, cache_read_tokens,
        cache_write_tokens, duration_ms, cost_usd, call_kind`` — the
        runner uses this to thread its own ``on_llm_call`` hook so the
        summarizer's spend shows up in the dashboard tagged as
        compaction overhead instead of being invisible (Gap 4).
        """
        messages = transcript.get_messages()
        tokens_before = transcript.estimate_tokens()
        messages_before = len(transcript.entries)
        images_before = _count_images(messages)

        if len(messages) <= self.keep_recent:
            return CompactionResult(
                success=False,
                summary=None,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                error=(
                    f"Nothing to summarize — only {len(messages)} message"
                    f"{'s' if len(messages) != 1 else ''} exist and the "
                    f"recent-tail keeps {self.keep_recent}"
                ),
            )

        split_point = len(messages) - self.keep_recent
        if split_point <= 0:
            return CompactionResult(
                success=False,
                summary=None,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                error="All messages are recent",
            )

        # Estimate tokens in the to-summarize slice. ~4 chars/token is
        # the rough heuristic used elsewhere in the codebase; this gates
        # against summarizing trivially-small old slices that wouldn't
        # recover meaningful space. Use Message.get_text() directly to
        # avoid the heavier per-call formatting overhead.
        summarizable_chars = 0
        older_image_bytes = 0
        for m in messages[:split_point]:
            try:
                text = m.get_text() if hasattr(m, "get_text") else str(m.content)
                summarizable_chars += len(text or "")
            except Exception:
                # Defensive: never let a malformed message block the gate.
                pass
            # Also count image bytes in the older slice so we don't
            # falsely skip when there's a heavy image worth dropping
            # via summarization (summarization replaces the messages
            # holding the images with a text-only summary, freeing
            # ALL their image bytes — that can be the largest win even
            # when the text itself is small).
            try:
                content = getattr(m, "content", None)
                if isinstance(content, list):
                    older_image_bytes += _count_image_bytes_in_blocks(content)
            except Exception:
                pass
        summarizable_tokens = summarizable_chars // 4
        # An older slice carrying >= ~512 KiB of image bytes is worth
        # summarizing even with thin text — the byte savings dwarf
        # the LLM-call cost.
        IMAGE_BYTES_FORCE_SUMMARIZE = 512 * 1024
        if (
            len(messages) < self.min_messages
            and summarizable_tokens < MIN_TOKENS_TO_SUMMARIZE
            and older_image_bytes < IMAGE_BYTES_FORCE_SUMMARIZE
        ):
            return CompactionResult(
                success=False,
                summary=None,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                error=(
                    f"Nothing worth summarizing — {split_point} older "
                    f"message{'s' if split_point != 1 else ''} totals "
                    f"~{summarizable_tokens:,} tokens "
                    f"(threshold {MIN_TOKENS_TO_SUMMARIZE:,}) and "
                    f"~{older_image_bytes // 1024} KiB of images "
                    f"(threshold {IMAGE_BYTES_FORCE_SUMMARIZE // 1024} KiB). "
                    "The recent-tail probably holds the heavy block; "
                    "the runner's overflow cascade prunes oldest images "
                    "instead of summarizing in that case."
                ),
            )

        split_point = self._find_safe_split(messages, split_point)
        if split_point <= 0:
            return CompactionResult(
                success=False,
                summary=None,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                error="Cannot find safe split point (all messages are in tool-call groups)",
            )

        # Respect pinned (compaction_excluded=True) entries: if any pin
        # would land in the to_summarize slice, pull split_point back to
        # the earliest pin's index so pinned content stays in the kept
        # tail. F1 from the design doc.
        split_point = self._honor_pins(transcript, split_point)
        if split_point <= 0:
            return CompactionResult(
                success=False,
                summary=None,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                error="Nothing to summarize after honoring pinned entries",
            )

        # Anchor the latest user message in the kept tail. Without this,
        # `_find_safe_split` can pull split_point past a recent user
        # message when walking backward through a tool-call group, which
        # makes the summarizer write the active task into a section the
        # post-compaction system prompt tells the model NOT to act on —
        # the agent then either stalls or repeats completed work.
        split_point = self._ensure_latest_user_message_kept(messages, split_point)

        to_summarize = messages[:split_point]
        to_keep = messages[split_point:]

        # Iterative path (Gap 2): if the transcript already has a prior
        # compaction entry, ask the summarizer to EXTEND that summary
        # rather than re-derive everything from scratch. The previous
        # compaction's summary is the canonical reference for turns
        # 1..K; we only need to merge the new turns since then.
        previous_summary = self._find_previous_summary(transcript)
        resumed_from_previous = previous_summary is not None

        # When the iterative path is in play, the messages list starts
        # with the [Previous conversation summary] sys_summary inject
        # (from TranscriptManager.get_messages). That message carries
        # the full prior summary text. Without filtering, we'd feed the
        # previous summary to the summarizer TWICE — once via the
        # SUMMARY_UPDATE_PROMPT's PREVIOUS SUMMARY: section, and once
        # embedded in the conversation transcript itself. Strip the
        # inject from the to_summarize slice in that case so the
        # summarizer sees only NEW turns.
        if resumed_from_previous:
            to_summarize = [
                m for m in to_summarize
                if not _is_summary_inject(m)
            ]

        conversation_text = self._format_conversation(to_summarize)

        # Collect summarizer-call telemetry so the result can carry it
        # back to the caller (Gap 4). The hook is fired regardless of
        # whether the caller passed on_summarizer_call so we get the
        # stats locally; the callback is just a passthrough.
        summarizer_stats: dict[str, Any] = {}
        summary: str | None = None
        try:
            # Two parallel calls off the same conversation payload: Call A
            # (prose summary, returned here) and Call B (structured working
            # memory, applied via on_working_memory_upserts). See
            # `_run_summary_and_working_memory`.
            summary = self._run_summary_and_working_memory(
                conversation_text,
                provider,
                previous_summary=previous_summary,
                stats_out=summarizer_stats,
                on_summarizer_call=on_summarizer_call,
                ground_truth=ground_truth,
                working_memory_state=working_memory_state,
                on_working_memory_upserts=on_working_memory_upserts,
            )
        except Exception as e:
            logger.warning(f"Summary generation failed: {e}")
            error_str = str(e).lower()
            is_overflow = any(
                term in error_str
                for term in ("too long", "too large", "context", "token", "exceeds")
            )
            if is_overflow:
                logger.info("Using fallback truncation due to context overflow")
                summary = self._generate_fallback_summary(to_summarize)
            else:
                return CompactionResult(
                    success=False,
                    summary=None,
                    tokens_before=tokens_before,
                    tokens_after=tokens_before,
                    error=str(e),
                )

        if not summary:
            return CompactionResult(
                success=False,
                summary=None,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                error="Failed to generate summary",
            )

        first_kept_id = transcript.entries[split_point].id if transcript.entries else ""
        transcript.append_compaction(
            summary=summary,
            first_kept_id=first_kept_id,
            tokens_before=tokens_before,
        )

        tokens_after = transcript.estimate_tokens()
        entries_removed = split_point
        messages_after = len(transcript.entries)
        images_after = _count_images(transcript.get_messages())

        logger.info(
            f"Compaction complete: {tokens_before} -> {tokens_after} tokens, "
            f"{entries_removed} entries summarized"
        )

        # summary_tokens estimate. ~4 chars/token is the same heuristic
        # used by append_compaction's tokens_after fallback math; lets
        # callers forecast prompt-prefix size after compaction.
        summary_tokens = (len(summary) // 4) if summary else 0

        return CompactionResult(
            success=True,
            summary=summary,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            resumed_from_previous=resumed_from_previous,
            entries_removed=entries_removed,
            messages_before=messages_before,
            messages_after=messages_after,
            images_before=images_before,
            images_after=images_after,
            summary_tokens=summary_tokens,
            summarizer_input_tokens=int(summarizer_stats.get("input_tokens", 0) or 0),
            summarizer_output_tokens=int(summarizer_stats.get("output_tokens", 0) or 0),
            summarizer_duration_ms=int(summarizer_stats.get("duration_ms", 0) or 0),
            summarizer_model=summarizer_stats.get("model"),
            summarizer_cost_usd=summarizer_stats.get("cost_usd"),
        )

    @staticmethod
    def _honor_pins(transcript: TranscriptManager, split_point: int) -> int:
        """Pull split_point back to before any pinned (compaction_excluded)
        entry in the to_summarize range.

        Maps transcript-entry indices to message-list indices so the
        returned split_point matches the caller's coordinate system.
        Key subtlety: ``get_messages()`` injects a synthetic system
        message (``[Previous conversation summary]\n...``) right before
        the FIRST real message that follows a compaction entry. The
        messages list therefore has the SAME length as transcript.entries
        — but the inject sits where the compaction entry would have, so
        every real message after a compaction is at a message-list index
        ONE HIGHER than a naive "skip compaction entries" walk would
        give you. We track the inject explicitly via ``inject_pending``.

        Bug history: the previous implementation skipped compaction
        entries entirely and never counted them, so any pin on a message
        right after a prior compaction reported earliest_pin_message_idx=0
        — split_point was forced to 0 and compaction failed with
        "Nothing to summarize after honoring pinned entries" even though
        there were plenty of older messages to compact.
        """
        try:
            entries = transcript.entries
        except Exception:  # noqa: BLE001
            return split_point
        if not entries:
            return split_point

        message_idx = 0
        inject_pending = False
        earliest_pin_message_idx: int | None = None
        for entry in entries:
            if getattr(entry, "is_compaction", False):
                # A compaction entry contributes a sys_summary inject
                # immediately before the NEXT real message — but only
                # if there's a real summary to inject and a real
                # message will follow it. Defer accounting until we see
                # the next real message.
                if entry.compaction_summary:
                    inject_pending = True
                continue
            if entry.message is None:
                # Defensive: ignore non-compaction entries with no
                # message. Don't drop a pending inject — wait for the
                # actual next real message.
                continue
            if inject_pending:
                # The sys_summary occupies this slot; the real message
                # takes the NEXT slot.
                message_idx += 1
                inject_pending = False
            if getattr(entry, "compaction_excluded", False):
                if message_idx < split_point:
                    earliest_pin_message_idx = message_idx
                    break
            message_idx += 1
        if earliest_pin_message_idx is None:
            return split_point
        # Pull back to right BEFORE the earliest pin so the pin stays in
        # the kept tail. Min with the original split_point so we never
        # *expand* the to_summarize range.
        return max(0, min(split_point, earliest_pin_message_idx))

    @staticmethod
    def _find_previous_summary(transcript: TranscriptManager) -> str | None:
        """Return the most recent compaction entry's summary, if any.

        Walks ``transcript.entries`` newest-to-oldest looking for the
        latest ``is_compaction=True`` entry. Used by the iterative
        summary path to extend the prior summary instead of re-deriving
        from scratch. Returns None on a fresh session.
        """
        for entry in reversed(transcript.entries):
            if getattr(entry, "is_compaction", False):
                summary = getattr(entry, "compaction_summary", None)
                if summary:
                    return str(summary)
        return None

    @staticmethod
    def _ensure_latest_user_message_kept(
        messages: list[Message], split_point: int
    ) -> int:
        """Guarantee the most recent user message survives compaction.

        If the latest user-role message at index `latest_idx >= split_point`
        we're fine. Otherwise the safe-split walk pulled the boundary past
        it (typically when the user message is followed by an interrupted
        tool_use group) — pull split_point back to the user message so
        the summary doesn't swallow the active task.

        Mirrors hermes-agent's `_ensure_last_user_message_in_tail`
        (context_compressor.py:1148-1193), which originated as a bug fix
        for active-task loss after compression.
        """
        latest_user_idx = -1
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].role == "user":
                latest_user_idx = i
                break
        if latest_user_idx < 0 or latest_user_idx >= split_point:
            return split_point
        return max(0, latest_user_idx)

    @staticmethod
    def _find_safe_split(messages: list[Message], split_point: int) -> int:
        """Adjust split_point so kept messages don't start with orphaned tool results."""
        while split_point > 0 and messages[split_point].role in ("tool", "tool_result"):
            split_point -= 1

        if split_point > 0 and split_point < len(messages):
            msg = messages[split_point]
            if msg.role == "assistant" and msg.tool_calls:
                pass

        return split_point

    def _format_conversation(
        self,
        messages: list[Message],
        max_chars: int | None = None,
    ) -> str:
        """Format messages as conversation text for summarization."""
        max_chars = max_chars or self._MAX_CHARS_TO_SUMMARIZE

        def format_message(idx: int, msg: Message) -> str:
            role = msg.role.upper()
            content = msg.get_text() if hasattr(msg, "get_text") else str(msg.content)

            tool_info = ""
            if msg.tool_calls:
                tool_names = [tc.name for tc in msg.tool_calls]
                tool_info = f" [tools: {', '.join(tool_names)}]"

            if len(content) > COMPACTION_CONTENT_TRUNCATION_THRESHOLD:
                head = content[:COMPACTION_CONTENT_HEAD]
                tail = content[-COMPACTION_CONTENT_TAIL:]
                content = f"{head}\n... [truncated {len(content)} chars, middle removed] ...\n{tail}"

            return f"[{idx+1}] {role}{tool_info}: {content}"

        all_lines = [format_message(i, msg) for i, msg in enumerate(messages)]
        all_chars = sum(len(line) + 2 for line in all_lines)

        if all_chars <= max_chars:
            return "\n\n".join(all_lines)

        start_budget = int(max_chars * COMPACTION_START_BUDGET_RATIO)
        end_budget = int(max_chars * COMPACTION_END_BUDGET_RATIO)

        start_lines = []
        start_chars = 0
        start_idx = 0
        for i, line in enumerate(all_lines):
            line_chars = len(line) + 2
            if start_chars + line_chars > start_budget:
                break
            start_lines.append(line)
            start_chars += line_chars
            start_idx = i + 1

        end_lines = []
        end_chars = 0
        end_idx = len(all_lines)
        for i in range(len(all_lines) - 1, start_idx - 1, -1):
            line = all_lines[i]
            line_chars = len(line) + 2
            if end_chars + line_chars > end_budget:
                break
            end_lines.insert(0, line)
            end_chars += line_chars
            end_idx = i

        removed_count = end_idx - start_idx
        if removed_count > 0:
            truncation_marker = (
                f"\n[... {removed_count} messages from middle removed "
                f"to fit context limit ...]\n"
            )
        else:
            truncation_marker = ""

        result = "\n\n".join(start_lines) + truncation_marker + "\n\n".join(end_lines)

        # Append an artifact reference footer so artifact paths survive
        # even when the messages containing them get truncated away.
        import re
        artifact_pattern = re.compile(r'[/~][^\s]*?/artifacts/[^\s]+\.md')
        full_text = "\n".join(all_lines)
        artifact_paths = sorted(set(artifact_pattern.findall(full_text)))
        if artifact_paths:
            footer = "\n\n--- ARTIFACT REFERENCES (MUST PRESERVE IN SUMMARY) ---\n"
            for p in artifact_paths:
                footer += f"- {p}\n"
            result += footer

        return result

    def _generate_summary(
        self,
        conversation: str,
        provider: "ModelProvider",
        *,
        previous_summary: str | None = None,
        stats_out: dict[str, Any] | None = None,
        on_summarizer_call: Callable[[dict[str, Any]], None] | None = None,
        ground_truth: str | None = None,
    ) -> str:
        """Generate a summary using the model provider.

        Two-pass redaction: scrub credentials from the input we send to
        the summarizer, then scrub the output the summarizer returns
        (some models echo credentials back verbatim even when prompted
        not to). The summary persists across the session, so a leaked
        key sticks around.

        When ``previous_summary`` is provided this is the iterative
        path (Gap 2): we ask the model to EXTEND the existing summary
        rather than re-derive the whole conversation from scratch.
        Saves the per-compaction summarizer cost for long sessions and
        keeps early-turn facts pinned across many compactions, since the
        model doesn't have to rediscover them from the (now-shorter)
        conversation slice.

        ``stats_out`` is populated with the summarizer call's
        input/output/cache tokens, duration_ms, model id, and cost (if
        priced). Caller is responsible for surfacing these in the
        CompactionResult / telemetry. ``on_summarizer_call`` is fired
        once with the same dict + a ``call_kind: 'summarizer'`` tag so
        downstream hooks (the runner's on_llm_call) can tag the cost
        as compaction overhead instead of regular session spend (Gap 4).
        """
        import re
        import time as _time
        from engine.redact import redact_sensitive_text

        safe_conversation = redact_sensitive_text(conversation)
        if previous_summary:
            safe_previous = redact_sensitive_text(previous_summary)
            prompt = self.SUMMARY_UPDATE_PROMPT.format(
                previous_summary=safe_previous,
                conversation=safe_conversation,
            )
        else:
            prompt = self.SUMMARY_PROMPT.format(conversation=safe_conversation)

        # Seed the summarizer with the runtime's deterministic ledger of
        # confirmed write-actions (when the caller supplies one). This makes
        # "I created widget_tools.py" impossible to omit from the summary even
        # if the conversation slice that recorded it was truncated — the model
        # is handed the action list as ground truth rather than having to
        # rediscover it from a lossy transcript.
        if ground_truth:
            safe_gt = redact_sensitive_text(ground_truth)
            prompt = (
                "Confirmed actions (ground truth from the runtime ledger — these "
                "file writes/edits and state changes definitely happened this "
                "session). Your summary must list each in the first-person "
                "'Actions I performed' section, attributed to yourself; do not "
                "omit or soften them:\n"
                f"{safe_gt}\n\n"
            ) + prompt

        start_perf = _time.perf_counter()
        # CRITICAL: explicitly disable thinking on the summarizer call
        # (Anthropic-family providers only — others bake reasoning
        # into the request via config-time settings). The provider's
        # default thinking config (carried from the session's
        # reasoning_level) is "adaptive thinking, high effort" on
        # Opus 4.7+. Without this override, the model spends the full
        # output budget on reasoning tokens and emits zero visible
        # text — the wrapper then sees `response.content` = "" and
        # raises "Empty summary generated". Summarization is a
        # structured-text ask; no thinking needed.
        complete_kwargs: dict[str, Any] = {
            "messages": [Message(role="user", content=prompt)],
            "max_tokens": self.summary_max_tokens,
        }
        try:
            response = provider.complete(
                **complete_kwargs,
                thinking=ThinkingConfig(enabled=False, effort="none"),
            )
        except TypeError:
            # Provider's complete() doesn't accept `thinking` kwarg
            # (non-Anthropic). Fall through with defaults — those
            # providers configure reasoning at provider-construction
            # time, not per-call.
            response = provider.complete(**complete_kwargs)
        duration_ms = int((_time.perf_counter() - start_perf) * 1000)

        # Pull usage off the response so callers can attribute the
        # summarizer's spend (Gap 4). All providers populate APIUsage
        # — when they don't, the fields default to 0.
        usage = getattr(response, "usage", None)
        in_tok = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
        out_tok = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0
        cr_tok = int(getattr(usage, "cache_read_tokens", 0) or 0) if usage else 0
        cw_tok = int(getattr(usage, "cache_write_tokens", 0) or 0) if usage else 0
        model_id = (
            getattr(response, "model", None)
            or getattr(provider, "model_id", None)
            or "unknown"
        )
        # Estimate cost via the same compute_cost the rest of the codebase
        # uses; None if the model isn't in the pricing table.
        cost: float | None = None
        try:
            from engine.providers import compute_cost
            cost = compute_cost(
                model_id,
                input_tokens=in_tok,
                output_tokens=out_tok,
                cache_read_tokens=cr_tok,
                cache_write_tokens=cw_tok,
            )
        except Exception:
            cost = None
        if stats_out is not None:
            stats_out.update({
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cache_read_tokens": cr_tok,
                "cache_write_tokens": cw_tok,
                "duration_ms": duration_ms,
                "model": model_id,
                "cost_usd": cost,
            })
        if on_summarizer_call is not None:
            try:
                on_summarizer_call({
                    "call_kind": "summarizer",
                    "provider": getattr(provider, "name", "unknown"),
                    "model": model_id,
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "cache_read_tokens": cr_tok,
                    "cache_write_tokens": cw_tok,
                    "duration_ms": duration_ms,
                    "cost_usd": cost,
                    "iterative": previous_summary is not None,
                })
            except Exception:
                logger.exception("on_summarizer_call hook failed")

        raw_output = response.content.strip()
        if not raw_output:
            raise ValueError("Empty summary generated")

        summary_match = re.search(
            r"<summary>(.*?)</summary>",
            raw_output,
            re.DOTALL,
        )

        if summary_match:
            summary = summary_match.group(1).strip()
        else:
            analysis_pattern = r"<analysis>.*?</analysis>\s*"
            summary = re.sub(analysis_pattern, "", raw_output, flags=re.DOTALL).strip()

        if not summary:
            raise ValueError("Could not extract summary from model output")

        # Working memory is no longer derived from this call's output. It is
        # produced by a dedicated structured-output call (Call B,
        # `_extract_working_memory`) that runs in parallel off the same
        # conversation payload — see `_run_summary_and_working_memory`. This
        # call (Call A) is now summary-only.
        return redact_sensitive_text(summary)

    def _extract_working_memory(
        self,
        conversation: str,
        provider: "ModelProvider",
        *,
        working_memory_state: str | None = None,
        ground_truth: str | None = None,
        stats_out: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Call B — extract structured working memory from the conversation.

        A dedicated, schema-constrained LLM call that runs in parallel with
        the prose-summary call (Call A) off the *same* conversation payload.
        Returns the parsed structured dict
        ``{summary, actions_completed, entities}`` or ``None`` if the provider
        can't do structured output / the call fails / the payload is empty.

        Best-effort by contract: callers treat ``None`` as "no LLM-derived
        working memory this round" and fall back to the deterministic
        ledger-based refresh. Quality, not speed, is the priority here (the
        user's explicit ask) — we hand the model the *current* working memory
        and the confirmed-action ground truth so it updates/extends rather
        than re-derives, and we give it a generous token budget.
        """
        import asyncio
        import time as _time

        from engine.redact import redact_sensitive_text

        # Structured output is a provider capability; not every backend has it.
        if not hasattr(provider, "complete_structured"):
            return None

        safe_conversation = redact_sensitive_text(conversation)
        if not safe_conversation.strip():
            return None

        wm_section = ""
        if working_memory_state and working_memory_state.strip():
            wm_section = (
                "\nCURRENT WORKING MEMORY (update and extend this — do not "
                "start over):\n"
                f"{redact_sensitive_text(working_memory_state)}\n"
            )
        gt_section = ""
        if ground_truth and ground_truth.strip():
            gt_section = (
                "\nCONFIRMED ACTIONS (ground truth from the runtime ledger — "
                "these file writes/edits and state changes definitely happened "
                "this session; every one must appear in actions_completed):\n"
                f"{redact_sensitive_text(ground_truth)}\n"
            )

        system_prompt = self.WORKING_MEMORY_EXTRACTION_PROMPT.format(
            current_working_memory=wm_section,
            ground_truth=gt_section,
            conversation=safe_conversation,
        )

        async def _run() -> "StructuredResponse":
            return await provider.complete_structured(
                messages=[Message(role="user", content="Extract the working memory.")],
                schema=_working_memory_schema(),
                schema_name="working_memory",
                schema_description=(
                    "High-level summary, actions completed, and the durable "
                    "entity graph for the session."
                ),
                system_prompt=system_prompt,
                max_tokens=self.summary_max_tokens,
                thinking=ThinkingConfig(enabled=False),
            )

        start_perf = _time.perf_counter()
        try:
            # Runs in a ThreadPoolExecutor worker (no event loop) — asyncio.run
            # is safe here; the sibling summary call runs concurrently.
            resp = asyncio.run(_run())
        except Exception:
            logger.debug("working-memory extraction call failed", exc_info=True)
            return None
        duration_ms = int((_time.perf_counter() - start_perf) * 1000)

        # Capture spend so the orchestrator can attribute it as compaction
        # overhead (same treatment as the summary call).
        if stats_out is not None:
            usage = getattr(resp, "usage", None)
            in_tok = int(getattr(usage, "input_tokens", 0) or 0) if usage else 0
            out_tok = int(getattr(usage, "output_tokens", 0) or 0) if usage else 0
            cr_tok = int(getattr(usage, "cache_read_tokens", 0) or 0) if usage else 0
            cw_tok = int(getattr(usage, "cache_write_tokens", 0) or 0) if usage else 0
            model_id = (
                getattr(resp, "model", None)
                or getattr(provider, "model_id", None)
                or "unknown"
            )
            cost: float | None = None
            try:
                from engine.providers import compute_cost
                cost = compute_cost(
                    model_id,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cache_read_tokens=cr_tok,
                    cache_write_tokens=cw_tok,
                )
            except Exception:
                cost = None
            stats_out.update({
                "call_kind": "working_memory_extraction",
                "provider": getattr(provider, "name", "unknown"),
                "model": model_id,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cache_read_tokens": cr_tok,
                "cache_write_tokens": cw_tok,
                "duration_ms": duration_ms,
                "cost_usd": cost,
            })

        data = getattr(resp, "data", None)
        if not isinstance(data, dict) or not data:
            return None
        return self._normalize_working_memory(data)

    @staticmethod
    def _normalize_working_memory(data: dict[str, Any]) -> dict[str, Any]:
        """Coerce Call B's raw structured output into the shape the bridge
        sink expects: ``{summary: str, actions_completed: [str],
        entities: [dict]}``, dropping malformed entries defensively so a
        partial model response never breaks the apply step."""
        summary = data.get("summary")
        summary = summary.strip() if isinstance(summary, str) else ""

        actions = data.get("actions_completed")
        actions_out: list[str] = []
        if isinstance(actions, list):
            for a in actions:
                if isinstance(a, str) and a.strip():
                    actions_out.append(a.strip())

        entities = data.get("entities")
        entities_out: list[dict[str, Any]] = []
        if isinstance(entities, list):
            for ent in entities:
                if isinstance(ent, dict) and ent.get("type") in _WM_TYPES:
                    # Drop null/empty fields so apply_working_memory_upserts
                    # only stores fields the model actually filled.
                    entities_out.append({
                        k: v for k, v in ent.items()
                        if v is not None and v != ""
                    })
        return {
            "summary": summary,
            "actions_completed": actions_out,
            "entities": entities_out[:24],  # bound the per-round graph growth
        }

    def _run_summary_and_working_memory(
        self,
        conversation: str,
        provider: "ModelProvider",
        *,
        previous_summary: str | None = None,
        stats_out: dict[str, Any] | None = None,
        on_summarizer_call: Callable[[dict[str, Any]], None] | None = None,
        ground_truth: str | None = None,
        working_memory_state: str | None = None,
        on_working_memory_upserts: "Callable[[dict[str, Any] | None], None] | None" = None,
    ) -> str:
        """Run the prose-summary call (Call A) and the structured
        working-memory extraction (Call B) concurrently against the same
        conversation payload, and return Call A's summary.

        The two calls are fully decoupled — quality of each is independent of
        the other (the user's explicit design ask):

        - Call A's result IS the compaction summary; its exceptions propagate
          so ``compact()``'s overflow-fallback path still fires.
        - Call B is best-effort. Its structured result (or ``None``) is handed
          to ``on_working_memory_upserts``, which is ALWAYS invoked when wired
          — even with ``None`` — so the bridge's deterministic ledger-based
          refresh runs on every compaction.

        Telemetry for both calls is fired from this (main) thread after both
        workers have finished, so the ``on_summarizer_call`` hook is never
        invoked concurrently.
        """
        from concurrent.futures import ThreadPoolExecutor

        # No working-memory sink wired → nothing to extract; just the summary
        # (single call, on its own thread-free path).
        if on_working_memory_upserts is None:
            return self._generate_summary(
                conversation,
                provider,
                previous_summary=previous_summary,
                stats_out=stats_out,
                on_summarizer_call=on_summarizer_call,
                ground_truth=ground_truth,
            )

        wm_stats: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="compact") as pool:
            # Call A fires on_summarizer_call itself (inside its worker); we
            # let it, then resolve it last so its hook has completed before we
            # fire Call B's from this thread — no concurrent hook calls.
            fa = pool.submit(
                self._generate_summary,
                conversation,
                provider,
                previous_summary=previous_summary,
                stats_out=stats_out,
                on_summarizer_call=on_summarizer_call,
                ground_truth=ground_truth,
            )
            fb = pool.submit(
                self._extract_working_memory,
                conversation,
                provider,
                working_memory_state=working_memory_state,
                ground_truth=ground_truth,
                stats_out=wm_stats,
            )

            # Resolve Call B first (best-effort, never raises out of here).
            wm_result: dict[str, Any] | None
            try:
                wm_result = fb.result()
            except Exception:
                logger.debug("working-memory extraction failed", exc_info=True)
                wm_result = None

            # Resolve Call A, capturing (not raising yet) so working memory is
            # applied even when the summary call overflowed.
            summary_exc: Exception | None = None
            summary: str | None = None
            try:
                summary = fa.result()
            except Exception as e:  # noqa: BLE001
                summary_exc = e

        # Both workers are now done — safe to touch shared hooks/state.
        try:
            on_working_memory_upserts(wm_result)
        except Exception:
            logger.debug("working_memory sink failed", exc_info=True)

        # Attribute Call B's spend as compaction overhead (Call A already
        # fired its own hook inside its worker).
        if on_summarizer_call is not None and wm_stats:
            try:
                on_summarizer_call({
                    "call_kind": "working_memory_extraction",
                    "provider": wm_stats.get("provider", "unknown"),
                    "model": wm_stats.get("model", "unknown"),
                    "input_tokens": wm_stats.get("input_tokens", 0),
                    "output_tokens": wm_stats.get("output_tokens", 0),
                    "cache_read_tokens": wm_stats.get("cache_read_tokens", 0),
                    "cache_write_tokens": wm_stats.get("cache_write_tokens", 0),
                    "duration_ms": wm_stats.get("duration_ms", 0),
                    "cost_usd": wm_stats.get("cost_usd"),
                    "iterative": previous_summary is not None,
                })
            except Exception:
                logger.debug("on_summarizer_call (wm) hook failed", exc_info=True)

        if summary_exc is not None:
            raise summary_exc
        return summary  # type: ignore[return-value]

    def _generate_fallback_summary(self, messages: list[Message]) -> str:
        """Generate a simple fallback summary when LLM summarization fails."""
        user_count = sum(1 for m in messages if m.role == "user")
        assistant_count = sum(1 for m in messages if m.role == "assistant")

        tools_used = set()
        for msg in messages:
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    tools_used.add(tc.name)

        first_user = next((m for m in messages if m.role == "user"), None)
        first_user_text = ""
        if first_user:
            content = (
                first_user.get_text()
                if hasattr(first_user, "get_text")
                else str(first_user.content)
            )
            first_user_text = content[:MIN_CONTENT_LENGTH_FOR_TRUNCATION] + "..." if len(content) > MIN_CONTENT_LENGTH_FOR_TRUNCATION else content

        recent_context = []
        for msg in messages[-COMPACTION_RECENT_MESSAGES_FOR_FOOTER:]:
            role = msg.role
            content = (
                msg.get_text() if hasattr(msg, "get_text") else str(msg.content)
            )
            if len(content) > 400:
                truncated = content[:150] + "..." + content[-150:]
            else:
                truncated = content
            recent_context.append(f"- {role}: {truncated}")

        summary_parts = [
            "[Conversation Summary - Auto-generated due to context limits]",
            "",
            f"Messages removed: {len(messages)} ({user_count} user, {assistant_count} assistant)",
            "",
            "Initial context:",
            f"  {first_user_text}" if first_user_text else "  (no user message found)",
            "",
        ]

        if tools_used:
            summary_parts.extend([
                "Tools used:",
                f"  {', '.join(sorted(tools_used))}",
                "",
            ])

        summary_parts.extend([
            "Recent messages before truncation:",
            *recent_context,
        ])

        return "\n".join(summary_parts)


# ============================================================================
# No-Op Compaction (for testing)
# ============================================================================

class NoOpCompaction:
    """A no-op compaction strategy that never compacts."""

    def should_compact(
        self,
        transcript: TranscriptManager,
        context_window: int,
        threshold: float = 0.8,
    ) -> bool:
        return False

    def compact(
        self,
        transcript: TranscriptManager,
        provider: "ModelProvider",
    ) -> CompactionResult:
        tokens = transcript.estimate_tokens()
        return CompactionResult(
            success=False,
            summary=None,
            tokens_before=tokens,
            tokens_after=tokens,
            error="No-op compaction does not compact",
        )
