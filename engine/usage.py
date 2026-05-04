"""
Usage accumulator for tracking token consumption.

Implements the "last value" pattern from OpenClaw to prevent token inflation
across tool-use loops. See run.ts:131-179.

Key insight: Cache reads and inputs are replaced (not summed) for context
size calculation because each tool-call round-trip reports cacheRead
approximately equal to current_context_size. Summing N calls gives
N * context_size which incorrectly inflates usage.

Reference: https://github.com/openclaw/openclaw/issues/13698
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.types import APIUsage, UsageStats


@dataclass
class UsageAccumulator:
    """
    Tracks token usage with both cumulative and 'last' values.

    Mirrors OpenClaw's UsageAccumulator type from run.ts:100-110:

        type UsageAccumulator = {
            input: number;           // Cumulative input tokens
            output: number;          // Cumulative output tokens
            cacheRead: number;       // Cumulative cache read tokens
            cacheWrite: number;      // Cumulative cache write tokens
            total: number;           // Cumulative total
            lastCacheRead: number;   // Last API call's cache read
            lastCacheWrite: number;  // Last API call's cache write
            lastInput: number;       // Last API call's input tokens
        }

    The "last" fields are critical for accurate context-size reporting.
    Without them, accumulated cache totals inflate context size when there
    are multiple tool-call round-trips.
    """

    # Cumulative values (sum across all API calls)
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    total: int = 0

    # Last-call values (for accurate context size)
    last_input: int = 0
    last_cache_read: int = 0
    last_cache_write: int = 0

    def update(self, usage: APIUsage) -> None:
        """
        Update accumulator with new API response usage.

        From OpenClaw run.ts:131-151 (mergeUsageIntoAccumulator):
        - All fields accumulate for billing/logging purposes
        - "last" fields track the most recent API call's values

        Key insight: cache reads and inputs use "last" semantics for
        context size calculation because each tool-call round-trip
        reports cacheRead approximately equal to current_context_size.
        """
        # Skip if no meaningful usage values
        if not self._has_values(usage):
            return

        # Accumulate all values
        input_tokens = usage.input_tokens or 0
        output_tokens = usage.output_tokens or 0
        cache_read = usage.cache_read_tokens or 0
        cache_write = usage.cache_write_tokens or 0
        reasoning_tokens = usage.reasoning_tokens or 0

        self.input += input_tokens
        self.output += output_tokens
        self.cache_read += cache_read
        self.cache_write += cache_write
        self.reasoning += reasoning_tokens
        self.total += input_tokens + output_tokens + cache_read + cache_write

        # Track the most recent API call's values for context-size reporting
        self.last_input = input_tokens
        self.last_cache_read = cache_read
        self.last_cache_write = cache_write

    def _has_values(self, usage: APIUsage) -> bool:
        """Check if usage has any meaningful token counts."""
        values = [
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_read_tokens,
            usage.cache_write_tokens,
        ]
        return any(
            v is not None and isinstance(v, (int, float)) and v > 0
            for v in values
        )

    def effective_context_tokens(self) -> int:
        """
        Calculate current context window utilization using 'last' semantics.

        From OpenClaw's toNormalizedUsage (run.ts:153-179):
            lastPromptTokens = lastInput + lastCacheRead + lastCacheWrite
            total = lastPromptTokens + output

        Uses the LAST API call's cache fields for context-size calculation.
        The accumulated cacheRead/cacheWrite inflate context size because
        each tool-call round-trip reports cacheRead approximately equal to
        current_context_size, and summing N calls gives N * context_size.
        """
        last_prompt_tokens = self.last_input + self.last_cache_read + self.last_cache_write
        return last_prompt_tokens + self.output

    def to_stats(self) -> UsageStats:
        """
        Export final usage statistics.

        Uses "last" semantics for input/cache fields (accurate context size)
        but accumulated output (total generated text this turn).
        """
        last_prompt_tokens = self.last_input + self.last_cache_read + self.last_cache_write
        return UsageStats(
            input_tokens=self.last_input,
            output_tokens=self.output,
            cache_read_tokens=self.last_cache_read,
            cache_write_tokens=self.last_cache_write,
            reasoning_tokens=self.reasoning,
            total_tokens=last_prompt_tokens + self.output,
        )

    def reset(self) -> None:
        """Reset all counters to zero."""
        self.input = 0
        self.output = 0
        self.cache_read = 0
        self.cache_write = 0
        self.reasoning = 0
        self.total = 0
        self.last_input = 0
        self.last_cache_read = 0
        self.last_cache_write = 0

    def __repr__(self) -> str:
        return (
            f"UsageAccumulator("
            f"input={self.input}, output={self.output}, "
            f"cache_read={self.cache_read}, cache_write={self.cache_write}, "
            f"reasoning={self.reasoning}, total={self.total}, "
            f"last_input={self.last_input}, last_cache_read={self.last_cache_read}, "
            f"last_cache_write={self.last_cache_write})"
        )
