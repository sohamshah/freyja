"""
Core agent runner implementing the retry/recovery state machine.

This is the heart of the engine, implementing the resilience patterns
from OpenClaw's runEmbeddedPiAgent() function (run.ts:400-1100).

Key behaviors:
1. Retry iteration formula: min(160, max(100, 24 + 8 * num_profiles))
2. Three-tier overflow cascade for context management
3. Auth profile rotation with cooldown awareness
4. Model fallback chain with primary probing
5. Error classification for appropriate recovery actions
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, AsyncIterator, Awaitable, Callable

from engine.compaction import CompactionResult, CompactionStrategy, NoOpCompaction
from engine.constants import (
    CONSECUTIVE_IDENTICAL_CALL_THRESHOLD,
    CONTEXT_COMPACTION_THRESHOLD,
    DEFAULT_THINKING_BUDGET_TOKENS,
    KEEP_RECENT_COMPUTER_IMAGES,
    ERROR_LOG_TRUNCATION,
    KEEP_RECENT_TOOL_RESULTS,
    MAX_REQUEST_IMAGES_SAFETY,
    LOOP_DETECTION_EXEMPT_TOOLS,
    PRIMARY_PROBE_MAX_TOKENS,
    STEERING_TAG_CLOSE,
    STEERING_TAG_OPEN,
    TOOL_ARGS_LOG_PREVIEW,
    TOOL_RESULT_LOG_PREVIEW,
    VERIFICATION_CONSECUTIVE_THRESHOLD,
    VERIFICATION_ITERATION_THRESHOLD,
)
from engine.errors import (
    classify_failover_reason,
    is_context_overflow_error,
    is_retryable_error,
)
from engine.providers import (
    AuthProfileManager,
    ContextOverflowError,
    ImagePayloadTooLargeError,
    ModelFallbackChain,
    ModelProvider,
    ProviderError,
    ProviderResponse,
)
from engine.session import Session
from engine.tools import ToolRegistry, ToolResultTruncator
from engine.types import (
    AgentConfig,
    AgentError,
    AgentResult,
    ContentBlock,
    FailoverReason,
    Message,
    RedactedThinkingBlock,
    StreamEvent,
    SystemEvent,
    TextDeltaEvent,
    ThinkingBlock,
    ThinkingConfig,
    ThinkingDeltaEvent,
    ToolCall,
    ToolInputDeltaEvent,
    ToolUseStartEvent,
)
from engine.usage import UsageAccumulator

logger = logging.getLogger(__name__)


# ============================================================================
# Runner State
# ============================================================================

class RunnerState(Enum):
    """State machine states for the agent runner."""

    IDLE = auto()
    """Not running."""

    RUNNING = auto()
    """Actively processing."""

    AWAITING_TOOL = auto()
    """Waiting for tool execution."""

    RECOVERING = auto()
    """In error recovery."""

    COMPACTING = auto()
    """Performing context compaction."""

    COMPLETED = auto()
    """Successfully completed."""

    FAILED = auto()
    """Failed after exhausting retries."""


@dataclass
class RunnerContext:
    """
    Mutable context for a single agent run.

    Tracks iteration counts, compaction attempts, and recovery state.
    """

    iteration: int = 0
    """Current iteration number."""

    compaction_attempts: int = 0
    """Number of compaction attempts this run."""

    consecutive_errors: int = 0
    """Consecutive errors without progress."""

    last_error: ProviderError | None = None
    """Most recent error encountered."""

    last_failover_reason: FailoverReason | None = None
    """Reason for last failover."""

    tool_results_truncated: int = 0
    """Number of tool results truncated this run."""

    state: RunnerState = RunnerState.IDLE
    """Current runner state."""

    # Turn Completion Verification
    consecutive_same_tool: int = 0
    """Count of consecutive iterations using the same tool(s) (name-only)."""

    last_tool_names: tuple[str, ...] = field(default_factory=tuple)
    """Sorted tool names from the last iteration (for end-turn verification)."""

    consecutive_identical_call: int = 0
    """Count of consecutive iterations with identical tool calls (name + args)."""

    last_call_keys: tuple[str, ...] = field(default_factory=tuple)
    """Sorted call keys (name:args_hash) from the last iteration."""

    verification_injected: bool = False
    """Whether turn verification has been injected this run (prevents re-injection)."""

    loop_break_injected: bool = False
    """Whether a loop-break correction has been injected this run."""

    # Anti-thrash tracking: when consecutive compactions each save very
    # little (the compactor is going in circles), skip further compaction
    # attempts and surface the condition. Same pattern as hermes-agent
    # (`_ineffective_compression_count >= 2` -> bail).
    last_compaction_savings_pct: float = 100.0
    """Percent of tokens removed by the most recent compaction (0..100)."""

    ineffective_compaction_count: int = 0
    """Count of consecutive compactions that saved < 10% — when this
    reaches 2, further compactions are skipped until the agent does
    something that resets the count (e.g. user sends a fresh message)."""


def _call_key(name: str, arguments: dict) -> str:
    """Stable key for a (tool_name, arguments) pair."""
    import hashlib, json as _json
    args_str = _json.dumps(arguments, sort_keys=True, default=str)
    h = hashlib.md5(args_str.encode()).hexdigest()[:12]
    return f"{name}:{h}"


# ============================================================================
# Stop Condition
# ============================================================================

@dataclass
class StopCondition:
    """
    Defines when the agent should stop.

    From OpenClaw run.ts:300-350: the agent stops when:
    1. Model returns end_turn without tool calls
    2. A stop phrase is detected
    3. Maximum iterations reached
    4. Unrecoverable error occurs
    """

    stop_phrases: list[str] = field(default_factory=list)
    """Phrases that trigger stop (e.g., "TASK COMPLETE")."""

    max_iterations: int | None = None
    """Override for maximum iterations (None uses config default)."""

    def should_stop(self, response: ProviderResponse, iteration: int, max_iter: int) -> bool:
        """Check if the agent should stop based on response."""
        # Check iteration limit
        if iteration >= max_iter:
            logger.info(f"Stopping: reached max iterations ({max_iter})")
            return True

        # Check for end_turn without tool calls
        if response.stop_reason == "end_turn" and not response.tool_calls:
            logger.debug("Stopping: end_turn without tool calls")
            return True

        # Check for max_tokens without tool calls
        if response.stop_reason == "max_tokens" and not response.tool_calls:
            logger.warning(
                "Stopping: max_tokens without tool calls. "
                "Text generation was truncated."
            )
            return True

        # Check stop phrases
        content_lower = response.content.lower()
        for phrase in self.stop_phrases:
            if phrase.lower() in content_lower:
                logger.info(f"Stopping: detected stop phrase '{phrase}'")
                return True

        return False


# ============================================================================
# Agent Runner (Sync)
# ============================================================================

class AgentRunner:
    """
    Core agent runner implementing the retry/recovery state machine.

    Mirrors OpenClaw's runEmbeddedPiAgent() with:
    - Dynamic retry calculation based on auth profiles
    - Three-tier overflow cascade
    - Auth profile rotation on failures
    - Model fallback chain support
    - Compaction integration for context management
    """

    def __init__(
        self,
        provider: ModelProvider,
        config: AgentConfig | None = None,
        *,
        auth_manager: AuthProfileManager | None = None,
        fallback_chain: ModelFallbackChain | None = None,
        compaction_strategy: CompactionStrategy | None = None,
        tool_registry: ToolRegistry | None = None,
        on_tool_call: Callable[[ToolCall], str | None] | None = None,
    ):
        self.provider = provider
        self.config = config or AgentConfig()
        self.auth_manager = auth_manager
        self.fallback_chain = fallback_chain
        self.compaction = compaction_strategy or NoOpCompaction()
        self.tool_registry = tool_registry or ToolRegistry()
        self.on_tool_call = on_tool_call

        # Tool result truncator
        self.truncator = ToolResultTruncator(self.config)

        # Usage tracking with "last value" semantics
        self.usage = UsageAccumulator()

    def _compute_truncation_budget(self, session: Session) -> int:
        """Compute a context-aware token budget for truncating a tool result."""
        tool_defs = self.tool_registry.list_definitions()
        session.tool_tokens = self.truncator.estimate_tool_definition_tokens(tool_defs)

        budget = self.truncator.compute_dynamic_budget(
            context_window=self.provider.context_window,
            session_tokens=session.estimate_tokens(),
        )
        return budget

    def run(
        self,
        session: Session,
        user_message: str | list[ContentBlock],
        *,
        stop_condition: StopCondition | None = None,
    ) -> AgentResult:
        """Run the agent loop for a user message."""
        ctx = RunnerContext(state=RunnerState.RUNNING)
        stop = stop_condition or StopCondition()

        num_profiles = len(self.auth_manager.profiles) if self.auth_manager else 1
        max_iterations = stop.max_iterations or self.config.compute_max_iterations(num_profiles)

        logger.info(
            f"Starting agent run: max_iterations={max_iterations}, "
            f"num_profiles={num_profiles}"
        )

        self.usage.reset()
        session.add_user_message(user_message)

        try:
            return self._run_loop(session, ctx, stop, max_iterations)
        except Exception as e:
            logger.exception(f"Unexpected error in agent run: {e}")
            ctx.state = RunnerState.FAILED
            return AgentResult(
                success=False,
                error=AgentError(
                    reason="unknown",
                    message=str(e),
                    retryable=False,
                    code="unexpected_error",
                ),
                usage=self.usage.to_stats(),
                iterations=ctx.iteration,
            )

    def _run_loop(
        self,
        session: Session,
        ctx: RunnerContext,
        stop: StopCondition,
        max_iterations: int,
    ) -> AgentResult:
        """Main agent loop with retry/recovery."""
        final_response: str = ""

        while ctx.iteration < max_iterations and ctx.state == RunnerState.RUNNING:
            ctx.iteration += 1
            self.current_iteration = ctx.iteration
            logger.debug(f"Iteration {ctx.iteration}/{max_iterations}")

            try:
                response = self._call_provider(session)
                self.usage.update(response.usage)
                self._handle_context_pressure(session, ctx)

                ctx.consecutive_errors = 0
                ctx.last_error = None

                if response.tool_calls:
                    tool_calls_for_message = [
                        ToolCall(
                            id=tc.id,
                            name=tc.name,
                            arguments=tc.arguments,
                            provider_kind=getattr(tc, "provider_kind", None),
                            provider_data=getattr(tc, "provider_data", {}) or {},
                        )
                        for tc in response.tool_calls
                    ]

                    session.add_assistant_message(
                        response.content,
                        tool_calls=tool_calls_for_message,
                        thinking_blocks=response.thinking_blocks,
                        input_tokens=response.usage.input_tokens if response.usage else 0,
                        output_tokens=response.usage.output_tokens if response.usage else 0,
                        cache_read_tokens=getattr(response.usage, "cache_read_tokens", 0) or 0,
                        cache_write_tokens=getattr(response.usage, "cache_write_tokens", 0) or 0,
                    )

                    if response.stop_reason == "max_tokens":
                        logger.warning(
                            "Tool call may be truncated: stop_reason=max_tokens."
                        )
                        for tc in response.tool_calls:
                            truncation_msg = (
                                "Error: Your tool call was truncated due to output token limits. "
                                "The tool arguments were incomplete. For large content, use "
                                "edit_file with incremental changes instead of write_file with "
                                "the entire content at once."
                            )
                            session.add_tool_result(tc.id, truncation_msg, is_error=True)
                        continue

                    ctx.state = RunnerState.AWAITING_TOOL
                    self._handle_tool_calls(session, response.tool_calls, ctx)
                    ctx.state = RunnerState.RUNNING

                    tracked_calls = [
                        tc for tc in response.tool_calls
                        if tc.name not in LOOP_DETECTION_EXEMPT_TOOLS
                    ]

                    if tracked_calls:
                        current_tools = tuple(sorted(tc.name for tc in tracked_calls))
                        if current_tools == ctx.last_tool_names:
                            ctx.consecutive_same_tool += 1
                        else:
                            ctx.consecutive_same_tool = 1
                            ctx.last_tool_names = current_tools

                        current_keys = tuple(sorted(
                            _call_key(tc.name, tc.arguments)
                            for tc in tracked_calls
                        ))
                        if current_keys == ctx.last_call_keys:
                            ctx.consecutive_identical_call += 1
                        else:
                            ctx.consecutive_identical_call = 1
                            ctx.last_call_keys = current_keys

                        if (
                            ctx.consecutive_identical_call >= CONSECUTIVE_IDENTICAL_CALL_THRESHOLD
                            and not ctx.loop_break_injected
                        ):
                            tool_names_str = ", ".join(
                                sorted(set(tc.name for tc in tracked_calls))
                            )
                            loop_msg = (
                                f"You have called {tool_names_str} "
                                f"{ctx.consecutive_identical_call} times in a row with "
                                f"the exact same arguments. The call already succeeded "
                                f"-- repeating it will not change the result. Move on "
                                f"to the next step."
                            )
                            logger.warning(
                                "Identical-call loop: %s x%d",
                                tool_names_str, ctx.consecutive_identical_call,
                            )
                            session.add_user_message(
                                f"{STEERING_TAG_OPEN}{loop_msg}{STEERING_TAG_CLOSE}"
                            )
                            ctx.loop_break_injected = True
                            continue

                else:
                    session.add_assistant_message(
                        response.content,
                        thinking_blocks=response.thinking_blocks,
                        input_tokens=response.usage.input_tokens if response.usage else 0,
                        output_tokens=response.usage.output_tokens if response.usage else 0,
                        cache_read_tokens=getattr(response.usage, "cache_read_tokens", 0) or 0,
                        cache_write_tokens=getattr(response.usage, "cache_write_tokens", 0) or 0,
                    )

                if stop.should_stop(response, ctx.iteration, max_iterations):
                    final_response = response.content
                    ctx.state = RunnerState.COMPLETED
                    break

                if not response.tool_calls:
                    final_response = response.content

                if self.fallback_chain and self.fallback_chain.should_probe_primary():
                    self._probe_primary()

            except ProviderError as e:
                ctx.state = RunnerState.RECOVERING

                # Per-image payload-size cap — handled before the generic
                # provider-error path because the right remedy is pruning
                # the oversized block, not retrying the same payload.
                if isinstance(e, ImagePayloadTooLargeError):
                    max_bytes = e.max_bytes or 5 * 1024 * 1024
                    stats = session.transcript.prune_oversized_images(
                        max_bytes=max_bytes,
                    )
                    if stats.changed:
                        ctx.consecutive_errors = 0
                        ctx.state = RunnerState.RUNNING
                        continue

                should_continue = self._handle_provider_error(e, session, ctx)
                if not should_continue:
                    ctx.state = RunnerState.FAILED
                    return AgentResult(
                        success=False,
                        error=AgentError(
                            reason=classify_failover_reason(str(e)),
                            message=str(e),
                            retryable=e.retryable,
                            code=e.code or "provider_error",
                        ),
                        usage=self.usage.to_stats(),
                        iterations=ctx.iteration,
                    )
                ctx.state = RunnerState.RUNNING

        if ctx.state == RunnerState.COMPLETED:
            return AgentResult(
                success=True,
                response=final_response,
                usage=self.usage.to_stats(),
                iterations=ctx.iteration,
            )
        elif ctx.iteration >= max_iterations:
            logger.warning(f"Agent reached max iterations ({max_iterations})")
            return AgentResult(
                success=False,
                response=final_response,
                error=AgentError(
                    reason="unknown",
                    message=f"Reached maximum iterations ({max_iterations})",
                    retryable=True,
                    code="max_iterations",
                ),
                usage=self.usage.to_stats(),
                iterations=ctx.iteration,
            )
        else:
            return AgentResult(
                success=False,
                error=AgentError(
                    reason="unknown",
                    message="Agent loop exited unexpectedly",
                    retryable=False,
                ),
                usage=self.usage.to_stats(),
                iterations=ctx.iteration,
            )

    def _call_provider(self, session: Session) -> ProviderResponse:
        """Make a completion call to the provider."""
        provider = (
            self.fallback_chain.current if self.fallback_chain else self.provider
        )
        session.transcript.prune_old_tool_result_images(
            keep_recent=KEEP_RECENT_COMPUTER_IMAGES,
            hard_limit=MAX_REQUEST_IMAGES_SAFETY,
        )

        tool_defs = None
        if self.tool_registry and len(self.tool_registry) > 0:
            tool_defs = self.tool_registry.list_definitions()

        return provider.complete(
            messages=session.get_messages(),
            tools=tool_defs,
            system_prompt=session.system_prompt,
            max_tokens=self.config.max_tokens_per_turn,
        )

    def _handle_tool_calls(
        self,
        session: Session,
        tool_calls: list,
        ctx: RunnerContext,
    ) -> None:
        """Execute tool calls and add results to session."""
        for tc in tool_calls:
            tool_call = ToolCall(
                id=tc.id,
                name=tc.name,
                arguments=tc.arguments,
            )

            if self.on_tool_call:
                result_content = self.on_tool_call(tool_call)
                if result_content is None:
                    result_content = f"Tool {tc.name} not found"
                    is_error = True
                else:
                    is_error = False
            elif self.tool_registry and tc.name in self.tool_registry:
                result = self.tool_registry.execute_sync(tool_call)
                result_content = result.content
                is_error = result.is_error
            else:
                result_content = f"Unknown tool: {tc.name}"
                is_error = True

            if isinstance(result_content, str):
                dynamic_budget = self._compute_truncation_budget(session)
                truncated_content, was_truncated = self.truncator.truncate_if_needed(
                    result_content, max_tokens=dynamic_budget, tool_name=tc.name
                )
                if was_truncated:
                    ctx.tool_results_truncated += 1
                    session_tokens = session.estimate_tokens()
                    logger.info(
                        "Truncated tool result for %s (budget=%dk, context_window=%dk, session=%dk)",
                        tc.name, dynamic_budget // 1000,
                        self.provider.context_window // 1000, session_tokens // 1000,
                    )
                result_content = truncated_content

            session.add_tool_result(tc.id, result_content, is_error=is_error)

    def _handle_provider_error(
        self,
        error: ProviderError,
        session: Session,
        ctx: RunnerContext,
    ) -> bool:
        """Handle provider errors with appropriate recovery."""
        ctx.consecutive_errors += 1
        ctx.last_error = error

        reason = classify_failover_reason(str(error))
        ctx.last_failover_reason = reason

        logger.warning(
            f"Provider error (attempt {ctx.consecutive_errors}): "
            f"{error} (reason: {reason})"
        )

        if is_context_overflow_error(str(error)) or isinstance(error, ContextOverflowError):
            return self._handle_overflow_cascade(session, ctx)

        if not is_retryable_error(str(error)) and not error.retryable:
            logger.error(f"Non-retryable error: {error}")
            return False

        if reason in ("auth", "rate_limit", "billing") and self.auth_manager:
            if self.auth_manager.advance():
                logger.info(f"Rotated to new auth profile: {self.auth_manager.current.id}")
                if ctx.last_failover_reason:
                    self.auth_manager.mark_cooldown(
                        self.auth_manager.current.id,
                        ctx.last_failover_reason,
                    )
                return True

        if self.fallback_chain:
            if self.fallback_chain.advance():
                logger.info(
                    f"Fell back to: {self.fallback_chain.current.name}/"
                    f"{self.fallback_chain.current.model_id}"
                )
                return True

        if ctx.consecutive_errors >= self.config.max_consecutive_errors:
            logger.error(
                f"Too many consecutive errors ({ctx.consecutive_errors})"
            )
            return False

        return True

    def _handle_overflow_cascade(
        self,
        session: Session,
        ctx: RunnerContext,
    ) -> bool:
        """Handle context overflow with three-tier cascade.

        Same order as the async path: image-byte elision first (cheap,
        no LLM call), then text summarization, then fallback chain
        advance. See the async ``_handle_overflow_cascade`` docstring
        for the rationale.
        """
        logger.warning(
            f"Context overflow detected - attempting summarization "
            f"(attempt {ctx.compaction_attempts + 1}/{self.config.max_compaction_attempts})"
        )

        if ctx.compaction_attempts >= self.config.max_compaction_attempts:
            logger.error("Exhausted compaction attempts")
            return False

        # Tier 1 — image-byte elision before paying for summarization.
        try:
            image_prune = session.transcript.prune_cumulative_image_payload(
                target_bytes=4 * 1024 * 1024,
            )
        except Exception:  # noqa: BLE001
            logger.exception("image elision raised")
            image_prune = None
        if image_prune is not None:
            elided = image_prune.images_before - image_prune.images_after
            if elided > 0:
                logger.warning(
                    "Pruned %d oldest image block(s) to free payload "
                    "(images: %d → %d) — retrying without summarization",
                    elided,
                    image_prune.images_before,
                    image_prune.images_after,
                )
                ctx.consecutive_errors = 0
                return True

        ctx.state = RunnerState.COMPACTING
        result = self._attempt_compaction(session, ctx)
        ctx.state = RunnerState.RECOVERING

        if result.success:
            logger.warning(
                f"Summarization complete: {result.tokens_before:,} -> "
                f"{result.tokens_after:,} tokens ({result.entries_removed} entries summarized)"
            )
            return True

        if self.fallback_chain and self.fallback_chain.advance():
            logger.warning(
                f"Summarization failed ({result.error}), falling back to: "
                f"{self.fallback_chain.current.name}/{self.fallback_chain.current.model_id}"
            )
            return True

        if ctx.compaction_attempts < self.config.max_compaction_attempts:
            logger.warning(f"Summarization failed: {result.error}, will retry")
            return True

        return False

    def _attempt_compaction(
        self,
        session: Session,
        ctx: RunnerContext,
    ) -> CompactionResult:
        """Attempt context compaction with anti-thrash protection.

        Skips compaction when the last 2 attempts each saved < 10% — the
        agent is in a loop where compaction can't free meaningful space
        and the right action is to halt with a clear signal rather than
        burn API calls on summaries that don't help.
        """
        if ctx.ineffective_compaction_count >= 2:
            tokens = session.estimate_tokens()
            logger.warning(
                "Compaction skipped — last %d attempts each saved <10%% "
                "(thrash detector tripped) | session=%s",
                ctx.ineffective_compaction_count, session.id,
            )
            # Surface to the telemetry stream so the metrics dashboard
            # can count thrash-skip events.
            try:
                from bridge.compaction_telemetry import append_telemetry  # type: ignore[import-not-found]

                append_telemetry({
                    "type": "compaction_event",
                    "session_id": session.id,
                    "subtype": "thrash_skip",
                    "mechanism": "thrash_skip",
                    "trigger": "thrash_detector",
                    "tokens_before": tokens,
                    "tokens_after": tokens,
                    "ineffective_run": ctx.ineffective_compaction_count,
                })
            except Exception:  # noqa: BLE001
                pass
            return CompactionResult(
                success=False,
                summary=None,
                tokens_before=tokens,
                tokens_after=tokens,
                error=(
                    f"Compaction thrash detected ({ctx.ineffective_compaction_count} "
                    f"ineffective compactions in a row). Recommend starting a fresh "
                    f"session or branching from an earlier message."
                ),
            )

        ctx.compaction_attempts += 1
        session.compaction_count += 1

        try:
            # Thread the runner's on_llm_call hook into the compactor so
            # the summarizer's input/output tokens land in the dashboard
            # tagged as compaction overhead (Gap 4). Without this the
            # summarizer call goes straight to provider.complete() and
            # bypasses every measurement we have — its cost would be
            # invisible.
            def _on_sum_call(payload: dict[str, Any]) -> None:
                if self.on_llm_call is None:
                    return
                try:
                    self.on_llm_call({
                        "provider": payload.get("provider", "unknown"),
                        "model": payload.get("model", "unknown"),
                        "duration_ms": payload.get("duration_ms", 0),
                        "streaming": False,
                        "input_tokens": payload.get("input_tokens", 0),
                        "output_tokens": payload.get("output_tokens", 0),
                        "cache_read_tokens": payload.get("cache_read_tokens", 0),
                        "cache_write_tokens": payload.get("cache_write_tokens", 0),
                        "reasoning_tokens": 0,
                        "stop_reason": "end_turn",
                        "tool_calls": 0,
                        "thinking_blocks": 0,
                        "error": None,
                        "call_kind": payload.get("call_kind", "summarizer"),
                        "iterative": payload.get("iterative", False),
                    })
                except Exception:  # noqa: BLE001
                    logger.exception("forwarding summarizer call to on_llm_call failed")

            _ground_truth = None
            _gct = getattr(self, "get_compaction_ground_truth", None)
            if _gct is not None:
                try:
                    _ground_truth = _gct()
                except Exception:  # noqa: BLE001
                    _ground_truth = None
            _wm_state = None
            _gws = getattr(self, "get_working_memory_state", None)
            if _gws is not None:
                try:
                    _wm_state = _gws()
                except Exception:  # noqa: BLE001
                    _wm_state = None
            result = self.compaction.compact(
                session.transcript,
                self.provider,
                on_summarizer_call=_on_sum_call,
                ground_truth=_ground_truth,
                on_working_memory_upserts=getattr(self, "apply_working_memory_upserts", None),
                working_memory_state=_wm_state,
            )
            # Track savings for the thrash detector. <10% savings on
            # this attempt counts toward the cooldown counter.
            if result.success and result.tokens_before > 0:
                savings_pct = (
                    (result.tokens_before - result.tokens_after)
                    / result.tokens_before * 100
                )
                ctx.last_compaction_savings_pct = savings_pct
                if savings_pct < 10:
                    ctx.ineffective_compaction_count += 1
                else:
                    ctx.ineffective_compaction_count = 0
            return result
        except Exception as e:
            logger.error(f"Compaction failed with exception: {e}")
            return CompactionResult(
                success=False,
                summary=None,
                tokens_before=session.estimate_tokens(),
                tokens_after=session.estimate_tokens(),
                error=str(e),
            )

    def _handle_context_pressure(self, session: Session, ctx: RunnerContext) -> None:
        """Handle context pressure using REAL token count from API response."""
        context_window = self.provider.context_window
        used_tokens = self.usage.effective_context_tokens()
        usage_fraction = used_tokens / context_window

        if usage_fraction > self.config.compaction_threshold:
            pruned = session.transcript.prune_old_tool_results(keep_recent=KEEP_RECENT_TOOL_RESULTS)
            if pruned > 0:
                logger.info(
                    f"Context pressure ({usage_fraction:.1%}): halved {pruned} old tool results"
                )

        if usage_fraction > CONTEXT_COMPACTION_THRESHOLD:
            ctx.state = RunnerState.COMPACTING
            compaction_result = self._attempt_compaction(session, ctx)
            if not compaction_result.success:
                logger.warning("Compaction failed under context pressure | session=%s", session.id)
            ctx.state = RunnerState.RUNNING

    def _probe_primary(self) -> None:
        """Probe the primary provider to check if it's available."""
        if not self.fallback_chain:
            return

        try:
            primary = self.fallback_chain.primary
            primary.complete(
                messages=[Message(role="user", content="ping")],
                max_tokens=PRIMARY_PROBE_MAX_TOKENS,
            )
            logger.info("Primary provider probe succeeded, resetting")
            self.fallback_chain.reset_to_primary()
        except ProviderError:
            logger.debug("Primary provider still unavailable")


# ============================================================================
# Stream Callback Types
# ============================================================================

StreamCallback = Callable[[StreamEvent], None]
"""Callback for stream events (sync)."""

AsyncStreamCallback = Callable[[StreamEvent], Awaitable[None]]
"""Callback for stream events (async)."""

SystemEventCallback = Callable[[SystemEvent], None]
"""Callback for system events (compaction, truncation, etc.)."""

AsyncSystemEventCallback = Callable[[SystemEvent], Awaitable[None]]
"""Callback for system events (async)."""


# ============================================================================
# Async Agent Runner
# ============================================================================

class AsyncAgentRunner:
    """
    Async agent runner with streaming support.

    Extends the sync AgentRunner with async/await operations and
    streaming callbacks for real-time output.
    """

    def __init__(
        self,
        provider: Any,  # ModelProvider with async methods
        config: AgentConfig | None = None,
        *,
        auth_manager: AuthProfileManager | None = None,
        fallback_chain: ModelFallbackChain | None = None,
        compaction_strategy: CompactionStrategy | None = None,
        tool_registry: ToolRegistry | None = None,
        on_tool_call: Callable[[ToolCall], str | None] | None = None,
        async_tool_call: Callable[[ToolCall], Awaitable[str | None]] | None = None,
        on_stream: StreamCallback | AsyncStreamCallback | None = None,
        on_system_event: SystemEventCallback | AsyncSystemEventCallback | None = None,
        on_llm_call: Callable[[dict[str, Any]], None] | None = None,
        on_tool_metric: Callable[[dict[str, Any]], None] | None = None,
        thinking: ThinkingConfig | None = None,
        on_pre_iteration: Callable[[Any, int], Awaitable[None]] | None = None,
        # Domain-specific system-reminder source. The bridge wires this
        # to supply (e.g.) stale-task reminders that ride alongside the
        # runner-managed context-pressure note. Called once per provider
        # request; returns a list of `<system-reminder>` blocks to append
        # to the trailing user message. Empty list = nothing to add.
        # Errors are swallowed so a bad callback can't break the runner.
        get_extra_system_reminders: Callable[[], list[str]] | None = None,
        # Optional provider of a deterministic "confirmed actions" string
        # (the action ledger) seeded into the summarizer so a runtime-forced
        # compaction can't drop the agent's own write-actions. Returns None
        # when unavailable. Kept as a plain-string callback so the engine
        # stays ignorant of the bridge's ledger.
        get_compaction_ground_truth: Callable[[], str | None] | None = None,
        # Optional sink for the structured working memory produced by
        # compaction's dedicated extraction call (Call B). Receives the full
        # ``{summary, actions_completed, entities}`` dict (or None); the bridge
        # folds it into the session's working memory. Engine stays ignorant of
        # the bridge store — it just forwards the result.
        apply_working_memory_upserts: Callable[[dict[str, Any] | None], None] | None = None,
        # Optional provider of the session's CURRENT working-memory state as a
        # rendered string, fed to compaction's extraction call (Call B) so it
        # updates/extends the existing memory rather than re-deriving it.
        # Returns None when unavailable.
        get_working_memory_state: Callable[[], str | None] | None = None,
    ):
        self.provider = provider
        self.config = config or AgentConfig()
        self.auth_manager = auth_manager
        self.fallback_chain = fallback_chain
        self.compaction = compaction_strategy or NoOpCompaction()
        self.tool_registry = tool_registry or ToolRegistry()
        self.on_tool_call = on_tool_call
        self.async_tool_call = async_tool_call
        self.on_stream = on_stream
        self.on_system_event = on_system_event
        self.on_llm_call = on_llm_call
        self.on_tool_metric = on_tool_metric
        self.thinking = thinking or ThinkingConfig()
        # Pre-iteration hook: invoked at the top of every iteration in
        # the async loop, BEFORE _ensure_context_room and BEFORE the
        # provider call. Bridge wires this to drain the session's inbox
        # and prepend incoming messages as attributed user turns.
        # Signature: async (session, iteration_index) -> None
        self.on_pre_iteration = on_pre_iteration

        # Tool result truncator
        self.truncator = ToolResultTruncator(self.config)

        # Usage tracking
        self.usage = UsageAccumulator()
        # Live iteration count — readable after a cancel/fail so callers
        # (e.g. sub_agent_tool's cancellation paths) can attribute partial
        # work to the agent before the AgentResult was produced.
        self.current_iteration: int = 0
        # Pressure-band crossing detector for Channel 3 (mid-stream
        # advisory). Tracks the band as observed at the start of the
        # *current* turn so that mid-turn tool-result wrappers can
        # decide whether the band escalated *during* the turn. Use
        # ``None`` as the "not yet recorded this turn" sentinel; ``0``
        # is a valid band (clean) and would conflate uninitialized
        # with "actually-clean" if used as the sentinel.
        self._turn_start_pressure_band: int | None = None
        self._channel3_advisory_pending: str = ""
        self.get_extra_system_reminders = get_extra_system_reminders
        self.get_compaction_ground_truth = get_compaction_ground_truth
        self.apply_working_memory_upserts = apply_working_memory_upserts
        self.get_working_memory_state = get_working_memory_state

    def _compute_truncation_budget(self, session: Session) -> int:
        """Compute a context-aware token budget for truncating a tool result."""
        tool_defs = self.tool_registry.list_definitions()
        session.tool_tokens = self.truncator.estimate_tool_definition_tokens(tool_defs)

        budget = self.truncator.compute_dynamic_budget(
            context_window=self.provider.context_window,
            session_tokens=session.estimate_tokens(),
        )
        return budget

    async def _emit_system_event(self, event: SystemEvent) -> None:
        """Emit a system event to the callback if registered."""
        if self.on_system_event:
            result = self.on_system_event(event)
            if asyncio.iscoroutine(result):
                await result

    async def run(
        self,
        session: Session,
        user_message: str | list[ContentBlock],
        *,
        stop_condition: StopCondition | None = None,
        stream: bool = True,
    ) -> AgentResult:
        """Run the agent loop asynchronously."""
        ctx = RunnerContext(state=RunnerState.RUNNING)
        stop = stop_condition or StopCondition()

        num_profiles = len(self.auth_manager.profiles) if self.auth_manager else 1
        max_iterations = stop.max_iterations or self.config.compute_max_iterations(num_profiles)

        # Propagate session ID to provider for log correlation
        provider = self.fallback_chain.current if self.fallback_chain else self.provider
        if hasattr(provider, "session_id"):
            provider.session_id = session.id

        logger.info(
            "Starting agent run | session=%s | max_iterations=%d | streaming=%s | model=%s",
            session.id, max_iterations, stream,
            getattr(provider, "model_id", "unknown"),
        )

        self.usage.reset()
        session.add_user_message(user_message)

        try:
            return await self._run_loop(session, ctx, stop, max_iterations, stream)
        except Exception as e:
            logger.exception("Unexpected error in agent run | session=%s: %s", session.id, e)
            ctx.state = RunnerState.FAILED
            usage_stats = self.usage.to_stats()
            return AgentResult(
                success=False,
                error=AgentError(
                    reason="unknown",
                    message=str(e),
                    retryable=False,
                    code="unexpected_error",
                ),
                usage=usage_stats,
                iterations=ctx.iteration,
            )

    async def _run_loop(
        self,
        session: Session,
        ctx: RunnerContext,
        stop: StopCondition,
        max_iterations: int,
        stream: bool,
    ) -> AgentResult:
        """Main async agent loop with streaming."""
        final_response: str = ""
        pre_verification_response: str = ""

        while ctx.iteration < max_iterations and ctx.state == RunnerState.RUNNING:
            ctx.iteration += 1
            self.current_iteration = ctx.iteration
            # Reset the Channel 3 start-of-turn band snapshot. Each
            # iteration is a fresh "turn" for the purposes of detecting
            # mid-turn pressure escalation.
            self.reset_turn_pressure_state()
            logger.info(
                "Agent iteration %d/%d | session=%s",
                ctx.iteration, max_iterations, session.id,
            )

            # Pre-iteration hook: lets the bridge drain the session's
            # inbox and prepend any new operator/agent messages as
            # attributed user turns before this iteration's LLM call.
            # Runs even on iteration 1 — a force-message can arrive
            # before the first provider call.
            if self.on_pre_iteration is not None:
                try:
                    await self.on_pre_iteration(session, ctx.iteration)
                except Exception:
                    logger.exception("on_pre_iteration hook raised")

            try:
                # Pre-request safety check: compact BEFORE sending
                await self._ensure_context_room(session, ctx)

                if stream:
                    response = await self._call_provider_stream(session)
                else:
                    response = await self._call_provider_async(session)

                self.usage.update(response.usage)
                # Channel 3 (mid-stream advisory): now that usage is
                # updated and the band is current, see if pressure
                # crossed mid-turn so the next tool result wrapper can
                # surface the advisory.
                self.mark_channel3_crossing()
                await self._handle_context_pressure_async(session, ctx)

                ctx.consecutive_errors = 0
                ctx.last_error = None

                if response.tool_calls:
                    tool_calls_for_message = [
                        ToolCall(
                            id=tc.id,
                            name=tc.name,
                            arguments=tc.arguments,
                            provider_kind=getattr(tc, "provider_kind", None),
                            provider_data=getattr(tc, "provider_data", {}) or {},
                        )
                        for tc in response.tool_calls
                    ]

                    session.add_assistant_message(
                        response.content,
                        tool_calls=tool_calls_for_message,
                        thinking_blocks=response.thinking_blocks,
                        input_tokens=response.usage.input_tokens if response.usage else 0,
                        output_tokens=response.usage.output_tokens if response.usage else 0,
                        cache_read_tokens=getattr(response.usage, "cache_read_tokens", 0) or 0,
                        cache_write_tokens=getattr(response.usage, "cache_write_tokens", 0) or 0,
                    )

                    if response.stop_reason == "max_tokens":
                        logger.warning(
                            "Tool call may be truncated: stop_reason=max_tokens | session=%s",
                            session.id,
                        )
                        tool_names = [tc.name for tc in response.tool_calls]
                        await self._emit_system_event(SystemEvent(
                            type="tool_truncation",
                            message=f"Tool call truncated (hit output limit): {', '.join(tool_names)}",
                            details={"tool_names": tool_names},
                        ))
                        for tc in response.tool_calls:
                            truncation_msg = (
                                "Error: Your tool call was truncated due to output token limits. "
                                "The tool arguments were incomplete. For large content, use "
                                "edit_file with incremental changes instead of write_file with "
                                "the entire content at once."
                            )
                            session.add_tool_result(tc.id, truncation_msg, is_error=True)
                        continue

                    ctx.state = RunnerState.AWAITING_TOOL
                    await self._handle_tool_calls(session, response.tool_calls, ctx)
                    ctx.state = RunnerState.RUNNING

                    tracked_calls = [
                        tc for tc in response.tool_calls
                        if tc.name not in LOOP_DETECTION_EXEMPT_TOOLS
                    ]

                    if tracked_calls:
                        current_tools = tuple(sorted(tc.name for tc in tracked_calls))
                        if current_tools == ctx.last_tool_names:
                            ctx.consecutive_same_tool += 1
                        else:
                            ctx.consecutive_same_tool = 1
                            ctx.last_tool_names = current_tools

                        current_keys = tuple(sorted(
                            _call_key(tc.name, tc.arguments)
                            for tc in tracked_calls
                        ))
                        if current_keys == ctx.last_call_keys:
                            ctx.consecutive_identical_call += 1
                        else:
                            ctx.consecutive_identical_call = 1
                            ctx.last_call_keys = current_keys

                        if (
                            ctx.consecutive_identical_call >= CONSECUTIVE_IDENTICAL_CALL_THRESHOLD
                            and not ctx.loop_break_injected
                        ):
                            tool_names_str = ", ".join(
                                sorted(set(tc.name for tc in tracked_calls))
                            )
                            loop_msg = (
                                f"You have called {tool_names_str} "
                                f"{ctx.consecutive_identical_call} times in a row with "
                                f"the exact same arguments. The call already succeeded "
                                f"-- repeating it will not change the result. Move on "
                                f"to the next step."
                            )
                            logger.warning(
                                "Identical-call loop: %s x%d | session=%s",
                                tool_names_str, ctx.consecutive_identical_call,
                                getattr(session, 'session_id', '-'),
                            )
                            session.add_user_message(
                                f"{STEERING_TAG_OPEN}{loop_msg}{STEERING_TAG_CLOSE}"
                            )
                            ctx.loop_break_injected = True
                            continue

                else:
                    session.add_assistant_message(
                        response.content,
                        thinking_blocks=response.thinking_blocks,
                        input_tokens=response.usage.input_tokens if response.usage else 0,
                        output_tokens=response.usage.output_tokens if response.usage else 0,
                        cache_read_tokens=getattr(response.usage, "cache_read_tokens", 0) or 0,
                        cache_write_tokens=getattr(response.usage, "cache_write_tokens", 0) or 0,
                    )

                if stop.should_stop(response, ctx.iteration, max_iterations):
                    if response.stop_reason == "max_tokens" and not response.tool_calls:
                        await self._emit_system_event(SystemEvent(
                            type="output_truncation",
                            message="Response truncated (hit output token limit)",
                            details={"content_length": len(response.content)},
                        ))
                        final_response = response.content
                        ctx.state = RunnerState.COMPLETED
                        break

                    # Turn verification before ending
                    if (
                        response.stop_reason == "end_turn"
                        and ctx.consecutive_same_tool >= VERIFICATION_CONSECUTIVE_THRESHOLD
                        and ctx.iteration >= VERIFICATION_ITERATION_THRESHOLD
                        and not ctx.verification_injected
                    ):
                        tool_names_str = ", ".join(ctx.last_tool_names) if ctx.last_tool_names else "unknown"
                        verification_msg = (
                            f"You made {ctx.consecutive_same_tool} consecutive {tool_names_str} calls. "
                            f"Before finishing, verify all items from the original task/error were addressed. "
                            f"Continue if there's remaining work, or confirm completion."
                        )
                        pre_verification_response = response.content
                        session.add_user_message(
                            f"{STEERING_TAG_OPEN}{verification_msg}{STEERING_TAG_CLOSE}"
                        )
                        ctx.verification_injected = True
                        continue

                    if ctx.verification_injected and pre_verification_response:
                        final_response = pre_verification_response + "\n\n" + response.content
                    else:
                        final_response = response.content
                    ctx.state = RunnerState.COMPLETED
                    break

                if not response.tool_calls:
                    final_response = response.content

            except ProviderError as e:
                ctx.state = RunnerState.RECOVERING
                _err_reason = classify_failover_reason(str(e))
                _is_rate_limit = _err_reason == "rate_limit"
                _is_too_much_media = "too much media" in str(e).lower()

                # Per-image payload-size cap (Anthropic: 5 MB, OpenAI: 20 MB).
                # Distinct from context overflow — summarization keeps the
                # offending image in the recent tail, so we have to prune
                # the specific oversized block. Retry once the transcript
                # is rewritten; if nothing was prunable we fall through to
                # the standard error handler.
                if isinstance(e, ImagePayloadTooLargeError):
                    max_bytes = e.max_bytes or 5 * 1024 * 1024
                    stats = session.transcript.prune_oversized_images(
                        max_bytes=max_bytes,
                    )
                    if stats.changed:
                        await self._emit_system_event(SystemEvent(
                            type="media_pruning",
                            message=(
                                f"Provider rejected image payload ({stats.omitted_images} "
                                f"image{'s' if stats.omitted_images != 1 else ''} exceeded "
                                f"{max_bytes // (1024 * 1024)} MB); pruned oversized blocks and retrying."
                            ),
                            details={
                                **stats.to_details(),
                                "trigger": "provider_error",
                                "strategy": "oversized_image_prune",
                                "max_bytes": max_bytes,
                            },
                        ))
                        ctx.consecutive_errors = 0
                        ctx.state = RunnerState.RUNNING
                        continue

                if _is_too_much_media:
                    stats = session.transcript.prune_old_tool_result_images(
                        keep_recent=max(1, min(KEEP_RECENT_COMPUTER_IMAGES, 2)),
                        hard_limit=min(MAX_REQUEST_IMAGES_SAFETY, 60),
                    )
                    if stats.changed:
                        await self._emit_system_event(SystemEvent(
                            type="media_pruning",
                            message=(
                                f"Provider rejected media payload; omitted "
                                f"{stats.omitted_images} older screenshot image"
                                f"{'s' if stats.omitted_images != 1 else ''} and retrying."
                            ),
                            details={
                                **stats.to_details(),
                                "trigger": "provider_error",
                                "strategy": "recent_computer_screenshots_retry",
                            },
                        ))
                        ctx.state = RunnerState.RUNNING
                        continue

                if _is_rate_limit:
                    try:
                        await self._ensure_context_room(session, ctx)
                    except Exception as ce:  # noqa: BLE001
                        logger.debug("pre-retry compaction skipped: %s", ce)

                should_continue = await self._handle_provider_error(e, session, ctx)
                if should_continue:
                    attempt = ctx.consecutive_errors
                    if _is_rate_limit:
                        delay = min(60.0, 5.0 * (2 ** (attempt - 1)))
                    else:
                        delay = min(8.0, 0.5 * (2 ** (attempt - 1)))
                    logger.info(
                        "Retrying after provider error in %.1fs (attempt %d, reason=%s) | session=%s",
                        delay, attempt, _err_reason, session.id,
                    )
                    try:
                        await asyncio.sleep(delay)
                    except asyncio.CancelledError:
                        raise
                if not should_continue:
                    ctx.state = RunnerState.FAILED
                    usage_stats = self.usage.to_stats()
                    error_reason = classify_failover_reason(str(e))
                    return AgentResult(
                        success=False,
                        error=AgentError(
                            reason=error_reason,
                            message=str(e),
                            retryable=e.retryable,
                            code=e.code or "provider_error",
                        ),
                        usage=usage_stats,
                        iterations=ctx.iteration,
                    )
                ctx.state = RunnerState.RUNNING

        # Determine final state and build result
        usage_stats = self.usage.to_stats()

        if ctx.state == RunnerState.COMPLETED:
            result = AgentResult(
                success=True,
                response=final_response,
                usage=usage_stats,
                iterations=ctx.iteration,
            )
        elif ctx.iteration >= max_iterations:
            logger.warning("Agent reached max iterations (%d) | session=%s", max_iterations, session.id)
            result = AgentResult(
                success=False,
                response=final_response,
                error=AgentError(
                    reason="unknown",
                    message=f"Reached maximum iterations ({max_iterations})",
                    retryable=True,
                    code="max_iterations",
                ),
                usage=usage_stats,
                iterations=ctx.iteration,
            )
        else:
            logger.warning("Agent loop exited unexpectedly | session=%s", session.id)
            result = AgentResult(
                success=False,
                error=AgentError(
                    reason="unknown",
                    message="Agent loop exited unexpectedly",
                    retryable=False,
                ),
                usage=usage_stats,
                iterations=ctx.iteration,
            )

        logger.info(
            "Agent run complete | session=%s | success=%s | iterations=%d | input_tokens=%d | output_tokens=%d | cache_read=%d",
            session.id, result.success, ctx.iteration,
            usage_stats.input_tokens, usage_stats.output_tokens,
            usage_stats.cache_read_tokens,
        )
        return result

    async def _call_provider_async(self, session: Session) -> ProviderResponse:
        """Make an async completion call without streaming."""
        provider = (
            self.fallback_chain.current if self.fallback_chain else self.provider
        )

        tool_defs = None
        if self.tool_registry and len(self.tool_registry) > 0:
            tool_defs = self.tool_registry.list_definitions()

        start = time.perf_counter()
        # Channel 2 of the cooperative protocol: snapshot the pressure
        # band before the call, then tail-append the pressure note to
        # the last user message (not the system prompt — see
        # ``_augment_messages_with_pressure_note``).
        self._snapshot_turn_start_band()
        request_messages = self._augment_messages_with_pressure_note(
            session.get_messages(),
        )
        try:
            response = await provider.complete_async(
                messages=request_messages,
                tools=tool_defs,
                system_prompt=session.system_prompt,
                max_tokens=self.config.max_tokens_per_turn,
                thinking=self.thinking if self.thinking.enabled else None,
            )
        except Exception as exc:
            self._notify_llm_call(provider, start, streaming=False, response=None, error=exc)
            raise
        self._notify_llm_call(provider, start, streaming=False, response=response, error=None)
        return response

    async def _call_provider_stream(self, session: Session) -> ProviderResponse:
        """Make a streaming completion call."""
        provider = (
            self.fallback_chain.current if self.fallback_chain else self.provider
        )

        tool_defs = None
        if self.tool_registry and len(self.tool_registry) > 0:
            tool_defs = self.tool_registry.list_definitions()

        async def _handle_event(event: StreamEvent) -> None:
            if self.on_stream:
                result = self.on_stream(event)
                if asyncio.iscoroutine(result):
                    await result

        start = time.perf_counter()
        # Channel 2 of the cooperative protocol — see the non-stream
        # ``_call_provider_async`` for rationale.
        self._snapshot_turn_start_band()
        request_messages = self._augment_messages_with_pressure_note(
            session.get_messages(),
        )
        try:
            response = await provider.stream_to_response(
                messages=request_messages,
                tools=tool_defs,
                system_prompt=session.system_prompt,
                max_tokens=self.config.max_tokens_per_turn,
                thinking=self.thinking if self.thinking.enabled else None,
                on_event=_handle_event if self.on_stream else None,
            )
        except Exception as exc:
            self._notify_llm_call(provider, start, streaming=True, response=None, error=exc)
            raise
        self._notify_llm_call(provider, start, streaming=True, response=response, error=None)
        return response

    def _current_pressure_ratio(self) -> float:
        """Live pressure ratio = effective context tokens / effective window.

        ``effective_window`` = ``context_window - max_tokens_per_turn`` —
        the same math used by `_emit_pressure_telemetry_if_changed` in the
        bridge. Returns 0.0 when the math underflows (e.g. before the
        first call's usage lands).
        """
        try:
            provider = (
                self.fallback_chain.current if self.fallback_chain else self.provider
            )
            window = int(getattr(provider, "context_window", 0) or 0)
            if window <= 0:
                return 0.0
            reserved = int(getattr(self.config, "max_tokens_per_turn", 0) or 0)
            effective = max(1, window - reserved)
            used = int(self.usage.effective_context_tokens())
            return used / effective
        except Exception:  # noqa: BLE001
            return 0.0

    def _build_pressure_note(self, ratio: float) -> str:
        """Channel 2: pressure-aware advisory text.

        Empty below 40% (soft band). Above 40% the percentage is
        *quantized* into 10-point bands so the text is stable within
        each band and changes only on band crossings.

        Why ``<system-reminder>`` framing: the note lives in the last
        user-role message (cache-friendly — see
        ``_augment_messages_with_pressure_note``), but a plain
        TextBlock at the tail of a 50KB tool_results blob is easy to
        miss. Anthropic-family models are trained to attend to
        ``<system-reminder>...</system-reminder>`` blocks as automated
        runtime guidance — not user intent, not the model's own
        thought, just a side-channel notification. OpenAI/Fireworks
        models read it as plain text with a clearly demarcated
        wrapper, which still works. This dodges the awkwardness of
        pretending to be the user (the previous framing) or trying
        to inject a synthetic assistant message (Anthropic's prefill
        mode contaminates the response and bills at output rate).

        Why NOT the system prompt: injecting into ``system`` would
        evict the cache_control breakpoint on the static system block
        every band crossing, billing the cache_write rate (1.25× input
        on Anthropic) for a ~30k token block to deliver ~200 bytes of
        dynamic guidance.
        """
        if ratio < 0.40:
            return ""
        # Quantize: ratio 0.42 → "40-49%", 0.55 → "50-59%", 0.61 → "60-69%".
        decile_low = int(ratio * 10) * 10
        decile_high = min(99, decile_low + 9)
        band_label = f"{decile_low}-{decile_high}%"
        if ratio >= 0.70:
            recommendation = (
                "call summarize_context() NOW — runtime fallback is imminent "
                "(forced compaction at 70%) and forced summaries lose more "
                "context than self-timed ones"
            )
        elif ratio >= 0.55:
            recommendation = (
                "call summarize_context() before issuing more tool calls — "
                "the cooperative window is closing. "
                "scope='since_last_compaction' is the cheapest option"
            )
        else:
            recommendation = (
                "call summarize_context() at the next natural break "
                "(after this task completes, before starting a new one). "
                "scope='since_last_compaction' extends the prior summary"
            )
        return (
            "\n\n"
            "<system-reminder>\n"
            f"Context window is at {band_label} of capacity. "
            f"Recommendation: {recommendation}.\n"
            "</system-reminder>"
        )

    @staticmethod
    def _classify_pressure_band(ratio: float) -> int:
        """Coarse band: 0 clean, 1 awareness, 2 soft, 3 strong, 4 fallback.

        Bands align with the constants in engine/constants.py — pulling
        the forced band from 0.80 down to 0.70 (Phase 2.5) shifts the
        strong band down to 0.55 so there's still meaningful daylight
        between strong-suggest and forced.
        """
        if ratio >= 0.70:
            return 4
        if ratio >= 0.55:
            return 3
        if ratio >= 0.40:
            return 2
        if ratio >= 0.25:
            return 1
        return 0

    def _snapshot_turn_start_band(self) -> None:
        """Snapshot the pressure band at turn start for Channel 3.

        Called once per iteration before the provider call. Records the
        band so ``mark_channel3_crossing`` (run after the response)
        can detect an upward crossing into ≥ strong during the turn.
        Subsequent calls within the same iteration leave the snapshot
        alone — only the FIRST call sets it.
        """
        if self._turn_start_pressure_band is None:
            self._turn_start_pressure_band = self._classify_pressure_band(
                self._current_pressure_ratio(),
            )

    def _gather_extra_reminders(self) -> str:
        """Call the bridge-supplied reminder source and concatenate
        whatever it returns. Swallow exceptions so a bad source
        callback can't break the request pipeline — we'd rather miss
        a reminder than fail the turn."""
        cb = self.get_extra_system_reminders
        if cb is None:
            return ""
        try:
            entries = cb() or []
        except Exception:  # noqa: BLE001
            return ""
        chunks = [str(e).strip() for e in entries if str(e).strip()]
        if not chunks:
            return ""
        # Each entry is expected to be a complete `<system-reminder>`
        # block; we just join with blank lines so they don't run
        # together visually if multiple sources fire on the same turn.
        return "\n\n" + "\n\n".join(chunks)

    def _augment_messages_with_pressure_note(
        self, messages: list[Message],
    ) -> list[Message]:
        """Channel 2: tail-append the pressure note to the last user-role
        message in the request, without mutating the session transcript.

        Why not the system prompt: appending to ``system`` invalidates
        Anthropic's cache_control breakpoint on the static system block
        every time the pressure band changes — paying the cache_write
        rate (1.25× input) for ~30k tokens to deliver ~200 bytes of
        dynamic guidance. Tail-appending leaves the system + tools +
        compaction-summary cache markers intact; only the suffix of the
        last user message is reprocessed at full input rate (~200
        tokens), which is the right cost shape.

        Why the last user message: at every provider-call moment the
        last entry in the messages list is a user-role message —
        either the initial user prompt (iter 1) or the most recent
        tool_results batch. Anthropic + OpenAI both accept extra text
        content appended to the end of that message. Tool_use_id /
        tool_result_id pairing is unaffected because we add a new
        TextBlock alongside the existing tool_result blocks (or
        concatenate to string content).

        Returns a new list with a single cloned tail message so the
        transcript stays clean; the original session entries are
        never mutated.
        """
        ratio = self._current_pressure_ratio()
        note = self._build_pressure_note(ratio)
        # Bridge-supplied extras (stale-task reminders, etc) ride
        # alongside the pressure note via the same tail-append path so
        # they share the same cache cost — one user-message suffix per
        # turn instead of multiple injection mechanisms.
        extras = self._gather_extra_reminders()
        combined = (note or "") + (extras or "")
        if not combined or not messages:
            return messages
        last = messages[-1]
        if last.role != "user":
            # Conservative: don't touch non-user trailing messages. In
            # practice the loop only sends after a user/tool_result is
            # appended, so this guard rarely fires.
            return messages
        if isinstance(last.content, str):
            new_content: Any = last.content + combined
        elif isinstance(last.content, list):
            from engine.types import TextBlock as _TextBlock
            new_content = list(last.content) + [_TextBlock(text=combined)]
        else:
            return messages
        cloned = Message(
            role=last.role,
            content=new_content,
            tool_calls=last.tool_calls,
            tool_call_id=last.tool_call_id,
            thinking_blocks=last.thinking_blocks,
            input_tokens=last.input_tokens,
            output_tokens=last.output_tokens,
            cache_read_tokens=last.cache_read_tokens,
            cache_write_tokens=last.cache_write_tokens,
        )
        return messages[:-1] + [cloned]

    def mark_channel3_crossing(self) -> None:
        """Channel 3 (Approach A): detect mid-turn band escalation.

        Call this after a provider response lands. If the band crossed
        upward into ≥ strong during the turn, stash an advisory string
        that the bridge's tool-result wrapper will prepend to the *next*
        tool result. Piggybacks on the natural reactor cadence (no
        stream interruption) per the design doc's preferred Approach A.
        """
        try:
            ratio = self._current_pressure_ratio()
            band_now = self._classify_pressure_band(ratio)
            band_then = self._turn_start_pressure_band
            # If the snapshot wasn't taken (very first call after reset)
            # treat it as 0 — we can't claim "escalation" against an
            # unknown baseline, so the comparison naturally short-circuits.
            if band_then is None:
                band_then = band_now
            # Only fire on a real escalation INTO ≥ strong (3) — soft
            # already has Channel 2 nudging via the pre-turn system note;
            # mid-turn injection earns its keep at strong+.
            if band_now >= 3 and band_now > band_then:
                # Quantized window label keeps the advisory cache-friendly
                # if it ever ends up in a cached prefix.
                decile_low = int(ratio * 10) * 10
                decile_high = min(99, decile_low + 9)
                self._channel3_advisory_pending = (
                    f"[!CTX PRESSURE: window crossed {decile_low}-{decile_high}% "
                    f"during this turn (was band {band_then} at turn start). "
                    "Finish your current immediate goal, then call "
                    "summarize_context() before issuing more tool calls. "
                    "Tool calls beyond ~5 more may be rejected.]"
                )
        except Exception:  # noqa: BLE001
            pass

    def consume_channel3_advisory(self) -> str:
        """Pop the pending Channel 3 advisory (one-shot).

        Called by the bridge's tool-result wrapper to inject the
        advisory into the *first* tool result after the crossing. Once
        consumed, the slot is cleared so we don't spam advisories for
        every tool call in a multi-tool turn.
        """
        advisory = self._channel3_advisory_pending
        self._channel3_advisory_pending = ""
        return advisory

    def reset_turn_pressure_state(self) -> None:
        """Reset start-of-turn band snapshot at the top of each iteration."""
        self._turn_start_pressure_band = None
        # Don't clear _channel3_advisory_pending here — it may have been
        # set during the previous turn and not yet consumed.

    def _notify_llm_call(
        self,
        provider: Any,
        start: float,
        *,
        streaming: bool,
        response: ProviderResponse | None,
        error: Exception | None,
    ) -> None:
        """Fire the on_llm_call hook with structured per-call diagnostics."""
        if self.on_llm_call is None:
            return
        usage = response.usage if response and response.usage else None
        payload: dict[str, Any] = {
            "provider": getattr(provider, "name", "unknown"),
            "model": getattr(provider, "model_id", "unknown"),
            "duration_ms": int((time.perf_counter() - start) * 1000),
            "streaming": streaming,
            "input_tokens": usage.input_tokens if usage else 0,
            "output_tokens": usage.output_tokens if usage else 0,
            "cache_read_tokens": usage.cache_read_tokens if usage else 0,
            "cache_write_tokens": usage.cache_write_tokens if usage else 0,
            "reasoning_tokens": usage.reasoning_tokens if usage else 0,
            "stop_reason": response.stop_reason if response else None,
            # `stop_details` is the structured refusal categorization
            # Anthropic Opus 4.7+ returns alongside stop_reason="refusal".
            # Surfaced here so a consumer (the bridge) can route a refusal
            # differently from a normal end_turn — currently the bridge
            # emits a `refusal_detected` system_event for the UI.
            "stop_details": response.stop_details if response else None,
            "tool_calls": len(response.tool_calls or []) if response else 0,
            "thinking_blocks": len(response.thinking_blocks or []) if response else 0,
            "error": None if error is None else f"{type(error).__name__}: {error}",
        }
        try:
            self.on_llm_call(payload)
        except Exception:  # noqa: BLE001
            logger.exception("on_llm_call hook failed")

    async def _handle_tool_calls(
        self,
        session: Session,
        tool_calls: list,
        ctx: RunnerContext,
    ) -> None:
        """Execute tool calls and add results to session."""
        if self.config.parallel_tool_execution and len(tool_calls) > 1:
            await self._handle_tool_calls_parallel(session, tool_calls, ctx)
        else:
            await self._handle_tool_calls_sequential(session, tool_calls, ctx)

    async def _handle_tool_calls_sequential(
        self,
        session: Session,
        tool_calls: list,
        ctx: RunnerContext,
    ) -> None:
        """Execute tool calls sequentially."""
        for tc in tool_calls:
            result_content, is_error = await self._execute_single_tool(tc, ctx, session)
            session.add_tool_result(tc.id, result_content, is_error=is_error)

    async def _handle_tool_calls_parallel(
        self,
        session: Session,
        tool_calls: list,
        ctx: RunnerContext,
    ) -> None:
        """Execute tool calls in parallel with concurrency limit."""
        semaphore = asyncio.Semaphore(self.config.max_parallel_tools)

        async def execute_with_semaphore(tc):
            async with semaphore:
                return tc.id, await self._execute_single_tool(tc, ctx, session)

        tasks = [execute_with_semaphore(tc) for tc in tool_calls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                tc = tool_calls[i]
                logger.error("Tool execution error for %s: %s", tc.name, result)
                session.add_tool_result(
                    tc.id,
                    f"Tool execution error: {result}",
                    is_error=True,
                )
                continue
            tc_id, (result_content, is_error) = result
            session.add_tool_result(tc_id, result_content, is_error=is_error)

    async def _execute_single_tool(
        self,
        tc: Any,
        ctx: RunnerContext,
        session: Session | None = None,
    ) -> tuple[str, bool]:
        """Execute a single tool and return (result_content, is_error)."""
        tool_call = ToolCall(
            id=tc.id,
            name=tc.name,
            arguments=tc.arguments,
        )

        # Log tool invocation with args preview
        args_preview = ""
        if tc.arguments:
            import json as _json
            try:
                args_str = _json.dumps(tc.arguments) if isinstance(tc.arguments, dict) else str(tc.arguments)
                args_preview = args_str[:TOOL_ARGS_LOG_PREVIEW]
            except Exception:
                args_preview = str(tc.arguments)[:TOOL_ARGS_LOG_PREVIEW]
        sid = session.id if session else "-"
        logger.info(
            "TOOL CALL [iter=%d] %s(%s) call_id=%s | session=%s",
            ctx.iteration, tc.name, args_preview, tc.id, sid,
        )
        _tool_start = time.time()

        # Execute tool
        if self.async_tool_call:
            result_content = await self.async_tool_call(tool_call)
            if result_content is None:
                result_content = f"Tool {tc.name} not found"
                is_error = True
            else:
                is_error = False
        elif self.on_tool_call:
            loop = asyncio.get_event_loop()
            result_content = await loop.run_in_executor(
                None, self.on_tool_call, tool_call
            )
            if result_content is None:
                result_content = f"Tool {tc.name} not found"
                is_error = True
            else:
                is_error = False
        elif self.tool_registry and tc.name in self.tool_registry:
            result = await self.tool_registry.execute(tool_call)
            result_content = result.content
            is_error = result.is_error
        else:
            result_content = f"Unknown tool: {tc.name}"
            is_error = True

        # Log tool result
        _tool_duration = (time.time() - _tool_start) * 1000
        if isinstance(result_content, str):
            result_preview = result_content[:TOOL_RESULT_LOG_PREVIEW].replace("\n", " ")
        elif isinstance(result_content, list):
            text_parts = [
                getattr(b, "text", "")
                for b in result_content
                if hasattr(b, "text")
            ]
            preview_text = " ".join(t for t in text_parts if t)[:TOOL_RESULT_LOG_PREVIEW]
            non_text = sum(
                1 for b in result_content if not hasattr(b, "text")
            )
            result_preview = (
                preview_text.replace("\n", " ")
                + (f" [+{non_text} non-text blocks]" if non_text else "")
            )
        else:
            result_preview = ""
        if is_error:
            logger.warning(
                "TOOL ERROR [%s] %s (%.0fms) | session=%s: %s",
                tc.id, tc.name, _tool_duration, sid, result_preview,
            )
        else:
            logger.info(
                "TOOL OK [%s] %s (%.0fms) %d chars | session=%s: %s",
                tc.id, tc.name, _tool_duration,
                len(result_content) if isinstance(result_content, str) else 0,
                sid, result_preview,
            )

        # Fire the per-tool telemetry hook. Lets the bridge persist a
        # tool_call_metric JSONL row that powers the dashboard's
        # per-profile tool histograms and re-fetch detection.
        if self.on_tool_metric is not None:
            try:
                if isinstance(result_content, str):
                    result_bytes = len(result_content.encode("utf-8", errors="ignore"))
                else:
                    result_bytes = 0
                self.on_tool_metric({
                    "tool_call_id": tc.id,
                    "tool_name": tc.name,
                    "duration_ms": int(_tool_duration),
                    "ok": not is_error,
                    "result_bytes": result_bytes,
                    "arguments_preview": args_preview,
                })
            except Exception:
                logger.exception("on_tool_metric hook failed")

        # Truncate if needed
        if isinstance(result_content, str):
            if session is not None:
                dynamic_budget = self._compute_truncation_budget(session)
            else:
                dynamic_budget = self.truncator.max_tokens
            truncated_content, was_truncated = self.truncator.truncate_if_needed(
                result_content, max_tokens=dynamic_budget, tool_name=tc.name
            )
            if was_truncated:
                ctx.tool_results_truncated += 1
                session_tokens = session.estimate_tokens() if session else 0
                logger.info(
                    "Truncated tool result for %s (budget=%dk, context_window=%dk, session_tokens=%dk) | session=%s",
                    tc.name, dynamic_budget // 1000,
                    self.provider.context_window // 1000, session_tokens // 1000, sid,
                )

                await self._emit_system_event(SystemEvent(
                    type="tool_truncation",
                    message=f"Truncated {tc.name} result (budget={dynamic_budget:,}, context_window={self.provider.context_window:,})",
                    details={
                        "tool_name": tc.name,
                        "was_truncated": True,
                        "budget_tokens": dynamic_budget,
                        "context_window": self.provider.context_window,
                        "session_tokens": session_tokens,
                        "original_chars": len(result_content),
                    },
                ))

            result_content = truncated_content

        return result_content, is_error

    async def _handle_provider_error(
        self,
        error: ProviderError,
        session: Session,
        ctx: RunnerContext,
    ) -> bool:
        """Handle provider errors (same logic as sync runner)."""
        ctx.consecutive_errors += 1
        ctx.last_error = error

        reason = classify_failover_reason(str(error))
        ctx.last_failover_reason = reason

        logger.warning(
            "Provider error (attempt %d) | session=%s | reason=%s: %s",
            ctx.consecutive_errors, session.id, reason, error,
        )

        if is_context_overflow_error(str(error)) or isinstance(error, ContextOverflowError):
            return await self._handle_overflow_cascade(session, ctx)

        if not is_retryable_error(str(error)) and not error.retryable:
            logger.error("Non-retryable error | session=%s: %s", session.id, error)
            return False

        if reason in ("auth", "rate_limit", "billing") and self.auth_manager:
            if self.auth_manager.advance():
                logger.info("Rotated to new auth profile: %s | session=%s", self.auth_manager.current.id, session.id)
                if ctx.last_failover_reason:
                    self.auth_manager.mark_cooldown(
                        self.auth_manager.current.id,
                        ctx.last_failover_reason,
                    )
                return True

        if self.fallback_chain:
            if self.fallback_chain.advance():
                logger.info(
                    "Fell back to: %s/%s | session=%s",
                    self.fallback_chain.current.name,
                    self.fallback_chain.current.model_id,
                    session.id,
                )
                return True

        if ctx.consecutive_errors >= self.config.max_consecutive_errors:
            logger.error("Too many consecutive errors (%d) | session=%s", ctx.consecutive_errors, session.id)
            return False

        return True

    async def _handle_overflow_cascade(
        self,
        session: Session,
        ctx: RunnerContext,
    ) -> bool:
        """Handle context overflow with three-tier cascade.

        Order of attempts:
          1. Image-byte elision (cheap, no LLM call). When the bloat
             is image bytes — common after browser_screenshot,
             generate_image, or a user dragging in a 3-4 MiB
             attachment — text summarization can't help because the
             tiny older slice has little to summarize and the bulk of
             the bytes is in the recent-tail that the compactor
             preserves verbatim. Image elision walks oldest-first and
             replaces the heavy blocks with text placeholders.
          2. Text summarization via the compactor.
          3. Fallback chain advance.

        Emits compaction_start before the attempt and compaction_complete /
        compaction_skipped after — without these the dashboard's compaction
        count + savings trend + trigger-source breakdown all under-count
        any session that hit context overflow (Gap 1).
        """
        logger.warning(
            "Context overflow detected | session=%s | attempt %d/%d",
            session.id, ctx.compaction_attempts + 1, self.config.max_compaction_attempts,
        )

        if ctx.compaction_attempts >= self.config.max_compaction_attempts:
            logger.error("Exhausted compaction attempts | session=%s", session.id)
            return False

        # Tier 1 — image-byte elision before paying for an LLM
        # summarization round-trip. Anthropic's request-body cap is
        # tighter than the context window, so a session can hit
        # request_too_large at well-under-window token counts when a
        # single image attachment is heavy. Drop to 8 MiB so even
        # one ~4 MiB user attachment triggers elision before we
        # spend on summarization.
        try:
            image_prune = session.transcript.prune_cumulative_image_payload(
                target_bytes=4 * 1024 * 1024,
            )
        except Exception:  # noqa: BLE001
            logger.exception("image elision raised | session=%s", session.id)
            image_prune = None
        if image_prune is not None:
            elided = image_prune.images_before - image_prune.images_after
            if elided > 0:
                # Emit a compaction event so the dashboard reflects
                # this recovery in the same telemetry stream as text
                # summarization. The trigger label distinguishes it.
                await self._emit_system_event(SystemEvent(
                    type="compaction_complete",
                    message=(
                        f"Overflow-cascade image elision: pruned "
                        f"{elided} oldest image block(s) to free request "
                        "payload before retrying"
                    ),
                    details={
                        "trigger": "overflow_cascade_image_elision",
                        "images_before": image_prune.images_before,
                        "images_after": image_prune.images_after,
                        "elided": elided,
                        "target_bytes": image_prune.hard_limit,
                    },
                ))
                ctx.consecutive_errors = 0
                return True

        tokens_before = self._current_context_tokens(session)
        await self._emit_system_event(SystemEvent(
            type="compaction_start",
            message=(
                f"Overflow-cascade compaction starting "
                f"({tokens_before:,} context tokens)"
            ),
            details={
                "tokens_before": tokens_before,
                "trigger": "overflow_cascade",
                "attempt": ctx.compaction_attempts + 1,
                "max_attempts": self.config.max_compaction_attempts,
            },
        ))

        ctx.state = RunnerState.COMPACTING
        result = self._attempt_compaction(session, ctx)
        ctx.state = RunnerState.RECOVERING

        if result.success:
            try:
                tokens_after = int(session.estimate_tokens())
            except Exception:  # noqa: BLE001
                tokens_after = result.tokens_after
            self._mark_context_compacted(tokens_after)
            logger.warning(
                "Summarization complete: %s -> %s tokens (%d entries) | session=%s",
                f"{result.tokens_before:,}", f"{result.tokens_after:,}",
                result.entries_removed, session.id,
            )
            await self._emit_system_event(SystemEvent(
                type="compaction_complete",
                message=(
                    f"Overflow-cascade compaction complete: "
                    f"{tokens_before:,} → {tokens_after:,} tokens"
                ),
                details=self._compaction_event_details(
                    result,
                    trigger="overflow_cascade",
                    tokens_before=tokens_before,
                    tokens_after=tokens_after,
                ),
            ))
            return True

        # Compaction failed — surface so the dashboard / UI know.
        await self._emit_system_event(SystemEvent(
            type="compaction_skipped",
            message=f"Overflow-cascade compaction failed: {result.error or 'unknown'}",
            details={
                "trigger": "overflow_cascade",
                "reason": result.error or "unknown",
                "tokens_before": tokens_before,
                "attempt": ctx.compaction_attempts,
                "max_attempts": self.config.max_compaction_attempts,
            },
        ))

        if self.fallback_chain and self.fallback_chain.advance():
            logger.warning(
                "Summarization failed (%s), falling back to: %s/%s | session=%s",
                result.error, self.fallback_chain.current.name,
                self.fallback_chain.current.model_id, session.id,
            )
            return True

        if ctx.compaction_attempts < self.config.max_compaction_attempts:
            logger.warning("Summarization failed: %s, will retry | session=%s", result.error, session.id)
            return True

        return False

    def _attempt_compaction(
        self,
        session: Session,
        ctx: RunnerContext,
    ) -> CompactionResult:
        """Attempt context compaction with anti-thrash protection.

        Skips compaction when the last 2 attempts each saved < 10% — the
        agent is in a loop where compaction can't free meaningful space
        and the right action is to halt with a clear signal rather than
        burn API calls on summaries that don't help.
        """
        if ctx.ineffective_compaction_count >= 2:
            tokens = session.estimate_tokens()
            logger.warning(
                "Compaction skipped — last %d attempts each saved <10%% "
                "(thrash detector tripped) | session=%s",
                ctx.ineffective_compaction_count, session.id,
            )
            # Surface to the telemetry stream so the metrics dashboard
            # can count thrash-skip events.
            try:
                from bridge.compaction_telemetry import append_telemetry  # type: ignore[import-not-found]

                append_telemetry({
                    "type": "compaction_event",
                    "session_id": session.id,
                    "subtype": "thrash_skip",
                    "mechanism": "thrash_skip",
                    "trigger": "thrash_detector",
                    "tokens_before": tokens,
                    "tokens_after": tokens,
                    "ineffective_run": ctx.ineffective_compaction_count,
                })
            except Exception:  # noqa: BLE001
                pass
            return CompactionResult(
                success=False,
                summary=None,
                tokens_before=tokens,
                tokens_after=tokens,
                error=(
                    f"Compaction thrash detected ({ctx.ineffective_compaction_count} "
                    f"ineffective compactions in a row). Recommend starting a fresh "
                    f"session or branching from an earlier message."
                ),
            )

        ctx.compaction_attempts += 1
        session.compaction_count += 1

        try:
            # Thread the runner's on_llm_call hook into the compactor so
            # the summarizer's input/output tokens land in the dashboard
            # tagged as compaction overhead (Gap 4). Without this the
            # summarizer call goes straight to provider.complete() and
            # bypasses every measurement we have — its cost would be
            # invisible.
            def _on_sum_call(payload: dict[str, Any]) -> None:
                if self.on_llm_call is None:
                    return
                try:
                    self.on_llm_call({
                        "provider": payload.get("provider", "unknown"),
                        "model": payload.get("model", "unknown"),
                        "duration_ms": payload.get("duration_ms", 0),
                        "streaming": False,
                        "input_tokens": payload.get("input_tokens", 0),
                        "output_tokens": payload.get("output_tokens", 0),
                        "cache_read_tokens": payload.get("cache_read_tokens", 0),
                        "cache_write_tokens": payload.get("cache_write_tokens", 0),
                        "reasoning_tokens": 0,
                        "stop_reason": "end_turn",
                        "tool_calls": 0,
                        "thinking_blocks": 0,
                        "error": None,
                        "call_kind": payload.get("call_kind", "summarizer"),
                        "iterative": payload.get("iterative", False),
                    })
                except Exception:  # noqa: BLE001
                    logger.exception("forwarding summarizer call to on_llm_call failed")

            _ground_truth = None
            _gct = getattr(self, "get_compaction_ground_truth", None)
            if _gct is not None:
                try:
                    _ground_truth = _gct()
                except Exception:  # noqa: BLE001
                    _ground_truth = None
            _wm_state = None
            _gws = getattr(self, "get_working_memory_state", None)
            if _gws is not None:
                try:
                    _wm_state = _gws()
                except Exception:  # noqa: BLE001
                    _wm_state = None
            result = self.compaction.compact(
                session.transcript,
                self.provider,
                on_summarizer_call=_on_sum_call,
                ground_truth=_ground_truth,
                on_working_memory_upserts=getattr(self, "apply_working_memory_upserts", None),
                working_memory_state=_wm_state,
            )
            # Track savings for the thrash detector. <10% savings on
            # this attempt counts toward the cooldown counter.
            if result.success and result.tokens_before > 0:
                savings_pct = (
                    (result.tokens_before - result.tokens_after)
                    / result.tokens_before * 100
                )
                ctx.last_compaction_savings_pct = savings_pct
                if savings_pct < 10:
                    ctx.ineffective_compaction_count += 1
                else:
                    ctx.ineffective_compaction_count = 0
            return result
        except Exception as e:
            logger.error(f"Compaction failed with exception: {e}")
            return CompactionResult(
                success=False,
                summary=None,
                tokens_before=session.estimate_tokens(),
                tokens_after=session.estimate_tokens(),
                error=str(e),
            )

    def _current_context_tokens(self, session: Session) -> int:
        """Best-effort estimate of how big the NEXT request will be."""
        api_based = self.usage.effective_context_tokens()
        try:
            tokenizer_based = session.estimate_tokens()
        except Exception:  # noqa: BLE001
            tokenizer_based = 0
        return max(api_based, tokenizer_based)

    def _mark_context_compacted(self, context_tokens_after: int) -> None:
        """Drop stale last-call context counters after compaction."""
        try:
            self.usage.last_input = max(0, context_tokens_after)
            self.usage.last_output = 0
            self.usage.last_cache_read = 0
            self.usage.last_cache_write = 0
            self.usage.cache_read = 0
            self.usage.cache_write = 0
        except Exception:  # noqa: BLE001
            return

    async def _ensure_media_room(self, session: Session) -> None:
        """Keep computer-use image history inside provider media limits."""
        stats = session.transcript.prune_old_tool_result_images(
            keep_recent=KEEP_RECENT_COMPUTER_IMAGES,
            hard_limit=MAX_REQUEST_IMAGES_SAFETY,
        )
        if not stats.changed:
            return
        await self._emit_system_event(SystemEvent(
            type="media_pruning",
            message=(
                f"Omitted {stats.omitted_images} older screenshot image"
                f"{'s' if stats.omitted_images != 1 else ''} from model history "
                f"({stats.images_before} -> {stats.images_after} request images; "
                f"keeping latest {stats.kept_recent})."
            ),
            details={
                **stats.to_details(),
                "trigger": "pre_request",
                "strategy": "recent_computer_screenshots",
            },
        ))

    def _compaction_event_details(
        self,
        result: CompactionResult,
        *,
        trigger: str,
        tokens_before: int | None = None,
        tokens_after: int | None = None,
        effective_window: int | None = None,
    ) -> dict[str, Any]:
        """Canonical details dict for every compaction system_event.

        Standardized across all triggers (Gap 5). The dashboard and
        renderer pull the same keys regardless of which path produced
        the event; the old per-path variations (request_tokens vs
        context_tokens vs transcript_tokens) are still computed
        downstream but are derived from these canonical fields.
        """
        # canonical pre/post sizes — transcript.estimate_tokens
        # measured before and after append_compaction.
        tx_before = result.tokens_before
        tx_after = result.tokens_after
        # Caller's perspective (e.g. ``_current_context_tokens`` for the
        # runtime path). Falls back to the transcript measurement.
        before = tokens_before if tokens_before is not None else tx_before
        after = tokens_after if tokens_after is not None else tx_after
        # Effective window for pressure-pct math. Defaults to the
        # runner's current provider window minus reserved output budget
        # so any path that doesn't pass effective_window explicitly
        # still ends up with the same denominator.
        if effective_window is None:
            try:
                provider = (
                    self.fallback_chain.current
                    if self.fallback_chain else self.provider
                )
                window = int(getattr(provider, "context_window", 0) or 0)
                reserved = int(getattr(self.config, "max_tokens_per_turn", 0) or 0)
                effective_window = max(1, window - reserved) if window > 0 else None
            except Exception:  # noqa: BLE001
                effective_window = None
        pct_before = (
            round((before / effective_window) * 100, 1)
            if effective_window and effective_window > 0 else None
        )
        pct_after = (
            round((after / effective_window) * 100, 1)
            if effective_window and effective_window > 0 else None
        )
        return {
            # Canonical token fields (Gap 5).
            "tokens_before": before,
            "tokens_after": after,
            "transcript_tokens_before": tx_before,
            "transcript_tokens_after": tx_after,
            # Back-compat aliases — the renderer's inline marker reads
            # context_tokens_*; remove once that callsite is migrated.
            "context_tokens_before": before,
            "context_tokens_after": after,
            "effective_window": effective_window,
            "pressure_pct_before": pct_before,
            "pressure_pct_after": pct_after,
            # Counts.
            "entries_removed": result.entries_removed,
            "messages_before": result.messages_before,
            "messages_after": result.messages_after,
            "images_before": result.images_before,
            "images_after": result.images_after,
            # Summary content + size.
            "summary_chars": len(result.summary or ""),
            "summary_preview": (result.summary or "")[:700],
            "summary_text": (result.summary or ""),
            "summary_tokens": getattr(result, "summary_tokens", 0),
            # Summarizer call accounting (Gap 4) — pass through so the
            # dashboard can show compaction overhead distinct from
            # main-loop spend even before the on_llm_call mirror runs.
            "summarizer_input_tokens": getattr(result, "summarizer_input_tokens", 0),
            "summarizer_output_tokens": getattr(result, "summarizer_output_tokens", 0),
            "summarizer_duration_ms": getattr(result, "summarizer_duration_ms", 0),
            "summarizer_model": getattr(result, "summarizer_model", None),
            "summarizer_cost_usd": getattr(result, "summarizer_cost_usd", None),
            "resumed_from_previous": getattr(result, "resumed_from_previous", False),
            "trigger": trigger,
            "strategy": "llm_summary",
        }

    async def _ensure_context_room(
        self, session: Session, ctx: RunnerContext
    ) -> None:
        """Pre-request safety check: compact BEFORE sending a request
        that would overflow the provider's context window."""
        await self._ensure_media_room(session)

        context_window = self.provider.context_window
        # Effective window subtracts the reserved output budget — the
        # provider rejects a request whose input + max_tokens_per_turn
        # exceeds the raw window, so the threshold needs to track the
        # space we actually have for input bytes, not the nominal total.
        effective_window = max(1, context_window - self.config.max_tokens_per_turn)
        used_tokens = self._current_context_tokens(session)
        if context_window <= 0:
            return
        usage_fraction = used_tokens / effective_window

        if usage_fraction > self.config.compaction_threshold:
            pruned = session.transcript.prune_old_tool_results(
                keep_recent=KEEP_RECENT_TOOL_RESULTS
            )
            if pruned > 0:
                logger.info(
                    "Pre-request pressure (%.1f%%): halved %d old tool results | session=%s",
                    usage_fraction * 100, pruned, session.id,
                )
                await self._emit_system_event(SystemEvent(
                    type="context_pruning",
                    message=f"Pre-request pressure ({usage_fraction:.0%}): halved {pruned} old tool results",
                    details={
                        "pruned_count": pruned,
                        "usage_fraction": usage_fraction,
                        "trigger": "pre_request",
                    },
                ))
                used_tokens = self._current_context_tokens(session)
                usage_fraction = used_tokens / context_window

        if usage_fraction > CONTEXT_COMPACTION_THRESHOLD:
            ctx.state = RunnerState.COMPACTING
            tokens_before = used_tokens
            await self._emit_system_event(SystemEvent(
                type="compaction_start",
                message="Pre-request compaction...",
                details={"tokens_before": tokens_before, "trigger": "pre_request"},
            ))
            compaction_result = self._attempt_compaction(session, ctx)
            if compaction_result.success:
                try:
                    tokens_after = int(session.estimate_tokens())
                except Exception:  # noqa: BLE001
                    tokens_after = compaction_result.tokens_after
                self._mark_context_compacted(tokens_after)
                await self._emit_system_event(SystemEvent(
                    type="compaction_complete",
                    message=(
                        f"Pre-request compaction complete: "
                        f"{tokens_before} -> {tokens_after} tokens"
                    ),
                    details=self._compaction_event_details(
                        compaction_result,
                        trigger="pre_request",
                        tokens_before=tokens_before,
                        tokens_after=tokens_after,
                    ),
                ))
            else:
                logger.warning(
                    "Pre-request compaction failed | session=%s: %s",
                    session.id, compaction_result.error,
                )
            ctx.state = RunnerState.RUNNING

    async def _handle_context_pressure_async(
        self, session: Session, ctx: RunnerContext
    ) -> None:
        """Handle context pressure using REAL token count from API response."""
        context_window = self.provider.context_window
        effective_window = max(1, context_window - self.config.max_tokens_per_turn)
        used_tokens = self.usage.effective_context_tokens()
        usage_fraction = used_tokens / effective_window

        if usage_fraction > self.config.compaction_threshold:
            pruned = session.transcript.prune_old_tool_results(keep_recent=KEEP_RECENT_TOOL_RESULTS)
            if pruned > 0:
                logger.info(
                    "Context pressure (%.1f%%): halved %d old tool results | session=%s",
                    usage_fraction * 100, pruned, session.id,
                )
                await self._emit_system_event(SystemEvent(
                    type="context_pruning",
                    message=f"Context pressure ({usage_fraction:.0%}): halved {pruned} old tool results",
                    details={
                        "pruned_count": pruned,
                        "usage_fraction": usage_fraction,
                    },
                ))

        if usage_fraction > CONTEXT_COMPACTION_THRESHOLD:
            ctx.state = RunnerState.COMPACTING
            tokens_before = used_tokens
            await self._emit_system_event(SystemEvent(
                type="compaction_start",
                message="Compacting conversation history...",
                details={"tokens_before": tokens_before},
            ))
            compaction_result = self._attempt_compaction(session, ctx)
            if compaction_result.success:
                try:
                    tokens_after = int(session.estimate_tokens())
                except Exception:  # noqa: BLE001
                    tokens_after = compaction_result.tokens_after
                self._mark_context_compacted(tokens_after)
                await self._emit_system_event(SystemEvent(
                    type="compaction_complete",
                    message=f"Compaction complete: {tokens_before} -> {tokens_after} tokens",
                    details=self._compaction_event_details(
                        compaction_result,
                        trigger="context_pressure",
                        tokens_before=tokens_before,
                        tokens_after=tokens_after,
                    ),
                ))
            else:
                logger.warning("Compaction failed under context pressure | session=%s", session.id)
            ctx.state = RunnerState.RUNNING


# ============================================================================
# Convenience Functions
# ============================================================================

async def run_agent_async(
    provider: Any,
    system_prompt: str,
    user_message: str,
    *,
    config: AgentConfig | None = None,
    tools: ToolRegistry | None = None,
    on_stream: StreamCallback | None = None,
    thinking: ThinkingConfig | None = None,
) -> AgentResult:
    """Convenience function to run a single async agent turn with streaming."""
    session = Session.create(
        system_prompt=system_prompt,
        tools=list(tools._tools.values()) if tools else None,
    )
    runner = AsyncAgentRunner(
        provider=provider,
        config=config,
        tool_registry=tools,
        on_stream=on_stream,
        thinking=thinking,
    )
    return await runner.run(session, user_message)


def run_agent(
    provider: ModelProvider,
    system_prompt: str,
    user_message: str,
    *,
    config: AgentConfig | None = None,
    tools: ToolRegistry | None = None,
) -> AgentResult:
    """Convenience function to run a single agent turn."""
    session = Session.create(
        system_prompt=system_prompt,
        tools=list(tools._tools.values()) if tools else None,
    )
    runner = AgentRunner(
        provider=provider,
        config=config,
        tool_registry=tools,
    )
    return runner.run(session, user_message)
