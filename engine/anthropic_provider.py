"""
Anthropic Claude provider implementation with streaming and extended thinking.

Implements the ModelProvider protocol for Claude models via the
Anthropic Python SDK, with full support for:
- Async/await operations
- Streaming responses
- Extended thinking (claude-sonnet-4-5, claude-opus-4-5) — budget_tokens
- Adaptive thinking (claude-sonnet-4-6, claude-haiku-4-5, claude-opus-4-6,
  claude-opus-4-7, claude-opus-4-8, claude-fable-5) — type="adaptive" + output_config.effort
- Tool use with thinking block preservation
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

try:
    import anthropic
    from anthropic import APIError, APIStatusError
    from anthropic import AuthenticationError as AnthropicAuthError
except ImportError:
    raise ImportError(
        "anthropic package not installed. Install with: uv sync --extra anthropic"
    )

from engine.providers import (
    AuthenticationError,
    BillingError,
    ContextOverflowError,
    ImagePayloadTooLargeError,
    ModelNotFoundError,
    ProviderError,
    ProviderResponse,
    RateLimitError,
    ToolCallResponse,
)
from engine.constants import (
    ANTHROPIC_API_TIMEOUT,
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_MAX_TOKENS,
    DEFAULT_THINKING_BUDGET_TOKENS,
    MODEL_CONTEXT_WINDOWS,
)
from engine.tools import ToolDefinition
from engine.types import (
    APIUsage,
    DocumentBlock,
    ImageBlock,
    Message,
    RedactedThinkingBlock,
    StreamEvent,
    TextBlock,
    TextDeltaEvent,
    ThinkingBlock,
    ThinkingConfig,
    ThinkingDeltaEvent,
    ToolCall,
    ToolInputDeltaEvent,
    ToolUseStartEvent,
    image_media_type_supported,
    unsupported_image_placeholder_text,
)

logger = logging.getLogger(__name__)


def _anthropic_image_block(block: ImageBlock) -> dict[str, Any]:
    """Serialize an ImageBlock for the Anthropic API, swapping any media type
    Anthropic can't accept (e.g. image/svg+xml) for a text placeholder so one
    bad block can't 400 the whole request. See SUPPORTED_IMAGE_MEDIA_TYPES."""
    if image_media_type_supported(block.media_type):
        return block.to_api_format()
    return {"type": "text", "text": unsupported_image_placeholder_text(block.media_type)}


# Re-export for backward compatibility (some tests/callers import from here)
# Canonical definitions live in engine.constants

# Models that support any form of extended thinking (budget OR adaptive).
# Adaptive-thinking models live in ADAPTIVE_THINKING_MODELS below; the union
# of the two sets gates the `supports_thinking` property. Pre-4.6 Opus/Sonnet
# use the legacy `{type: "enabled", budget_tokens: N}` shape; 4.6+ uses
# `{type: "adaptive"}` and is rejected with 400 if budget_tokens is set.
# When adding a model see docs/ADDING-A-MODEL.md — these three sets +
# `engine/types.py:_ADAPTIVE_THINKING_MODEL_IDS` must move together.
ADAPTIVE_THINKING_MODELS = {
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-haiku-4-5",
    "claude-opus-4-7",
    "claude-opus-4-8",
    "claude-fable-5",
}
LEGACY_THINKING_MODELS = {
    "claude-sonnet-4-5",
    "claude-opus-4-5",
}
THINKING_MODELS = ADAPTIVE_THINKING_MODELS | LEGACY_THINKING_MODELS

# Models that accept fast mode (speed: "fast" + fast-mode-2026-02-01 beta).
# Per Anthropic docs: 4.6 fast mode is deprecated as of 4.8 launch and falls
# back to standard speed at standard pricing; we still list it so existing
# requests don't silently break, but 4.8 is the only one we register a
# distinct fast-tier model id for in AVAILABLE_MODELS.
FAST_MODE_MODELS = {
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
}
FAST_MODE_BETA = "fast-mode-2026-02-01"

# Models that accept `role: "system"` mid-conversation in the messages
# array (after a user turn). Per Anthropic docs only Opus 4.8 supports
# this at the moment; everything else 400s. The provider's
# _convert_messages path falls back to a `[System context]:` user-prefix
# squash on unsupported models so post-compaction summaries still reach
# the model.
INLINE_SYSTEM_MESSAGE_MODELS = {
    "claude-opus-4-8",
}

# Models that REJECT forced tool_choice ({"type":"tool"} / {"type":"any"}) with
# a 400 "tool_choice forces tool use is not compatible with this model." Their
# always-on (Mythos-class) reasoning makes forced tool use incompatible even
# when the request omits the thinking param. Verified empirically against the
# live API (2026-06): claude-fable-5 rejects both forced modes but reliably
# calls a single tool under tool_choice:"auto"; opus-4-8/4-7, sonnet-4-6, and
# haiku-4-5 all accept forced tool use. complete_structured falls back to
# "auto" for these; the retry-on-400 path covers any future model not listed.
FORCED_TOOL_CHOICE_UNSUPPORTED_MODELS = {
    "claude-fable-5",
}


def _model_rejects_forced_tool_choice(model: str) -> bool:
    """True if `model` 400s on forced tool_choice (substring match tolerates a
    dated/`-fast` suffix on the model id)."""
    m = (model or "").lower()
    return any(base in m for base in FORCED_TOOL_CHOICE_UNSUPPORTED_MODELS)

# Model speed tiers for user selection
MODEL_SPEED_TIERS = {
    "fast": "claude-haiku-4-5",       # Fastest, most cost-effective
    "medium": "claude-sonnet-4-6",    # Balanced speed/capability
    "slow": "claude-opus-4-8",        # Most capable (adaptive thinking, 128k out)
}


@dataclass
class AnthropicConfig:
    """Configuration for Anthropic provider."""

    api_key: str | None = None
    """API key (defaults to ANTHROPIC_API_KEY env var)."""

    model: str = "claude-sonnet-4-6"
    """Model to use."""

    max_tokens: int = DEFAULT_MAX_TOKENS
    """Default max tokens for responses."""

    timeout: float = ANTHROPIC_API_TIMEOUT
    """Request timeout in seconds (longer for thinking)."""

    base_url: str | None = None
    """Optional custom base URL."""

    thinking: ThinkingConfig = field(default_factory=ThinkingConfig)
    """Extended thinking configuration."""


# Type alias for stream event callbacks
StreamCallback = Callable[[StreamEvent], None]


class AnthropicProvider:
    """
    Anthropic Claude model provider with streaming and extended thinking.

    Implements the ModelProvider protocol for the agent harness.

    Example:
        provider = AnthropicProvider(
            config=AnthropicConfig(
                model="claude-sonnet-4-6",
                thinking=ThinkingConfig(enabled=True, budget_tokens=10000),
            )
        )

        # Sync completion
        response = provider.complete(
            messages=[Message(role="user", content="Hello!")],
            system_prompt="You are a helpful assistant.",
        )

        # Async streaming
        async for event in provider.stream(messages, system_prompt=prompt):
            if isinstance(event, TextDeltaEvent):
                print(event.text, end="", flush=True)
    """

    def __init__(self, config: AnthropicConfig | None = None):
        """
        Initialize the Anthropic provider.

        Args:
            config: Provider configuration. If None, uses defaults
                    with API key from environment.
        """
        import os

        self._config = config or AnthropicConfig()

        # Get API key
        api_key = self._config.api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError(
                "Anthropic API key required. Set ANTHROPIC_API_KEY environment "
                "variable or pass api_key in config."
            )

        # Initialize sync client
        self._client = anthropic.Anthropic(
            api_key=api_key,
            base_url=self._config.base_url,
            timeout=self._config.timeout,
        )

        # Initialize async client
        self._async_client = anthropic.AsyncAnthropic(
            api_key=api_key,
            base_url=self._config.base_url,
            timeout=self._config.timeout,
        )

        # The `-fast` suffix is a Freyja-side tier marker, not a real
        # Anthropic model id. Strip it before sending to the API and
        # remember to attach `speed: "fast"` + the fast-mode beta header
        # on every request. Per Anthropic docs, fast mode runs the same
        # weights at higher OTPS, so for context-window lookup and
        # capability gating we treat the base id as authoritative.
        configured_model = self._config.model
        if configured_model.endswith("-fast"):
            self._fast_mode = True
            self._model = configured_model[: -len("-fast")]
            self._public_model_id = configured_model
        else:
            self._fast_mode = False
            self._model = configured_model
            self._public_model_id = configured_model
        if self._fast_mode and self._model not in FAST_MODE_MODELS:
            raise ValueError(
                f"Fast mode requested for {self._model}, but only "
                f"{sorted(FAST_MODE_MODELS)} support it."
            )
        self._context_window = MODEL_CONTEXT_WINDOWS.get(
            self._model, DEFAULT_CONTEXT_WINDOW
        )
        # Mutable session_id set by the runner before each call for log correlation
        self.session_id: str | None = None

    @property
    def name(self) -> str:
        """Provider name."""
        return "anthropic"

    @property
    def model_id(self) -> str:
        """Model identifier."""
        return self._model

    @property
    def context_window(self) -> int:
        """Maximum context window size in tokens."""
        return self._context_window

    @property
    def supports_thinking(self) -> bool:
        """Whether the current model supports extended thinking."""
        return self._model in THINKING_MODELS

    async def close(self) -> None:
        """Close the async client to release resources."""
        if hasattr(self, "_async_client") and self._async_client:
            await self._async_client.close()

    def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        thinking: ThinkingConfig | None = None,
    ) -> ProviderResponse:
        """
        Send a completion request to Claude (synchronous).

        Args:
            messages: Conversation history
            tools: Available tool definitions
            system_prompt: System prompt to prepend
            max_tokens: Maximum tokens to generate
            thinking: Override thinking config for this request

        Returns:
            ProviderResponse with content, usage, and thinking blocks

        Raises:
            ProviderError: On API errors
        """
        # Build request
        request_kwargs = self._build_request(
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            thinking=thinking,
        )

        # Make request
        try:
            response = self._client.messages.create(**request_kwargs)
        except AnthropicAuthError as e:
            raise AuthenticationError(str(e)) from e
        except APIStatusError as e:
            raise self._convert_api_error(e) from e
        except APIError as e:
            raise ProviderError(str(e), retryable=True) from e

        # Parse response
        return self._parse_response(response)

    async def complete_async(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        thinking: ThinkingConfig | None = None,
        tool_choice: dict | None = None,
    ) -> ProviderResponse:
        """
        Send a completion request to Claude (async).

        Same as complete() but async.
        """
        request_kwargs = self._build_request(
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            thinking=thinking,
            tool_choice=tool_choice,
        )
        self._log_request(request_kwargs, "complete_async")

        try:
            response = await self._async_client.messages.create(**request_kwargs)
        except AnthropicAuthError as e:
            raise AuthenticationError(str(e)) from e
        except APIStatusError as e:
            raise self._convert_api_error(e) from e
        except APIError as e:
            raise ProviderError(str(e), retryable=True) from e

        return self._parse_response(response)

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
        thinking: ThinkingConfig | None = None,
    ) -> "StructuredResponse":
        """
        Generate a structured JSON response matching the given schema.

        Anthropic has no native response_format / json_schema mode — instead,
        this method synthesizes a tool from the schema, forces the tool call
        via tool_choice, and extracts the tool call arguments as the parsed
        structured data. This is the pattern Anthropic documentation recommends
        for structured output.
        """
        from engine.providers import StructuredResponse
        from engine.tools import ToolDefinition, ToolTier

        # Synthesize a tool from the schema
        synthetic_tool = ToolDefinition(
            name=schema_name,
            description=schema_description or f"Emit the {schema_name} structured output",
            summary=schema_name,
            parameters=schema,
            tier=ToolTier.HOT,
            strict=strict,
        )

        # Disable thinking for structured output (the model should just emit
        # the tool call, not reason about it)
        effective_thinking = thinking or ThinkingConfig(enabled=False)

        async def _call(tool_choice: dict) -> ProviderResponse:
            return await self.complete_async(
                messages=messages,
                tools=[synthetic_tool],
                system_prompt=system_prompt,
                max_tokens=max_tokens,
                thinking=effective_thinking,
                tool_choice=tool_choice,
            )

        forced_choice = {"type": "tool", "name": schema_name}
        auto_choice = {"type": "auto"}

        # Some models (e.g. claude-fable-5) 400 on forced tool_choice but call a
        # single tool reliably under "auto" — skip the doomed forced attempt for
        # those. For every other model, force the tool call (guarantees the
        # structured output) and fall back to "auto" only if the model turns out
        # to reject forced use too (future-proofs the list above).
        use_forced = not _model_rejects_forced_tool_choice(self._model)
        try:
            response = await _call(forced_choice if use_forced else auto_choice)
        except ProviderError as exc:
            if use_forced and "forces tool use is not compatible" in str(exc).lower():
                logger.info(
                    "complete_structured: %s rejects forced tool_choice; "
                    "retrying with tool_choice=auto",
                    self._model,
                )
                response = await _call(auto_choice)
            else:
                raise

        # Raw response logging — debugging structured output failures.
        tool_call_names = (
            [tc.name for tc in response.tool_calls] if response.tool_calls else []
        )
        logger.info(
            "complete_structured RAW \u2190 %s | schema=%s | stop=%s | content_len=%d | tool_calls=%s | in=%d out=%d",
            self._model,
            schema_name,
            response.stop_reason,
            len(response.content or ""),
            tool_call_names,
            response.usage.input_tokens,
            response.usage.output_tokens,
        )

        # Extract the tool call arguments as the structured data
        if response.tool_calls:
            for tc in response.tool_calls:
                if tc.name == schema_name:
                    logger.info(
                        "complete_structured PARSED | schema=%s | keys=%s | sample=%.200s",
                        schema_name,
                        list(tc.arguments.keys()) if isinstance(tc.arguments, dict) else "non-dict",
                        str(tc.arguments),
                    )
                    return StructuredResponse(
                        data=tc.arguments,
                        usage=response.usage,
                        stop_reason=response.stop_reason,
                        model=response.model,
                    )

        # Model didn't call the tool — return raw text for caller to handle
        logger.warning(
            "complete_structured FAILED | schema=%s | stop=%s | content=%.500s",
            schema_name,
            response.stop_reason,
            response.content or "<empty>",
        )
        return StructuredResponse(
            data={},
            usage=response.usage,
            stop_reason=response.stop_reason,
            model=response.model,
            raw_text=response.content,
        )

    async def stream(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        thinking: ThinkingConfig | None = None,
        on_event: StreamCallback | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """
        Stream a completion response from Claude.

        Yields stream events as they arrive, including:
        - TextDeltaEvent: Text content chunks
        - ThinkingDeltaEvent: Thinking content chunks
        - ToolUseStartEvent: Tool use block started
        - ToolInputDeltaEvent: Tool input JSON chunks

        Args:
            messages: Conversation history
            tools: Available tool definitions
            system_prompt: System prompt to prepend
            max_tokens: Maximum tokens to generate
            thinking: Override thinking config for this request
            on_event: Optional callback for each event

        Yields:
            StreamEvent instances as they arrive
        """
        request_kwargs = self._build_request(
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            thinking=thinking,
        )
        self._log_request(request_kwargs, "stream")

        try:
            async with self._async_client.messages.stream(
                **request_kwargs
            ) as stream:
                current_block_type: str | None = None
                current_tool_id: str = ""
                current_tool_name: str = ""

                async for event in stream:
                    stream_event: StreamEvent | None = None

                    if event.type == "content_block_start":
                        block = event.content_block
                        current_block_type = block.type

                        if block.type == "tool_use":
                            current_tool_id = block.id
                            current_tool_name = block.name
                            stream_event = ToolUseStartEvent(
                                id=block.id,
                                name=block.name,
                            )

                    elif event.type == "content_block_delta":
                        delta = event.delta

                        if delta.type == "text_delta":
                            stream_event = TextDeltaEvent(text=delta.text)

                        elif delta.type == "thinking_delta":
                            stream_event = ThinkingDeltaEvent(
                                thinking=delta.thinking
                            )

                        elif delta.type == "input_json_delta":
                            stream_event = ToolInputDeltaEvent(
                                partial_json=delta.partial_json
                            )

                    if stream_event:
                        if on_event:
                            on_event(stream_event)
                        yield stream_event

        except AnthropicAuthError as e:
            raise AuthenticationError(str(e)) from e
        except APIStatusError as e:
            raise self._convert_api_error(e) from e
        except APIError as e:
            raise ProviderError(str(e), retryable=True) from e

    async def stream_to_response(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        thinking: ThinkingConfig | None = None,
        on_event: StreamCallback | None = None,
    ) -> ProviderResponse:
        """
        Stream a completion and return the final response.

        Combines streaming with getting the complete response object.
        Useful when you want to stream events but also need the final
        ProviderResponse with full content and usage.

        Args:
            messages: Conversation history
            tools: Available tool definitions
            system_prompt: System prompt to prepend
            max_tokens: Maximum tokens to generate
            thinking: Override thinking config for this request
            on_event: Optional callback for each event

        Returns:
            ProviderResponse with complete content and usage
        """
        request_kwargs = self._build_request(
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            thinking=thinking,
        )
        self._log_request(request_kwargs, "stream_to_response")

        try:
            async with self._async_client.messages.stream(
                **request_kwargs
            ) as stream:
                # Process events if callback provided
                if on_event:
                    async for event in stream:
                        stream_event: StreamEvent | None = None

                        if event.type == "content_block_start":
                            block = event.content_block
                            if block.type == "tool_use":
                                stream_event = ToolUseStartEvent(
                                    id=block.id,
                                    name=block.name,
                                )

                        elif event.type == "content_block_delta":
                            delta = event.delta
                            if delta.type == "text_delta":
                                stream_event = TextDeltaEvent(text=delta.text)
                            elif delta.type == "thinking_delta":
                                stream_event = ThinkingDeltaEvent(
                                    thinking=delta.thinking
                                )
                            elif delta.type == "input_json_delta":
                                stream_event = ToolInputDeltaEvent(
                                    partial_json=delta.partial_json
                                )

                        if stream_event:
                            # Handle both sync and async callbacks
                            result = on_event(stream_event)
                            if asyncio.iscoroutine(result):
                                await result

                # Get final message
                response = await stream.get_final_message()
                parsed = self._parse_response(response)

                # Log response summary
                tool_names = [tc.name for tc in (parsed.tool_calls or [])]
                text_preview = (parsed.content or "")[:100].replace("\n", " ")
                logger.info(
                    "LLM RESPONSE ← %s | stop=%s | in=%d out=%d tokens | tools=%s | text=%s",
                    request_kwargs.get("model", self._model),
                    parsed.stop_reason,
                    parsed.usage.input_tokens if parsed.usage else 0,
                    parsed.usage.output_tokens if parsed.usage else 0,
                    tool_names if tool_names else "none",
                    text_preview if text_preview else "(empty)",
                )
                return parsed

        except AnthropicAuthError as e:
            raise AuthenticationError(str(e)) from e
        except APIStatusError as e:
            raise self._convert_api_error(e) from e
        except APIError as e:
            raise ProviderError(str(e), retryable=True) from e

    def _log_request(self, request_kwargs: dict[str, Any], method: str) -> None:
        """Log key details about an outgoing LLM request."""
        model = request_kwargs.get("model", self._model)
        msgs = request_kwargs.get("messages", [])
        tools = request_kwargs.get("tools", [])
        system = request_kwargs.get("system", "")
        max_tokens = request_kwargs.get("max_tokens", 0)
        thinking = request_kwargs.get("thinking")

        # Estimate message payload size (rough char count → ~4 chars/token)
        msg_chars = sum(
            len(json.dumps(m.get("content", ""), default=str)) for m in msgs
        )
        est_tokens = msg_chars // 4

        # Last message role + truncated preview
        last_role = msgs[-1].get("role", "?") if msgs else "?"
        last_content = msgs[-1].get("content", "") if msgs else ""
        if isinstance(last_content, list):
            # Multi-block: grab first text block
            for block in last_content:
                if isinstance(block, dict) and block.get("type") == "text":
                    last_content = block.get("text", "")
                    break
            else:
                last_content = str(last_content)[:80]
        preview = str(last_content)[:500].replace("\n", " ")

        sid = self.session_id or "-"
        logger.info(
            "LLM %s → %s | session=%s | %d msgs (~%dk tok) | %d tools | max_tokens=%d | thinking=%s | last=[%s] %s",
            method,
            model,
            sid,
            len(msgs),
            est_tokens // 1000,
            len(tools) if tools else 0,
            max_tokens,
            "on" if thinking else "off",
            last_role,
            preview,
        )

    def _build_request(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        thinking: ThinkingConfig | None = None,
        tool_choice: dict | None = None,
    ) -> dict[str, Any]:
        """Build the API request kwargs."""
        # Convert messages to Anthropic format
        anthropic_messages = self._convert_messages(messages)

        # Convert tools to Anthropic format
        anthropic_tools = None
        if tools:
            anthropic_tools = [self._convert_tool(t) for t in tools]

        # Determine thinking config
        think_config = thinking or self._config.thinking

        # Calculate max_tokens
        effective_max_tokens = max_tokens or self._config.max_tokens

        # For Claude 4.5 models with thinking, ensure max_tokens > budget_tokens
        # Claude 4.6 uses adaptive thinking and doesn't need this constraint
        is_legacy_model = "4-5" in self._model or "4.5" in self._model
        if think_config.enabled and self.supports_thinking and is_legacy_model:
            # Ensure max_tokens > budget_tokens for legacy models
            effective_max_tokens = max(
                effective_max_tokens,
                think_config.budget_tokens + 1024,
            )

        # Build request
        request_kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": effective_max_tokens,
            "messages": anthropic_messages,
        }

        if system_prompt:
            # Use block format with cache_control so the system prompt
            # (often ~20k tokens) is cached across iterations within a turn.
            request_kwargs["system"] = [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        if anthropic_tools:
            # Mark the last tool with cache_control so the full tool
            # definition block is included in the cached prefix.
            anthropic_tools[-1]["cache_control"] = {"type": "ephemeral"}
            request_kwargs["tools"] = anthropic_tools

        # Third cache breakpoint: the most recent compaction summary, if
        # one is in the message list. The summary text is stable across
        # iterations (it changes only when a new compaction happens, by
        # which point the cache reset is unavoidable anyway), so adding
        # a third ephemeral breakpoint here lets long-lived sessions
        # keep cache reuse going past the system + tools prefix.
        # Anthropic allows up to 4 cache_control markers per request;
        # we still have one in reserve for future use.
        _try_cache_compaction_summary(anthropic_messages)

        # Anthropic format: {"type": "tool", "name": "..."}
        if tool_choice:
            request_kwargs["tool_choice"] = tool_choice

        # Add thinking if enabled and supported
        if think_config.enabled and self.supports_thinking:
            request_kwargs["thinking"] = think_config.to_api_param(self._model)

            # Add output_config with effort level for adaptive models (4.6+)
            output_config = think_config.get_output_config(self._model)
            if output_config:
                request_kwargs["output_config"] = output_config

        # Fast mode (research preview). The SDK doesn't know `speed` yet,
        # so we inject it via `extra_body`, and the beta header via
        # `extra_headers`. Per Anthropic docs, fast-mode requests don't
        # share cached prefixes with standard-mode requests, so swapping
        # mode mid-session burns the cache once at the switch — operator-
        # visible as a one-time spike in input_tokens.
        if self._fast_mode:
            extra_body = request_kwargs.get("extra_body") or {}
            extra_body["speed"] = "fast"
            request_kwargs["extra_body"] = extra_body
            extra_headers = request_kwargs.get("extra_headers") or {}
            extra_headers["anthropic-beta"] = FAST_MODE_BETA
            request_kwargs["extra_headers"] = extra_headers

        return request_kwargs

    @staticmethod
    def _sanitize_tool_id(tool_id: str) -> str:
        """Sanitize tool_use/tool_result IDs for Anthropic API.

        Anthropic requires IDs to match ^[a-zA-Z0-9_-]+$.
        IDs from other providers (Cerebras/OpenAI) may contain dots,
        colons, or other characters that fail validation.
        """
        return re.sub(r"[^a-zA-Z0-9_-]", "_", tool_id)

    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert internal messages to Anthropic format.

        For most Anthropic models, an inline ``role: "system"`` entry in
        the messages array returns a 400. We squash those into a
        ``role: "user"`` message prefixed with ``[System context]:`` —
        works on every model, even though it weakens the instruction's
        priority. On Opus 4.8+ (per `INLINE_SYSTEM_MESSAGE_MODELS`) we
        pass it through verbatim IF the placement rule is satisfied
        (immediately after a user turn). The 4.8 path preserves prompt-
        cache hits across compaction summaries — every other turn after
        a compaction used to invalidate cache because the user-prefix
        squash changed the prefix hash.
        """
        result = []

        supports_inline_system = self._model in INLINE_SYSTEM_MESSAGE_MODELS

        def _last_is_user_turn() -> bool:
            """Whether the previously-appended message is a real user
            turn — the docs require system messages to immediately
            follow a user turn (or an assistant turn ending in server
            tool use, which Freyja doesn't currently emit). A
            ``tool_result`` is wrapped here as ``role: "user"`` with a
            single tool_result content block; that's not a real user
            turn for placement purposes, so we exclude it explicitly to
            avoid the API returning a 400 on a borderline-legal shape."""
            if not result or result[-1].get("role") != "user":
                return False
            content = result[-1].get("content")
            if isinstance(content, list):
                # Pure tool_result wrapper → not a real user turn.
                if all(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                ):
                    return False
            return True

        for msg in messages:
            if msg.role == "system":
                content_text = (
                    msg.content if isinstance(msg.content, str)
                    else "".join(
                        getattr(b, "text", "") for b in msg.content
                        if isinstance(b, TextBlock)
                    )
                )
                if supports_inline_system and _last_is_user_turn():
                    # 4.8 path. Cache-friendly because it doesn't change
                    # the byte-identical prefix of earlier turns.
                    result.append({
                        "role": "system",
                        "content": content_text,
                    })
                else:
                    # Fallback for every other model OR when placement
                    # doesn't satisfy the 4.8 rule (e.g. a compaction
                    # summary inserted right after a tool_result).
                    result.append({
                        "role": "user",
                        "content": f"[System context]: {content_text}",
                    })
            elif msg.role == "user":
                # Handle user messages with potential image content
                if isinstance(msg.content, str):
                    result.append({
                        "role": "user",
                        "content": msg.content,
                    })
                else:
                    # Convert content blocks to API format
                    api_content: list[dict[str, Any]] = []
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            api_content.append({"type": "text", "text": block.text})
                        elif isinstance(block, ImageBlock):
                            api_content.append(_anthropic_image_block(block))
                        elif isinstance(block, DocumentBlock):
                            api_content.append(block.to_api_format())
                        else:
                            # Fallback for other block types
                            api_content.append({"type": "text", "text": str(block)})
                    result.append({
                        "role": "user",
                        "content": api_content,
                    })
            elif msg.role == "assistant":
                # Build content including thinking and tool_use blocks
                content: list[dict[str, Any]] = []

                # Add thinking blocks first (must be preserved for tool use)
                if msg.thinking_blocks:
                    for block in msg.thinking_blocks:
                        if isinstance(block, ThinkingBlock):
                            content.append({
                                "type": "thinking",
                                "thinking": block.thinking,
                                "signature": block.signature,
                            })
                        elif isinstance(block, RedactedThinkingBlock):
                            content.append({
                                "type": "redacted_thinking",
                                "data": block.data,
                            })

                # Add text content if present
                if msg.content:
                    text = msg.content if isinstance(msg.content, str) else str(msg.content)
                    if text:
                        content.append({"type": "text", "text": text})

                # Add tool_use blocks if present
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        content.append({
                            "type": "tool_use",
                            "id": self._sanitize_tool_id(tc.id),
                            "name": tc.name,
                            "input": tc.arguments,
                        })

                # Use list content if we have thinking/tool calls, string otherwise
                if msg.thinking_blocks or msg.tool_calls:
                    result.append({"role": "assistant", "content": content})
                else:
                    result.append({
                        "role": "assistant",
                        "content": msg.content if isinstance(msg.content, str) else msg.content,
                    })

            elif msg.role == "tool_result":
                # Tool results go in user message with tool_result block.
                # The Anthropic API accepts either a string or a list of
                # text/image content blocks for the tool_result content
                # field — so a tool that returns a ToolResult whose
                # content is `list[TextBlock | ImageBlock]` (e.g. a
                # screenshot tool) can round-trip the image back to the
                # model.
                tr_content: Any
                if isinstance(msg.content, str):
                    tr_content = msg.content
                elif isinstance(msg.content, list):
                    api_blocks: list[dict[str, Any]] = []
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            api_blocks.append(
                                {"type": "text", "text": block.text}
                            )
                        elif isinstance(block, ImageBlock):
                            api_blocks.append(_anthropic_image_block(block))
                        else:
                            api_blocks.append(
                                {"type": "text", "text": str(block)}
                            )
                    tr_content = api_blocks
                else:
                    tr_content = str(msg.content)
                result.append({
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": self._sanitize_tool_id(msg.tool_call_id or ""),
                            "content": tr_content,
                        }
                    ],
                })

        # Ensure messages alternate properly (user/assistant)
        return self._ensure_alternating(result)

    def _ensure_alternating(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Ensure messages alternate between user and assistant."""
        if not messages:
            return messages

        result = []
        prev_role = None

        for msg in messages:
            role = msg["role"]

            # If same role as previous, merge content
            if role == prev_role and result:
                prev_content = result[-1]["content"]
                curr_content = msg["content"]

                # Merge content
                if isinstance(prev_content, str) and isinstance(curr_content, str):
                    result[-1]["content"] = prev_content + "\n\n" + curr_content
                elif isinstance(prev_content, list) and isinstance(curr_content, list):
                    result[-1]["content"] = prev_content + curr_content
                elif isinstance(prev_content, str) and isinstance(curr_content, list):
                    result[-1]["content"] = [{"type": "text", "text": prev_content}] + curr_content
                elif isinstance(prev_content, list) and isinstance(curr_content, str):
                    result[-1]["content"] = prev_content + [{"type": "text", "text": curr_content}]
            else:
                result.append(msg)
                prev_role = role

        return result

    def _convert_tool(self, tool: ToolDefinition) -> dict[str, Any]:
        """Convert internal tool definition to Anthropic format."""
        return {
            "name": tool.name,
            "description": tool.description,
            "input_schema": tool.parameters,
        }

    def _parse_response(self, response: Any) -> ProviderResponse:
        """Parse Anthropic response to internal format."""
        # Extract content by type
        text_parts = []
        tool_calls = []
        thinking_blocks = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCallResponse(
                        id=block.id,
                        name=block.name,
                        arguments=block.input,
                    )
                )
            elif block.type == "thinking":
                thinking_blocks.append(
                    ThinkingBlock(
                        thinking=block.thinking,
                        signature=getattr(block, "signature", ""),
                    )
                )
            elif block.type == "redacted_thinking":
                thinking_blocks.append(
                    RedactedThinkingBlock(
                        data=block.data,
                    )
                )

        # Build usage
        usage = APIUsage(
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            cache_read_tokens=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        )

        # When the request asked for fast mode, the response's
        # `usage.speed` field tells us which speed was actually used.
        # Per the Opus 4.8 launch notes, 4.6 with `speed: "fast"` will
        # eventually fall back to standard at standard pricing — log the
        # actual mode so a silent fallback is visible in the bridge log
        # instead of hiding under the cost panel.
        actual_speed = getattr(response.usage, "speed", None)
        if self._fast_mode and actual_speed and actual_speed != "fast":
            logger.warning(
                "fast mode requested but response usage.speed=%s | model=%s",
                actual_speed, self._model,
            )

        # Capture `stop_details` (Opus 4.7+ refusal categorization)
        # straight through. Pydantic models may expose this as either an
        # object with attrs or a raw dict, so normalize to a dict for
        # downstream consumers.
        stop_details_raw = getattr(response, "stop_details", None)
        stop_details: dict[str, Any] | None = None
        if stop_details_raw is not None:
            if isinstance(stop_details_raw, dict):
                stop_details = stop_details_raw
            elif hasattr(stop_details_raw, "model_dump"):
                stop_details = stop_details_raw.model_dump()
            elif hasattr(stop_details_raw, "__dict__"):
                stop_details = {
                    k: v for k, v in stop_details_raw.__dict__.items()
                    if not k.startswith("_")
                }
        if stop_details and response.stop_reason == "refusal":
            logger.warning(
                "refusal stop_details | model=%s | details=%s",
                self._model, stop_details,
            )

        return ProviderResponse(
            content="\n".join(text_parts),
            tool_calls=tool_calls if tool_calls else None,
            usage=usage,
            stop_reason=response.stop_reason,
            stop_details=stop_details,
            model=response.model,
            thinking_blocks=thinking_blocks if thinking_blocks else None,
        )

    def _convert_api_error(self, error: APIStatusError) -> ProviderError:
        """Convert Anthropic API error to internal error type."""
        message = str(error)
        status = error.status_code

        # Mid-stream SSE error events are raised by the SDK with the
        # status of the already-connected stream response (200), so the
        # status switch below can't see their real nature. The SDK
        # preserves the body's error type — dispatch on it when the
        # status carries no error signal. (getattr: APIStatusError.type
        # only exists since anthropic 0.87 — never crash in the error
        # converter on an older SDK.)
        error_type = getattr(error, "type", None)
        if status < 400 and error_type:
            if error_type in ("api_error", "overloaded_error", "timeout_error"):
                # Transient server-side faults; their HTTP-level twins
                # (500/529) take the retryable branch below.
                return ProviderError(
                    message, status=status, code=error_type, retryable=True
                )
            if error_type == "rate_limit_error":
                return RateLimitError(message)

        if status == 401:
            return AuthenticationError(message)
        elif status == 402:
            return BillingError(message)
        elif status == 429:
            retry_after = None
            if hasattr(error, "response") and error.response:
                retry_after_str = error.response.headers.get("retry-after")
                if retry_after_str:
                    try:
                        retry_after = float(retry_after_str)
                    except ValueError:
                        pass
            return RateLimitError(message, retry_after=retry_after)
        elif status == 404:
            return ModelNotFoundError(message)
        elif status == 400:
            lower = message.lower()
            # Per-image payload-size errors. Match these BEFORE the
            # generic "exceeds" heuristic — they share the word "exceeds"
            # but represent a different problem and need image-specific
            # recovery, not summarization. Example:
            #   "messages.2.content.1.tool_result.content.1.image.source.
            #    base64: image exceeds 5 MB maximum: 5433096 bytes >
            #    5242880 bytes"
            if "image" in lower and (
                ("exceeds" in lower and "bytes" in lower)
                or "image exceeds" in lower
            ):
                max_bytes: int | None = None
                m = re.search(r">\s*(\d+)\s*bytes", message)
                if m:
                    try:
                        max_bytes = int(m.group(1))
                    except ValueError:
                        max_bytes = None
                return ImagePayloadTooLargeError(message, max_bytes=max_bytes)
            # Context overflow heuristic.
            if any(
                term in lower
                for term in (
                    "context",
                    "token",
                    "too long",
                    "too large",
                    "too much media",
                    "exceeds",
                )
            ):
                return ContextOverflowError(message)
            return ProviderError(message, status=status, retryable=False)
        elif status >= 500:
            return ProviderError(message, status=status, retryable=True)
        else:
            return ProviderError(message, status=status, retryable=False)


_COMPACTION_SUMMARY_MARKER = "[Previous conversation summary]"


def _try_cache_compaction_summary(anthropic_messages: list[dict[str, Any]]) -> None:
    """Mark the most-recent compaction summary message as a cache breakpoint.

    `engine.session.TranscriptManager.get_messages` injects compaction
    summaries as system-role messages, which Anthropic's API doesn't
    support inside ``messages`` — the provider's message-builder rewrites
    them as ``role=user`` with a ``[System context]: [Previous
    conversation summary]\n...`` prefix. This helper walks the rewritten
    message list from the end forward, finds the latest such message,
    and attaches ``cache_control: ephemeral`` to its content so the
    summary text is part of the cached prefix. No-op if no summary is
    present.

    Caps to one cache_control per call to stay under Anthropic's 4-marker
    limit (system + last tool + here = 3 markers, leaving headroom).
    """
    for msg in reversed(anthropic_messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            if _COMPACTION_SUMMARY_MARKER not in content:
                continue
            msg["content"] = [
                {
                    "type": "text",
                    "text": content,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
            return
        if isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and _COMPACTION_SUMMARY_MARKER in (block.get("text") or ""):
                    block["cache_control"] = {"type": "ephemeral"}
                    return


def create_anthropic_provider(
    api_key: str | None = None,
    model: str = "claude-sonnet-4-6",
    thinking: ThinkingConfig | None = None,
    **kwargs,
) -> AnthropicProvider:
    """
    Convenience function to create an Anthropic provider.

    Args:
        api_key: API key (defaults to ANTHROPIC_API_KEY env var)
        model: Model to use
        thinking: Thinking configuration
        **kwargs: Additional config options

    Returns:
        Configured AnthropicProvider
    """
    config = AnthropicConfig(
        api_key=api_key,
        model=model,
        thinking=thinking or ThinkingConfig(),
        **kwargs,
    )
    return AnthropicProvider(config)
