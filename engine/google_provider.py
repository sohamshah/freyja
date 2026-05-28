"""
Google Gemini provider implementation using the google-genai SDK.

Gemini's content format is structurally different from OpenAI's chat-completions
shape: it speaks in ``Content``/``Part`` objects with ``user`` and ``model``
roles only, function calls and responses are dedicated Part types rather than
side-channel ``tool_calls`` fields, and the system prompt lives on the request
config rather than the message list. This provider speaks Gemini natively
instead of bolting an OpenAI-compat shim over it.

The same SDK powers ``bridge/tools/video_analysis_tool.py``. That tool will be
ported to route through this provider in a follow-up so there's one place that
knows about ``GEMINI_API_KEY``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable

try:
    from google import genai
    from google.genai import types as gtypes
    from google.genai import errors as genai_errors
except ImportError as exc:  # pragma: no cover - import guard
    raise ImportError(
        "google-genai package not installed. Install with: uv add google-genai"
    ) from exc

from engine.providers import (
    AuthenticationError,
    BillingError,
    ContextOverflowError,
    ModelNotFoundError,
    ProviderError,
    ProviderResponse,
    RateLimitError,
    StructuredResponse,
    ToolCallResponse,
)
from engine.tools import ToolDefinition
from engine.types import (
    APIUsage,
    ContentBlock,
    DocumentBlock,
    ImageBlock,
    Message,
    StreamEvent,
    TextBlock,
    TextDeltaEvent,
    ToolInputDeltaEvent,
    ToolUseBlock,
    ToolUseStartEvent,
    VideoBlock,
)

logger = logging.getLogger(__name__)


# Maps ThinkingConfig.effort (low/medium/high/max) onto Gemini's ThinkingLevel
# enum. Gemini doesn't expose a "max" level, so it pins to HIGH.
_EFFORT_TO_THINKING_LEVEL: dict[str, str] = {
    "minimal": "MINIMAL",
    "low": "LOW",
    "medium": "MEDIUM",
    "high": "HIGH",
    "max": "HIGH",
}


@dataclass
class GoogleConfig:
    """Configuration for the Google Gemini provider."""

    api_key: str | None = None
    """API key (defaults to GEMINI_API_KEY env var)."""

    model: str = "gemini-3.1-pro-preview"
    """Gemini model id."""

    max_tokens: int = 8192
    """Default max output tokens per request."""

    timeout: float = 180.0
    """Request timeout in seconds."""

    context_window: int = 1_048_576
    """Context window size in tokens (1M for 3.x Pro/Flash)."""


StreamCallback = Callable[[StreamEvent], None]
AsyncStreamCallback = Callable[[StreamEvent], Awaitable[None]]


class GoogleProvider:
    """Google Gemini LLM provider via the google-genai SDK.

    Implements the ``ModelProvider`` protocol — ``complete``, ``complete_async``,
    ``complete_structured``, ``stream``, ``stream_to_response``. Tool calls are
    surfaced as ``ToolCallResponse`` objects so the runner can dispatch them
    through the existing tool harness regardless of provider.
    """

    def __init__(self, config: GoogleConfig | None = None):
        self._config = config or GoogleConfig()
        api_key = self._config.api_key or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise AuthenticationError(
                "GEMINI_API_KEY not set. Provide api_key in config or set the env var."
            )

        self._model = self._config.model
        # google-genai exposes both sync and async surfaces from a single
        # Client; the async surface lives at .aio. The SDK manages an
        # internal httpx pool so we don't need a separate async_client.
        self._client = genai.Client(
            api_key=api_key,
            http_options=gtypes.HttpOptions(timeout=int(self._config.timeout * 1000)),
        )
        self.session_id: str | None = None

    # ── Protocol surface ────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "google"

    @property
    def model_id(self) -> str:
        return self._model

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
        contents = self._convert_messages(messages)
        config = self._build_config(
            tools=tools,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
        )
        self._log_request(contents, tools, max_tokens, method="complete")

        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=contents,
                config=config,
            )
        except genai_errors.APIError as exc:
            raise self._convert_api_error(exc) from exc

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
        contents = self._convert_messages(messages)
        config = self._build_config(
            tools=tools,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            thinking=thinking,
            tool_choice=tool_choice,
        )
        self._log_request(contents, tools, max_tokens, method="complete_async")

        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=contents,
                config=config,
            )
        except genai_errors.APIError as exc:
            raise self._convert_api_error(exc) from exc

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
    ) -> StructuredResponse:
        """Generate a structured JSON response matching the given schema.

        Uses Gemini's ``response_mime_type=application/json`` plus
        ``response_json_schema`` so the server enforces the schema during
        decoding. Verified end-to-end against the goal-mode
        ``GOAL_VERDICT_JSON_SCHEMA`` (additionalProperties=false, enums,
        bounded numbers, nested arrays of objects) — Gemini's JSON-mode
        accepts the same shape OpenAI and Anthropic do, with the caveat
        that the field name is ``response_json_schema`` and not the older
        OpenAPI-3-based ``response_schema``.

        ``schema_name`` is dropped — Gemini has no slot for it.
        ``schema_description``, when present, is folded into the system
        instruction so the model still gets the contextual hint.
        ``strict`` is ignored: Gemini's JSON-mode is always strict.
        """
        contents = self._convert_messages(messages)

        # Fold the schema description into the system prompt so the model
        # still sees the contextual hint that other providers attach via
        # json_schema.description. Mirrors how the runner builds these prompts
        # for OpenAI/Anthropic.
        effective_system_prompt = system_prompt
        if schema_description:
            extra = f"Schema purpose: {schema_description}"
            effective_system_prompt = (
                f"{system_prompt}\n\n{extra}" if system_prompt else extra
            )

        kwargs: dict[str, Any] = {
            "response_mime_type": "application/json",
            "response_json_schema": schema,
            "max_output_tokens": max_tokens or self._config.max_tokens,
        }
        if effective_system_prompt:
            kwargs["system_instruction"] = effective_system_prompt
        thinking_config = self._build_thinking_config(thinking)
        if thinking_config is None:
            # Gemini 3.x reasoning models default to a thinking level that
            # interferes with strict JSON-mode constrained decoding (same
            # failure mode Cerebras documents for GLM-4.7 — they hard-disable
            # reasoning on structured output). Pin to MINIMAL whenever the
            # caller didn't pass an explicit thinking config so the
            # schema-constrained path stays reliable. Callers that DO want
            # thinking on a structured call (rare) can still pass an
            # explicit enabled config.
            thinking_config = gtypes.ThinkingConfig(
                thinking_level="MINIMAL", include_thoughts=False
            )
        kwargs["thinking_config"] = thinking_config

        config = gtypes.GenerateContentConfig(**kwargs)
        self._log_request(
            contents,
            None,
            max_tokens,
            method="complete_structured",
            schema_name=schema_name,
        )

        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=contents,
                config=config,
            )
        except genai_errors.APIError as exc:
            raise self._convert_api_error(exc) from exc

        text = getattr(response, "text", "") or ""
        usage = self._extract_usage(response)
        stop_reason = self._extract_finish_reason(response)

        if not text:
            logger.warning(
                "complete_structured EMPTY | schema=%s | stop=%s",
                schema_name,
                stop_reason,
            )
            return StructuredResponse(
                data={},
                usage=usage,
                stop_reason=stop_reason,
                model=self._model,
                raw_text=text,
            )

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            logger.warning(
                "complete_structured PARSE FAILED | schema=%s | error=%s | content=%.500s",
                schema_name,
                exc,
                text,
            )
            return StructuredResponse(
                data={},
                usage=usage,
                stop_reason=stop_reason,
                model=self._model,
                raw_text=text,
            )

        if not isinstance(data, dict):
            logger.warning(
                "complete_structured PARSE NON-DICT | schema=%s | type=%s",
                schema_name,
                type(data).__name__,
            )
            return StructuredResponse(
                data={},
                usage=usage,
                stop_reason=stop_reason,
                model=self._model,
                raw_text=text,
            )

        return StructuredResponse(
            data=data,
            usage=usage,
            stop_reason=stop_reason,
            model=self._model,
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
        contents = self._convert_messages(messages)
        config = self._build_config(
            tools=tools,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            thinking=thinking,
        )
        self._log_request(contents, tools, max_tokens, method="stream")

        try:
            stream = await self._client.aio.models.generate_content_stream(
                model=self._model,
                contents=contents,
                config=config,
            )
        except genai_errors.APIError as exc:
            raise self._convert_api_error(exc) from exc

        # Track tool calls so the consumer can see the start + arg delta.
        seen_tool_call_ids: set[str] = set()

        async for chunk in stream:
            for event in self._chunk_to_events(chunk, seen_tool_call_ids):
                if on_event:
                    on_event(event)
                yield event

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
        contents = self._convert_messages(messages)
        config = self._build_config(
            tools=tools,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
            thinking=thinking,
        )
        self._log_request(contents, tools, max_tokens, method="stream_to_response")

        text_parts: list[str] = []
        tool_calls: list[ToolCallResponse] = []
        usage = APIUsage()
        finish_reason: str | None = None
        seen_tool_call_ids: set[str] = set()

        try:
            stream = await self._client.aio.models.generate_content_stream(
                model=self._model,
                contents=contents,
                config=config,
            )

            async for chunk in stream:
                for event in self._chunk_to_events(chunk, seen_tool_call_ids):
                    if isinstance(event, TextDeltaEvent):
                        text_parts.append(event.text)
                    if on_event:
                        result = on_event(event)
                        if asyncio.iscoroutine(result):
                            await result

                # Pull whole function calls out of the chunk (the streaming
                # API delivers them as complete parts, not character deltas).
                for fc in self._extract_function_calls(chunk):
                    if fc.id in seen_tool_call_ids:
                        continue
                    seen_tool_call_ids.add(fc.id)
                    tool_calls.append(fc)

                # Usage + finish_reason arrive in the final chunk; keep the
                # last non-empty values we see.
                chunk_usage = self._extract_usage(chunk)
                if chunk_usage.input_tokens or chunk_usage.output_tokens:
                    usage = chunk_usage
                fr = self._extract_finish_reason(chunk)
                if fr:
                    finish_reason = fr

        except genai_errors.APIError as exc:
            raise self._convert_api_error(exc) from exc

        stop_reason = "tool_use" if tool_calls else (finish_reason or "end_turn")
        return ProviderResponse(
            content="".join(text_parts),
            tool_calls=tool_calls or None,
            usage=usage,
            stop_reason=stop_reason,
            model=self._model,
        )

    async def close(self) -> None:
        """Best-effort close of the underlying client."""
        # google-genai's Client doesn't currently expose an explicit close()
        # on the public surface — the SDK manages its own httpx lifetime.
        return None

    # ── Internal helpers ───────────────────────────────────────────

    def _build_config(
        self,
        *,
        tools: list[ToolDefinition] | None,
        system_prompt: str | None,
        max_tokens: int | None,
        thinking: Any = None,
        tool_choice: dict | None = None,
    ) -> gtypes.GenerateContentConfig:
        kwargs: dict[str, Any] = {
            "max_output_tokens": max_tokens or self._config.max_tokens,
        }
        if system_prompt:
            kwargs["system_instruction"] = system_prompt

        if tools:
            kwargs["tools"] = [self._convert_tool(t) for t in tools]
            # Disable automatic function calling — the engine runner dispatches
            # tools through its own harness; we only want declarations passed
            # through and the function_call parts returned for routing.
            kwargs["automatic_function_calling"] = gtypes.AutomaticFunctionCallingConfig(
                disable=True
            )

        mode = self._resolve_tool_mode(tool_choice)
        if mode is not None:
            kwargs["tool_config"] = gtypes.ToolConfig(
                function_calling_config=gtypes.FunctionCallingConfig(mode=mode),
            )

        thinking_config = self._build_thinking_config(thinking)
        if thinking_config is not None:
            kwargs["thinking_config"] = thinking_config

        return gtypes.GenerateContentConfig(**kwargs)

    def _build_thinking_config(self, thinking: Any) -> gtypes.ThinkingConfig | None:
        """Map our internal ThinkingConfig onto Gemini's ThinkingConfig.

        - ``enabled=False`` → MINIMAL (Gemini doesn't allow disabling thinking
          on 3.x reasoning models; MINIMAL is the lowest effort tier).
        - ``enabled=True`` → ``effort`` maps onto ThinkingLevel.
        """
        if thinking is None:
            return None

        enabled = bool(getattr(thinking, "enabled", False))
        if not enabled:
            return gtypes.ThinkingConfig(thinking_level="MINIMAL", include_thoughts=False)

        effort = str(getattr(thinking, "effort", "high")).lower()
        level = _EFFORT_TO_THINKING_LEVEL.get(effort, "HIGH")
        return gtypes.ThinkingConfig(thinking_level=level, include_thoughts=False)

    @staticmethod
    def _resolve_tool_mode(tool_choice: dict | None) -> str | None:
        """Translate the engine's tool_choice dict to Gemini's mode string."""
        if not tool_choice:
            return None
        kind = str(tool_choice.get("type", "")).lower()
        if kind == "any" or kind == "required":
            return "ANY"
        if kind == "auto":
            return "AUTO"
        if kind == "none":
            return "NONE"
        return None

    def _convert_tool(self, tool: ToolDefinition) -> gtypes.Tool:
        """Convert a ToolDefinition to a Gemini Tool with a FunctionDeclaration."""
        declaration = gtypes.FunctionDeclaration(
            name=tool.name,
            description=tool.description,
            parameters_json_schema=tool.parameters or {"type": "object", "properties": {}},
        )
        return gtypes.Tool(function_declarations=[declaration])

    def _convert_messages(
        self, messages: list[Message]
    ) -> list[gtypes.Content]:
        """Convert internal Message list to Gemini Content list.

        Gemini has two roles: ``user`` and ``model``. Tool-result messages
        from the runner are surfaced as ``user`` Contents containing
        ``Part.from_function_response`` parts so the model can see the result
        of the call it just made.
        """
        contents: list[gtypes.Content] = []

        for msg in messages:
            if msg.role == "system":
                # System prompts are passed via system_instruction on the
                # config — drop any inline system messages here. The runner
                # already extracts the system prompt separately.
                continue

            if msg.role == "user":
                parts = self._content_blocks_to_parts(msg.content)
                if not parts:
                    continue
                contents.append(gtypes.Content(role="user", parts=parts))
                continue

            if msg.role == "assistant":
                parts: list[gtypes.Part] = []
                text = ""
                if isinstance(msg.content, str):
                    text = msg.content
                elif msg.content:
                    text_chunks: list[str] = []
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text_chunks.append(block.text)
                    text = "".join(text_chunks)
                if text:
                    parts.append(gtypes.Part.from_text(text=text))
                if msg.tool_calls:
                    import base64 as _b64

                    for tc in msg.tool_calls:
                        args = tc.arguments if isinstance(tc.arguments, dict) else {}
                        sig_bytes: bytes | None = None
                        provider_data = getattr(tc, "provider_data", None) or {}
                        sig_b64 = provider_data.get("thought_signature")
                        if isinstance(sig_b64, str) and sig_b64:
                            try:
                                sig_bytes = _b64.b64decode(sig_b64)
                            except Exception:  # noqa: BLE001
                                sig_bytes = None
                        if sig_bytes is not None:
                            # Construct the Part directly so we can pin
                            # the signature onto the same Part that
                            # carries the function_call — Gemini 3.x
                            # rejects function_call replays whose Part
                            # lacks the original thought_signature.
                            parts.append(
                                gtypes.Part(
                                    function_call=gtypes.FunctionCall(
                                        name=tc.name, args=args
                                    ),
                                    thought_signature=sig_bytes,
                                )
                            )
                        else:
                            # No captured signature — either this call
                            # came from a non-reasoning model, an older
                            # transcript predating signature capture, or
                            # a provider switch. Best-effort fallback;
                            # Gemini 3.x will warn (and eventually
                            # reject) but we don't have a signature to
                            # supply.
                            parts.append(
                                gtypes.Part.from_function_call(
                                    name=tc.name, args=args
                                )
                            )
                if parts:
                    contents.append(gtypes.Content(role="model", parts=parts))
                continue

            if msg.role == "tool_result":
                # Gemini wants function responses tagged with the *name* of
                # the function, not the call id. The runner sets
                # tool_call_id but doesn't carry the name through — we
                # serialize whatever text we have under a stable response
                # shape so the model still sees the result.
                if isinstance(msg.content, str):
                    response_payload: dict[str, Any] = {"result": msg.content}
                    function_name = msg.tool_call_id or "tool_result"
                else:
                    response_payload = {
                        "result": _content_blocks_to_text(msg.content),
                    }
                    function_name = msg.tool_call_id or "tool_result"
                parts = [
                    gtypes.Part.from_function_response(
                        name=function_name,
                        response=response_payload,
                    )
                ]
                contents.append(gtypes.Content(role="user", parts=parts))
                continue

        return contents

    def _content_blocks_to_parts(
        self, content: str | list[ContentBlock]
    ) -> list[gtypes.Part]:
        """Flatten a content payload into Gemini Parts."""
        if isinstance(content, str):
            return [gtypes.Part.from_text(text=content)] if content else []

        parts: list[gtypes.Part] = []
        for block in content:
            if isinstance(block, TextBlock):
                if block.text:
                    parts.append(gtypes.Part.from_text(text=block.text))
            elif isinstance(block, ImageBlock):
                if block.source_type == "url" and block.url:
                    parts.append(
                        gtypes.Part.from_uri(
                            file_uri=block.url, mime_type=block.media_type
                        )
                    )
                elif block.data:
                    # Gemini wants raw bytes here; our ImageBlock carries a
                    # base64 string already, so decode before handing it off.
                    import base64

                    try:
                        raw = base64.b64decode(block.data)
                    except Exception:  # noqa: BLE001
                        continue
                    parts.append(
                        gtypes.Part.from_bytes(data=raw, mime_type=block.media_type)
                    )
            elif isinstance(block, DocumentBlock):
                if block.source_type == "url" and block.url:
                    parts.append(
                        gtypes.Part.from_uri(
                            file_uri=block.url, mime_type=block.media_type
                        )
                    )
                elif block.data:
                    import base64

                    try:
                        raw = base64.b64decode(block.data)
                    except Exception:  # noqa: BLE001
                        continue
                    parts.append(
                        gtypes.Part.from_bytes(data=raw, mime_type=block.media_type)
                    )
            elif isinstance(block, VideoBlock):
                # Gemini accepts video via Part.from_bytes (inline, capped
                # around 20 MB by the API) or Part.from_uri (Files API
                # URI, or public URL). The bridge picks the right source
                # type based on payload size; this branch just routes.
                if block.source_type == "file_uri" and block.file_uri:
                    parts.append(
                        gtypes.Part.from_uri(
                            file_uri=block.file_uri, mime_type=block.media_type
                        )
                    )
                elif block.source_type == "url" and block.url:
                    parts.append(
                        gtypes.Part.from_uri(
                            file_uri=block.url, mime_type=block.media_type
                        )
                    )
                elif block.data:
                    import base64

                    try:
                        raw = base64.b64decode(block.data)
                    except Exception:  # noqa: BLE001
                        continue
                    parts.append(
                        gtypes.Part.from_bytes(data=raw, mime_type=block.media_type)
                    )
            elif isinstance(block, ToolUseBlock):
                # Should only appear in assistant messages; routed via
                # msg.tool_calls above. Skip if it leaked into raw content.
                continue
            # Anything else (thinking blocks, etc.) is intentionally dropped —
            # Gemini doesn't accept them on input.
        return parts

    def _parse_response(self, response: Any) -> ProviderResponse:
        """Convert a non-streaming Gemini response to ProviderResponse."""
        # Walk parts directly instead of `response.text`. The SDK's `.text`
        # accessor warns to stderr on every call when non-text Parts (like
        # function_call) are present — see the explicit hint in that
        # warning to use `candidates.content.parts`. Walking ourselves
        # also makes the thought-filter contract local: we decide what
        # counts as visible assistant text, not the SDK's internal rules.
        text = self._extract_response_text(response)
        tool_calls = self._extract_function_calls(response) or None
        usage = self._extract_usage(response)
        finish_reason = self._extract_finish_reason(response) or "end_turn"
        stop_reason = "tool_use" if tool_calls else finish_reason

        return ProviderResponse(
            content=text,
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=stop_reason,
            model=getattr(response, "model_version", None) or self._model,
        )

    def _chunk_to_events(
        self, chunk: Any, seen_tool_call_ids: set[str]
    ) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        # Same reason as _parse_response: avoid the SDK's `.text` accessor
        # so streaming chunks that mix text + function_call don't dump a
        # warning per chunk into stderr.
        text = self._extract_response_text(chunk)
        if text:
            events.append(TextDeltaEvent(text=text))

        for fc in self._extract_function_calls(chunk):
            if fc.id in seen_tool_call_ids:
                continue
            events.append(ToolUseStartEvent(id=fc.id, name=fc.name))
            try:
                payload = json.dumps(fc.arguments)
            except (TypeError, ValueError):
                payload = "{}"
            events.append(ToolInputDeltaEvent(partial_json=payload))
        return events

    @staticmethod
    def _extract_response_text(response: Any) -> str:
        """Concatenate visible text from ``candidates[0].content.parts``.

        Replaces ``response.text`` / ``chunk.text`` so we don't trip the
        SDK's "non-text parts present" warning on every tool-using turn.
        Skips parts flagged as thoughts (``thought=True``) so internal
        reasoning never leaks into the assistant's visible content,
        independent of any future SDK-default changes.
        """
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return ""
        content = getattr(candidates[0], "content", None)
        if content is None:
            return ""
        parts = getattr(content, "parts", None) or []
        chunks: list[str] = []
        for part in parts:
            if getattr(part, "thought", False):
                continue
            text = getattr(part, "text", None)
            if isinstance(text, str) and text:
                chunks.append(text)
        return "".join(chunks)

    @staticmethod
    def _extract_function_calls(response: Any) -> list[ToolCallResponse]:
        """Pull function_call parts off a response or streaming chunk.

        Walks ``candidates[].content.parts`` directly rather than using the
        convenience ``response.function_calls`` accessor — we need each
        function_call paired with its parent Part's ``thought_signature``
        so the call can be replayed correctly on the next turn. Gemini 3.x
        reasoning models reject 400 ``"Function call is missing a
        thought_signature in functionCall parts"`` if the signature isn't
        round-tripped exactly where it came from.

        The signature is per-Part. We stash it as base64 in
        ``ToolCallResponse.provider_data["thought_signature"]`` so it
        survives JSON round-trip through the persisted transcript; the
        replay path in ``_convert_messages`` base64-decodes it back to
        bytes when building the outgoing Part.
        """
        import base64 as _b64

        calls: list[ToolCallResponse] = []
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return calls
        # We only ever care about the first candidate (we don't request
        # ``candidate_count`` > 1 anywhere). Mirrors the SDK's own
        # ``response.function_calls`` shortcut.
        content = getattr(candidates[0], "content", None)
        parts = getattr(content, "parts", None) or [] if content is not None else []
        for part in parts:
            fc = getattr(part, "function_call", None)
            if fc is None:
                continue
            name = getattr(fc, "name", "") or ""
            args = getattr(fc, "args", None) or {}
            if not isinstance(args, dict):
                args = {}
            # Gemini sometimes echoes back a server-assigned id but
            # usually doesn't; synthesize a stable one so the runner
            # can correlate the eventual tool_result back to this call.
            call_id = getattr(fc, "id", None) or f"call_{uuid.uuid4().hex[:8]}"
            provider_data: dict[str, Any] = {}
            sig = getattr(part, "thought_signature", None)
            if isinstance(sig, (bytes, bytearray)) and sig:
                provider_data["thought_signature"] = _b64.b64encode(bytes(sig)).decode("ascii")
            calls.append(
                ToolCallResponse(
                    id=call_id,
                    name=name,
                    arguments=args,
                    provider_data=provider_data,
                )
            )
        return calls

    @staticmethod
    def _extract_usage(response: Any) -> APIUsage:
        meta = getattr(response, "usage_metadata", None)
        if not meta:
            return APIUsage()
        return APIUsage(
            input_tokens=int(getattr(meta, "prompt_token_count", 0) or 0),
            output_tokens=int(getattr(meta, "candidates_token_count", 0) or 0),
            cache_read_tokens=int(getattr(meta, "cached_content_token_count", 0) or 0),
        )

    @staticmethod
    def _extract_finish_reason(response: Any) -> str | None:
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            return None
        reason = getattr(candidates[0], "finish_reason", None)
        if reason is None:
            return None
        # Reason is an enum; map the few we care about onto our internal
        # vocabulary so the runner doesn't have to learn Gemini-specific
        # strings.
        name = getattr(reason, "name", None) or str(reason)
        mapping = {
            "STOP": "end_turn",
            "MAX_TOKENS": "max_tokens",
            "SAFETY": "safety",
            "RECITATION": "recitation",
            "TOOL_USE": "tool_use",
            "MALFORMED_FUNCTION_CALL": "tool_use",
        }
        return mapping.get(name, name.lower() if isinstance(name, str) else None)

    def _log_request(
        self,
        contents: list[gtypes.Content],
        tools: list[ToolDefinition] | None,
        max_tokens: int | None,
        *,
        method: str,
        schema_name: str | None = None,
    ) -> None:
        msg_count = len(contents)
        last_role = contents[-1].role if contents else "?"
        preview_parts = contents[-1].parts if contents else []
        preview = ""
        for part in preview_parts:
            t = getattr(part, "text", None)
            if isinstance(t, str) and t:
                preview = t[:400].replace("\n", " ")
                break
        logger.info(
            "LLM %s → %s | session=%s | %d msgs | %d tools | max_tokens=%s | schema=%s | last=[%s] %s",
            method,
            self._model,
            self.session_id or "-",
            msg_count,
            len(tools) if tools else 0,
            max_tokens or self._config.max_tokens,
            schema_name or "-",
            last_role,
            preview,
        )

    @staticmethod
    def _convert_api_error(error: Exception) -> ProviderError:
        """Translate a google-genai APIError to our internal error hierarchy."""
        message = str(error)
        status = getattr(error, "code", None) or getattr(error, "status_code", None)
        if isinstance(status, str):
            try:
                status = int(status)
            except ValueError:
                status = None

        if status == 401 or status == 403:
            return AuthenticationError(message)
        if status == 402:
            return BillingError(message)
        if status == 429:
            return RateLimitError(message, retry_after=5.0)
        if status == 404:
            return ModelNotFoundError(message)
        if status == 400:
            lower = message.lower()
            if any(
                term in lower
                for term in ("context", "token", "too long", "too large", "exceeds")
            ):
                return ContextOverflowError(message)
            return ProviderError(message, status=status, retryable=False)
        if isinstance(status, int) and status >= 500:
            return ProviderError(message, status=status, retryable=True)
        return ProviderError(message, status=status, retryable=False)


def _content_blocks_to_text(content: str | list[ContentBlock]) -> str:
    """Flatten tool-result blocks for the function-response payload."""
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


def create_google_provider(
    api_key: str | None = None,
    model: str = "gemini-3.1-pro-preview",
    **kwargs: Any,
) -> GoogleProvider:
    """Convenience function to create a Google Gemini provider."""
    config = GoogleConfig(api_key=api_key, model=model, **kwargs)
    return GoogleProvider(config)
