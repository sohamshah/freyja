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
        on_system_event: Callable[[dict[str, Any]], None] | None = None,
        on_pin_changed: Callable[[dict[str, Any]], None] | None = None,
        on_summarizer_llm_call: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._get_session = get_session
        self._get_provider = get_provider
        self._get_compactor = get_compactor
        self._on_summarize_call = on_summarize_call
        self._get_current_pressure_pct = get_current_pressure_pct
        # System-event emit so agent-driven compactions show up inline
        # in the conversation timeline alongside runtime-driven ones
        # (Gap N). Bridge wires this to its renderer emit channel.
        self._on_system_event = on_system_event
        # Pin-change emit so the renderer's pin badge appears when the
        # agent pins via pin_entries on this tool (Gap M).
        self._on_pin_changed = on_pin_changed
        # Forwarded to ``compactor.compact(..., on_summarizer_call=...)``
        # so the summarizer's input/output tokens land in the runner's
        # on_llm_call hook tagged as compaction overhead (Gap 4).
        # Without this the agent-path summarizer is invisible to spend
        # metrics.
        self._on_summarizer_llm_call = on_summarizer_llm_call

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
                            "Use for credentials, exact paths, error messages. "
                            "If the summarizer paraphrases any of these, the "
                            "runtime auto-appends them to the summary."
                        ),
                    },
                    "pin_entries": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": (
                            "Message ordinals (0-indexed across user+assistant "
                            "message-bearing entries) to PIN before compaction "
                            "runs. Pinned messages are excluded from the "
                            "summarized slice and survive verbatim through "
                            "this and every future compaction. Use when a "
                            "specific tool result or user message is "
                            "load-bearing for ongoing work."
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
        pin_raw = arguments.get("pin_entries") or []
        pin_ordinals: list[int] = []
        if isinstance(pin_raw, list):
            for item in pin_raw:
                try:
                    n = int(item)
                except (TypeError, ValueError):
                    continue
                if n >= 0:
                    pin_ordinals.append(n)
        reason = str(arguments.get("reason") or "").strip()

        session = self._get_session()
        provider = self._get_provider()
        compactor = self._get_compactor()
        if session is None or provider is None or compactor is None:
            return _err(call_id, "summarize_context unavailable (session not ready)")

        transcript = getattr(session, "transcript", None)
        if transcript is None:
            return _err(call_id, "summarize_context unavailable (no transcript)")

        # Apply pin_entries BEFORE the compactor runs so _honor_pins
        # sees the new flags. We pin by message ordinal (the same
        # addressing scheme used by edit/rerun/delete IPC commands)
        # rather than transcript-entry id since the agent doesn't have
        # entry-id visibility. Each ordinal maps to the Nth message
        # across user+assistant entries that contain a Message.
        pinned_now: list[int] = []
        if pin_ordinals:
            try:
                pinned_now = _apply_pins_by_ordinal(transcript, pin_ordinals)
                # Notify the renderer for each successful pin so the pin
                # badge appears without waiting for a session reload
                # (Gap M).
                session_id_for_pin = getattr(session, "id", "") or ""
                for ord_idx in pinned_now:
                    self._emit_pin_changed({
                        "type": "entry_pin_changed",
                        "sessionId": session_id_for_pin,
                        "entryId": None,
                        "messageOrdinal": ord_idx,
                        "pinned": True,
                        "source": "agent_summarize_context",
                    })
            except Exception:
                logger.exception("pin_entries application failed")

        tokens_before = transcript.estimate_tokens()
        pressure_pct = (
            self._get_current_pressure_pct() if self._get_current_pressure_pct else None
        )
        started_at = time.time()

        # Gap N: emit a compaction_start system event so the agent-driven
        # compaction shows up inline in the conversation timeline (same
        # way runtime-driven compactions do via _attempt_compaction).
        # Best-effort — telemetry errors must never block the actual
        # compaction.
        self._emit_system_event({
            "type": "system_event",
            "sessionId": getattr(session, "id", "") or "",
            "subtype": "compaction_start",
            "message": (
                f"Agent-driven compaction started "
                f"(scope={scope}, {tokens_before:,} tokens before)"
            ),
            "details": {
                "trigger": "agent_summarize_context",
                "scope": scope,
                "tokens_before": tokens_before,
                "reason": reason[:240] if reason else None,
                "pressure_pct_at_call": pressure_pct,
            },
        })

        try:
            result = self._dispatch(
                scope, level, compactor, transcript, provider,
                on_summarizer_call=self._on_summarizer_llm_call,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("summarize_context dispatch failed")
            self._emit_system_event({
                "type": "system_event",
                "sessionId": getattr(session, "id", "") or "",
                "subtype": "compaction_skipped",
                "message": f"Agent-driven compaction failed: {exc}",
                "details": {
                    "trigger": "agent_summarize_context",
                    "scope": scope,
                    "error": str(exc),
                },
            })
            return _err(call_id, f"Compaction failed: {exc}")

        # Verify preserve_facts — repair (or warn) if any are missing.
        # The summarizer may paraphrase even verbatim strings; if the
        # agent declared a fact preserve-worthy we owe it a stronger
        # guarantee than "we asked nicely". Two-tier remedy:
        #   1. If exact substring missing, append a "Preserved facts"
        #      appendix to the just-written summary so the strings are
        #      literally present (this also passes the verification
        #      check on subsequent reads).
        #   2. Surface the missing list in the tool response so the
        #      agent knows the summary text didn't naturally contain
        #      them and can adjust strategy (e.g. pin source messages).
        missing: list[str] = []
        if result.success and result.summary and preserve_facts:
            for fact in preserve_facts:
                if fact and fact not in result.summary:
                    missing.append(fact)
            if missing:
                self._repair_preserve_facts(transcript, missing)
                # Re-read the (now repaired) summary from the transcript
                # so result.summary reflects the appendix we just added.
                # Defensive: tolerate transcript shape changes.
                try:
                    last_entry = next(
                        (
                            e for e in reversed(transcript.entries)
                            if getattr(e, "is_compaction", False)
                        ),
                        None,
                    )
                    if last_entry and last_entry.compaction_summary:
                        result.summary = last_entry.compaction_summary
                except Exception:
                    pass

        # Always emit telemetry, even on failure — the bad outcomes are
        # part of the training corpus.
        # Two rows per call:
        #   1. summarize_context_call (the trigger-decision corpus) —
        #      via the on_summarize_call callback; the bridge writes it.
        #   2. compaction_event (the aggregate-metrics row) — written
        #      directly here so the existing dashboard surfaces
        #      (trigger-source bar, savings trend, per-session compaction
        #      count) include agent-driven compactions alongside the
        #      runtime-driven ones. Without this, the Profiles + Sessions
        #      views under-count compactions on cooperative sessions.
        # compaction_event JSONL rows are written by the bridge's
        # _emit_summarize_event handler when it sees the
        # compaction_complete / context_pruning system_event above —
        # that path also enriches the row with scope/reason/excerpt.
        # Writing here would double-count.

        try:
            if self._on_summarize_call is not None:
                self._on_summarize_call({
                    "scope": scope,
                    "level_requested": level,
                    "level_used": _classify_level(level, result),
                    "preserve_facts_count": len(preserve_facts),
                    "preserve_facts_missing": missing,
                    "pinned_ordinals": pinned_now,
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
            self._emit_system_event({
                "type": "system_event",
                "sessionId": getattr(session, "id", "") or "",
                "subtype": "compaction_skipped",
                "message": (
                    f"Agent-driven compaction skipped: "
                    f"{result.error or 'unknown reason'}"
                ),
                "details": {
                    "trigger": "agent_summarize_context",
                    "scope": scope,
                    "reason": result.error,
                    "tokens_before": result.tokens_before,
                    "tokens_after": result.tokens_after,
                },
            })
            return _err(
                call_id,
                f"Compaction failed: {result.error or 'unknown'}",
            )

        # Emit a compaction_complete inline marker so the UI shows the
        # agent's compaction at the right place in the timeline.
        completion_subtype = (
            "context_pruning"
            if scope in ("tool_results_only", "exploration_only")
            else "compaction_complete"
        )
        self._emit_system_event({
            "type": "system_event",
            "sessionId": getattr(session, "id", "") or "",
            "subtype": completion_subtype,
            "message": (
                f"Agent-driven compaction complete "
                f"({result.tokens_before:,} → {result.tokens_after:,} tokens; "
                f"scope={scope})"
            ),
            "details": {
                "trigger": "agent_summarize_context",
                "scope": scope,
                "mechanism": (
                    scope if scope in ("tool_results_only", "exploration_only")
                    else "summary_iterative"
                    if getattr(result, "resumed_from_previous", False)
                    else "summary"
                ),
                "tokens_before": result.tokens_before,
                "tokens_after": result.tokens_after,
                "entries_removed": result.entries_removed,
                "resumed_from_previous": getattr(result, "resumed_from_previous", False),
                "reason": reason or None,
                # Excerpt for the inline glance, FULL text for the
                # expand panel and dashboard detail view.
                "summary_excerpt": (result.summary or "")[:240],
                "summary_text": result.summary or "",
            },
        })

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
        if pinned_now:
            body["pinned_ordinals"] = pinned_now
        if missing:
            body["preserve_facts_missing"] = missing
            body["warning"] = (
                f"{len(missing)} preserve_facts entries were auto-appended to "
                "the summary because the summarizer paraphrased them. Future "
                "reads of the summary will contain them verbatim. If a fact "
                "is load-bearing for ongoing work, prefer pinning the source "
                "message via pin_entries on this tool."
            )

        return ToolResult(
            call_id=call_id,
            content=json.dumps(body, indent=2),
            is_error=False,
        )

    def _emit_system_event(self, event: dict[str, Any]) -> None:
        """Fire the on_system_event callback if wired. Swallows all
        exceptions — UI plumbing must never block a real compaction."""
        if self._on_system_event is None:
            return
        try:
            self._on_system_event(event)
        except Exception:
            logger.exception("summarize_context system_event emit failed")

    def _emit_pin_changed(self, payload: dict[str, Any]) -> None:
        """Fire the on_pin_changed callback if wired (Gap M)."""
        if self._on_pin_changed is None:
            return
        try:
            self._on_pin_changed(payload)
        except Exception:
            logger.exception("summarize_context pin_changed emit failed")

    def _repair_preserve_facts(self, transcript: Any, missing: list[str]) -> None:
        """Append a Preserved Facts appendix to the most recent compaction
        entry's summary so the missing strings literally appear.

        Pragmatic, deterministic, no extra LLM call: when the summarizer
        paraphrases a fact the agent flagged as load-bearing, we just
        tack the verbatim string onto the end of the summary inside a
        clearly-labeled section. The summary now passes the substring
        check on every subsequent read (including by the iterative-
        path's PREVIOUS SUMMARY include) and the agent can still see
        which strings needed repair via the tool result's
        preserve_facts_missing field.
        """
        try:
            target_entry = None
            for entry in reversed(transcript.entries):
                if getattr(entry, "is_compaction", False):
                    target_entry = entry
                    break
            if target_entry is None or not target_entry.compaction_summary:
                return
            appendix_lines = ["", "## Preserved Facts (auto-repaired)"]
            for fact in missing:
                appendix_lines.append(f"- {fact}")
            target_entry.compaction_summary = (
                target_entry.compaction_summary.rstrip()
                + "\n"
                + "\n".join(appendix_lines)
                + "\n"
            )
        except Exception:
            logger.exception("preserve_facts repair failed")

    def _dispatch(
        self,
        scope: str,
        level: str,
        compactor: Any,
        transcript: Any,
        provider: Any,
        on_summarizer_call: Callable[[dict[str, Any]], None] | None = None,
    ):
        """Execute the requested scope by invoking the compactor.

        For most scopes we just call ``compactor.compact(transcript, provider)``
        — the existing compactor already implements the iterative path
        and the safe-split logic, so ``since_last_compaction`` and
        ``all`` (and ``early`` at small-history) all reduce to the same
        call. The fine-grained scopes (``tool_results_only``,
        ``exploration_only``) live in their own helpers.

        ``on_summarizer_call`` is forwarded into ``compactor.compact``
        so the summarizer LLM call gets tagged in the dashboard as
        compaction overhead (Gap 4). The in-place rewrite scopes
        (tool_results_only / exploration_only) don't make any LLM call,
        so the callback isn't passed there.
        """
        if scope in ("since_last_compaction", "all", "early"):
            return compactor.compact(
                transcript, provider, on_summarizer_call=on_summarizer_call,
            )
        if scope == "tool_results_only":
            return self._compact_tool_results_only(transcript)
        if scope == "exploration_only":
            return self._compact_exploration_only(transcript)
        # Shouldn't be reached — scope was validated above.
        return compactor.compact(
            transcript, provider, on_summarizer_call=on_summarizer_call,
        )

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


def _apply_pins_by_ordinal(transcript: Any, ordinals: list[int]) -> list[int]:
    """Mark transcript entries pinned, addressed by message ordinal.

    Ordinal indexing matches the bridge's IPC convention used by edit /
    rerun / delete commands: 0-indexed across entries that carry a
    Message (i.e. excluding compaction-only entries). Returns the
    ordinals that were successfully pinned.
    """
    seen = set(ordinals)
    pinned: list[int] = []
    idx = 0
    for entry in transcript.entries:
        if entry.message is None:
            continue
        if idx in seen:
            try:
                transcript.set_entry_pinned(entry.id, True)
                pinned.append(idx)
            except Exception:
                pass
        idx += 1
    return pinned


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
