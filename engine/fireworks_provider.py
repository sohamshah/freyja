"""
Fireworks AI provider implementation using the OpenAI-compatible API.

Supports models hosted on Fireworks: Kimi K2.5, GLM5, MiniMax M2.5, etc.
Uses the OpenAI Python SDK pointed at Fireworks' API endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Awaitable

try:
    import openai
    from openai import APIError, APIStatusError
    from openai import AuthenticationError as OpenAIAuthError
except ImportError:
    raise ImportError(
        "openai package not installed. Install with: uv add openai"
    )

from engine.providers import (
    AuthenticationError,
    BillingError,
    ContextOverflowError,
    ModelNotFoundError,
    ProviderError,
    ProviderResponse,
    RateLimitError,
    ToolCallResponse,
)
from engine.tools import ToolDefinition
from engine.types import (
    APIUsage,
    ContentBlock,
    ImageBlock,
    Message,
    StreamEvent,
    TextBlock,
    TextDeltaEvent,
    ToolInputDeltaEvent,
    ToolUseStartEvent,
    content_blocks_to_text,
)

logger = logging.getLogger(__name__)

FIREWORKS_BASE_URL = "https://api.fireworks.ai/inference/v1"

# Map short model names to Fireworks model IDs
FIREWORKS_MODEL_MAP: dict[str, str] = {
    "kimi-k2.5": "accounts/fireworks/models/kimi-k2p5",
    "glm5": "accounts/fireworks/models/glm-5",
    "minimax-m2.5": "accounts/fireworks/models/minimax-m2p5",
}


# Models that support vision (image_url content blocks)
FIREWORKS_VISION_MODELS: set[str] = {
    "kimi-k2.5",
    "accounts/fireworks/models/kimi-k2p5",
}


def resolve_fireworks_model(model: str) -> str:
    """Resolve a short model name to the full Fireworks model ID."""
    return FIREWORKS_MODEL_MAP.get(model, model)


def _content_blocks_to_openai(
    content: str | list[ContentBlock], vision: bool = False,
) -> str | list[dict[str, Any]]:
    """Convert content blocks to OpenAI chat format.

    When *vision* is True, ImageBlocks are converted to ``image_url``
    content parts.  Otherwise falls back to ``content_blocks_to_text``.
    """
    if isinstance(content, str):
        return content
    if not vision:
        return content_blocks_to_text(content)

    parts: list[dict[str, Any]] = []
    for block in content:
        if isinstance(block, TextBlock):
            parts.append({"type": "text", "text": block.text})
        elif isinstance(block, ImageBlock):
            if block.source_type == "url" and block.url:
                parts.append({"type": "image_url", "image_url": {"url": block.url}})
            elif block.data:
                data_uri = f"data:{block.media_type};base64,{block.data}"
                parts.append({"type": "image_url", "image_url": {"url": data_uri}})
        else:
            # DocumentBlock and others — include as text description
            text = content_blocks_to_text([block])
            if text:
                parts.append({"type": "text", "text": text})
    return parts if parts else ""


@dataclass
class FireworksConfig:
    """Configuration for the Fireworks provider."""

    api_key: str | None = None
    """API key (defaults to FIREWORKS_API_KEY env var)."""

    model: str = "kimi-k2.5"
    """Model identifier (short name or full Fireworks model ID)."""

    max_tokens: int = 8192
    """Default max tokens per request."""

    timeout: float = 120.0
    """Request timeout in seconds."""

    base_url: str = FIREWORKS_BASE_URL
    """API base URL."""

    context_window: int = 131072
    """Context window size in tokens."""


# Type aliases for callbacks
StreamCallback = Callable[[StreamEvent], None]
AsyncStreamCallback = Callable[[StreamEvent], Awaitable[None]]


class FireworksProvider:
    """
    Fireworks AI LLM provider using OpenAI-compatible API.

    Implements the ModelProvider protocol with complete, complete_async,
    stream, and stream_to_response methods.
    """

    def __init__(self, config: FireworksConfig | None = None):
        self._config = config or FireworksConfig()
        api_key = self._config.api_key or os.environ.get("FIREWORKS_API_KEY")
        if not api_key:
            raise AuthenticationError(
                "FIREWORKS_API_KEY not set. Provide api_key in config or set the env var."
            )

        self._model = resolve_fireworks_model(self._config.model)
        self._short_model = self._config.model
        # max_retries=0: let 429s propagate to runner fallback chain
        self._client = openai.OpenAI(
            base_url=self._config.base_url,
            api_key=api_key,
            timeout=self._config.timeout,
            max_retries=0,
        )
        self._async_client = openai.AsyncOpenAI(
            base_url=self._config.base_url,
            api_key=api_key,
            timeout=self._config.timeout,
            max_retries=0,
        )
        # Mutable session_id set by the runner before each call for log correlation
        self.session_id: str | None = None

    @property
    def name(self) -> str:
        return "fireworks"

    @property
    def model_id(self) -> str:
        return self._short_model

    @property
    def context_window(self) -> int:
        return self._config.context_window

    def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        """Send a synchronous completion request."""
        request_kwargs = self._build_request(
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
        )

        try:
            response = self._client.chat.completions.create(**request_kwargs)
        except OpenAIAuthError as e:
            raise AuthenticationError(str(e)) from e
        except APIStatusError as e:
            raise self._convert_api_error(e) from e
        except APIError as e:
            raise ProviderError(str(e), retryable=True) from e

        return self._parse_response(response)

    async def complete_async(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        thinking: Any = None,  # Ignored — Fireworks doesn't support thinking
        tool_choice: dict | None = None,
    ) -> ProviderResponse:
        """Send an async completion request."""
        request_kwargs = self._build_request(
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
        )
        self._log_request(request_kwargs, "complete_async")

        try:
            response = await self._async_client.chat.completions.create(**request_kwargs)
        except OpenAIAuthError as e:
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
        thinking: Any = None,
    ) -> "StructuredResponse":
        """
        Generate a structured JSON response matching the given schema.

        Uses Fireworks' response_format with json_schema. Note that Kimi K2.5
        and other Fireworks models have mixed support for strict structured
        output; the schema is passed and enforcement is best-effort.
        """
        from engine.providers import StructuredResponse

        openai_messages = self._convert_messages(messages, system_prompt)
        effective_max_tokens = max_tokens or self._config.max_tokens

        request_kwargs: dict[str, Any] = {
            "model": self._model,
            "max_completion_tokens": effective_max_tokens,
            "messages": openai_messages,
            "response_format": {
                "type": "json_object",
                "schema": schema,
            },
        }

        self._log_request(request_kwargs, "complete_structured")

        try:
            response = await self._async_client.chat.completions.create(**request_kwargs)
        except OpenAIAuthError as e:
            raise AuthenticationError(str(e)) from e
        except APIStatusError as e:
            raise self._convert_api_error(e) from e
        except APIError as e:
            raise ProviderError(str(e), retryable=True) from e

        choice = response.choices[0] if response.choices else None
        content = (choice.message.content or "") if choice else ""
        stop_reason = choice.finish_reason if choice else None
        usage = APIUsage(
            input_tokens=response.usage.prompt_tokens if response.usage else 0,
            output_tokens=response.usage.completion_tokens if response.usage else 0,
        )

        logger.info(
            "complete_structured RAW \u2190 %s | schema=%s | stop=%s | content_len=%d | in=%d out=%d | preview=%.300s",
            self._model,
            schema_name,
            stop_reason,
            len(content),
            usage.input_tokens,
            usage.output_tokens,
            content.replace("\n", " "),
        )

        try:
            data = json.loads(content) if content else {}
        except json.JSONDecodeError as e:
            logger.warning(
                "complete_structured PARSE FAILED | schema=%s | error=%s | content=%.500s",
                schema_name,
                e,
                content,
            )
            return StructuredResponse(
                data={},
                usage=usage,
                stop_reason=stop_reason,
                model=self._model,
                raw_text=content,
            )

        if isinstance(data, dict):
            logger.info(
                "complete_structured PARSED | schema=%s | keys=%s",
                schema_name,
                list(data.keys()),
            )
            return StructuredResponse(
                data=data,
                usage=usage,
                stop_reason=stop_reason,
                model=self._model,
            )

        logger.warning(
            "complete_structured PARSE NON-DICT | schema=%s | type=%s | content=%.500s",
            schema_name,
            type(data).__name__,
            content,
        )
        return StructuredResponse(
            data={},
            usage=usage,
            stop_reason=stop_reason,
            model=self._model,
            raw_text=content,
        )

    async def stream(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        thinking: Any = None,
        on_event: StreamCallback | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Stream completion response, yielding StreamEvent instances."""
        request_kwargs = self._build_request(
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
        )
        self._log_request(request_kwargs, "stream")
        request_kwargs["stream"] = True

        try:
            stream = await self._async_client.chat.completions.create(**request_kwargs)

            tool_calls_in_progress: dict[int, dict] = {}

            async for chunk in stream:
                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta
                stream_event: StreamEvent | None = None

                if delta.content:
                    stream_event = TextDeltaEvent(text=delta.content)

                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tool_calls_in_progress:
                            tool_calls_in_progress[idx] = {
                                "id": tc_delta.id or f"call_{uuid.uuid4().hex[:8]}",
                                "name": tc_delta.function.name if tc_delta.function and tc_delta.function.name else "",
                                "arguments": "",
                            }
                            if tc_delta.function and tc_delta.function.name:
                                stream_event = ToolUseStartEvent(
                                    id=tool_calls_in_progress[idx]["id"],
                                    name=tc_delta.function.name,
                                )
                        if tc_delta.function and tc_delta.function.arguments:
                            tool_calls_in_progress[idx]["arguments"] += tc_delta.function.arguments
                            stream_event = ToolInputDeltaEvent(
                                partial_json=tc_delta.function.arguments
                            )

                if stream_event:
                    if on_event:
                        on_event(stream_event)
                    yield stream_event

        except OpenAIAuthError as e:
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
        thinking: Any = None,
        on_event: StreamCallback | AsyncStreamCallback | None = None,
    ) -> ProviderResponse:
        """Stream completion and return the assembled ProviderResponse."""
        request_kwargs = self._build_request(
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
        )
        self._log_request(request_kwargs, "stream_to_response")
        request_kwargs["stream"] = True
        request_kwargs["stream_options"] = {"include_usage": True}

        text_parts: list[str] = []
        tool_calls_in_progress: dict[int, dict] = {}
        usage = APIUsage()

        try:
            stream = await self._async_client.chat.completions.create(**request_kwargs)

            async for chunk in stream:
                if chunk.usage:
                    usage = APIUsage(
                        input_tokens=chunk.usage.prompt_tokens or 0,
                        output_tokens=chunk.usage.completion_tokens or 0,
                    )

                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta
                stream_event: StreamEvent | None = None

                if delta.content:
                    text_parts.append(delta.content)
                    stream_event = TextDeltaEvent(text=delta.content)

                if delta.tool_calls:
                    for tc_delta in delta.tool_calls:
                        idx = tc_delta.index
                        if idx not in tool_calls_in_progress:
                            tool_calls_in_progress[idx] = {
                                "id": tc_delta.id or f"call_{uuid.uuid4().hex[:8]}",
                                "name": tc_delta.function.name if tc_delta.function and tc_delta.function.name else "",
                                "arguments": "",
                            }
                            if tc_delta.function and tc_delta.function.name:
                                stream_event = ToolUseStartEvent(
                                    id=tool_calls_in_progress[idx]["id"],
                                    name=tc_delta.function.name,
                                )
                        if tc_delta.function and tc_delta.function.arguments:
                            tool_calls_in_progress[idx]["arguments"] += tc_delta.function.arguments
                            if stream_event is None:
                                stream_event = ToolInputDeltaEvent(
                                    partial_json=tc_delta.function.arguments
                                )

                if stream_event and on_event:
                    result = on_event(stream_event)
                    if asyncio.iscoroutine(result):
                        await result

            # Build tool call responses
            tool_call_responses = None
            if tool_calls_in_progress:
                tool_call_responses = []
                for idx in sorted(tool_calls_in_progress.keys()):
                    tc = tool_calls_in_progress[idx]
                    try:
                        args = json.loads(tc["arguments"]) if tc["arguments"] else {}
                    except json.JSONDecodeError:
                        args = {}
                    tool_call_responses.append(
                        ToolCallResponse(
                            id=tc["id"],
                            name=tc["name"],
                            arguments=args,
                        )
                    )

            stop_reason = "end_turn"
            if tool_call_responses:
                stop_reason = "tool_use"

            return ProviderResponse(
                content="".join(text_parts),
                tool_calls=tool_call_responses,
                usage=usage,
                stop_reason=stop_reason,
                model=self._short_model,
            )

        except OpenAIAuthError as e:
            raise AuthenticationError(str(e)) from e
        except APIStatusError as e:
            raise self._convert_api_error(e) from e
        except APIError as e:
            raise ProviderError(str(e), retryable=True) from e

    async def close(self) -> None:
        """Close the provider clients."""
        await self._async_client.close()
        self._client.close()

    def _log_request(self, request_kwargs: dict[str, Any], method: str) -> None:
        """Log key details about an outgoing LLM request."""
        model = request_kwargs.get("model", self._model)
        msgs = request_kwargs.get("messages", [])
        tools = request_kwargs.get("tools", [])
        max_tokens = request_kwargs.get("max_completion_tokens", 0)

        msg_chars = sum(len(json.dumps(m.get("content", ""), default=str)) for m in msgs)
        est_tokens = msg_chars // 4

        last_role = msgs[-1].get("role", "?") if msgs else "?"
        last_content = msgs[-1].get("content", "") if msgs else ""
        if isinstance(last_content, list):
            last_content = str(last_content)[:80]
        preview = str(last_content)[:500].replace("\n", " ")

        sid = self.session_id or "-"
        logger.info(
            "LLM %s → %s | session=%s | %d msgs (~%dk tok) | %d tools | max_tokens=%d | last=[%s] %s",
            method,
            model,
            sid,
            len(msgs),
            est_tokens // 1000,
            len(tools) if tools else 0,
            max_tokens,
            last_role,
            preview,
        )

    # ---- Internal helpers ----

    def _build_request(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        tool_choice: dict | None = None,
    ) -> dict[str, Any]:
        """Build the OpenAI-compatible request kwargs."""
        openai_messages = self._convert_messages(messages, system_prompt)
        effective_max_tokens = max_tokens or self._config.max_tokens

        request_kwargs: dict[str, Any] = {
            "model": self._model,
            "max_completion_tokens": effective_max_tokens,
            "messages": openai_messages,
        }

        if tools:
            request_kwargs["tools"] = [self._convert_tool(t) for t in tools]

        # OpenAI format: {"type": "function", "function": {"name": "..."}}
        if tool_choice:
            request_kwargs["tool_choice"] = tool_choice

        return request_kwargs

    def _convert_messages(
        self, messages: list[Message], system_prompt: str | None = None
    ) -> list[dict[str, Any]]:
        """Convert internal Message format to OpenAI chat format."""
        vision = (
            self._short_model in FIREWORKS_VISION_MODELS
            or self._model in FIREWORKS_VISION_MODELS
        )
        result: list[dict[str, Any]] = []

        if system_prompt:
            result.append({"role": "system", "content": system_prompt})

        for msg in messages:
            if msg.role == "system":
                result.append({"role": "system", "content": content_blocks_to_text(msg.content)})

            elif msg.role == "user":
                content = _content_blocks_to_openai(msg.content, vision=vision)
                result.append({"role": "user", "content": content})

            elif msg.role == "assistant":
                entry: dict[str, Any] = {"role": "assistant"}

                content = ""
                if msg.content:
                    content = content_blocks_to_text(msg.content)

                if msg.tool_calls:
                    entry["content"] = content or None
                    entry["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.name,
                                "arguments": json.dumps(tc.arguments) if isinstance(tc.arguments, dict) else tc.arguments,
                            },
                        }
                        for tc in msg.tool_calls
                    ]
                else:
                    entry["content"] = content

                result.append(entry)

            elif msg.role == "tool_result":
                result.append({
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id,
                    "content": content_blocks_to_text(msg.content),
                })

        return result

    def _convert_tool(self, tool: ToolDefinition) -> dict[str, Any]:
        """Convert internal ToolDefinition to OpenAI function format.

        Fireworks does not document strict mode support. We include the flag
        when set — Fireworks will silently ignore it if unsupported. Validates
        the tool name against the portable OpenAI-compat pattern.
        """
        from engine.cerebras_provider import _validate_tool_name
        _validate_tool_name(tool.name)
        fn: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }
        if tool.strict:
            fn["strict"] = True
        return {"type": "function", "function": fn}

    def _parse_response(self, response: Any) -> ProviderResponse:
        """Parse OpenAI-compatible response to ProviderResponse."""
        choice = response.choices[0] if response.choices else None

        content = ""
        tool_calls = None

        if choice:
            content = choice.message.content or ""

            if choice.message.tool_calls:
                tool_calls = []
                for tc in choice.message.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                    except json.JSONDecodeError:
                        args = {}
                    tool_calls.append(
                        ToolCallResponse(
                            id=tc.id,
                            name=tc.function.name,
                            arguments=args,
                        )
                    )

        usage = APIUsage(
            input_tokens=response.usage.prompt_tokens if response.usage else 0,
            output_tokens=response.usage.completion_tokens if response.usage else 0,
        )

        stop_reason = "end_turn"
        if choice and choice.finish_reason == "tool_calls":
            stop_reason = "tool_use"
        elif choice and choice.finish_reason == "length":
            stop_reason = "max_tokens"

        return ProviderResponse(
            content=content,
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=stop_reason,
            model=self._short_model,
        )

    def _convert_api_error(self, error: APIStatusError) -> ProviderError:
        """Convert OpenAI API error to internal error type."""
        message = str(error)
        status = error.status_code

        if status == 401:
            return AuthenticationError(message)
        elif status == 402:
            return BillingError(message)
        elif status == 429:
            return RateLimitError(message, retry_after=5.0)
        elif status == 404:
            return ModelNotFoundError(message)
        elif status == 400:
            lower = message.lower()
            if any(term in lower for term in ("context", "token", "too long", "too large", "exceeds")):
                return ContextOverflowError(message)
            return ProviderError(message, status=status, retryable=False)
        elif status >= 500:
            return ProviderError(message, status=status, retryable=True)
        else:
            return ProviderError(message, status=status, retryable=False)
