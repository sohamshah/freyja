"""
Error classification for the engine.

Mirrors OpenClaw's error classification from pi-embedded-helpers/errors.ts.
Pattern-based message parsing to classify errors into FailoverReason categories.
"""

from __future__ import annotations

import re
from typing import Pattern

from engine.constants import ERROR_MESSAGE_TRUNCATION, TRANSIENT_HTTP_CODES
from engine.types import FailoverReason

# ============================================================================
# Error Patterns
# ============================================================================

# From OpenClaw errors.ts:613-676
_RATE_LIMIT_PATTERNS: list[Pattern[str] | str] = [
    re.compile(r"rate[_ ]limit|too many requests|429", re.IGNORECASE),
    "exceeded your current quota",
    "resource has been exhausted",
    "quota exceeded",
    "resource_exhausted",
    "usage limit",
    "tpm",  # tokens per minute
    "tokens per minute",
]

_OVERLOADED_PATTERNS: list[Pattern[str] | str] = [
    re.compile(r'overloaded_error|"type"\s*:\s*"overloaded_error"', re.IGNORECASE),
    "overloaded",
    "service unavailable",
    "high demand",
]

_TIMEOUT_PATTERNS: list[Pattern[str] | str] = [
    "timeout",
    "timed out",
    "deadline exceeded",
    "context deadline exceeded",
    re.compile(r"without sending (?:any )?chunks?", re.IGNORECASE),
    re.compile(r"\bstop reason:\s*abort\b", re.IGNORECASE),
    re.compile(r"\breason:\s*abort\b", re.IGNORECASE),
    re.compile(r"\bunhandled stop reason:\s*abort\b", re.IGNORECASE),
]

_BILLING_PATTERNS: list[Pattern[str] | str] = [
    re.compile(
        r'["\'"]?(?:status|code)["\']?\s*[:=]\s*402\b|'
        r"\bhttp\s*402\b|"
        r"\berror(?:\s+code)?\s*[:=]?\s*402\b|"
        r"\b(?:got|returned|received)\s+(?:a\s+)?402\b|"
        r"^\s*402\s+payment",
        re.IGNORECASE,
    ),
    "payment required",
    "insufficient credits",
    "credit balance",
    "plans & billing",
    "insufficient balance",
]

_AUTH_PATTERNS: list[Pattern[str] | str] = [
    re.compile(r"invalid[_ ]?api[_ ]?key", re.IGNORECASE),
    "incorrect api key",
    "invalid token",
    "authentication",
    "re-authenticate",
    "oauth token refresh failed",
    "unauthorized",
    "forbidden",
    "access denied",
    "insufficient permissions",
    "insufficient permission",
    re.compile(r"missing scopes?:", re.IGNORECASE),
    "expired",
    "token has expired",
    re.compile(r"\b401\b"),
    re.compile(r"\b403\b"),
    "no credentials found",
    "no api key found",
]

_FORMAT_PATTERNS: list[Pattern[str] | str] = [
    "string should match pattern",
    "tool_use.id",
    "tool_use_id",
    "messages.1.content.1.tool_use.id",
    "invalid request format",
    re.compile(r"tool call id was.*must be", re.IGNORECASE),
]

_MODEL_NOT_FOUND_PATTERNS: list[Pattern[str] | str] = [
    "unknown model",
    "model not found",
    "model_not_found",
    "not_found_error",
    re.compile(r"does not exist.*model", re.IGNORECASE),
    re.compile(r"models/[^\s]+ is not found", re.IGNORECASE),
]

# Context overflow patterns from errors.ts:50-92
_CONTEXT_OVERFLOW_PATTERNS: list[str] = [
    "request_too_large",
    "request size exceeds",
    "request exceeds the maximum size",
    "too much media",
    "context length exceeded",
    "maximum context length",
    "prompt is too long",
    "exceeds model context window",
    "model token limit",
    "context overflow:",
    "exceed context limit",
    "exceeds the model's maximum context",
]

# Anthropic body-level "api_error" type marker (their 500-equivalent).
# Matches both the compact-JSON form ("type":"api_error") and the Python
# dict-repr form the SDK produces for mid-stream SSE error events
# ('type': 'api_error' — single quotes, colon-space).
_API_INTERNAL_ERROR_PATTERN = re.compile(
    r"['\"]type['\"]\s*:\s*['\"]api_error['\"]", re.IGNORECASE
)

# Transient HTTP error codes (5xx that should be treated as timeout)
_TRANSIENT_HTTP_CODES = TRANSIENT_HTTP_CODES


# ============================================================================
# Pattern Matching
# ============================================================================

def _matches_patterns(message: str, patterns: list[Pattern[str] | str]) -> bool:
    """Check if message matches any pattern in the list."""
    if not message:
        return False
    lower = message.lower()
    for pattern in patterns:
        if isinstance(pattern, re.Pattern):
            if pattern.search(message):
                return True
        elif pattern in lower:
            return True
    return False


def _extract_http_status(message: str) -> int | None:
    """Extract leading HTTP status code from message."""
    match = re.match(r"^(?:http\s*)?(\d{3})(?:\s|$)", message.strip(), re.IGNORECASE)
    if match:
        code = int(match.group(1))
        if 100 <= code < 600:
            return code
    return None


# ============================================================================
# Classification Functions
# ============================================================================

def is_rate_limit_error(message: str) -> bool:
    """Check if message indicates a rate limit error."""
    return _matches_patterns(message, _RATE_LIMIT_PATTERNS)


def is_overloaded_error(message: str) -> bool:
    """Check if message indicates an overloaded service error."""
    return _matches_patterns(message, _OVERLOADED_PATTERNS)


def is_timeout_error(message: str) -> bool:
    """Check if message indicates a timeout error."""
    return _matches_patterns(message, _TIMEOUT_PATTERNS)


def is_billing_error(message: str) -> bool:
    """Check if message indicates a billing error."""
    if _matches_patterns(message, _BILLING_PATTERNS):
        return True
    # Check for billing-related head patterns
    lower = message.lower()
    if re.match(r"^(?:error[:\s-]+)?billing", message, re.IGNORECASE):
        return any(
            term in lower for term in ("upgrade", "credits", "payment", "plan")
        )
    return False


def is_auth_error(message: str) -> bool:
    """Check if message indicates an authentication error."""
    return _matches_patterns(message, _AUTH_PATTERNS)


def is_format_error(message: str) -> bool:
    """Check if message indicates a request format error."""
    # Exclude image dimension errors from format errors
    if "image dimensions exceed" in message.lower():
        return False
    return _matches_patterns(message, _FORMAT_PATTERNS)


def is_model_not_found_error(message: str) -> bool:
    """Check if message indicates a model not found error."""
    if _matches_patterns(message, _MODEL_NOT_FOUND_PATTERNS):
        return True
    # Check for 404 combined with not-found text
    lower = message.lower()
    if re.search(r"\b404\b", message) and "not" in lower and "found" in lower:
        return True
    return False


def is_transient_http_error(message: str) -> bool:
    """Check if message indicates a transient HTTP error (5xx)."""
    status = _extract_http_status(message)
    return status is not None and status in _TRANSIENT_HTTP_CODES


def is_api_internal_error(message: str) -> bool:
    """Check if message carries an Anthropic body-level api_error.

    "api_error" is Anthropic's 500-equivalent ("an unexpected error
    internal to Anthropic's systems") and is transient. Mid-stream SSE
    error events surface it with the stream's HTTP 200 status, so the
    body type marker is the only reliable signal.
    """
    if not message:
        return False
    return bool(_API_INTERNAL_ERROR_PATTERN.search(message))


def is_context_overflow_error(message: str) -> bool:
    """
    Check if message indicates a context overflow error.

    From OpenClaw errors.ts:50-92. Note that TPM (tokens per minute)
    errors are rate limits, not context overflow.
    """
    if not message:
        return False
    lower = message.lower()

    # Exclude rate limit errors that mention tokens
    if "tpm" in lower or "tokens per minute" in lower:
        return False

    # Check for reasoning constraint errors (not overflow)
    if any(
        term in lower
        for term in ("reasoning is mandatory", "reasoning is required", "requires reasoning")
    ):
        return False

    # Check overflow patterns
    return any(pattern in lower for pattern in _CONTEXT_OVERFLOW_PATTERNS)


def is_likely_context_overflow_error(message: str) -> bool:
    """
    Check if message is likely a context overflow error (broader matching).

    Includes heuristic patterns beyond exact matches.
    """
    if is_context_overflow_error(message):
        return True

    if not message:
        return False

    # Exclude rate limits first
    if is_rate_limit_error(message):
        return False

    # Broader context overflow hints
    pattern = re.compile(
        r"context.*overflow|"
        r"context window.*(too (?:large|long)|exceed|over|limit|max)|"
        r"prompt.*(too (?:large|long)|exceed|over|limit|max)|"
        r"(?:request|input).*(?:context|window|length|token).*(too (?:large|long)|exceed|over|limit|max)",
        re.IGNORECASE,
    )
    return bool(pattern.search(message))


# ============================================================================
# Main Classification Function
# ============================================================================

def classify_failover_reason(message: str) -> FailoverReason | None:
    """
    Classify error message into a FailoverReason.

    Mirrors OpenClaw's classifyFailoverReason from errors.ts:851-887.
    Classification order matters: check specific patterns first.

    Returns None if the error doesn't match any known failover pattern.
    """
    if not message:
        return None

    # Check in priority order (matches OpenClaw's cascade)

    # Image errors don't trigger failover
    if "image dimensions exceed" in message.lower():
        return None
    if "image exceeds" in message.lower() and "mb" in message.lower():
        return None

    # Model not found
    if is_model_not_found_error(message):
        return "model_not_found"

    # Transient HTTP errors treated as timeout
    if is_transient_http_error(message):
        return "timeout"

    # Anthropic body-level api_error (internal server error) treated as
    # timeout. Quote-agnostic: matches both compact JSON and the dict
    # repr the SDK produces for mid-stream SSE error events.
    if is_api_internal_error(message):
        return "timeout"

    # Rate limit (includes overloaded)
    if is_rate_limit_error(message):
        return "rate_limit"
    if is_overloaded_error(message):
        return "rate_limit"  # Overloaded maps to rate_limit

    # Format errors
    if is_format_error(message):
        return "format"

    # Billing
    if is_billing_error(message):
        return "billing"

    # Timeout
    if is_timeout_error(message):
        return "timeout"

    # Auth
    if is_auth_error(message):
        return "auth"

    return None


def is_failover_error(message: str) -> bool:
    """Check if message indicates any failover-triggering error."""
    return classify_failover_reason(message) is not None


def is_retryable_error(message: str) -> bool:
    """
    Check if an error is retryable.

    Retryable errors include:
    - Rate limits
    - Timeouts
    - Context overflow (can be recovered via compaction)
    - Overloaded service
    - Transient HTTP errors (5xx)
    - Anthropic body-level api_error (server-side internal error)

    Non-retryable errors include:
    - Authentication failures
    - Invalid request format
    - Model not found
    - Billing errors (need user action)
    """
    if not message:
        return False

    # These are retryable
    if is_rate_limit_error(message):
        return True
    if is_timeout_error(message):
        return True
    if is_context_overflow_error(message):
        return True
    if is_overloaded_error(message):
        return True
    if is_transient_http_error(message):
        return True
    if is_api_internal_error(message):
        return True

    # These are not retryable
    if is_auth_error(message):
        return False
    if is_format_error(message):
        return False
    if is_model_not_found_error(message):
        return False
    if is_billing_error(message):
        return False

    # Default: not retryable
    return False


# ============================================================================
# User-Friendly Error Messages
# ============================================================================

def format_billing_error_message(provider: str | None = None, model: str | None = None) -> str:
    """Format a user-friendly billing error message."""
    if provider and model:
        label = f"{provider} ({model})"
    elif provider:
        label = provider
    else:
        label = "API provider"

    return (
        f"Billing error from {label} - your API key has run out of credits "
        "or has an insufficient balance. Check your billing dashboard and "
        "top up or switch to a different API key."
    )


def format_error_for_user(
    message: str,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> str:
    """
    Format an error message for user display.

    Converts technical API errors into user-friendly messages.
    """
    if not message:
        return "LLM request failed with an unknown error."

    if is_context_overflow_error(message):
        return (
            "Context overflow: prompt too large for the model. "
            "Try starting a fresh session or use a larger-context model."
        )

    if is_rate_limit_error(message):
        return "API rate limit reached. Please try again later."

    if is_overloaded_error(message):
        return "The AI service is temporarily overloaded. Please try again in a moment."

    if is_timeout_error(message):
        return "LLM request timed out."

    if is_billing_error(message):
        return format_billing_error_message(provider, model)

    if is_auth_error(message):
        return "Authentication failed. Please check your API key configuration."

    if is_model_not_found_error(message):
        return f"Model not found. Please check the model name is correct."

    # Truncate long messages
    if len(message) > ERROR_MESSAGE_TRUNCATION:
        return message[:ERROR_MESSAGE_TRUNCATION] + "..."

    return message
