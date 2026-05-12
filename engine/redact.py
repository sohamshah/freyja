"""Secret redaction for compaction inputs and outputs.

Sweep common credential / token shapes and replace with `[REDACTED]`.
Used twice per compaction: once on the conversation fed to the
summarizer, once on the summary the summarizer returns (LLMs sometimes
echo back credentials verbatim even when prompted not to).

Pattern set intentionally narrow — we want low false-positive rate
because over-redaction makes summaries useless. Add patterns as we
encounter leaks in practice.
"""

from __future__ import annotations

import re

# Each pattern is (compiled regex, replacement). Order matters: more
# specific patterns first so e.g. an Anthropic key isn't caught by the
# generic bearer-token catch-all.
_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Anthropic API keys
    (re.compile(r"sk-ant-[a-zA-Z0-9_-]{90,}"), "[REDACTED:anthropic_key]"),
    # OpenAI API keys (sk-... or sk-proj-...)
    (re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{40,}"), "[REDACTED:openai_key]"),
    # AWS access keys
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED:aws_access_key]"),
    # AWS secret keys (40 chars base64ish, hard to distinguish without context)
    # We only catch them when prefixed by clear keyword.
    (re.compile(r'(?i)(aws_secret_access_key\s*[:=]\s*["\']?)([A-Za-z0-9/+=]{40})'),
     r"\1[REDACTED:aws_secret]"),
    # GitHub tokens
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{36,251}"), "[REDACTED:github_token]"),
    # Slack tokens
    (re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"), "[REDACTED:slack_token]"),
    # Generic bearer-shaped (40+ chars of high entropy after "Bearer "/"token=")
    (re.compile(r'(?i)(bearer\s+)([A-Za-z0-9_.\-=]{40,})'), r"\1[REDACTED:bearer]"),
    # Private key blocks
    (re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    ), "[REDACTED:private_key]"),
]


def redact_sensitive_text(text: str) -> str:
    """Apply every redaction pattern. Returns the input unchanged if no
    pattern matches, so it's safe to call indiscriminately."""
    if not text:
        return text
    out = text
    for pattern, replacement in _PATTERNS:
        out = pattern.sub(replacement, out)
    return out
