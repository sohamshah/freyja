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
from typing import TYPE_CHECKING, Protocol, runtime_checkable

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
    SUMMARY_MAX_TOKENS,
)
from engine.session import TranscriptManager
from engine.types import ImageBlock, Message

if TYPE_CHECKING:
    from engine.providers import ModelProvider

logger = logging.getLogger(__name__)


def _count_images(messages: list[Message]) -> int:
    total = 0
    for msg in messages:
        if isinstance(msg.content, list):
            total += sum(1 for block in msg.content if isinstance(block, ImageBlock))
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
    ) -> CompactionResult:
        """Compact the transcript by summarizing old messages."""
        messages = transcript.get_messages()
        tokens_before = transcript.estimate_tokens()
        messages_before = len(transcript.entries)
        images_before = _count_images(messages)

        if len(messages) < self.min_messages:
            return CompactionResult(
                success=False,
                summary=None,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                error="Not enough messages to compact",
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

        split_point = self._find_safe_split(messages, split_point)
        if split_point <= 0:
            return CompactionResult(
                success=False,
                summary=None,
                tokens_before=tokens_before,
                tokens_after=tokens_before,
                error="Cannot find safe split point (all messages are in tool-call groups)",
            )

        to_summarize = messages[:split_point]
        to_keep = messages[split_point:]

        conversation_text = self._format_conversation(to_summarize)

        summary: str | None = None
        try:
            summary = self._generate_summary(conversation_text, provider)
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

        return CompactionResult(
            success=True,
            summary=summary,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            entries_removed=entries_removed,
            messages_before=messages_before,
            messages_after=messages_after,
            images_before=images_before,
            images_after=images_after,
        )

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
    ) -> str:
        """Generate a summary using the model provider."""
        import re

        prompt = self.SUMMARY_PROMPT.format(conversation=conversation)

        response = provider.complete(
            messages=[Message(role="user", content=prompt)],
            max_tokens=self.summary_max_tokens,
        )

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

        return summary

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
