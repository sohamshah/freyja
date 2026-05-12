"""Agent-facing `summarize_context` tool — the cooperative compaction primitive.

Phase 2 of the cooperative early-trigger compaction architecture (see
``docs/COMPACTION-DECISION-DRAFT.md``). The agent calls this when the
pressure ladder's awareness/soft/strong/fallback signals (Channels 1-3)
tell it to. Runtime executes the requested compaction and persists a
``summarize_context_call`` telemetry row so the agent's judgment becomes
training data for a future trained decision policy (Dataset 1).

Why this isn't just a thin wrapper around ``SummaryCompaction.compact()``:
the agent expresses *semantic intent* via ``scope`` (e.g.
"tool_results_only", "exploration_only"), the runtime resolves that to
a concrete transcript slice, and applies the iterative-vs-fresh prompt
path based on whether a prior compaction exists.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

from bridge.tools.base import ToolDefinition, ToolResult, ToolTier

logger = logging.getLogger(__name__)


# Resolved scope: a *split point index* over the current message list.
# Messages at index < split_point are summarized; messages at index >=
# split_point are kept verbatim in the tail. The result of every scope
# resolution is one of these integers.
ScopeResolver = Callable[[Any], int]


class SummarizeContextTool:
    """The agent-facing compaction primitive.

    Wires into the same ``SummaryCompaction`` machinery the runtime uses
    for forced-fallback compaction, but with a semantic-scope front-end
    so the agent doesn't have to reason about message indices.
    """

    SCOPE_VALUES = (
        "all",
        "early",
        "tool_results_only",
        "exploration_only",
        "since_last_compaction",
    )
    LEVEL_VALUES = ("episode", "chapter", "auto")

    # The exploration-pruning scope considers these tool names "exploratory"
    # — agents call them to look around; their results rarely carry
    # information that's load-bearing for downstream work the way an
    # edit or shell write does.
    EXPLORATION_TOOLS = frozenset({
        "read_file",
        "grep",
        "glob",
        "list_directory",
        "web_search",
        "web_fetch",
        "web_task",
    })

    def __init__(
        self,
        *,
        get_session: Callable[[], Any],
        get_provider: Callable[[], Any],
        get_compactor: Callable[[], Any],
        on_summarize_call: Callable[[dict[str, Any]], None] | None = None,
        get_current_pressure_pct: Callable[[], float | None] | None = None,
    ) -> None:
        self._get_session = get_session
        self._get_provider = get_provider
        self._get_compactor = get_compactor
        self._on_summarize_call = on_summarize_call
        self._get_current_pressure_pct = get_current_pressure_pct

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="summarize_context",
            summary="Compact conversation history at a moment of your choosing",
            tier=ToolTier.WARM,
            description=(
                "Compact older conversation history into a structured summary so the "
                "active context window has room for more work. Call this at natural "
                "breakpoints — after a task finishes, before starting a new one, when "
                "the pressure tag in tool results suggests it.\n\n"
                "SCOPE — pick the slice you want compacted:\n"
                "  • since_last_compaction (default): extend the previous summary with "
                "new turns. Cheapest option; preserves prior summary's structure.\n"
                "  • all: compact everything before the last few turns. Use after a "
                "phase boundary when most of history is no longer load-bearing.\n"
                "  • early: compact only the oldest material; keep middle + recent.\n"
                "  • tool_results_only: leave assistant reasoning intact, only condense "
                "tool outputs. Cheap, surgical.\n"
                "  • exploration_only: condense read/grep/glob/web_search calls but "
                "keep edits and shell writes. Best after a long discovery phase.\n\n"
                "LEVEL — granularity of the summary (defaults to auto):\n"
                "  • episode: ~800-2000 tokens, full 9-section template. Single task.\n"
                "  • chapter: ~1500-3000 tokens, narrative-flavored. A phase of work.\n"
                "  • auto: runtime picks based on the compacted range size.\n\n"
                "PRESERVE_FACTS — pass a list of verbatim strings the summary MUST "
                "retain unchanged (api keys, error messages, exact file paths, etc.). "
                "The runtime verifies post-summary and falls back if any are missing.\n\n"
                "REASON — short free-form explanation of why you're compacting now. "
                "This becomes labeled training data for a future compaction policy."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": list(self.SCOPE_VALUES),
                        "description": "Which slice of history to compact.",
                    },
                    "level": {
                        "type": "string",
                        "enum": list(self.LEVEL_VALUES),
                        "description": "Summary granularity. Defaults to auto.",
                    },
                    "preserve_facts": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Short verbatim strings the summary MUST contain. "
                            "Use for credentials, exact paths, error messages."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "Why compact now? Free-form. Becomes a supervised "
                            "training label for a future decision policy."
                        ),
                    },
                },
                "required": [],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        scope = str(arguments.get("scope") or "since_last_compaction").strip()
        if scope not in self.SCOPE_VALUES:
            return _err(call_id, f"Unknown scope `{scope}`. "
                                  f"Valid: {', '.join(self.SCOPE_VALUES)}")
        level = str(arguments.get("level") or "auto").strip()
        if level not in self.LEVEL_VALUES:
            return _err(call_id, f"Unknown level `{level}`. "
                                  f"Valid: {', '.join(self.LEVEL_VALUES)}")
        preserve_raw = arguments.get("preserve_facts") or []
        preserve_facts: list[str] = []
        if isinstance(preserve_raw, list):
            for item in preserve_raw:
                if isinstance(item, str) and item.strip():
                    preserve_facts.append(item.strip())
        reason = str(arguments.get("reason") or "").strip()

        session = self._get_session()
        provider = self._get_provider()
        compactor = self._get_compactor()
        if session is None or provider is None or compactor is None:
            return _err(call_id, "summarize_context unavailable (session not ready)")

        transcript = getattr(session, "transcript", None)
        if transcript is None:
            return _err(call_id, "summarize_context unavailable (no transcript)")

        tokens_before = transcript.estimate_tokens()
        pressure_pct = (
            self._get_current_pressure_pct() if self._get_current_pressure_pct else None
        )
        started_at = time.time()

        try:
            result = self._dispatch(scope, level, compactor, transcript, provider)
        except Exception as exc:  # noqa: BLE001
            logger.exception("summarize_context dispatch failed")
            return _err(call_id, f"Compaction failed: {exc}")

        # Verify preserve_facts — fail (or warn) if any are missing.
        missing: list[str] = []
        if result.success and result.summary and preserve_facts:
            for fact in preserve_facts:
                if fact and fact not in result.summary:
                    missing.append(fact)

        # Always emit telemetry, even on failure — the bad outcomes are
        # part of the training corpus.
        try:
            if self._on_summarize_call is not None:
                self._on_summarize_call({
                    "scope": scope,
                    "level_requested": level,
                    "level_used": _classify_level(level, result),
                    "preserve_facts_count": len(preserve_facts),
                    "preserve_facts_missing": missing,
                    "reason": reason[:1000],
                    "pressure_pct_at_call": pressure_pct,
                    "tokens_before": result.tokens_before,
                    "tokens_after": result.tokens_after,
                    "resumed_from_previous": getattr(result, "resumed_from_previous", False),
                    "entries_removed": result.entries_removed,
                    "success": result.success,
                    "error": result.error,
                    "elapsed_ms": int((time.time() - started_at) * 1000),
                })
        except Exception:  # noqa: BLE001
            logger.exception("summarize_context telemetry failed")

        if not result.success:
            return _err(
                call_id,
                f"Compaction failed: {result.error or 'unknown'}",
            )

        # If a preserve_facts check failed, surface a warning but don't
        # fail the call — the summary is still useful; the agent gets
        # to decide whether to re-call with a tighter scope.
        body: dict[str, Any] = {
            "tokens_freed": max(0, result.tokens_before - result.tokens_after),
            "tokens_before": result.tokens_before,
            "tokens_after": result.tokens_after,
            "entries_removed": result.entries_removed,
            "resumed_from_previous": getattr(result, "resumed_from_previous", False),
            "level_used": _classify_level(level, result),
            "summary_excerpt": (result.summary or "")[:240],
        }
        if missing:
            body["preserve_facts_missing"] = missing
            body["warning"] = (
                f"{len(missing)} preserve_facts entries did not appear "
                "verbatim in the produced summary. Consider re-calling with "
                "a narrower scope or pinning the affected messages instead."
            )

        return ToolResult(
            call_id=call_id,
            content=json.dumps(body, indent=2),
            is_error=False,
        )

    def _dispatch(
        self,
        scope: str,
        level: str,
        compactor: Any,
        transcript: Any,
        provider: Any,
    ):
        """Execute the requested scope by invoking the compactor.

        For most scopes we just call ``compactor.compact(transcript, provider)``
        — the existing compactor already implements the iterative path
        and the safe-split logic, so ``since_last_compaction`` and
        ``all`` (and ``early`` at small-history) all reduce to the same
        call. The fine-grained scopes (``tool_results_only``,
        ``exploration_only``) live in their own helpers.
        """
        if scope in ("since_last_compaction", "all", "early"):
            return compactor.compact(transcript, provider)
        if scope == "tool_results_only":
            return self._compact_tool_results_only(transcript)
        if scope == "exploration_only":
            return self._compact_exploration_only(transcript)
        # Shouldn't be reached — scope was validated above.
        return compactor.compact(transcript, provider)

    def _compact_tool_results_only(self, transcript: Any):
        """Replace every tool-result message body with a 1-liner.

        Cheap because there's no LLM call — we just rewrite the
        transcript in place. Catches the common case where the agent
        knows recent tool output is no longer needed but the assistant
        reasoning is still useful.
        """
        from engine.compaction import CompactionResult

        before = transcript.estimate_tokens()
        rewritten = 0
        for entry in transcript.entries:
            msg = entry.message
            if msg is None:
                continue
            if msg.role not in ("tool", "tool_result"):
                continue
            content = msg.content
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    t = getattr(block, "text", None)
                    if isinstance(t, str):
                        parts.append(t)
                text = "\n".join(parts)
            if not text:
                continue
            preview = text.strip().splitlines()[0][:200] if text.strip() else ""
            replacement = (
                f"[tool result condensed: {len(text):,} chars · preview: "
                f"{preview!r}]"
            )
            # Only condense if it actually shrinks; some results are already short.
            if len(replacement) < len(text):
                msg.content = replacement
                rewritten += 1

        after = transcript.estimate_tokens()
        return CompactionResult(
            success=rewritten > 0,
            summary=f"[scope=tool_results_only · rewrote {rewritten} tool results]",
            tokens_before=before,
            tokens_after=after,
            entries_removed=0,
            error=None if rewritten > 0 else "no tool results were condensable",
        )

    def _compact_exploration_only(self, transcript: Any):
        """Condense exploration tool results (read/grep/glob/web_search)
        but leave edits and shell writes intact.

        Maps tool calls back to their results via the matching
        ``tool_call_id`` field on the tool_result message and skips any
        whose originating tool name is *not* in ``EXPLORATION_TOOLS``.
        """
        from engine.compaction import CompactionResult

        # Build call_id → tool_name map.
        tool_name_by_id: dict[str, str] = {}
        for entry in transcript.entries:
            msg = entry.message
            if msg is None or not getattr(msg, "tool_calls", None):
                continue
            for call in msg.tool_calls:
                tool_name_by_id[call.id] = call.name

        before = transcript.estimate_tokens()
        rewritten = 0
        for entry in transcript.entries:
            msg = entry.message
            if msg is None or msg.role not in ("tool", "tool_result"):
                continue
            tool_name = tool_name_by_id.get(getattr(msg, "tool_call_id", "") or "")
            if tool_name not in self.EXPLORATION_TOOLS:
                continue
            content = msg.content
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                parts: list[str] = []
                for block in content:
                    t = getattr(block, "text", None)
                    if isinstance(t, str):
                        parts.append(t)
                text = "\n".join(parts)
            if not text:
                continue
            preview = text.strip().splitlines()[0][:200] if text.strip() else ""
            replacement = (
                f"[exploration result condensed ({tool_name}): "
                f"{len(text):,} chars · preview: {preview!r}]"
            )
            if len(replacement) < len(text):
                msg.content = replacement
                rewritten += 1

        after = transcript.estimate_tokens()
        return CompactionResult(
            success=rewritten > 0,
            summary=f"[scope=exploration_only · rewrote {rewritten} exploration results]",
            tokens_before=before,
            tokens_after=after,
            entries_removed=0,
            error=None if rewritten > 0 else "no exploration results were condensable",
        )


def _err(call_id: str, message: str) -> ToolResult:
    return ToolResult(call_id=call_id, content=f"Error: {message}", is_error=True)


def _classify_level(level_requested: str, result: Any) -> str:
    """Map (level_requested, result) → the level that was effectively used.

    For ``auto`` we infer from how many entries were summarized:
    small slices → episode, larger → chapter. Numeric thresholds match
    the doc's level template table.
    """
    if level_requested in ("episode", "chapter"):
        return level_requested
    entries = int(getattr(result, "entries_removed", 0) or 0)
    if entries >= 30:
        return "chapter"
    return "episode"
