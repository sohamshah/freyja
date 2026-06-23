"""
Model provider abstraction and authentication management.

Provides:
- ModelProvider protocol for LLM provider implementations
- AuthProfile and AuthProfileManager for credential rotation
- ModelFallbackChain for multi-model failover

Key behaviors from OpenClaw:
1. Profile lock: user-specified profiles are never rotated away from
2. Cooldown skip: failing profiles are temporarily excluded
3. Timeout exception: timeouts don't trigger cooldown (run.ts:990)
"""

from __future__ import annotations

import logging
import time
from abc import abstractmethod
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING, Literal, Protocol, runtime_checkable

from engine.types import (
    APIUsage,
    FailoverReason,
    Message,
    ThinkingBlock,
    ThinkingConfig,
    RedactedThinkingBlock,
)

if TYPE_CHECKING:
    from engine.tools import ToolDefinition

logger = logging.getLogger(__name__)


# ============================================================================
# Provider Response
# ============================================================================

@dataclass
class ProviderResponse:
    """Response from a model provider completion call."""

    content: str
    """The text content of the response."""

    tool_calls: list["ToolCallResponse"] | None = None
    """Tool calls requested by the model, if any."""

    usage: APIUsage = field(default_factory=APIUsage)
    """Token usage for this call."""

    stop_reason: str | None = None
    """Why the model stopped generating (e.g., 'end_turn', 'tool_use')."""

    stop_details: dict[str, Any] | None = None
    """Provider-native structured stop details when present.

    Anthropic Opus 4.7+ returns a ``stop_details`` object on refusal
    responses describing the category of refusal (e.g. ``safety``,
    ``policy``, ``capability``). Surfaced here so the bridge / UI can
    route differently than a bare ``stop_reason="refusal"``.
    """

    model: str | None = None
    """The model that generated this response."""

    thinking_blocks: list[ThinkingBlock | RedactedThinkingBlock] | None = None
    """Extended thinking blocks from the response, if any."""


@dataclass
class ToolCallResponse:
    """A tool call from the model."""

    id: str
    name: str
    arguments: dict
    provider_kind: str | None = None
    """Provider-native protocol marker, e.g. ``openai.computer_call``."""

    provider_data: dict[str, Any] = field(default_factory=dict)
    """Raw provider metadata needed to round-trip native tool calls."""


@dataclass
class StructuredResponse:
    """
    Response from a structured-output completion call.

    Returned by ModelProvider.complete_structured(). The `data` field holds
    the parsed JSON conforming to the requested schema. On parse failure or
    when the model didn't produce structured output, `data` is an empty dict
    and `raw_text` holds whatever the model actually returned for debugging.
    """

    data: dict
    """Parsed structured output. Empty dict if generation/parsing failed."""

    usage: APIUsage = field(default_factory=APIUsage)
    """Token usage for this call."""

    stop_reason: str | None = None
    """Why the model stopped generating."""

    model: str | None = None
    """The model that generated this response."""

    raw_text: str | None = None
    """Raw text output when parsing failed. None on success."""

    @property
    def success(self) -> bool:
        """True if structured data was returned."""
        return bool(self.data)


# ============================================================================
# Provider Errors
# ============================================================================

class ProviderError(Exception):
    """Base exception for provider errors."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable


class AuthenticationError(ProviderError):
    """Authentication or authorization failure."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, status=401, **kwargs)


class RateLimitError(ProviderError):
    """Rate limit or quota exceeded."""

    def __init__(self, message: str, retry_after: float | None = None, **kwargs):
        super().__init__(message, status=429, retryable=True, **kwargs)
        self.retry_after = retry_after


class BillingError(ProviderError):
    """Billing or payment required."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, status=402, **kwargs)


class ContextOverflowError(ProviderError):
    """Context window exceeded."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, status=400, retryable=True, **kwargs)


class ImagePayloadTooLargeError(ProviderError):
    """A single image block exceeds the provider's per-image payload limit.

    Distinct from ``ContextOverflowError`` — this is *not* about message
    count or aggregate token usage. Recovery is to prune the oversized
    image specifically; running summarization will not help because the
    offending image is typically pinned in the recent-tail of the
    transcript that summarization keeps verbatim.
    """

    def __init__(self, message: str, max_bytes: int | None = None, **kwargs):
        super().__init__(message, status=400, retryable=True, **kwargs)
        self.max_bytes = max_bytes


class ModelNotFoundError(ProviderError):
    """Model does not exist."""

    def __init__(self, message: str, **kwargs):
        super().__init__(message, status=404, **kwargs)


# ============================================================================
# Model Provider Protocol
# ============================================================================

@runtime_checkable
class ModelProvider(Protocol):
    """Abstract interface for LLM providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name (e.g., 'anthropic', 'openai')."""
        ...

    @property
    @abstractmethod
    def model_id(self) -> str:
        """Model identifier (e.g., 'claude-sonnet-4-6')."""
        ...

    @property
    @abstractmethod
    def context_window(self) -> int:
        """Maximum context window size in tokens."""
        ...

    @abstractmethod
    def complete(
        self,
        messages: list[Message],
        *,
        tools: list["ToolDefinition"] | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        """
        Send a completion request to the model.

        Args:
            messages: Conversation history
            tools: Available tool definitions
            system_prompt: System prompt to prepend
            max_tokens: Maximum tokens to generate

        Returns:
            ProviderResponse with content and usage

        Raises:
            ProviderError: On API errors
        """
        ...

    async def complete_structured(
        self,
        messages: list[Message],
        *,
        schema: dict,
        schema_name: str = "structured_output",
        schema_description: str | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        strict: bool = True,
        thinking: Any = None,
    ) -> StructuredResponse:
        """Generate a structured JSON response matching the given schema."""
        ...


# ============================================================================
# Authentication Profile
# ============================================================================

@dataclass
class AuthProfile:
    """An authentication profile for a model provider."""

    id: str
    api_key: str
    source: Literal["config", "user"] = "config"
    cooldown_until: float | None = None
    last_used: float | None = None
    failure_count: int = 0

    def is_in_cooldown(self, now: float | None = None) -> bool:
        """Check if this profile is currently in cooldown."""
        if self.cooldown_until is None:
            return False
        return (now or time.time()) < self.cooldown_until

    def mark_cooldown(self, duration_seconds: float) -> None:
        """Put this profile into cooldown."""
        self.cooldown_until = time.time() + duration_seconds
        self.failure_count += 1

    def clear_cooldown(self) -> None:
        """Clear cooldown and reset failure count."""
        self.cooldown_until = None
        self.failure_count = 0

    def mark_used(self) -> None:
        """Mark this profile as successfully used."""
        self.last_used = time.time()
        self.clear_cooldown()


# ============================================================================
# Auth Profile Manager
# ============================================================================

class AuthProfileManager:
    """Manages rotation through authentication profiles."""

    def __init__(
        self,
        profiles: list[AuthProfile],
        *,
        locked_profile_id: str | None = None,
        default_cooldown_seconds: float = 60.0,
    ):
        if not profiles:
            raise ValueError("At least one authentication profile is required")

        self._profiles = list(profiles)
        self._current_index = 0
        self._locked_profile_id = locked_profile_id
        self._default_cooldown = default_cooldown_seconds

    @property
    def current(self) -> AuthProfile:
        """Get the current active profile."""
        return self._profiles[self._current_index]

    @property
    def profiles(self) -> list[AuthProfile]:
        """Get all profiles (read-only view)."""
        return list(self._profiles)

    @property
    def is_locked(self) -> bool:
        """Check if profile rotation is locked."""
        return self._locked_profile_id is not None

    def advance(self) -> bool:
        """Rotate to next non-cooldown profile."""
        if self._locked_profile_id:
            logger.debug(f"Profile locked to {self._locked_profile_id}, not rotating")
            return False

        now = time.time()
        start_index = self._current_index

        for _ in range(len(self._profiles)):
            next_index = (self._current_index + 1) % len(self._profiles)

            if next_index == start_index:
                logger.warning("All auth profiles exhausted")
                return False

            candidate = self._profiles[next_index]
            self._current_index = next_index

            if candidate.is_in_cooldown(now):
                logger.debug(f"Skipping profile {candidate.id} (in cooldown)")
                continue

            logger.info(f"Rotated to auth profile {candidate.id}")
            return True

        return False

    def mark_cooldown(
        self,
        profile_id: str,
        reason: FailoverReason,
        duration_seconds: float | None = None,
    ) -> None:
        """Mark a profile as in cooldown."""
        if reason == "timeout":
            logger.debug(f"Not marking cooldown for {profile_id} (timeout)")
            return

        duration = duration_seconds or self._default_cooldown

        for profile in self._profiles:
            if profile.id == profile_id:
                profile.mark_cooldown(duration)
                logger.info(
                    f"Profile {profile_id} in cooldown for {duration}s "
                    f"(reason: {reason}, failures: {profile.failure_count})"
                )
                break

    def mark_success(self, profile_id: str) -> None:
        """Mark a profile as successfully used."""
        for profile in self._profiles:
            if profile.id == profile_id:
                profile.mark_used()
                logger.debug(f"Profile {profile_id} marked successful")
                break

    def get_profile(self, profile_id: str) -> AuthProfile | None:
        """Get a profile by ID."""
        for profile in self._profiles:
            if profile.id == profile_id:
                return profile
        return None

    def available_count(self) -> int:
        """Count profiles not in cooldown."""
        now = time.time()
        return sum(1 for p in self._profiles if not p.is_in_cooldown(now))


# ============================================================================
# Model Fallback Chain
# ============================================================================

class ModelFallbackChain:
    """Manages fallback between model providers."""

    def __init__(
        self,
        providers: list[ModelProvider],
        *,
        default_cooldown_seconds: float = 60.0,
        probe_interval_seconds: float = 30.0,
    ):
        if not providers:
            raise ValueError("At least one provider is required")

        self._providers = list(providers)
        self._current_index = 0
        self._default_cooldown = default_cooldown_seconds
        self._probe_interval = probe_interval_seconds
        self._cooldowns: dict[str, float] = {}
        self._last_probe: dict[str, float] = {}

    @property
    def current(self) -> ModelProvider:
        """Get the current active provider."""
        return self._providers[self._current_index]

    @property
    def primary(self) -> ModelProvider:
        """Get the primary (first) provider."""
        return self._providers[0]

    @property
    def is_on_fallback(self) -> bool:
        """Check if currently using a fallback provider."""
        return self._current_index > 0

    def advance(self) -> bool:
        """Move to next provider in the chain."""
        now = time.time()

        for _ in range(len(self._providers) - 1):
            next_index = self._current_index + 1
            if next_index >= len(self._providers):
                logger.warning("All fallback providers exhausted")
                return False

            provider = self._providers[next_index]
            cooldown_until = self._cooldowns.get(provider.name, 0)

            if now < cooldown_until:
                logger.debug(f"Skipping provider {provider.name} (in cooldown)")
                self._current_index = next_index
                continue

            self._current_index = next_index
            logger.info(f"Fell back to provider {provider.name}/{provider.model_id}")
            return True

        return False

    def mark_cooldown(
        self,
        provider_name: str,
        duration_seconds: float | None = None,
    ) -> None:
        """Put a provider into cooldown."""
        duration = duration_seconds or self._default_cooldown
        self._cooldowns[provider_name] = time.time() + duration
        logger.info(f"Provider {provider_name} in cooldown for {duration}s")

    def should_probe_primary(self) -> bool:
        """Check if we should probe the primary provider."""
        if not self.is_on_fallback:
            return False

        primary_name = self.primary.name
        last_probe = self._last_probe.get(primary_name, 0)
        now = time.time()

        if now - last_probe >= self._probe_interval:
            self._last_probe[primary_name] = now
            return True

        return False

    def reset_to_primary(self) -> None:
        """Reset to primary provider (after successful probe)."""
        if self._current_index != 0:
            logger.info(f"Resetting to primary provider {self.primary.name}")
            self._current_index = 0
            self._cooldowns.pop(self.primary.name, None)

    def clear_cooldowns(self) -> None:
        """Clear all cooldowns."""
        self._cooldowns.clear()
        self._current_index = 0


# ============================================================================
# Model Registry & Provider Factory
# ============================================================================

# NOTE: see docs/ADDING-A-MODEL.md — 14 codepoints to keep in sync when
# adding a model. This registry is the engine-side authority for
# provider routing + context window + thinking flag.
MODEL_REGISTRY: dict[str, dict[str, object]] = {
    # Anthropic models
    "claude-fable-5": {"provider": "anthropic", "context_window": 1_000_000, "thinking": True},
    "claude-opus-4-8": {"provider": "anthropic", "context_window": 1_000_000, "thinking": True},
    "claude-opus-4-8-fast": {"provider": "anthropic", "context_window": 1_000_000, "thinking": True},
    "claude-opus-4-7": {"provider": "anthropic", "context_window": 1_000_000, "thinking": True},
    "claude-sonnet-4-6": {"provider": "anthropic", "context_window": 1_000_000, "thinking": True},
    "claude-opus-4-6": {"provider": "anthropic", "context_window": 1_000_000, "thinking": True},
    "claude-haiku-4-5": {"provider": "anthropic", "context_window": 200_000, "thinking": True},
    "claude-sonnet-4-5": {"provider": "anthropic", "context_window": 1_000_000, "thinking": True},
    "claude-opus-4-5": {"provider": "anthropic", "context_window": 200_000, "thinking": True},
    # OpenAI models (Responses API)
    "gpt-5.5": {"provider": "openai", "context_window": 1_050_000, "thinking": True},
    "gpt-5.4": {"provider": "openai", "context_window": 1_050_000, "thinking": True},
    "gpt-5.4-pro": {"provider": "openai", "context_window": 1_050_000, "thinking": True},
    "gpt-5.4-mini": {"provider": "openai", "context_window": 400_000, "thinking": True},
    "gpt-5.4-nano": {"provider": "openai", "context_window": 400_000, "thinking": True},
    "gpt-5.3-codex": {"provider": "openai", "context_window": 400_000, "thinking": True},
    # Cerebras models
    "zai-glm-4.7": {"provider": "cerebras", "context_window": 131_072, "thinking": False, "reasoning_mode": "disabled"},
    # Fireworks models
    "deepseek-v4-pro": {
        "provider": "fireworks",
        "context_window": 1_048_576,
        "thinking": True,
        "reasoning_mode": "effort",
        "reasoning_levels": ("none", "low", "medium", "high", "max"),
        "reasoning_default": "high",
    },
    "glm-5.1": {
        "provider": "fireworks",
        "context_window": 202_752,
        "thinking": True,
        "reasoning_mode": "effort",
        "reasoning_levels": ("none", "low", "medium", "high"),
        "reasoning_default": "high",
    },
    "glm-5.2": {
        "provider": "fireworks",
        "context_window": 1_048_576,
        "thinking": True,
        "reasoning_mode": "effort",
        "reasoning_levels": ("none", "low", "medium", "high", "max"),
        "reasoning_default": "high",
    },
    "kimi-k2.6": {
        "provider": "fireworks",
        "context_window": 262_144,
        "thinking": True,
        "reasoning_mode": "effort",
        "reasoning_levels": ("none", "low", "medium", "high"),
        "reasoning_default": "high",
    },
    "kimi-k2.7-code": {
        "provider": "fireworks",
        "context_window": 262_144,
        "thinking": True,
        "reasoning_mode": "effort",
        "reasoning_levels": ("none", "low", "medium", "high"),
        "reasoning_default": "high",
    },
    "minimax-m2.7": {
        "provider": "fireworks",
        "context_window": 196_608,
        "thinking": True,
        "reasoning_mode": "required",
        "reasoning_levels": ("low", "medium", "high"),
        "reasoning_default": "medium",
    },
    "minimax-m3": {
        "provider": "fireworks",
        "context_window": 524_288,
        "thinking": True,
        "reasoning_mode": "required",
        "reasoning_levels": ("low", "medium", "high"),
        "reasoning_default": "medium",
    },
    "qwen3.6-plus": {
        "provider": "fireworks",
        "context_window": 1_000_000,
        "thinking": True,
        "reasoning_mode": "effort",
        "reasoning_levels": ("none", "low", "medium", "high"),
        "reasoning_default": "medium",
    },
    "qwen3.7-plus": {
        "provider": "fireworks",
        "context_window": 262_144,
        "thinking": True,
        "reasoning_mode": "effort",
        "reasoning_levels": ("none", "low", "medium", "high", "max"),
        "reasoning_default": "medium",
    },
    "kimi-k2.5": {"provider": "fireworks", "context_window": 262_144, "thinking": False, "reasoning_mode": "none"},
    # Google Gemini (GEMINI_API_KEY)
    "gemini-3.1-pro-preview": {
        "provider": "google",
        "context_window": 1_048_576,
        "thinking": True,
        "reasoning_mode": "effort",
        "reasoning_levels": ("minimal", "low", "medium", "high"),
        "reasoning_default": "high",
    },
    "gemini-3.5-flash": {
        "provider": "google",
        "context_window": 1_048_576,
        "thinking": True,
        "reasoning_mode": "effort",
        "reasoning_levels": ("minimal", "low", "medium", "high"),
        "reasoning_default": "medium",
    },
    "gemini-3.1-flash": {
        "provider": "google",
        "context_window": 1_048_576,
        "thinking": True,
        "reasoning_mode": "effort",
        "reasoning_levels": ("minimal", "low", "medium", "high"),
        "reasoning_default": "medium",
    },
    "gemini-3.1-flash-lite": {
        "provider": "google",
        "context_window": 1_048_576,
        "thinking": True,
        "reasoning_mode": "effort",
        "reasoning_levels": ("minimal", "low", "medium", "high"),
        "reasoning_default": "low",
    },
    "gemini-2.5-pro": {
        "provider": "google",
        "context_window": 1_048_576,
        "thinking": True,
        "reasoning_mode": "effort",
        "reasoning_levels": ("minimal", "low", "medium", "high"),
        "reasoning_default": "high",
    },
    "gemini-2.5-flash": {
        "provider": "google",
        "context_window": 1_048_576,
        "thinking": True,
        "reasoning_mode": "effort",
        "reasoning_levels": ("minimal", "low", "medium", "high"),
        "reasoning_default": "medium",
    },
}

MODEL_SPEED_TIERS = {
    "fast": "claude-haiku-4-5",
    "medium": "claude-sonnet-4-6",
    "slow": "claude-opus-4-7",   # latest Opus; 4-6 stays in registry as fallback target
    "openai": "gpt-5.5",         # OpenAI flagship
    "codex": "gpt-5.3-codex",    # agentic coding specialist
    "cerebras": "zai-glm-4.7",
    "kimi": "kimi-k2.6",
    "glm5": "glm-5.1",
    "deepseek": "deepseek-v4-pro",
    "minimax": "minimax-m2.7",
}

ALL_MODEL_CHOICES = list(MODEL_REGISTRY.keys())


# Approximate USD per 1M tokens. Tuple = (input, output, cache_read, cache_write).
# `cache_write` defaults to 1.25× input when omitted (Anthropic-style markup).
# Numbers are best-effort; missing entries make compute_cost() return None
# rather than mislead the diagnostics panel with a wrong figure.
# See docs/ADDING-A-MODEL.md for the full per-model checklist.
MODEL_PRICING_PER_M: dict[str, tuple[float, float, float] | tuple[float, float, float, float]] = {
    # Anthropic — 4.6/4.7/4.8 Opus are $5/$25 per Anthropic's models overview
    # (the migration guide for 4.7 says "at the same $5/$25 per MTok pricing"
    # as 4.6; 4.5 keeps the legacy $15/$75 tier). Cache_read = 10% of input.
    # Fable 5 is a premium $10/$50 tier (cache_read $1, cache_write $12.50).
    "claude-fable-5": (10.0, 50.0, 1.0, 12.5),
    "claude-opus-4-8": (5.0, 25.0, 0.50),
    "claude-opus-4-8-fast": (10.0, 50.0, 1.0),  # fast-mode multiplier on 4.8
    "claude-opus-4-7": (5.0, 25.0, 0.50),
    "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-opus-4-5": (15.0, 75.0, 1.50),
    "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-sonnet-4-5": (3.0, 15.0, 0.30),
    "claude-haiku-4-5": (0.80, 4.0, 0.08),
    # OpenAI Responses API
    "gpt-5.5": (5.0, 15.0, 0.50),
    "gpt-5.4": (3.0, 12.0, 0.30),
    "gpt-5.4-pro": (5.0, 20.0, 0.50),
    "gpt-5.4-mini": (0.30, 1.20, 0.03),
    "gpt-5.4-nano": (0.05, 0.40, 0.005),
    "gpt-5.3-codex": (2.0, 8.0, 0.20),
    # Cerebras
    "zai-glm-4.7": (0.50, 0.85, 0.0),
    # Fireworks
    "deepseek-v4-pro": (0.55, 1.65, 0.0),
    "glm-5.1": (0.55, 2.20, 0.0),
    "glm-5.2": (1.40, 4.40, 0.26),
    "kimi-k2.6": (0.55, 1.20, 0.0),
    "kimi-k2.7-code": (0.95, 4.00, 0.19),
    "kimi-k2.5": (0.55, 1.20, 0.0),
    "minimax-m2.7": (0.30, 1.20, 0.0),
    "minimax-m3": (0.30, 1.20, 0.06),
    "qwen3.6-plus": (0.40, 1.40, 0.0),
    "qwen3.7-plus": (0.40, 1.60, 0.08),
    # Google Gemini (USD per 1M tokens — input/output/cache_read).
    # Cache write defaults to 1.25× input.
    "gemini-3.1-pro-preview": (1.25, 10.0, 0.31),
    "gemini-3.5-flash": (0.30, 2.50, 0.075),
    "gemini-3.1-flash": (0.30, 2.50, 0.075),
    "gemini-3.1-flash-lite": (0.10, 0.40, 0.025),
    "gemini-2.5-pro": (1.25, 10.0, 0.31),
    "gemini-2.5-flash": (0.30, 2.50, 0.075),
}


def compute_cost(
    model: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float | None:
    """Estimate USD cost for one API call. Returns None if model is unpriced."""
    rates = MODEL_PRICING_PER_M.get(model)
    if rates is None:
        return None
    in_rate, out_rate, cache_read_rate = rates[0], rates[1], rates[2]
    cache_write_rate = rates[3] if len(rates) >= 4 else in_rate * 1.25
    return (
        input_tokens * in_rate
        + output_tokens * out_rate
        + cache_read_tokens * cache_read_rate
        + cache_write_tokens * cache_write_rate
    ) / 1_000_000

# See docs/ADDING-A-MODEL.md — fallback chain for graceful degradation
# when a primary model 503s or hits its rate limit.
FALLBACK_CHAINS: dict[str, list[str]] = {
    "claude-fable-5": ["claude-opus-4-8", "claude-opus-4-7", "kimi-k2.6"],
    "claude-opus-4-8": ["claude-opus-4-7", "kimi-k2.6", "deepseek-v4-pro"],
    "claude-opus-4-8-fast": ["claude-opus-4-8", "claude-opus-4-7"],
    "claude-opus-4-7": ["claude-opus-4-8", "claude-opus-4-6", "kimi-k2.6", "deepseek-v4-pro"],
    "claude-sonnet-4-6": ["kimi-k2.6", "deepseek-v4-pro"],
    "claude-opus-4-6": ["kimi-k2.6", "deepseek-v4-pro"],
    "claude-haiku-4-5": ["kimi-k2.6", "kimi-k2.5"],
    "zai-glm-4.7": ["kimi-k2.6", "kimi-k2.5"],
    "gpt-5.5": ["gpt-5.4", "gpt-5.4-mini"],
    "gpt-5.4": ["gpt-5.4-mini"],
    "gpt-5.4-pro": ["gpt-5.4", "gpt-5.4-mini"],
    "gpt-5.4-mini": ["gpt-5.4-nano"],
    "gpt-5.4-nano": ["gpt-5.4-mini"],
    "gpt-5.3-codex": ["gpt-5.4-mini"],
    "deepseek-v4-pro": ["glm-5.1", "kimi-k2.6"],
    "glm-5.1": ["glm-5.2", "deepseek-v4-pro", "kimi-k2.6"],
    "glm-5.2": ["glm-5.1", "deepseek-v4-pro", "kimi-k2.6"],
    "kimi-k2.6": ["deepseek-v4-pro", "glm-5.1", "kimi-k2.5"],
    "kimi-k2.7-code": ["kimi-k2.6", "deepseek-v4-pro"],
    "minimax-m2.7": ["kimi-k2.6", "glm-5.1"],
    "minimax-m3": ["minimax-m2.7", "kimi-k2.6"],
    "qwen3.6-plus": ["kimi-k2.6", "glm-5.1"],
    "qwen3.7-plus": ["qwen3.6-plus", "kimi-k2.6", "glm-5.1"],
    "kimi-k2.5": ["kimi-k2.6", "minimax-m2.7"],
    "gemini-3.1-pro-preview": ["gemini-3.5-flash", "gemini-3.1-flash"],
    "gemini-3.5-flash": ["gemini-3.1-flash", "gemini-3.1-flash-lite"],
    "gemini-3.1-flash": ["gemini-3.5-flash", "gemini-3.1-flash-lite"],
    "gemini-3.1-flash-lite": ["gemini-3.1-flash", "gemini-3.5-flash"],
    "gemini-2.5-pro": ["gemini-3.1-pro-preview", "gemini-2.5-flash"],
    "gemini-2.5-flash": ["gemini-3.5-flash", "gemini-3.1-flash"],
}


def get_context_window(model: str) -> int:
    """Get context window size for a model."""
    entry = MODEL_REGISTRY.get(model)
    if entry:
        return int(entry["context_window"])  # type: ignore[arg-type]
    return 200_000


def get_provider_name(model: str) -> str:
    """Infer provider from model ID."""
    entry = MODEL_REGISTRY.get(model)
    if entry:
        return str(entry["provider"])
    if model.startswith("gpt-"):
        return "openai"
    if model.startswith("zai-") or model.startswith("cerebras-"):
        return "cerebras"
    if model.startswith("accounts/fireworks/"):
        return "fireworks"
    if model.startswith("gemini-") or model.startswith("models/gemini-"):
        return "google"
    return "anthropic"


def supports_thinking(model: str) -> bool:
    """Check if a model supports extended thinking."""
    entry = MODEL_REGISTRY.get(model)
    if entry:
        return bool(entry["thinking"])
    return False


def _create_single_provider(
    model: str,
    *,
    max_tokens: int = 50_000,
    thinking_config: ThinkingConfig | None = None,
) -> "ModelProvider":
    """Create a single provider instance for a model."""
    provider_name = get_provider_name(model)
    thinking = thinking_config or ThinkingConfig(enabled=False)

    if provider_name == "openai":
        from engine.openai_provider import OpenAIConfig, OpenAIProvider

        return OpenAIProvider(OpenAIConfig(
            model=model,
            max_tokens=max_tokens,
            reasoning=thinking,
        ))
    elif provider_name == "cerebras":
        from engine.cerebras_provider import CerebrasConfig, CerebrasProvider

        return CerebrasProvider(CerebrasConfig(
            model=model,
            max_tokens=max_tokens,
        ))
    elif provider_name == "fireworks":
        from engine.fireworks_provider import FireworksConfig, FireworksProvider

        return FireworksProvider(FireworksConfig(
            model=model,
            max_tokens=max_tokens,
            reasoning=thinking,
        ))
    elif provider_name == "google":
        from engine.google_provider import GoogleConfig, GoogleProvider

        ctx_window = MODEL_REGISTRY.get(model, {}).get("context_window", 1_048_576)
        return GoogleProvider(GoogleConfig(
            model=model,
            max_tokens=max_tokens,
            context_window=int(ctx_window),
        ))
    else:
        from engine.anthropic_provider import AnthropicConfig, AnthropicProvider

        return AnthropicProvider(AnthropicConfig(
            model=model,
            max_tokens=max_tokens,
            thinking=thinking,
        ))


def create_provider(
    model: str,
    *,
    max_tokens: int = 50_000,
    thinking_config: ThinkingConfig | None = None,
) -> "ModelProvider":
    """Create the appropriate provider for a given model."""
    thinking = thinking_config or ThinkingConfig(enabled=False)

    if thinking.enabled and not supports_thinking(model):
        logger.warning("%s doesn't support thinking -- disabling.", model)
        thinking = ThinkingConfig(enabled=False)

    return _create_single_provider(model, max_tokens=max_tokens, thinking_config=thinking)


def create_provider_with_fallback(
    model: str,
    *,
    max_tokens: int = 50_000,
    thinking_config: ThinkingConfig | None = None,
) -> tuple["ModelProvider", ModelFallbackChain | None]:
    """Create a provider and its fallback chain (if configured)."""
    primary = create_provider(model, max_tokens=max_tokens, thinking_config=thinking_config)

    fallback_models = FALLBACK_CHAINS.get(model, [])
    if not fallback_models:
        return primary, None

    providers: list[ModelProvider] = [primary]
    for fb_model in fallback_models:
        try:
            fb_provider = _create_single_provider(fb_model, max_tokens=max_tokens)
            providers.append(fb_provider)
            logger.info("Fallback provider ready: %s (%s)", fb_model, fb_provider.name)
        except (ProviderError, Exception) as e:
            logger.debug("Skipping fallback %s: %s", fb_model, e)

    if len(providers) < 2:
        return primary, None

    chain = ModelFallbackChain(providers)
    logger.info(
        "Fallback chain for %s: %s",
        model,
        " -> ".join(f"{p.name}/{p.model_id}" for p in providers),
    )
    return primary, chain
