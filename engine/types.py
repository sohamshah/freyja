"""
Core data structures for the engine.

Types mirror OpenClaw's TypeScript definitions from:
- pi-embedded-helpers/types.ts (FailoverReason)
- pi-embedded-runner/run.ts (UsageAccumulator shape)
- failover-error.ts (FailoverError)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# ============================================================================
# Failover Reason
# ============================================================================

FailoverReason = Literal[
    "auth",           # Authentication/authorization failure (401, 403)
    "format",         # Request format error (400)
    "rate_limit",     # Rate limit or overloaded (429)
    "billing",        # Billing/payment issue (402)
    "timeout",        # Request timeout or transient 5xx
    "model_not_found",  # Model doesn't exist (404)
    "unknown",        # Unclassified error
]

# HTTP status code mapping for failover reasons
FAILOVER_STATUS_MAP: dict[FailoverReason, int] = {
    "auth": 401,
    "format": 400,
    "rate_limit": 429,
    "billing": 402,
    "timeout": 408,
    "model_not_found": 404,
    "unknown": 500,
}


# ============================================================================
# Content Blocks
# ============================================================================

@dataclass
class TextBlock:
    """Text content block."""
    type: Literal["text"] = "text"
    text: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"type": "text", "text": self.text}


@dataclass
class ImageBlock:
    """Image content block for the Anthropic API.

    Supports two source types:
    - base64: Raw base64-encoded image data
    - url: URL to fetch the image from
    """
    type: Literal["image"] = "image"
    source_type: Literal["base64", "url"] = "base64"
    """Source type: 'base64' for inline data, 'url' for remote images."""

    data: str = ""
    """Base64-encoded image data (when source_type='base64')."""

    url: str = ""
    """Image URL (when source_type='url')."""

    media_type: str = "image/png"
    """MIME type: image/png, image/jpeg, image/gif, or image/webp."""

    @classmethod
    def from_base64(cls, data: str, media_type: str = "image/png") -> "ImageBlock":
        """Create an image block from base64 data."""
        return cls(
            source_type="base64",
            data=data,
            media_type=media_type,
        )

    @classmethod
    def from_url(cls, url: str) -> "ImageBlock":
        """Create an image block from a URL."""
        return cls(
            source_type="url",
            url=url,
        )

    def to_api_format(self) -> dict:
        """Convert to Anthropic API format."""
        if self.source_type == "url":
            return {
                "type": "image",
                "source": {
                    "type": "url",
                    "url": self.url,
                },
            }
        else:
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": self.media_type,
                    "data": self.data,
                },
            }

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "type": "image",
            "source_type": self.source_type,
            "media_type": self.media_type,
        }
        if self.source_type == "url":
            d["url"] = self.url
        else:
            d["data"] = self.data
        return d


# Inline-image media types the major model vision APIs (Anthropic / OpenAI /
# Gemini) accept. Anything else — notably ``image/svg+xml`` from generate_svg —
# makes those APIs return a 400 ("Input should be 'image/jpeg', 'image/png',
# 'image/gif' or 'image/webp'"), and because the bad block lives in the
# transcript it poisons EVERY subsequent request, not just the one that
# produced it. Providers swap such blocks for a text placeholder before
# sending; the bridge keeps them out of the transcript in the first place.
SUPPORTED_IMAGE_MEDIA_TYPES: frozenset[str] = frozenset({
    "image/jpeg",
    "image/png",
    "image/gif",
    "image/webp",
})


def image_media_type_supported(media_type: str | None) -> bool:
    """True if ``media_type`` is an inline image type a model can actually view."""
    return (media_type or "").strip().lower() in SUPPORTED_IMAGE_MEDIA_TYPES


def unsupported_image_placeholder_text(media_type: str | None) -> str:
    """Text stand-in for an image a model can't view, so an unsupported block
    (e.g. an SVG) degrades to a note instead of a hard provider 400."""
    mt = (media_type or "").strip() or "unknown"
    return (
        f"[image omitted — {mt} is not a format this model can view inline "
        f"(supported: jpeg, png, gif, webp); it was saved and is available "
        f"to the operator]"
    )


@dataclass
class DocumentBlock:
    """PDF document content block for the Anthropic API.

    Supports two source types:
    - base64: Raw base64-encoded PDF data
    - url: URL to fetch the PDF from
    """
    type: Literal["document"] = "document"
    source_type: Literal["base64", "url"] = "base64"
    """Source type: 'base64' for inline data, 'url' for remote PDFs."""

    data: str = ""
    """Base64-encoded PDF data (when source_type='base64')."""

    url: str = ""
    """PDF URL (when source_type='url')."""

    media_type: str = "application/pdf"
    """MIME type: always application/pdf."""

    filename: str = "document.pdf"
    """Filename for the document (used by OpenAI Responses API)."""

    cache_control: dict[str, str] | None = None
    """Optional cache control, e.g. {"type": "ephemeral"}."""

    @classmethod
    def from_base64(
        cls,
        data: str,
        cache_control: dict[str, str] | None = None,
        filename: str = "document.pdf",
    ) -> "DocumentBlock":
        """Create a document block from base64-encoded PDF data."""
        return cls(source_type="base64", data=data, cache_control=cache_control, filename=filename)

    @classmethod
    def from_url(cls, url: str, cache_control: dict[str, str] | None = None) -> "DocumentBlock":
        """Create a document block from a URL."""
        return cls(source_type="url", url=url, cache_control=cache_control)

    def to_api_format(self) -> dict:
        """Convert to Anthropic API format."""
        if self.source_type == "url":
            block: dict = {
                "type": "document",
                "source": {
                    "type": "url",
                    "url": self.url,
                },
            }
        else:
            block = {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": self.media_type,
                    "data": self.data,
                },
            }
        if self.cache_control:
            block["cache_control"] = self.cache_control
        return block

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "type": "document",
            "source_type": self.source_type,
            "media_type": self.media_type,
            "filename": self.filename,
        }
        if self.source_type == "url":
            d["url"] = self.url
        else:
            d["data"] = self.data
        if self.cache_control:
            d["cache_control"] = self.cache_control
        return d


@dataclass
class VideoBlock:
    """Video content block.

    Native video input is currently only consumed by Google Gemini. Other
    providers' `_content_blocks_to_parts` paths ignore VideoBlock, so the
    bridge gates by model family before building one — if a video lands in
    an Anthropic / OpenAI / Cerebras transcript, the model just won't see
    it. The renderer enforces the same gate before allowing drag/drop or
    paste.

    Three source types:
      · `base64`   — inline bytes (Gemini caps inline content at ~20 MB).
      · `url`      — public HTTP(S) URL (Gemini fetches it).
      · `file_uri` — Files API URI returned by `client.files.upload`,
                     used for videos that exceed the inline cap.
    """

    type: Literal["video"] = "video"
    source_type: Literal["base64", "url", "file_uri"] = "base64"

    data: str = ""
    """Base64-encoded video bytes when source_type='base64'."""

    url: str = ""
    """Public URL when source_type='url'."""

    file_uri: str = ""
    """Gemini Files API URI when source_type='file_uri'."""

    media_type: str = "video/mp4"
    """MIME type; mp4/quicktime/webm/etc."""

    filename: str = ""
    """Original filename (display only)."""

    size_bytes: int = 0
    """Raw byte count (display only)."""

    @classmethod
    def from_base64(
        cls,
        data: str,
        *,
        media_type: str = "video/mp4",
        filename: str = "",
        size_bytes: int = 0,
    ) -> "VideoBlock":
        return cls(
            source_type="base64",
            data=data,
            media_type=media_type,
            filename=filename,
            size_bytes=size_bytes,
        )

    @classmethod
    def from_file_uri(
        cls,
        file_uri: str,
        *,
        media_type: str = "video/mp4",
        filename: str = "",
        size_bytes: int = 0,
    ) -> "VideoBlock":
        return cls(
            source_type="file_uri",
            file_uri=file_uri,
            media_type=media_type,
            filename=filename,
            size_bytes=size_bytes,
        )

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "type": "video",
            "source_type": self.source_type,
            "media_type": self.media_type,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
        }
        if self.source_type == "base64":
            d["data"] = self.data
        elif self.source_type == "url":
            d["url"] = self.url
        else:
            d["file_uri"] = self.file_uri
        return d


@dataclass
class ToolUseBlock:
    """Tool use request block."""
    type: Literal["tool_use"] = "tool_use"
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "tool_use", "id": self.id, "name": self.name, "input": self.input}


@dataclass
class ToolResultBlock:
    """Tool result block."""
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str = ""
    content: str | list[TextBlock | ImageBlock] = ""
    is_error: bool = False

    def to_dict(self) -> dict[str, Any]:
        if isinstance(self.content, str):
            content: Any = self.content
        else:
            content = [b.to_dict() for b in self.content]
        return {
            "type": "tool_result",
            "tool_use_id": self.tool_use_id,
            "content": content,
            "is_error": self.is_error,
        }


@dataclass
class ThinkingBlock:
    """Extended thinking content block."""
    type: Literal["thinking"] = "thinking"
    thinking: str = ""
    signature: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"type": "thinking", "thinking": self.thinking, "signature": self.signature}


@dataclass
class RedactedThinkingBlock:
    """Redacted thinking block (encrypted for safety)."""
    type: Literal["redacted_thinking"] = "redacted_thinking"
    data: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"type": "redacted_thinking", "data": self.data}


ContentBlock = TextBlock | ImageBlock | DocumentBlock | VideoBlock | ToolUseBlock | ToolResultBlock | ThinkingBlock | RedactedThinkingBlock


# ============================================================================
# Serialization Helpers
# ============================================================================

def content_block_from_dict(d: dict[str, Any]) -> ContentBlock:
    """Deserialize a content block from a dict (type field dispatches)."""
    t = d.get("type")
    if t == "text":
        return TextBlock(text=d.get("text", ""))
    if t == "image":
        return ImageBlock(
            source_type=d.get("source_type", "base64"),
            data=d.get("data", ""),
            url=d.get("url", ""),
            media_type=d.get("media_type", "image/png"),
        )
    if t == "document":
        return DocumentBlock(
            source_type=d.get("source_type", "base64"),
            data=d.get("data", ""),
            url=d.get("url", ""),
            media_type=d.get("media_type", "application/pdf"),
            filename=d.get("filename", "document.pdf"),
            cache_control=d.get("cache_control"),
        )
    if t == "tool_use":
        return ToolUseBlock(id=d.get("id", ""), name=d.get("name", ""), input=d.get("input", {}))
    if t == "tool_result":
        raw_content = d.get("content", "")
        if isinstance(raw_content, list):
            content: str | list[TextBlock | ImageBlock] = [
                content_block_from_dict(b) for b in raw_content  # type: ignore[misc]
            ]
        else:
            content = str(raw_content)
        return ToolResultBlock(
            tool_use_id=d.get("tool_use_id", ""),
            content=content,
            is_error=d.get("is_error", False),
        )
    if t == "thinking":
        return ThinkingBlock(thinking=d.get("thinking", ""), signature=d.get("signature", ""))
    if t == "redacted_thinking":
        return RedactedThinkingBlock(data=d.get("data", ""))
    # Fallback — treat as text so deserialization never crashes.
    return TextBlock(text=d.get("text", str(d)))


def thinking_block_from_dict(d: dict[str, Any]) -> ThinkingBlock | RedactedThinkingBlock:
    """Deserialize a thinking block."""
    if d.get("type") == "redacted_thinking":
        return RedactedThinkingBlock(data=d.get("data", ""))
    return ThinkingBlock(thinking=d.get("thinking", ""), signature=d.get("signature", ""))


def content_blocks_to_text(content: str | list[ContentBlock]) -> str:
    """Flatten a list of ContentBlocks to a plain text string.

    Useful for providers that don't support structured content blocks
    (e.g. Cerebras, Fireworks). Extracts text from TextBlocks and
    provides descriptive placeholders for non-text blocks.
    """
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for block in content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        elif isinstance(block, ImageBlock):
            parts.append(f"[Image: {block.media_type}]")
        elif isinstance(block, DocumentBlock):
            parts.append(f"[Document: {block.media_type}]")
    return "\n".join(parts)


# ============================================================================
# Thinking Configuration
# ============================================================================

ThinkingLevel = Literal["off", "minimal", "low", "medium", "high"]
"""Thinking level presets that map to effort levels or budget_tokens."""

# Effort level mappings for thinking levels (Claude 4.6+)
THINKING_LEVEL_EFFORT: dict[ThinkingLevel, str] = {
    "off": "low",
    "minimal": "low",
    "low": "low",
    "medium": "medium",
    "high": "high",
}

# Budget token mappings for thinking levels (Claude 4.5, legacy)
THINKING_LEVEL_BUDGETS: dict[ThinkingLevel, int] = {
    "off": 0,
    "minimal": 1024,
    "low": 4000,
    "medium": 10000,
    "high": 32000,
}


@dataclass
class ThinkingConfig:
    """Configuration for extended thinking.

    From Anthropic docs:
    - Claude 4.6+: type="adaptive" with effort parameter (low/medium/high/max)
    - Claude 4.5: type="enabled" with budget_tokens (deprecated on 4.6)
    """
    enabled: bool = False
    effort: str = "high"
    """Effort level for adaptive thinking: low, medium, high, max (Opus 4.6 only)."""
    budget_tokens: int = 10000
    """Budget tokens for legacy models (Claude 4.5). Ignored on Claude 4.6."""

    @classmethod
    def from_level(cls, level: ThinkingLevel) -> "ThinkingConfig":
        """Create config from a thinking level preset."""
        if level == "off":
            return cls(enabled=False)
        return cls(
            enabled=True,
            effort=THINKING_LEVEL_EFFORT[level],
            budget_tokens=THINKING_LEVEL_BUDGETS[level],
        )

    def to_api_param(self, model: str = "") -> dict[str, Any] | None:
        """Convert to Anthropic API parameter format.

        Args:
            model: Model ID to determine format. Claude 4.6+ uses
                `{type: "adaptive", display: "summarized"}`; Claude 4.5
                and earlier use `{type: "enabled", budget_tokens: N}`.

        Returns:
            API parameter dict or None if disabled.
        """
        if not self.enabled:
            return None

        # Claude 4.6+ adaptive thinking. On 4.7 the docs silently changed
        # the default `display` to "omitted", which means thinking_delta
        # events arrive with thinking="" and the renderer shows nothing.
        # Pin display="summarized" explicitly so the UI keeps surfacing
        # the reasoning chain on every adaptive model (4.6 default was
        # already summarized; setting it explicitly is a no-op there but
        # protects against future server-default drift).
        if _is_adaptive_thinking_model(model):
            return {"type": "adaptive", "display": "summarized"}

        # Claude 4.5 and earlier use enabled + budget_tokens
        return {
            "type": "enabled",
            "budget_tokens": self.budget_tokens,
        }

    def get_output_config(self, model: str = "") -> dict[str, Any] | None:
        """Get output_config for effort level (Claude 4.6+).

        Args:
            model: Model ID to determine if effort is supported.

        Returns:
            output_config dict or None if not applicable.
        """
        if not self.enabled:
            return None

        # Adaptive-thinking models accept output_config.effort. Pre-4.6
        # models use budget_tokens for the same purpose, so effort is
        # silently ignored there.
        if _is_adaptive_thinking_model(model):
            return {"effort": self.effort}

        return None


# Anthropic models that take the adaptive thinking shape (type="adaptive")
# instead of the legacy budget_tokens shape. Defined here rather than in
# anthropic_provider so engine.types can be imported without pulling the
# Anthropic SDK. Keep in sync with anthropic_provider.ADAPTIVE_THINKING_MODELS.
# See docs/ADDING-A-MODEL.md — this set is codepoint #9 of 14.
_ADAPTIVE_THINKING_MODEL_IDS: set[str] = {
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-haiku-4-5",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-fable-5",
}


def _is_adaptive_thinking_model(model: str) -> bool:
    """Whether the given model id takes the `{type: "adaptive"}` shape."""
    if not model:
        return False
    # Strip the optional `-fast` variant suffix so fast-mode tiers route
    # the same way as their base model (fast mode is a request flag, not
    # a different family).
    base = model[:-5] if model.endswith("-fast") else model
    return base in _ADAPTIVE_THINKING_MODEL_IDS


# ============================================================================
# Streaming Events
# ============================================================================

@dataclass
class StreamEvent:
    """Base class for stream events."""
    type: str


@dataclass
class TextDeltaEvent(StreamEvent):
    """Text content delta."""
    type: Literal["text_delta"] = "text_delta"
    text: str = ""


@dataclass
class ThinkingDeltaEvent(StreamEvent):
    """Thinking content delta."""
    type: Literal["thinking_delta"] = "thinking_delta"
    thinking: str = ""


@dataclass
class ToolUseStartEvent(StreamEvent):
    """Tool use block started."""
    type: Literal["tool_use_start"] = "tool_use_start"
    id: str = ""
    name: str = ""


@dataclass
class ToolInputDeltaEvent(StreamEvent):
    """Tool input JSON delta."""
    type: Literal["tool_input_delta"] = "tool_input_delta"
    partial_json: str = ""


@dataclass
class ContentBlockStopEvent(StreamEvent):
    """Content block completed."""
    type: Literal["content_block_stop"] = "content_block_stop"
    index: int = 0


@dataclass
class MessageStopEvent(StreamEvent):
    """Message completed."""
    type: Literal["message_stop"] = "message_stop"
    stop_reason: str = ""


# ============================================================================
# System Events (for notifications)
# ============================================================================

SystemEventType = Literal[
    "compaction_start",
    "compaction_complete",
    "context_pruning",
    "media_pruning",
    "tool_truncation",
    "output_truncation",
]


@dataclass
class SystemEvent:
    """System event for notifications."""
    type: SystemEventType
    message: str
    details: dict[str, Any] = field(default_factory=dict)


# ============================================================================
# Messages
# ============================================================================

MessageRole = Literal["user", "assistant", "tool_result", "system"]


@dataclass
class ToolCall:
    """A tool invocation request from the model."""
    id: str
    name: str
    arguments: dict[str, Any]
    provider_kind: str | None = None
    """Provider-native protocol marker, if this is not a plain function tool."""

    provider_data: dict[str, Any] = field(default_factory=dict)
    """Raw provider metadata needed to serialize the call back to that provider."""

    def to_tool_use_block(self) -> ToolUseBlock:
        """Convert to ToolUseBlock for message content."""
        return ToolUseBlock(
            type="tool_use",
            id=self.id,
            name=self.name,
            input=self.arguments,
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
        }
        if self.provider_kind:
            out["provider_kind"] = self.provider_kind
        if self.provider_data:
            out["provider_data"] = self.provider_data
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ToolCall":
        return cls(
            id=d["id"],
            name=d["name"],
            arguments=d.get("arguments", {}),
            provider_kind=d.get("provider_kind"),
            provider_data=d.get("provider_data") or {},
        )


@dataclass
class Message:
    """A conversation message."""
    role: MessageRole
    content: str | list[ContentBlock]
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None
    thinking_blocks: list[ThinkingBlock | RedactedThinkingBlock] | None = None
    input_tokens: int = 0
    """Input tokens from the LLM API response for this message (assistant only)."""
    output_tokens: int = 0
    """Output tokens from the LLM API response for this message (assistant only)."""
    cache_read_tokens: int = 0
    """Cache read tokens from the LLM API response (assistant only)."""
    cache_write_tokens: int = 0
    """Cache write tokens from the LLM API response (assistant only)."""

    def get_text(self) -> str:
        """Extract text content from message."""
        if isinstance(self.content, str):
            return self.content
        text_parts = []
        for block in self.content:
            if isinstance(block, TextBlock):
                text_parts.append(block.text)
        return "".join(text_parts)

    def get_thinking(self) -> str:
        """Extract thinking content from message."""
        if not self.thinking_blocks:
            return ""
        parts = []
        for block in self.thinking_blocks:
            if isinstance(block, ThinkingBlock):
                parts.append(block.thinking)
        return "\n".join(parts)

    def has_images(self) -> bool:
        """Check if message contains any images."""
        if isinstance(self.content, str):
            return False
        return any(isinstance(block, ImageBlock) for block in self.content)

    def get_images(self) -> list[ImageBlock]:
        """Get all image blocks from the message."""
        if isinstance(self.content, str):
            return []
        return [block for block in self.content if isinstance(block, ImageBlock)]

    @classmethod
    def user_with_images(
        cls,
        text: str,
        images: list[ImageBlock],
    ) -> "Message":
        """Create a user message with text and images.

        Images are placed before text as recommended by Anthropic.
        """
        content: list[ContentBlock] = []
        # Add images first (Anthropic recommends images before text)
        content.extend(images)
        # Add text
        if text:
            content.append(TextBlock(text=text))
        return cls(role="user", content=content)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a provider-agnostic dict for transcript persistence."""
        if isinstance(self.content, str):
            content_out: Any = self.content
        else:
            content_out = [b.to_dict() for b in self.content]
        d: dict[str, Any] = {"role": self.role, "content": content_out}
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        if self.tool_calls:
            d["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        if self.thinking_blocks:
            d["thinking_blocks"] = [b.to_dict() for b in self.thinking_blocks]
        if self.input_tokens:
            d["input_tokens"] = self.input_tokens
        if self.output_tokens:
            d["output_tokens"] = self.output_tokens
        if self.cache_read_tokens:
            d["cache_read_tokens"] = self.cache_read_tokens
        if self.cache_write_tokens:
            d["cache_write_tokens"] = self.cache_write_tokens
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Message":
        """Deserialize from a dict produced by to_dict()."""
        raw = d.get("content", "")
        if isinstance(raw, str):
            content: str | list[ContentBlock] = raw
        elif isinstance(raw, list):
            content = [content_block_from_dict(b) for b in raw]
        else:
            content = str(raw)
        tool_calls = None
        if d.get("tool_calls"):
            tool_calls = [ToolCall.from_dict(tc) for tc in d["tool_calls"]]
        thinking_blocks = None
        if d.get("thinking_blocks"):
            thinking_blocks = [thinking_block_from_dict(b) for b in d["thinking_blocks"]]
        return cls(
            role=d["role"],
            content=content,
            tool_call_id=d.get("tool_call_id"),
            tool_calls=tool_calls,
            thinking_blocks=thinking_blocks,
            input_tokens=d.get("input_tokens", 0),
            output_tokens=d.get("output_tokens", 0),
            cache_read_tokens=d.get("cache_read_tokens", 0),
            cache_write_tokens=d.get("cache_write_tokens", 0),
        )


# ============================================================================
# Tool Results
# ============================================================================

@dataclass
class ToolResult:
    """Result from executing a tool."""
    call_id: str
    content: str | list[ContentBlock]
    is_error: bool = False
    cached: bool = False
    """Whether this result was served from cache."""

    def to_message(self) -> Message:
        """Convert to a tool_result message."""
        return Message(
            role="tool_result",
            content=self.content,
            tool_call_id=self.call_id,
        )


# ============================================================================
# API Usage
# ============================================================================

@dataclass
class APIUsage:
    """Token usage from a single API call."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass
class UsageStats:
    """Final usage statistics for a run."""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0


# ============================================================================
# Agent Errors
# ============================================================================

@dataclass
class AgentError:
    """
    Error information matching OpenClaw's FailoverError.

    From failover-error.ts:
    - reason: The classified failure reason
    - message: Human-readable error message
    - retryable: Whether the error might succeed on retry
    - provider/model/profile_id: Context about where the error occurred
    - status/code: HTTP status and error code if available
    """
    reason: FailoverReason
    message: str
    retryable: bool
    provider: str | None = None
    model: str | None = None
    profile_id: str | None = None
    status: int | None = None
    code: str | None = None

    @classmethod
    def from_exception(
        cls,
        exc: Exception,
        reason: FailoverReason = "unknown",
        *,
        provider: str | None = None,
        model: str | None = None,
        profile_id: str | None = None,
    ) -> AgentError:
        """Create AgentError from an exception."""
        return cls(
            reason=reason,
            message=str(exc),
            retryable=reason in ("rate_limit", "timeout", "billing"),
            provider=provider,
            model=model,
            profile_id=profile_id,
            status=FAILOVER_STATUS_MAP.get(reason),
        )


# ============================================================================
# Agent Results
# ============================================================================

@dataclass
class AgentResult:
    """Result from an agent run."""
    success: bool
    response: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: UsageStats = field(default_factory=UsageStats)
    iterations: int = 0
    error: AgentError | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ============================================================================
# Configuration
# ============================================================================

@dataclass
class AgentConfig:
    """
    Agent configuration matching OpenClaw's constants.

    From run.ts:112-115:
    - BASE_RUN_RETRY_ITERATIONS = 24
    - RUN_RETRY_ITERATIONS_PER_PROFILE = 8
    - MIN_RUN_RETRY_ITERATIONS = 32
    - MAX_RUN_RETRY_ITERATIONS = 160
    - MAX_OVERFLOW_COMPACTION_ATTEMPTS = 3

    Iteration formula:
        MAX_ITERATIONS = min(160, max(100, 24 + 8 * num_profiles))
    """

    # Retry iteration bounds
    base_retry_iterations: int = 24
    iterations_per_profile: int = 8
    min_retry_iterations: int = 100
    max_retry_iterations: int = 160

    # Context overflow handling
    max_compaction_attempts: int = 3

    # Tool result truncation (in tokens)
    max_tool_result_tokens: int = 60_000
    """Maximum tokens for a single tool result (pre-flight truncation)."""

    # Resilience features
    profile_rotation_enabled: bool = True
    model_fallback_enabled: bool = True

    # Cooldown settings
    default_cooldown_seconds: float = 60.0
    probe_interval_seconds: float = 30.0

    # Compaction settings
    compaction_threshold: float = 0.25
    """Fraction of effective context window that triggers cheap pruning
    (tool-result halving). Lowered from 0.8 as part of the cooperative
    early-trigger architecture — pruning at 25% is silent and cheap.
    The companion CONTEXT_COMPACTION_THRESHOLD (0.40) triggers the
    LLM summary path."""

    # Token limits
    max_tokens_per_turn: int = 50000
    """Maximum tokens to generate per turn."""

    # Error handling
    max_consecutive_errors: int = 3
    """Maximum consecutive errors before failing."""

    # Parallel execution
    parallel_tool_execution: bool = True
    """Execute multiple tool calls in parallel (async runner only)."""

    max_parallel_tools: int = 10
    """Maximum number of tools to execute in parallel."""

    def compute_max_iterations(self, num_profiles: int) -> int:
        """
        Compute maximum retry iterations based on available auth profiles.

        From OpenClaw run.ts:116-121:
            const scaled = BASE + PER_PROFILE * profileCount;
            return Math.min(MAX, Math.max(MIN, scaled));
        """
        scaled = self.base_retry_iterations + (
            self.iterations_per_profile * num_profiles
        )
        return min(self.max_retry_iterations, max(self.min_retry_iterations, scaled))
