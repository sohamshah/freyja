"""
OpenAI provider implementation using the Responses API.

Uses the OpenAI Python SDK's `client.responses.create()` endpoint
for GPT-5.4 family models. All requests set `store=False` to prevent
OpenAI from retaining conversation data. Reasoning (thinking) is
supported via the `reasoning` parameter with encrypted round-trip
for multi-turn continuity.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable

try:
    import openai
    from openai import APIError, APIStatusError
    from openai import AuthenticationError as OpenAIAuthError
except ImportError:
    raise ImportError(
        "openai package not installed. Install with: uv add openai"
    )

from engine.provider_native import (
    OPENAI_COMPUTER_KIND,
    OPENAI_COMPUTER_TOOL_NAME,
    OPENAI_NATIVE_COMPUTER_SHADOWED_TOOLS,
    is_openai_computer_call,
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
)

if TYPE_CHECKING:
    from engine.providers import StructuredResponse

logger = logging.getLogger(__name__)

# Context window sizes for supported GPT-5.4 models (in tokens).
# Source: https://platform.openai.com/docs/models
MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-5.5": 1_050_000,
    "gpt-5.4": 1_050_000,
    "gpt-5.4-pro": 1_050_000,
    "gpt-5.4-mini": 400_000,
    "gpt-5.4-nano": 400_000,
    "gpt-5.3-codex": 400_000,
}

# Models that support the `reasoning` parameter.
REASONING_MODELS: set[str] = {
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-pro",
    "gpt-5.4-mini",
    "gpt-5.4-nano",
    "gpt-5.3-codex",
}

NATIVE_COMPUTER_MODELS: set[str] = {
    "gpt-5.5",
}

# Type aliases for streaming callbacks (matching other providers)
StreamCallback = Callable[[StreamEvent], None]
AsyncStreamCallback = Callable[[StreamEvent], Awaitable[None]]


def _api_usage_from_response_usage(response_usage: Any) -> APIUsage:
    """Convert OpenAI's ResponseUsage to our internal ``APIUsage``.

    OpenAI's ``input_tokens`` is the *total* prompt token count and
    ``input_tokens_details.cached_tokens`` is a SUBSET of that count
    (already included). Anthropic, by contrast, reports the three input
    buckets as disjoint. The rest of this codebase — ``engine.usage`` and
    ``engine.providers.compute_cost`` — assumes the disjoint convention.

    If we passed OpenAI's totals through unchanged, ``compute_cost`` would
    bill the cached portion twice (once at the full rate via input_tokens,
    once at the cache rate via cache_read_tokens), inflating displayed
    costs ~10× whenever a cache hit happens. Subtract the cached slice
    here so downstream code sees disjoint values for every provider.
    """
    if response_usage is None:
        return APIUsage()

    reasoning_tokens = 0
    output_details = getattr(response_usage, "output_tokens_details", None)
    if output_details is not None:
        reasoning_tokens = getattr(output_details, "reasoning_tokens", 0) or 0

    cached_tokens = 0
    input_details = getattr(response_usage, "input_tokens_details", None)
    if input_details is not None:
        cached_tokens = getattr(input_details, "cached_tokens", 0) or 0

    raw_input = getattr(response_usage, "input_tokens", 0) or 0
    fresh_input = max(0, raw_input - cached_tokens)

    return APIUsage(
        input_tokens=fresh_input,
        output_tokens=getattr(response_usage, "output_tokens", 0) or 0,
        cache_read_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
    )


@dataclass
class OpenAIConfig:
    """Configuration for the OpenAI Responses API provider.

    Parameters
    ----------
    api_key : str or None
        OpenAI API key. Defaults to the OPENAI_API_KEY environment variable.
    model : str
        Model identifier. Defaults to "gpt-5.4".
    max_tokens : int
        Maximum output tokens per request. Defaults to 50000.
    timeout : float
        Request timeout in seconds. Defaults to 300.0 (longer for reasoning).
    base_url : str or None
        Custom API endpoint override. None uses the default OpenAI endpoint.
    store : bool
        Whether OpenAI should persist responses server-side. Defaults to False
        to prevent data retention.
    reasoning : ThinkingConfig
        Reasoning (thinking) configuration. Controls effort level and whether
        reasoning is enabled.
    native_computer : bool
        Whether to expose OpenAI's native computer tool when Freyja computer
        primitives are available. The actual desktop execution still runs
        through Freyja's shared computer backend.
    """

    api_key: str | None = None
    model: str = "gpt-5.4"
    max_tokens: int = 50000
    timeout: float = 300.0
    base_url: str | None = None
    store: bool = False
    reasoning: ThinkingConfig = field(default_factory=ThinkingConfig)
    native_computer: bool = True


class OpenAIProvider:
    """OpenAI LLM provider using the Responses API.

    Implements the ModelProvider protocol with complete, complete_async,
    stream, and stream_to_response methods. Uses the `/v1/responses`
    endpoint rather than chat completions.

    Parameters
    ----------
    config : OpenAIConfig or None
        Provider configuration. If None, uses defaults with API key
        from the OPENAI_API_KEY environment variable.
    """

    def __init__(self, config: OpenAIConfig | None = None) -> None:
        self._config = config or OpenAIConfig()

        resolved_api_key = self._config.api_key or os.environ.get("OPENAI_API_KEY")
        if not resolved_api_key:
            raise ValueError(
                "API key required. Provide api_key in OpenAIConfig "
                "or set the OPENAI_API_KEY environment variable."
            )

        self._model = self._config.model
        self._context_window = MODEL_CONTEXT_WINDOWS.get(self._model, 400_000)

        # Disable SDK auto-retry so rate limit errors propagate to the
        # runner's fallback chain immediately instead of blocking.
        client_kwargs: dict[str, Any] = {
            "api_key": resolved_api_key,
            "timeout": self._config.timeout,
            "max_retries": 0,
        }
        if self._config.base_url:
            client_kwargs["base_url"] = self._config.base_url

        self._client = openai.OpenAI(**client_kwargs)
        self._async_client = openai.AsyncOpenAI(**client_kwargs)

    @property
    def name(self) -> str:
        """Provider name identifier.

        Returns
        -------
        str
            Always returns "openai".
        """
        return "openai"

    @property
    def model_id(self) -> str:
        """Model identifier configured for this provider.

        Returns
        -------
        str
            The model ID (e.g., "gpt-5.4", "gpt-5.4-mini").
        """
        return self._model

    @property
    def context_window(self) -> int:
        """Maximum context window size in tokens.

        Returns
        -------
        int
            Context window size for the configured model.
        """
        return self._context_window

    @property
    def supports_reasoning(self) -> bool:
        """Whether the configured model supports reasoning.

        Returns
        -------
        bool
            True if the model is in REASONING_MODELS.
        """
        return self._model in REASONING_MODELS

    def _native_computer_enabled(
        self, tools: list[ToolDefinition] | None
    ) -> bool:
        """Return True when this request should use OpenAI's computer tool."""

        if (
            not self._config.native_computer
            or self._model not in NATIVE_COMPUTER_MODELS
            or not tools
        ):
            return False
        names = {tool.name for tool in tools}
        return "screenshot" in names and "click" in names

    @staticmethod
    def _dump_provider_obj(value: Any) -> Any:
        """Convert OpenAI SDK response models into plain JSON-ish values."""

        if isinstance(value, dict):
            return {
                key: OpenAIProvider._dump_provider_obj(val)
                for key, val in value.items()
                if val is not None
            }
        if isinstance(value, list):
            return [OpenAIProvider._dump_provider_obj(item) for item in value]
        if hasattr(value, "model_dump"):
            return OpenAIProvider._dump_provider_obj(value.model_dump(exclude_none=True))
        return value

    @staticmethod
    def _image_url_for_block(block: ImageBlock) -> str:
        if block.source_type == "url":
            return block.url
        return f"data:{block.media_type};base64,{block.data}"

    @staticmethod
    def _last_image_block(content: Any) -> ImageBlock | None:
        if not isinstance(content, list):
            return None
        for block in reversed(content):
            if isinstance(block, ImageBlock):
                return block
        return None

    @staticmethod
    def _tool_result_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            text_parts = [
                block.text for block in content if isinstance(block, TextBlock)
            ]
            image_count = sum(1 for block in content if isinstance(block, ImageBlock))
            suffix = (
                f"\n\n[{image_count} image block(s) omitted from function output]"
                if image_count
                else ""
            )
            return "".join(text_parts) + suffix
        return str(content)

    @staticmethod
    def _tool_call_lookup(messages: list[Message]) -> dict[str, ToolCall]:
        lookup: dict[str, ToolCall] = {}
        for message in messages:
            for call in message.tool_calls or []:
                lookup[call.id] = call
        return lookup

    def _computer_call_input_item(self, tool_call: ToolCall) -> dict[str, Any]:
        data = tool_call.provider_data or {}
        item: dict[str, Any] = {
            "type": "computer_call",
            "id": data.get("id") or tool_call.id,
            "call_id": tool_call.id,
            "status": data.get("status") or "completed",
            "pending_safety_checks": data.get("pending_safety_checks") or [],
        }
        if data.get("actions") is not None:
            item["actions"] = data["actions"]
        elif data.get("action") is not None:
            item["action"] = data["action"]
        elif tool_call.arguments.get("actions") is not None:
            item["actions"] = tool_call.arguments["actions"]
        else:
            item["action"] = tool_call.arguments.get("action") or tool_call.arguments
        return item

    def _computer_call_output_item(
        self,
        message: Message,
        tool_call: ToolCall,
    ) -> dict[str, Any] | None:
        image = self._last_image_block(message.content)
        if image is None:
            return None
        pending = tool_call.provider_data.get("pending_safety_checks") or []
        output: dict[str, Any] = {
            "type": "computer_call_output",
            "call_id": message.tool_call_id,
            "status": "completed",
            "output": {
                "type": "computer_screenshot",
                "image_url": self._image_url_for_block(image),
                # OpenAI's current docs recommend original detail for CUA.
                "detail": "original",
            },
        }
        if pending:
            output["acknowledged_safety_checks"] = pending
        return output

    def _parse_output_items(
        self,
        output_items: Any,
        *,
        include_messages: bool = True,
        include_function_calls: bool = True,
        include_computer_calls: bool = True,
    ) -> tuple[str, list[ToolCallResponse], list[ThinkingBlock | RedactedThinkingBlock]]:
        text_parts: list[str] = []
        tool_call_responses: list[ToolCallResponse] = []
        thinking_blocks: list[ThinkingBlock | RedactedThinkingBlock] = []

        for output_item in output_items:
            item_type = getattr(output_item, "type", None)
            if item_type == "message" and include_messages:
                for content_block in getattr(output_item, "content", []) or []:
                    if getattr(content_block, "type", None) == "output_text":
                        text_parts.append(content_block.text)

            elif item_type == "function_call" and include_function_calls:
                try:
                    arguments = getattr(output_item, "arguments", "") or ""
                    parsed_arguments = json.loads(arguments) if arguments else {}
                except json.JSONDecodeError:
                    parsed_arguments = {}
                tool_call_responses.append(
                    ToolCallResponse(
                        id=getattr(output_item, "call_id", ""),
                        name=getattr(output_item, "name", ""),
                        arguments=parsed_arguments,
                    )
                )

            elif item_type == "computer_call" and include_computer_calls:
                action = self._dump_provider_obj(getattr(output_item, "action", None))
                actions = self._dump_provider_obj(getattr(output_item, "actions", None))
                arguments: dict[str, Any] = {}
                if actions is not None:
                    arguments["actions"] = actions
                if action is not None:
                    arguments["action"] = action
                call_id = getattr(output_item, "call_id", "")
                provider_data = {
                    "id": getattr(output_item, "id", "") or call_id,
                    "status": getattr(output_item, "status", None) or "in_progress",
                    "pending_safety_checks": self._dump_provider_obj(
                        getattr(output_item, "pending_safety_checks", None) or []
                    ),
                }
                if action is not None:
                    provider_data["action"] = action
                if actions is not None:
                    provider_data["actions"] = actions
                tool_call_responses.append(
                    ToolCallResponse(
                        id=call_id,
                        name=OPENAI_COMPUTER_TOOL_NAME,
                        arguments=arguments,
                        provider_kind=OPENAI_COMPUTER_KIND,
                        provider_data=provider_data,
                    )
                )

            elif item_type == "reasoning":
                encrypted_content = getattr(output_item, "encrypted_content", None) or ""
                summary_texts = [
                    summary_part.text
                    for summary_part in (getattr(output_item, "summary", None) or [])
                    if getattr(summary_part, "type", None) == "summary_text"
                ]
                if summary_texts:
                    thinking_blocks.append(
                        ThinkingBlock(
                            thinking="\n".join(summary_texts),
                            signature=encrypted_content,
                        )
                    )
                elif encrypted_content:
                    thinking_blocks.append(RedactedThinkingBlock(data=encrypted_content))

        return "".join(text_parts), tool_call_responses, thinking_blocks

    def build_request_params(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        tool_choice: dict | None = None,
    ) -> dict[str, Any]:
        """Build kwargs for ``client.responses.create()``.

        Converts internal Message objects, tools, and system prompt into the
        native Responses API input format. All message items use the typed
        ``{"type": "message", ...}`` wrapper. Non-message items (function_call,
        function_call_output, reasoning) are bare typed objects.

        Parameters
        ----------
        messages : list[Message]
            Conversation history in internal format.
        tools : list[ToolDefinition] or None
            Tool definitions to make available to the model.
        system_prompt : str or None
            System-level instructions (mapped to the ``instructions`` parameter).
        max_tokens : int or None
            Maximum output tokens. Falls back to config default.

        Returns
        -------
        dict[str, Any]
            Kwargs ready to be unpacked into ``client.responses.create()``.

        References
        ----------
        - Input format: https://platform.openai.com/docs/guides/conversation-state
        - Tool schema: https://platform.openai.com/docs/guides/function-calling
        - Reasoning: https://platform.openai.com/docs/guides/reasoning
        """
        # -- Convert messages to Responses API input items --
        input_items: list[dict[str, Any]] = []
        tool_calls_by_id = self._tool_call_lookup(messages)

        for message in messages:

            if message.role == "system":
                # System messages become developer role in the Responses API.
                text = message.content if isinstance(message.content, str) else message.get_text()
                input_items.append({
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": text}],
                })

            elif message.role == "user":
                # User messages can contain text, images, and documents.
                content_blocks: list[dict[str, Any]] = []

                if isinstance(message.content, str):
                    content_blocks.append({"type": "input_text", "text": message.content})
                else:
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            content_blocks.append({"type": "input_text", "text": block.text})

                        elif isinstance(block, ImageBlock):
                            # URL source uses the URL directly; base64 uses a data URI.
                            # Ref: https://platform.openai.com/docs/guides/images-vision
                            if block.source_type == "url":
                                image_url = block.url
                            else:
                                image_url = f"data:{block.media_type};base64,{block.data}"
                            content_blocks.append({"type": "input_image", "image_url": image_url})

                        elif isinstance(block, DocumentBlock):
                            # Base64 documents use a data URI; URL documents use file_url.
                            # Ref: https://platform.openai.com/docs/guides/pdf-files
                            if block.source_type == "url":
                                content_blocks.append({"type": "input_file", "file_url": block.url})
                            else:
                                file_data_uri = f"data:{block.media_type};base64,{block.data}"
                                content_blocks.append({
                                    "type": "input_file",
                                    "filename": block.filename,
                                    "file_data": file_data_uri,
                                })

                input_items.append({
                    "type": "message",
                    "role": "user",
                    "content": content_blocks,
                })

            elif message.role == "assistant":
                # Assistant messages may have thinking blocks, text content, and tool calls.
                # Thinking blocks become reasoning items (bare typed, before the message).
                # Tool calls become function_call items (bare typed, after the message).
                # Ref: https://platform.openai.com/docs/guides/conversation-state

                # Emit reasoning items first (from thinking_blocks)
                if message.thinking_blocks:
                    for thinking_block in message.thinking_blocks:
                        if isinstance(thinking_block, ThinkingBlock):
                            reasoning_item: dict[str, Any] = {
                                "type": "reasoning",
                                "encrypted_content": thinking_block.signature,
                            }
                            # Include summary if we have the thinking text
                            if thinking_block.thinking:
                                reasoning_item["summary"] = [
                                    {"type": "summary_text", "text": thinking_block.thinking}
                                ]
                            else:
                                reasoning_item["summary"] = []
                            input_items.append(reasoning_item)

                        elif isinstance(thinking_block, RedactedThinkingBlock):
                            # Redacted thinking only has encrypted content, no summary
                            input_items.append({
                                "type": "reasoning",
                                "encrypted_content": thinking_block.data,
                                "summary": [],
                            })

                # Emit the assistant message item with output_text content
                text = message.content if isinstance(message.content, str) else message.get_text()
                if text:
                    input_items.append({
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text}],
                    })

                # Emit function_call items (from tool_calls)
                if message.tool_calls:
                    for tool_call in message.tool_calls:
                        if is_openai_computer_call(tool_call):
                            input_items.append(
                                self._computer_call_input_item(tool_call)
                            )
                            continue
                        serialized_arguments = (
                            json.dumps(tool_call.arguments)
                            if isinstance(tool_call.arguments, dict)
                            else tool_call.arguments
                        )
                        input_items.append({
                            "type": "function_call",
                            "call_id": tool_call.id,
                            "name": tool_call.name,
                            "arguments": serialized_arguments,
                        })

            elif message.role == "tool_result":
                original_call = tool_calls_by_id.get(message.tool_call_id or "")
                if original_call is not None and is_openai_computer_call(original_call):
                    computer_output = self._computer_call_output_item(
                        message, original_call
                    )
                    if computer_output is not None:
                        input_items.append(computer_output)
                        continue

                # Tool results become function_call_output items (bare typed).
                # Ref: https://platform.openai.com/docs/guides/function-calling
                tool_output = self._tool_result_text(message.content)
                input_items.append({
                    "type": "function_call_output",
                    "call_id": message.tool_call_id,
                    "output": tool_output,
                })

        # -- Build request kwargs --
        effective_max_tokens = max_tokens or self._config.max_tokens

        request_params: dict[str, Any] = {
            "model": self._model,
            "input": input_items,
            "max_output_tokens": effective_max_tokens,
            "store": self._config.store,
        }

        # System prompt maps to the instructions parameter
        if system_prompt:
            request_params["instructions"] = system_prompt

        # Convert tool definitions to the flat Responses API function schema.
        # strict=True enables OpenAI strict mode (gpt-4o+) — constrained
        # decoding enforces the schema exactly. Requires additionalProperties
        # false on every object and all fields in required.
        if tools:
            from engine.cerebras_provider import _validate_tool_name

            converted_tools: list[dict[str, Any]] = []
            native_computer = self._native_computer_enabled(tools)
            if native_computer:
                converted_tools.append({"type": "computer"})
            for tool in tools:
                if tool.name == OPENAI_COMPUTER_TOOL_NAME:
                    continue
                if native_computer and tool.name in OPENAI_NATIVE_COMPUTER_SHADOWED_TOOLS:
                    continue
                _validate_tool_name(tool.name)
                entry: dict[str, Any] = {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
                if tool.strict:
                    entry["strict"] = True
                converted_tools.append(entry)
            request_params["tools"] = converted_tools

        if tool_choice:
            request_params["tool_choice"] = tool_choice

        # Build reasoning config from ThinkingConfig. An explicit effort="none"
        # must still be sent; otherwise OpenAI applies the model default.
        reasoning_effort = str(self._config.reasoning.effort or "").lower()
        reasoning_requested = self._config.reasoning.enabled or reasoning_effort == "none"
        if reasoning_requested and self._model in REASONING_MODELS:
            request_params["reasoning"] = {
                "effort": reasoning_effort or "high",
                "summary": "auto",
            }
            # Request encrypted reasoning content for round-trip when store is off
            if self._config.reasoning.enabled and not self._config.store:
                request_params["include"] = ["reasoning.encrypted_content"]

        return request_params

    def complete(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
    ) -> ProviderResponse:
        """Send a synchronous completion request via the Responses API.

        Builds the request from messages and tools, calls the OpenAI API,
        then parses the response into a ProviderResponse. Handles error
        mapping from OpenAI SDK exceptions to internal provider errors.

        Parameters
        ----------
        messages : list[Message]
            Conversation history in internal format.
        tools : list[ToolDefinition] or None
            Tool definitions to make available to the model.
        system_prompt : str or None
            System-level instructions for the model.
        max_tokens : int or None
            Maximum output tokens. Falls back to config default.

        Returns
        -------
        ProviderResponse
            Parsed response with content, tool_calls, usage, thinking_blocks.

        Raises
        ------
        AuthenticationError
            On 401 status from the API.
        RateLimitError
            On 429 status from the API.
        BillingError
            On 402 status from the API.
        ModelNotFoundError
            On 404 status from the API.
        ContextOverflowError
            On 400 status with context/token keywords in the message.
        ProviderError
            On other API errors.

        References
        ----------
        - Response object: https://platform.openai.com/docs/api-reference/responses/object
        - SDK types: openai.types.responses (Response, ResponseOutputMessage, etc.)
        """
        request_params = self.build_request_params(
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
        )

        try:
            response = self._client.responses.create(**request_params)
        except OpenAIAuthError as auth_error:
            raise AuthenticationError(str(auth_error)) from auth_error
        except APIStatusError as status_error:
            raise self._map_api_status_error(status_error) from status_error
        except APIError as api_error:
            raise ProviderError(str(api_error), retryable=True) from api_error

        # -- Parse response.output items into content, tool calls, and thinking blocks --
        # SDK types include message, function_call, computer_call, and reasoning.
        text, tool_call_responses, thinking_blocks = self._parse_output_items(
            response.output
        )

        # -- Build usage from response.usage --
        # SDK type: ResponseUsage with nested OutputTokensDetails and InputTokensDetails
        usage = _api_usage_from_response_usage(response.usage)

        # -- Determine stop_reason from response.status --
        # "completed" + tool calls -> "tool_use"
        # "completed" + no tool calls -> "end_turn"
        # "incomplete" -> "max_tokens" (hit output limit or content filter)
        stop_reason = "end_turn"
        if tool_call_responses:
            stop_reason = "tool_use"
        elif getattr(response, "status", None) == "incomplete":
            stop_reason = "max_tokens"

        return ProviderResponse(
            content=text,
            tool_calls=tool_call_responses if tool_call_responses else None,
            usage=usage,
            stop_reason=stop_reason,
            model=response.model,
            thinking_blocks=thinking_blocks if thinking_blocks else None,
        )

    async def complete_async(
        self,
        messages: list[Message],
        *,
        tools: list[ToolDefinition] | None = None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
        thinking: Any = None,  # Accepted for protocol compatibility, uses config instead
        tool_choice: dict | None = None,
    ) -> ProviderResponse:
        """Send an asynchronous completion request via the Responses API.

        Same logic as ``complete()`` but uses the async client. The reasoning
        configuration comes from ``OpenAIConfig.reasoning``, not the
        ``thinking`` parameter (which exists only for protocol compatibility
        with the runner).

        Parameters
        ----------
        messages : list[Message]
            Conversation history in internal format.
        tools : list[ToolDefinition] or None
            Tool definitions to make available to the model.
        system_prompt : str or None
            System-level instructions for the model.
        max_tokens : int or None
            Maximum output tokens. Falls back to config default.
        thinking : Any
            Ignored. Present for protocol compatibility with the runner,
            which passes this to all providers.

        Returns
        -------
        ProviderResponse
            Parsed response with content, tool_calls, usage, thinking_blocks.

        Raises
        ------
        AuthenticationError
            On 401 status from the API.
        RateLimitError
            On 429 status from the API.
        ProviderError
            On other API errors.
        """
        request_params = self.build_request_params(
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
        )

        try:
            response = await self._async_client.responses.create(**request_params)
        except OpenAIAuthError as auth_error:
            raise AuthenticationError(str(auth_error)) from auth_error
        except APIStatusError as status_error:
            raise self._map_api_status_error(status_error) from status_error
        except APIError as api_error:
            raise ProviderError(str(api_error), retryable=True) from api_error

        # -- Parse response.output items (same logic as complete()) --
        text, tool_call_responses, thinking_blocks = self._parse_output_items(
            response.output
        )

        # -- Build usage --
        usage = _api_usage_from_response_usage(response.usage)

        # -- Determine stop_reason --
        stop_reason = "end_turn"
        if tool_call_responses:
            stop_reason = "tool_use"
        elif getattr(response, "status", None) == "incomplete":
            stop_reason = "max_tokens"

        return ProviderResponse(
            content=text,
            tool_calls=tool_call_responses if tool_call_responses else None,
            usage=usage,
            stop_reason=stop_reason,
            model=response.model,
            thinking_blocks=thinking_blocks if thinking_blocks else None,
        )

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

        Uses OpenAI Responses API with `text.format = json_schema` and strict
        mode, which enforces the schema via constrained decoding (gpt-4o+).
        """
        from engine.providers import StructuredResponse

        request_params = self.build_request_params(
            messages=messages,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
        )
        # Override with structured output config
        request_params["text"] = {
            "format": {
                "type": "json_schema",
                "name": schema_name,
                "strict": strict,
                "schema": schema,
            }
        }
        # Drop tools if present — structured output and tools are mutually exclusive
        request_params.pop("tools", None)
        request_params.pop("tool_choice", None)

        try:
            response = await self._async_client.responses.create(**request_params)
        except OpenAIAuthError as auth_error:
            raise AuthenticationError(str(auth_error)) from auth_error
        except APIStatusError as status_error:
            raise self._map_api_status_error(status_error) from status_error
        except APIError as api_error:
            raise ProviderError(str(api_error), retryable=True) from api_error

        # Extract text from response.output
        text_parts: list[str] = []
        for output_item in response.output:
            if output_item.type == "message":
                for content_block in output_item.content:
                    if content_block.type == "output_text":
                        text_parts.append(content_block.text)

        content = "".join(text_parts)
        stop_reason = getattr(response, "stop_reason", None) or "end_turn"

        usage = _api_usage_from_response_usage(response.usage)

        logger.info(
            "complete_structured RAW <- %s | schema=%s | stop=%s | "
            "content_len=%d | in=%d out=%d | preview=%.300s",
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
                model=response.model,
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
                model=response.model,
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
            model=response.model,
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
        """Stream completion response, yielding StreamEvent instances.

        Uses the Responses API streaming mode which emits named SSE events
        rather than delta chunks. Maps OpenAI event types to internal
        StreamEvent types.

        Parameters
        ----------
        messages : list[Message]
            Conversation history in internal format.
        tools : list[ToolDefinition] or None
            Tool definitions to make available to the model.
        system_prompt : str or None
            System-level instructions for the model.
        max_tokens : int or None
            Maximum output tokens. Falls back to config default.
        thinking : Any
            Ignored. Present for protocol compatibility.
        on_event : StreamCallback or None
            Optional synchronous callback invoked for each stream event.

        Yields
        ------
        StreamEvent
            TextDeltaEvent, ThinkingDeltaEvent, ToolUseStartEvent, or
            ToolInputDeltaEvent as they arrive from the API.

        References
        ----------
        - Streaming events: https://platform.openai.com/docs/api-reference/responses/streaming
        """
        request_params = self.build_request_params(
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
        )
        request_params["stream"] = True

        try:
            event_stream = await self._async_client.responses.create(**request_params)

            async for sse_event in event_stream:
                stream_event: StreamEvent | None = None

                # Text content delta
                if sse_event.type == "response.output_text.delta":
                    stream_event = TextDeltaEvent(text=sse_event.delta)

                # Reasoning summary text delta
                elif sse_event.type == "response.reasoning_summary_text.delta":
                    stream_event = ThinkingDeltaEvent(thinking=sse_event.delta)

                # New output item added -- emit ToolUseStartEvent for function calls
                elif sse_event.type == "response.output_item.added":
                    output_item = sse_event.item
                    if getattr(output_item, "type", None) == "function_call":
                        stream_event = ToolUseStartEvent(
                            id=getattr(output_item, "call_id", ""),
                            name=getattr(output_item, "name", ""),
                        )
                    elif getattr(output_item, "type", None) == "computer_call":
                        stream_event = ToolUseStartEvent(
                            id=getattr(output_item, "call_id", ""),
                            name=OPENAI_COMPUTER_TOOL_NAME,
                        )

                # Function call arguments streaming
                elif sse_event.type == "response.function_call_arguments.delta":
                    stream_event = ToolInputDeltaEvent(partial_json=sse_event.delta)

                if stream_event:
                    if on_event:
                        on_event(stream_event)
                    yield stream_event

        except OpenAIAuthError as auth_error:
            raise AuthenticationError(str(auth_error)) from auth_error
        except APIStatusError as status_error:
            raise self._map_api_status_error(status_error) from status_error
        except APIError as api_error:
            raise ProviderError(str(api_error), retryable=True) from api_error

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
        """Stream completion and return the assembled ProviderResponse.

        Streams SSE events from the Responses API, optionally calling
        ``on_event`` for each event, and accumulates the final response
        with content, tool calls, thinking blocks, and usage.

        Parameters
        ----------
        messages : list[Message]
            Conversation history in internal format.
        tools : list[ToolDefinition] or None
            Tool definitions to make available to the model.
        system_prompt : str or None
            System-level instructions for the model.
        max_tokens : int or None
            Maximum output tokens. Falls back to config default.
        thinking : Any
            Ignored. Present for protocol compatibility.
        on_event : StreamCallback or AsyncStreamCallback or None
            Optional callback (sync or async) invoked for each stream event.

        Returns
        -------
        ProviderResponse
            The fully assembled response after streaming completes.
        """
        request_params = self.build_request_params(
            messages=messages,
            tools=tools,
            system_prompt=system_prompt,
            max_tokens=max_tokens,
        )
        request_params["stream"] = True

        # Accumulators for building the final response
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls_in_progress: dict[str, dict[str, Any]] = {}
        native_tool_calls: list[ToolCallResponse] = []
        thinking_blocks: list[ThinkingBlock | RedactedThinkingBlock] = []
        usage = APIUsage()
        response_status = "completed"
        response_model = self._model

        try:
            event_stream = await self._async_client.responses.create(**request_params)

            async for sse_event in event_stream:
                stream_event: StreamEvent | None = None

                # Text content delta
                if sse_event.type == "response.output_text.delta":
                    text_parts.append(sse_event.delta)
                    stream_event = TextDeltaEvent(text=sse_event.delta)

                # Reasoning summary text delta
                elif sse_event.type == "response.reasoning_summary_text.delta":
                    thinking_parts.append(sse_event.delta)
                    stream_event = ThinkingDeltaEvent(thinking=sse_event.delta)

                # New output item added
                elif sse_event.type == "response.output_item.added":
                    output_item = sse_event.item
                    if getattr(output_item, "type", None) == "function_call":
                        # Key by the item's own id (matches item_id on delta events),
                        # NOT call_id (which is the tool call identifier for results).
                        function_call_item_id = getattr(output_item, "id", "")
                        call_id = getattr(output_item, "call_id", "")
                        tool_name = getattr(output_item, "name", "")
                        tool_calls_in_progress[function_call_item_id] = {
                            "call_id": call_id,
                            "name": tool_name,
                            "arguments": "",
                        }
                        stream_event = ToolUseStartEvent(id=call_id, name=tool_name)
                    elif getattr(output_item, "type", None) == "computer_call":
                        call_id = getattr(output_item, "call_id", "")
                        stream_event = ToolUseStartEvent(
                            id=call_id, name=OPENAI_COMPUTER_TOOL_NAME
                        )

                # Function call arguments streaming
                elif sse_event.type == "response.function_call_arguments.delta":
                    # Delta events identify the tool call by item_id (the item's
                    # own id), not call_id. Look up the matching entry.
                    delta_item_id = getattr(sse_event, "item_id", "")
                    if delta_item_id in tool_calls_in_progress:
                        tool_calls_in_progress[delta_item_id]["arguments"] += sse_event.delta
                    stream_event = ToolInputDeltaEvent(partial_json=sse_event.delta)

                # Response completed -- extract usage and final status
                elif sse_event.type == "response.completed":
                    completed_response = sse_event.response
                    response_model = getattr(completed_response, "model", self._model)
                    response_status = getattr(completed_response, "status", "completed")

                    # Extract usage from the completed response
                    completed_usage = getattr(completed_response, "usage", None)
                    if completed_usage:
                        usage = _api_usage_from_response_usage(completed_usage)

                    # Computer calls do not stream JSON arguments the same way
                    # function tools do. Parse them from the completed response.
                    _, completed_native_calls, completed_thinking = self._parse_output_items(
                        getattr(completed_response, "output", []),
                        include_messages=False,
                        include_function_calls=False,
                        include_computer_calls=True,
                    )
                    native_tool_calls.extend(completed_native_calls)
                    thinking_blocks.extend(completed_thinking)

                # Dispatch the event to the callback
                if stream_event and on_event:
                    callback_result = on_event(stream_event)
                    if asyncio.iscoroutine(callback_result):
                        await callback_result

            # Build tool call responses from accumulated arguments
            tool_call_responses: list[ToolCallResponse] | None = None
            if tool_calls_in_progress or native_tool_calls:
                tool_call_responses = []
                for tool_call_data in tool_calls_in_progress.values():
                    try:
                        parsed_args = (
                            json.loads(tool_call_data["arguments"])
                            if tool_call_data["arguments"]
                            else {}
                        )
                    except json.JSONDecodeError:
                        parsed_args = {}
                    tool_call_responses.append(
                        ToolCallResponse(
                            id=tool_call_data["call_id"],
                            name=tool_call_data["name"],
                            arguments=parsed_args,
                        )
                    )
                tool_call_responses.extend(native_tool_calls)

            # Determine stop reason
            stop_reason = "end_turn"
            if tool_call_responses:
                stop_reason = "tool_use"
            elif response_status == "incomplete":
                stop_reason = "max_tokens"

            return ProviderResponse(
                content="".join(text_parts),
                tool_calls=tool_call_responses,
                usage=usage,
                stop_reason=stop_reason,
                model=response_model,
                thinking_blocks=thinking_blocks if thinking_blocks else None,
            )

        except OpenAIAuthError as auth_error:
            raise AuthenticationError(str(auth_error)) from auth_error
        except APIStatusError as status_error:
            raise self._map_api_status_error(status_error) from status_error
        except APIError as api_error:
            raise ProviderError(str(api_error), retryable=True) from api_error

    def _map_api_status_error(self, error: APIStatusError) -> ProviderError:
        """Map an OpenAI APIStatusError to the appropriate internal error type.

        Parameters
        ----------
        error : APIStatusError
            The SDK exception with HTTP status code and message.

        Returns
        -------
        ProviderError
            The mapped internal error (AuthenticationError, RateLimitError, etc.).
        """
        error_message = str(error)
        status_code = error.status_code

        if status_code == 401:
            return AuthenticationError(error_message)
        elif status_code == 402:
            return BillingError(error_message)
        elif status_code == 429:
            # Cap retry_after so the runner does a quick retry then falls
            # through to the fallback chain instead of blocking.
            return RateLimitError(error_message, retry_after=5.0)
        elif status_code == 404:
            return ModelNotFoundError(error_message)
        elif status_code == 400:
            lower_message = error_message.lower()
            context_overflow_keywords = ("context", "token", "too long", "too large", "exceeds")
            if any(keyword in lower_message for keyword in context_overflow_keywords):
                return ContextOverflowError(error_message)
            return ProviderError(error_message, status=status_code, retryable=False)
        elif status_code >= 500:
            return ProviderError(error_message, status=status_code, retryable=True)
        else:
            return ProviderError(error_message, status=status_code, retryable=False)

    async def close(self) -> None:
        """Close the sync and async OpenAI clients.

        Should be called when the provider is no longer needed to
        release connection resources.
        """
        await self._async_client.close()
        self._client.close()


def create_openai_provider(
    api_key: str | None = None,
    model: str = "gpt-5.4",
    reasoning: ThinkingConfig | None = None,
    **kwargs: Any,
) -> OpenAIProvider:
    """Convenience factory to create an OpenAI provider.

    Parameters
    ----------
    api_key : str or None
        OpenAI API key. Defaults to the OPENAI_API_KEY environment variable.
    model : str
        Model identifier. Defaults to "gpt-5.4".
    reasoning : ThinkingConfig or None
        Reasoning configuration. If None, reasoning is disabled.
    **kwargs
        Additional keyword arguments passed to OpenAIConfig.

    Returns
    -------
    OpenAIProvider
        A configured OpenAI provider instance.
    """
    config = OpenAIConfig(
        api_key=api_key,
        model=model,
        reasoning=reasoning or ThinkingConfig(),
        **kwargs,
    )
    return OpenAIProvider(config)
