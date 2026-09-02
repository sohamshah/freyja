"""
Z.ai (Zhipu) first-party provider using the OpenAI-compatible API.

Serves the GLM-5.3 family first-party: ``glm-5.3`` and the natively
multimodal ``glm-5.3-flash``. Both are also available third-party via
Fireworks (see fireworks_provider) — keeping both routes gives us a
second quota pool to fall back to. Uses the OpenAI Python SDK pointed at
Z.ai's PAAS endpoint.

GLM-5.3-family quirks this provider encodes (per docs.z.ai, 2026-08):
- Reasoning is MANDATORY. ``thinking.type: "disabled"`` returns HTTP 400
  code 1210. Requests always send ``thinking: {"type": "enabled"}`` plus a
  ``reasoning_effort`` from the model's ladder.
- The effort ladder is low/high/max only (no "none", no "medium"; Z.ai's
  default is max). Freyja's "medium" maps to "high"; disabled/minimal
  intent maps to "low" — the documented migration path for apps that used
  to disable thinking.
- Structured output supports ``response_format: {"type": "json_object"}``
  only (no strict json_schema); the schema is injected into the system
  prompt and validated by the caller.
- ``glm-5.3`` is text-only; ``glm-5.3-flash`` accepts images.

Subscribers on the GLM Coding Plan can point ``ZAI_BASE_URL`` at
``https://api.z.ai/api/coding/paas/v4`` to bill against plan credits
instead of pay-as-you-go.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable

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
    ThinkingBlock,
    ThinkingConfig,
    ThinkingDeltaEvent,
    ToolInputDeltaEvent,
    ToolUseStartEvent,
    content_blocks_to_text,
)

logger = logging.getLogger(__name__)

ZAI_DEFAULT_BASE_URL = "https://api.z.ai/api/paas/v4"

ZAI_CONTEXT_WINDOWS: dict[str, int] = {
    "glm-5.3": 1_048_576,
    "glm-5.3-flash": 1_048_576,
}

# effort ladder per model. Both GLM-5.3 and 5.3-Flash: low/high/max,
# reasoning always on, Z.ai's own default is max (we default to high —
# see MODEL_REGISTRY — since max is markedly more verbose/expensive).
ZAI_REASONING_LEVELS: dict[str, tuple[str, ...]] = {
    "glm-5.3": ("low", "high", "max"),
    "glm-5.3-flash": ("low", "high", "max"),
}

# Models accepting `image_url` content parts. GLM-5.3-Flash is the first
# natively multimodal model in the GLM-5 line; plain 5.3 is text-only.
ZAI_VISION_MODELS: frozenset[str] = frozenset({"glm-5.3-flash"})


def _content_blocks_to_openai(
    content: str | list[ContentBlock], vision: bool = False,
) -> str | list[dict[str, Any]]:
    """Convert content blocks to OpenAI chat format.

    When *vision* is True, ImageBlocks become ``image_url`` parts (Z.ai
    accepts both plain URLs and base64 data URLs). Otherwise everything
    flattens to text.
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
            text = content_blocks_to_text([block])
            if text:
                parts.append({"type": "text", "text": text})
    return parts if parts else ""


def _map_effort_to_zai(effort: str, levels: tuple[str, ...]) -> str:
    """Snap a Freyja effort level onto the model's supported ladder."""
    if effort in levels:
        return effort
    # Freyja rungs Z.ai doesn't have: none/minimal → lowest, medium → high,
    # anything unknown → high (Z.ai's own coding recommendation is max, but
    # high is the sane default for interactive use).
    if effort in ("none", "minimal", "low"):
        return levels[0]
    if "high" in levels:
        return "high"
    return levels[-1]


@dataclass
class ZaiConfig:
    """Configuration for the Z.ai provider."""

    api_key: str | None = None
    """API key (defaults to ZAI_API_KEY env var)."""

    model: str = "glm-5.3"
    """Model identifier as Z.ai names it."""

    max_tokens: int = 8192
    """Default max tokens per request."""

    timeout: float = 120.0
    """Request timeout in seconds."""

    base_url: str | None = None
    """API base URL. Defaults to ZAI_BASE_URL env var, then the PAAS URL."""

    context_window: int = 1_048_576
    """Context window size in tokens."""

    reasoning: ThinkingConfig = field(default_factory=ThinkingConfig)
    """Reasoning configuration. Mapped to reasoning_effort (always sent)."""


StreamCallback = Callable[[StreamEvent], None]
AsyncStreamCallback = Callable[[StreamEvent], Awaitable[None]]


class ZaiProvider:
    """
    Z.ai first-party LLM provider using the OpenAI-compatible API.

    Implements the ModelProvider protocol with complete, complete_async,
    stream, and stream_to_response methods.
    """

    def __init__(self, config: ZaiConfig | None = None):
        self._config = config or ZaiConfig()
        api_key = self._config.api_key or os.environ.get("ZAI_API_KEY")
        if not api_key:
            raise AuthenticationError(
                "ZAI_API_KEY not set. Provide api_key in config or set the env var."
            )

        self._model = self._config.model
        self._context_window = (
            ZAI_CONTEXT_WINDOWS.get(self._model) or self._config.context_window
        )
        base_url = (
            self._config.base_url
            or os.environ.get("ZAI_BASE_URL", "").strip()
            or ZAI_DEFAULT_BASE_URL
        )
        # max_retries=0: let 429s propagate to runner fallback chain
        self._client = openai.OpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=self._config.timeout,
            max_retries=0,
        )
        self._async_client = openai.AsyncOpenAI(
            base_url=base_url,
            api_key=api_key,
            timeout=self._config.timeout,
            max_retries=0,
        )
        # Mutable session_id set by the runner before each call for log correlation
        self.session_id: str | None = None

    @property
    def name(self) -> str:
        return "zai"

    @property
    def model_id(self) -> str:
        return self._model

    @property
    def context_window(self) -> int:
        return self._context_window

    def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        thinking: Any = None,
    ) -> ProviderResponse:
        """Send a synchronous completion request."""
        request_kwargs = self._build_request(
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            thinking=thinking,
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
        thinking: Any = None,
        tool_choice: dict | None = None,
    ) -> ProviderResponse:
        """Send an async completion request."""
        request_kwargs = self._build_request(
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
            thinking=thinking,
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

        Z.ai supports response_format json_object only (no strict
        json_schema), so the schema rides in the system prompt and the
        result is parsed client-side. Reasoning cannot be disabled on
        GLM-5.3 — it is pinned to the lowest effort tier instead.
        """
        from engine.providers import StructuredResponse

        schema_instruction = (
            "Respond ONLY with a JSON object conforming to this JSON Schema "
            f"(no prose, no code fences):\n{json.dumps(schema)}"
        )
        combined_system = (
            f"{system_prompt}\n\n{schema_instruction}" if system_prompt else schema_instruction
        )
        openai_messages = self._convert_messages(messages, combined_system)
        effective_max_tokens = max_tokens or self._config.max_tokens

        levels = ZAI_REASONING_LEVELS.get(self._model, ("low", "high", "max"))
        request_kwargs: dict[str, Any] = {
            "model": self._model,
            "max_completion_tokens": effective_max_tokens,
            "messages": openai_messages,
            "response_format": {"type": "json_object"},
            "reasoning_effort": levels[0],
            "extra_body": {"thinking": {"type": "enabled"}},
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
            "complete_structured RAW ← %s | schema=%s | stop=%s | content_len=%d | in=%d out=%d | preview=%.300s",
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
            thinking=thinking,
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
                stream_events: list[StreamEvent] = []

                reasoning_delta = getattr(delta, "reasoning_content", None)
                if reasoning_delta:
                    stream_events.append(ThinkingDeltaEvent(thinking=reasoning_delta))

                if delta.content:
                    stream_events.append(TextDeltaEvent(text=delta.content))

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
                                stream_events.append(
                                    ToolUseStartEvent(
                                        id=tool_calls_in_progress[idx]["id"],
                                        name=tc_delta.function.name,
                                    )
                                )
                        if tc_delta.function and tc_delta.function.arguments:
                            tool_calls_in_progress[idx]["arguments"] += tc_delta.function.arguments
                            stream_events.append(
                                ToolInputDeltaEvent(
                                    partial_json=tc_delta.function.arguments
                                )
                            )

                for stream_event in stream_events:
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
            thinking=thinking,
        )
        self._log_request(request_kwargs, "stream_to_response")
        request_kwargs["stream"] = True
        request_kwargs["stream_options"] = {"include_usage": True}

        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls_in_progress: dict[int, dict] = {}
        usage = APIUsage()

        try:
            stream = await self._async_client.chat.completions.create(**request_kwargs)

            async for chunk in stream:
                if chunk.usage:
                    details = getattr(chunk.usage, "completion_tokens_details", None)
                    usage = APIUsage(
                        input_tokens=chunk.usage.prompt_tokens or 0,
                        output_tokens=chunk.usage.completion_tokens or 0,
                        reasoning_tokens=getattr(details, "reasoning_tokens", 0) or 0,
                    )

                if not chunk.choices:
                    continue

                delta = chunk.choices[0].delta
                stream_events: list[StreamEvent] = []

                reasoning_delta = getattr(delta, "reasoning_content", None)
                if reasoning_delta:
                    thinking_parts.append(reasoning_delta)
                    stream_events.append(ThinkingDeltaEvent(thinking=reasoning_delta))

                if delta.content:
                    text_parts.append(delta.content)
                    stream_events.append(TextDeltaEvent(text=delta.content))

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
                                stream_events.append(
                                    ToolUseStartEvent(
                                        id=tool_calls_in_progress[idx]["id"],
                                        name=tc_delta.function.name,
                                    )
                                )
                        if tc_delta.function and tc_delta.function.arguments:
                            tool_calls_in_progress[idx]["arguments"] += tc_delta.function.arguments
                            stream_events.append(
                                ToolInputDeltaEvent(
                                    partial_json=tc_delta.function.arguments
                                )
                            )

                if on_event:
                    for stream_event in stream_events:
                        result = on_event(stream_event)
                        if asyncio.iscoroutine(result):
                            await result

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
                thinking_blocks=[
                    ThinkingBlock(thinking="".join(thinking_parts))
                ] if thinking_parts else None,
                model=self._model,
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
        thinking: Any = None,
    ) -> dict[str, Any]:
        """Build the OpenAI-compatible request kwargs."""
        openai_messages = self._convert_messages(messages, system_prompt)
        effective_max_tokens = max_tokens or self._config.max_tokens

        request_kwargs: dict[str, Any] = {
            "model": self._model,
            "max_completion_tokens": effective_max_tokens,
            "messages": openai_messages,
            # Thinking is mandatory on GLM-5.3 — "disabled" 400s (code 1210).
            "reasoning_effort": self._resolve_reasoning_effort(thinking),
            "extra_body": {"thinking": {"type": "enabled"}},
        }

        if tools:
            request_kwargs["tools"] = [self._convert_tool(t) for t in tools]

        # OpenAI format: {"type": "function", "function": {"name": "..."}}
        if tool_choice:
            request_kwargs["tool_choice"] = tool_choice

        return request_kwargs

    def _resolve_reasoning_effort(self, thinking: Any = None) -> str:
        """Map Freyja's ThinkingConfig onto Z.ai's effort ladder.

        Always returns a level — reasoning cannot be turned off, so
        "disabled" intent becomes the lowest supported tier.
        """
        levels = ZAI_REASONING_LEVELS.get(self._model, ("low", "high", "max"))
        config = thinking if thinking is not None else self._config.reasoning
        enabled = bool(getattr(config, "enabled", False))
        effort = str(getattr(config, "effort", "high") or "high")

        if not enabled:
            return levels[0]
        return _map_effort_to_zai(effort, levels)

    def _convert_messages(
        self, messages: list[Message], system_prompt: str | None = None
    ) -> list[dict[str, Any]]:
        """Convert internal Message format to OpenAI chat format.

        Vision models (GLM-5.3-Flash) get ``image_url`` parts; on
        text-only models (GLM-5.3) image/document blocks flatten to their
        text description via content_blocks_to_text.
        """
        vision = self._model in ZAI_VISION_MODELS
        result: list[dict[str, Any]] = []

        if system_prompt:
            result.append({"role": "system", "content": system_prompt})

        for msg in messages:
            if msg.role == "system":
                result.append({"role": "system", "content": content_blocks_to_text(msg.content)})

            elif msg.role == "user":
                result.append({
                    "role": "user",
                    "content": _content_blocks_to_openai(msg.content, vision=vision),
                })

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

                reasoning_content = msg.get_thinking()
                if reasoning_content:
                    entry["reasoning_content"] = reasoning_content

                result.append(entry)

            elif msg.role == "tool_result":
                result.append({
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id,
                    "content": content_blocks_to_text(msg.content),
                })

        return result

    def _convert_tool(self, tool: ToolDefinition) -> dict[str, Any]:
        """Convert internal ToolDefinition to OpenAI function format."""
        from engine.cerebras_provider import _validate_tool_name
        _validate_tool_name(tool.name)
        fn: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.parameters,
        }
        return {"type": "function", "function": fn}

    def _parse_response(self, response: Any) -> ProviderResponse:
        """Parse OpenAI-compatible response to ProviderResponse."""
        choice = response.choices[0] if response.choices else None

        content = ""
        reasoning_content = None
        tool_calls = None

        if choice:
            content = choice.message.content or ""
            reasoning_content = getattr(choice.message, "reasoning_content", None)

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

        reasoning_tokens = 0
        if response.usage:
            details = getattr(response.usage, "completion_tokens_details", None)
            reasoning_tokens = getattr(details, "reasoning_tokens", 0) or 0

        usage = APIUsage(
            input_tokens=response.usage.prompt_tokens if response.usage else 0,
            output_tokens=response.usage.completion_tokens if response.usage else 0,
            reasoning_tokens=reasoning_tokens,
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
            thinking_blocks=[
                ThinkingBlock(thinking=reasoning_content)
            ] if reasoning_content else None,
            model=self._model,
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
            # Z.ai returns 429 for BOTH real rate limits and "insufficient
            # balance or no resource package" (code 1113). The latter is
            # permanent — reporting it as a retryable rate limit burns a
            # backoff before the fallback chain takes over.
            lower = message.lower()
            if "1113" in message or "insufficient balance" in lower or "recharge" in lower:
                return BillingError(message)
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


def create_zai_provider(
    api_key: str | None = None,
    model: str = "glm-5.3",
    **kwargs: Any,
) -> ZaiProvider:
    """Convenience constructor for the Z.ai provider."""
    return ZaiProvider(ZaiConfig(api_key=api_key, model=model, **kwargs))
