"""
Token counting utilities using tiktoken.

Uses cl100k_base encoding which is close enough to Claude's tokenization
for practical purposes. This is far more accurate than character-based
heuristics (which can be off by 2-3x).

Typical ratios:
- English text: ~4 tokens per word, ~1 token per 3-4 characters
- Code: ~1.3-1.5 tokens per word
- JSON: varies widely based on structure

The old `len(text) / 4` heuristic assumed 4 chars = 1 token, which
significantly undercounts tokens (actual is often 3 chars or less per token).
"""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import TYPE_CHECKING

from engine.constants import (
    CHARS_PER_TOKEN_DEFAULT,
    CHARS_PER_TOKEN_ESTIMATE,
    MESSAGE_TOKEN_OVERHEAD,
)

if TYPE_CHECKING:
    import tiktoken

logger = logging.getLogger(__name__)

# Default encoding - cl100k_base is used by GPT-4/ChatGPT and is
# reasonably close to Claude's tokenization for estimation purposes
DEFAULT_ENCODING = "cl100k_base"


@lru_cache(maxsize=1)
def get_tokenizer(encoding: str = DEFAULT_ENCODING) -> "tiktoken.Encoding":
    """
    Get a cached tiktoken encoder.

    Args:
        encoding: The encoding name (default: cl100k_base)

    Returns:
        tiktoken Encoding instance
    """
    import tiktoken

    return tiktoken.get_encoding(encoding)


def count_tokens(text: str, encoding: str = DEFAULT_ENCODING) -> int:
    """
    Count tokens in text using tiktoken.

    Args:
        text: The text to tokenize
        encoding: The encoding to use (default: cl100k_base)

    Returns:
        Number of tokens
    """
    if not text:
        return 0

    try:
        enc = get_tokenizer(encoding)
        return len(enc.encode(text))
    except Exception as e:
        # Fallback to conservative character estimate if tiktoken fails
        logger.warning(f"tiktoken encoding failed, using fallback: {e}")
        return len(text) // CHARS_PER_TOKEN_ESTIMATE


def tokens_to_chars(tokens: int, chars_per_token: float = CHARS_PER_TOKEN_DEFAULT) -> int:
    """
    Estimate character count from token count.

    For truncation purposes, we need to convert a token budget to
    approximate characters. Uses 3.5 chars/token as a middle ground.

    Args:
        tokens: Number of tokens
        chars_per_token: Average characters per token (default: 3.5)

    Returns:
        Estimated character count
    """
    return int(tokens * chars_per_token)


def estimate_tokens_for_messages(messages: list[dict]) -> int:
    """
    Estimate token count for a list of messages.

    This approximates the token overhead for message structure
    (role markers, etc.) in addition to content.

    Args:
        messages: List of message dicts with 'role' and 'content'

    Returns:
        Estimated total tokens
    """
    total = 0
    # Overhead tokens per message for structure (role, separators, etc.)
    _MESSAGE_OVERHEAD = MESSAGE_TOKEN_OVERHEAD

    for msg in messages:
        total += _MESSAGE_OVERHEAD
        content = msg.get("content", "")
        if isinstance(content, str):
            total += count_tokens(content)
        elif isinstance(content, list):
            # Handle content blocks
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text", "")
                    if text:
                        total += count_tokens(text)
                elif hasattr(block, "text"):
                    total += count_tokens(block.text)

    return total
