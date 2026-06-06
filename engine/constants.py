"""
Centralised constants for the engine.

Every tuneable threshold, limit, timeout, budget, and magic number lives here
so they can be found, reviewed, and adjusted in one place.

Stripped of EMA-specific, server, sandbox, Redis, gRPC, and other
platform-specific constants. Retains only core engine constants for
context pressure, compaction, loop detection, and token estimation.
"""

# ============================================================================
# Context Window & Token Budgets
# ============================================================================

DEFAULT_CONTEXT_WINDOW = 200_000
"""Fallback context window when the model isn't recognised."""

MAX_TOOL_RESULT_TOKENS = 60_000
"""Hard cap on a single tool result before truncation."""

DEFAULT_MAX_TOKENS = 64_000
"""Default max output tokens per LLM call.

Claude Opus 4.7 supports up to 128k output (adaptive thinking, no extended thinking).
Claude Sonnet 4.6 / Haiku 4.5 support up to 64k output with extended thinking.
Claude 4.5 models support up to 16k (bumped when thinking is on).
"""

DEFAULT_THINKING_BUDGET_TOKENS = 10_000
"""Default thinking budget when extended thinking is enabled."""

SUBAGENT_MAX_TOKENS = 16_000
"""Max output tokens for sub-agent LLM calls."""

SUMMARY_MAX_TOKENS = 8_000
"""Max tokens for the LLM-generated compaction summary."""

# ============================================================================
# Context Window -- Per-model Overrides
# ============================================================================

# NOTE: when you add a model here, 13 other codepoints need matching
# updates. See docs/ADDING-A-MODEL.md for the full checklist — the
# missing-from-here case silently falls back to DEFAULT_CONTEXT_WINDOW
# (200k) and Opus 4.8 sessions compacted 5× early before this dict
# learned about it.
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # Claude 4.8
    "claude-opus-4-8": 1_000_000,
    "claude-opus-4-8-fast": 1_000_000,
    # Claude 4.7
    "claude-opus-4-7": 1_000_000,
    # Claude 4.6
    "claude-opus-4-6": 1_000_000,
    "claude-sonnet-4-6": 1_000_000,
    # Claude 4.5 / 4
    "claude-opus-4": 200_000,
    "claude-sonnet-4": 200_000,
    "claude-haiku-4-5": 200_000,
    "claude-sonnet-4-5": 1_000_000,
    "claude-opus-4-5": 200_000,
    # OpenAI
    "gpt-5.5": 1_050_000,
    "gpt-5.3-codex": 400_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
    # Fireworks
    "deepseek-v4-pro": 1_048_576,
    "glm-5.1": 202_752,
    "kimi-k2.6": 262_144,
    "minimax-m2.7": 196_608,
    "qwen3.6-plus": 1_000_000,
    "kimi-k2.5": 262_144,
    "glm5": 202_752,
    "minimax-m2.5": 196_608,
    # Google Gemini — keep in sync with engine/providers.py MODEL_REGISTRY.
    "gemini-3.1-pro-preview": 1_048_576,
    "gemini-3.5-flash": 1_048_576,
    "gemini-3.1-flash": 1_048_576,
    "gemini-3.1-flash-lite": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
    "gemini-2.5-flash": 1_048_576,
}

# ============================================================================
# Context Pressure & Compaction
# ============================================================================

CONTEXT_PRESSURE_THRESHOLD = 0.25
"""Fraction of EFFECTIVE context window (window minus reserved output) that
triggers cheap pruning — tool-result halving, image trim, dedup. Silent
to the agent. Lowered from 0.80 to 0.25 as part of the cooperative
early-trigger compaction architecture (see docs/COMPACTION-DECISION-DRAFT.md).
"""

CONTEXT_COMPACTION_THRESHOLD = 0.70
"""Fraction of EFFECTIVE context window that triggers *runtime-driven*
LLM compaction. This is the cooperative protocol's safety net — not its
primary mechanism.

History: this constant was lowered to 0.40 in the original Phase-1
ship, but that put the runtime on top of the soft-band pressure note
(Channel 2 also activates at 0.40). The runtime preempted the agent
every time → Channels 2 and 3 were inert. Restored to 0.80 for Phase 2
so the agent had a real 40–80% cooperation window: pressure notes at
40%, strong-band advisory at 60%, and runtime fallback at 80%.

Dropped to 0.70 for Phase 2.5 (May 2026) after Slack-session observation
showed the agent almost never self-calls summarize_context — strong-band
notes get ignored and long DMs cruise from ~60% up past 80% without
compacting. 0.70 narrows the cooperation window enough that the runtime
takes over before the model has time to drift into another long tool
chain in a near-full context. See docs/COMPACTION-DECISION-DRAFT.md."""

CONTEXT_AWARENESS_THRESHOLD = 0.25
"""Fraction at which we start appending per-observation token-usage
tags so the agent has continuous pressure visibility. Same as
CONTEXT_PRESSURE_THRESHOLD by design — pruning and awareness fire
together at the same band."""

CONTEXT_SOFT_SUGGEST_THRESHOLD = 0.40
"""Fraction at which the observation tag escalates from informational
to 'consider summarize_context() at next break'."""

CONTEXT_STRONG_SUGGEST_THRESHOLD = 0.55
"""Fraction at which we recommend compaction before the agent issues
more tool calls. Lowered from 0.60 alongside the 0.80 → 0.70 forced-band
shift so the strong cue stays meaningfully ahead of the runtime
fallback (was 20 pts of daylight; now 15 pts)."""

CONTEXT_FALLBACK_THRESHOLD = 0.70
"""Fraction at which the runtime stops trusting the agent to drive
compaction and forces it. Logged as 'fallback fired' for the training
corpus — every fallback is a label for 'the agent missed the cue'.
Kept in sync with CONTEXT_COMPACTION_THRESHOLD."""

KEEP_RECENT_TOOL_RESULTS = 3
"""Number of recent tool results preserved during pruning."""

KEEP_RECENT_COMPUTER_IMAGES = 4
"""Recent computer-use screenshots kept as image blocks in model history."""

MAX_REQUEST_IMAGES_SAFETY = 80
"""Soft request-level image ceiling; leaves provider headroom for attachments."""

KEEP_RECENT_MESSAGES = 10
"""Messages kept verbatim during compaction (most recent N)."""

MIN_MESSAGES_TO_COMPACT = 20
"""Don't *auto-trigger* compaction if fewer than this many messages — but
the manual compact path bypasses this in favor of a content-size check so
that short-but-heavy conversations (e.g. a single huge image) are still
recoverable. See ``compaction.MIN_TOKENS_TO_SUMMARIZE``."""

MIN_TOKENS_TO_SUMMARIZE = 2_000
"""Minimum estimated tokens of *summarizable* content (messages NOT in
the kept recent tail) before ``Compactor.compact()`` will run. Replaces
the old strict ``len(messages) < MIN_MESSAGES_TO_COMPACT`` floor so heavy
short sessions (one giant image, a few replies) can still be compacted."""

MAX_COMPACTION_ATTEMPTS = 3
"""How many times compaction can be retried in a single turn."""

MAX_CHARS_TO_SUMMARIZE = 400_000
"""Maximum characters of transcript fed to the compaction LLM."""

# ============================================================================
# Content Truncation Ratios
# ============================================================================

TRUNCATION_HEAD_RATIO = 0.70
"""When truncating long content, keep this fraction from the start."""

TRUNCATION_TAIL_RATIO = 0.25
"""When truncating long content, keep this fraction from the end."""

MIN_CONTENT_LENGTH_FOR_TRUNCATION = 500
"""Don't truncate content shorter than this (characters)."""

# ============================================================================
# Compaction Budget Allocation
# ============================================================================

COMPACTION_START_BUDGET_RATIO = 0.40
"""Fraction of compaction budget allocated to start of conversation."""

COMPACTION_END_BUDGET_RATIO = 0.40
"""Fraction of compaction budget allocated to end of conversation."""

COMPACTION_BUFFER_RATIO = 0.20
"""Fraction of compaction budget reserved as buffer."""

COMPACTION_CONTENT_TRUNCATION_THRESHOLD = 2_000
"""Content blocks larger than this get head/tail truncated during compaction."""

COMPACTION_CONTENT_HEAD = 1_200
"""Characters kept from the head of large content blocks."""

COMPACTION_CONTENT_TAIL = 600
"""Characters kept from the tail of large content blocks."""

COMPACTION_RECENT_MESSAGES_FOR_FOOTER = 3
"""Number of recent messages included in compaction summary footer."""

# ============================================================================
# Agent Loop -- Iterations
# ============================================================================

BASE_ITERATIONS = 24
"""Base number of iterations in the dynamic max_iterations formula."""

ITERATIONS_PER_AUTH_PROFILE = 8
"""Extra iterations added per auth profile."""

MIN_ITERATIONS = 100
"""Floor for max_iterations regardless of auth profiles."""

MAX_ITERATIONS = 160
"""Ceiling for max_iterations regardless of auth profiles."""

MAX_CONSECUTIVE_ERRORS = 3
"""Consecutive provider errors before the agent gives up."""

# ============================================================================
# Agent Loop -- Loop Detection
# ============================================================================

CONSECUTIVE_IDENTICAL_CALL_THRESHOLD = 2
"""Same tool+args called this many times in a row triggers a loop-break."""

VERIFICATION_CONSECUTIVE_THRESHOLD = 6
"""Same tool (name-only) called this many times triggers end-turn verification."""

VERIFICATION_ITERATION_THRESHOLD = 10
"""Minimum total iterations before end-turn verification can fire."""

STEERING_TAG_OPEN = "<agent-steering>"
STEERING_TAG_CLOSE = "</agent-steering>"
"""Wrap injected steering messages so the API layer can identify and redact them."""

LOOP_DETECTION_EXEMPT_TOOLS: frozenset[str] = frozenset({
    # Computer-use / UI manipulation tools -- repeating the same action
    # with the same args is a NORMAL pattern for direct screen control.
    "screenshot",
    "click",
    "move_mouse",
    "type_text",
    "press_key",
    "key_down",
    "key_up",
    "scroll",
    "focus_window",
    "list_windows",
    "list_displays",
    "read_ax_tree",
    "find_element",
    "cursor_position",
    "wait",
    "inspect_region",
    "computer_use",
})
"""Tools exempt from consecutive-call loop detection.

Computer-control / UI manipulation tools where repetition is the normal
idiom -- the screen state changes between calls even when arguments are
identical, so the 'you already called this' injection is a false positive
that breaks legitimate multi-step UI workflows.

When a batch contains only exempt tools, loop-tracking counters are left
untouched so the model can call freely without tripping either the
loop-break or verification injections."""

# ============================================================================
# Timeouts (seconds)
# ============================================================================

ANTHROPIC_API_TIMEOUT = 300.0
"""Timeout for Anthropic API calls."""

DEFAULT_BASH_TIMEOUT = 120.0
"""Default timeout for bash command execution."""

# ============================================================================
# Session Management
# ============================================================================

MAX_SESSION_EVENTS = 10_000
"""Max events kept per session before trimming."""

# ============================================================================
# Display & Formatting
# ============================================================================

PARALLEL_TOOL_LIMIT = 5
"""Default parallel tool execution limit in system prompt."""

# ============================================================================
# Tool Result Display
# ============================================================================

TOOL_ARGS_LOG_PREVIEW = 200
"""Truncation for tool args in log messages."""

TOOL_RESULT_LOG_PREVIEW = 200
"""Truncation for tool results in log messages."""

ERROR_LOG_TRUNCATION = 500
"""Error text truncation for metrics/logging."""

# ============================================================================
# Token Estimation
# ============================================================================

CHARS_PER_TOKEN_ESTIMATE = 3
"""Conservative chars-per-token for fallback estimation."""

CHARS_PER_TOKEN_DEFAULT = 3.5
"""Default chars-per-token for token-to-char conversion."""

MESSAGE_TOKEN_OVERHEAD = 4
"""Overhead tokens per message (structural markers)."""

# ============================================================================
# Errors & Retry
# ============================================================================

TRANSIENT_HTTP_CODES = frozenset({500, 502, 503, 504, 521, 522, 523, 524, 529})
"""HTTP status codes treated as transient/retryable."""

ERROR_MESSAGE_TRUNCATION = 600
"""Max error message length in error responses."""

# ============================================================================
# Primary Probe (Fallback Recovery)
# ============================================================================

PRIMARY_PROBE_MAX_TOKENS = 1
"""Max tokens for the lightweight primary-provider probe request."""
