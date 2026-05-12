from bridge.freyja_bridge import _build_user_message_with_attachments, _thinking_config_for_model
from engine.anthropic_provider import AnthropicConfig, AnthropicProvider
from engine.openai_provider import OpenAIConfig, OpenAIProvider
from engine.provider_native import OPENAI_COMPUTER_KIND, OPENAI_COMPUTER_TOOL_NAME
from engine.tools import ToolDefinition
from engine.types import ImageBlock, Message, TextBlock, ThinkingConfig, ToolCall


def test_bridge_preserves_pasted_image_attachments_as_image_blocks() -> None:
    message = _build_user_message_with_attachments(
        "use this reference",
        [
            {
                "type": "image",
                "mimeType": "image/jpeg",
                "dataBase64": "data:image/jpeg;base64, aW1hZ2U= ",
            },
            {"type": "image", "mimeType": "image/png", "dataBase64": ""},
        ],
    )

    assert isinstance(message, list)
    assert len(message) == 2
    assert isinstance(message[0], ImageBlock)
    assert message[0].source_type == "base64"
    assert message[0].media_type == "image/jpeg"
    assert message[0].data == "aW1hZ2U="
    assert isinstance(message[1], TextBlock)
    assert message[1].text == "use this reference"


def test_bridge_adds_image_ref_note_when_available() -> None:
    message = _build_user_message_with_attachments(
        "use this reference",
        [{"type": "image", "mimeType": "image/png", "dataBase64": "aW1hZ2U="}],
        "Image references available to tools: `img_001`, `latest_user_image`.",
    )

    assert isinstance(message, list)
    assert isinstance(message[1], TextBlock)
    assert message[1].text == (
        "use this reference\n\n"
        "[Image references available to tools: `img_001`, `latest_user_image`.]"
    )


def test_bridge_keeps_plain_text_when_no_valid_image_attachments() -> None:
    message = _build_user_message_with_attachments(
        "plain prompt",
        [{"type": "image", "mimeType": "image/png", "dataBase64": ""}],
    )

    assert message == "plain prompt"


def test_openai_provider_formats_image_blocks_for_responses_api() -> None:
    provider = OpenAIProvider(OpenAIConfig(api_key="test-key", model="gpt-5.5"))
    content = _build_user_message_with_attachments(
        "what is in this image?",
        [{"type": "image", "mimeType": "image/png", "dataBase64": "aW1hZ2U="}],
    )

    params = provider.build_request_params([Message(role="user", content=content)])

    content_blocks = params["input"][0]["content"]
    assert content_blocks == [
        {"type": "input_image", "image_url": "data:image/png;base64,aW1hZ2U="},
        {"type": "input_text", "text": "what is in this image?"},
    ]


def test_openai_provider_sends_explicit_reasoning_none() -> None:
    provider = OpenAIProvider(
        OpenAIConfig(
            api_key="test-key",
            model="gpt-5.5",
            reasoning=ThinkingConfig(enabled=False, effort="none"),
        )
    )

    params = provider.build_request_params([Message(role="user", content="quick answer")])

    assert params["reasoning"]["effort"] == "none"
    assert "include" not in params


def test_openai_provider_advertises_native_computer_tool_for_gpt55() -> None:
    provider = OpenAIProvider(OpenAIConfig(api_key="test-key", model="gpt-5.5"))
    tools = [
        ToolDefinition(name="screenshot", summary="screen", description="screen"),
        ToolDefinition(name="click", summary="click", description="click"),
        ToolDefinition(name="browser_screenshot", summary="browser", description="browser"),
        ToolDefinition(name="grep", summary="grep", description="grep"),
    ]

    params = provider.build_request_params(
        [Message(role="user", content="use the screen")],
        tools=tools,
    )

    assert {"type": "computer"} in params["tools"]
    function_names = [
        tool["name"] for tool in params["tools"] if tool["type"] == "function"
    ]
    assert "grep" in function_names
    assert "screenshot" not in function_names
    assert "click" not in function_names
    assert "browser_screenshot" not in function_names


def test_openai_provider_round_trips_native_computer_screenshot_output() -> None:
    provider = OpenAIProvider(OpenAIConfig(api_key="test-key", model="gpt-5.5"))
    actions = [{"type": "screenshot"}]
    safety = [{"id": "safe-1", "code": "demo", "message": "ack"}]
    messages = [
        Message(role="user", content="look"),
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(
                    id="cu-call-1",
                    name=OPENAI_COMPUTER_TOOL_NAME,
                    arguments={"actions": actions},
                    provider_kind=OPENAI_COMPUTER_KIND,
                    provider_data={
                        "id": "item-1",
                        "status": "in_progress",
                        "actions": actions,
                        "pending_safety_checks": safety,
                    },
                )
            ],
        ),
        Message(
            role="tool_result",
            tool_call_id="cu-call-1",
            content=[
                TextBlock(text="captured"),
                ImageBlock.from_base64("aW1hZ2U=", "image/png"),
            ],
        ),
    ]

    params = provider.build_request_params(messages)

    computer_call = params["input"][1]
    computer_output = params["input"][2]
    assert computer_call["type"] == "computer_call"
    assert computer_call["id"] == "item-1"
    assert computer_call["actions"] == actions
    assert computer_output["type"] == "computer_call_output"
    assert computer_output["call_id"] == "cu-call-1"
    assert computer_output["acknowledged_safety_checks"] == safety
    assert computer_output["output"] == {
        "type": "computer_screenshot",
        "image_url": "data:image/png;base64,aW1hZ2U=",
        "detail": "original",
    }


def test_bridge_maps_provider_reasoning_levels_to_thinking_config() -> None:
    openai_minimal = _thinking_config_for_model("gpt-5.5", "minimal")
    assert openai_minimal.enabled is True
    assert openai_minimal.effort == "minimal"

    claude_none = _thinking_config_for_model("claude-sonnet-4-6", "none")
    assert claude_none.enabled is False
    assert claude_none.effort == "none"


def test_anthropic_provider_formats_image_blocks_for_claude_messages_api() -> None:
    provider = AnthropicProvider(
        AnthropicConfig(api_key="test-key", model="claude-opus-4-7")
    )
    content = _build_user_message_with_attachments(
        "what is in this image?",
        [{"type": "image", "mimeType": "image/png", "dataBase64": "aW1hZ2U="}],
    )

    params = provider._build_request([Message(role="user", content=content)])

    content_blocks = params["messages"][0]["content"]
    assert content_blocks == [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/png",
                "data": "aW1hZ2U=",
            },
        },
        {"type": "text", "text": "what is in this image?"},
    ]


def test_anthropic_provider_classifies_oversized_image_as_image_too_large() -> None:
    """The 5 MB-per-image limit shares the word 'exceeds' with context
    overflow errors. The provider must route it to the dedicated
    ``ImagePayloadTooLargeError`` so the runner prunes the offending
    block instead of pointlessly running summarization."""
    from engine.providers import ContextOverflowError, ImagePayloadTooLargeError

    cfg = AnthropicConfig(api_key="test-key", model="claude-opus-4-7")
    provider = AnthropicProvider(cfg)

    msg = (
        "Error code: 400 - {'type': 'error', 'error': {'type': "
        "'invalid_request_error', 'message': 'messages.2.content.1."
        "tool_result.content.1.image.source.base64: image exceeds 5 MB "
        "maximum: 5433096 bytes > 5242880 bytes'}}"
    )

    class _StubAPIError(Exception):
        """Minimal stand-in for anthropic's APIStatusError so we can
        drive _convert_api_error without hitting the real SDK."""
        def __init__(self, m: str) -> None:
            super().__init__(m)
            self.status_code = 400

        def __str__(self) -> str:
            return self.args[0]

    translated = provider._convert_api_error(_StubAPIError(msg))
    assert isinstance(translated, ImagePayloadTooLargeError)
    assert not isinstance(translated, ContextOverflowError)
    assert translated.max_bytes == 5242880

    # And a generic context-window error should still classify as overflow.
    overflow_msg = (
        "Error code: 400 - prompt is too long: 1100000 tokens > 1000000"
    )
    overflow = provider._convert_api_error(_StubAPIError(overflow_msg))
    assert isinstance(overflow, ContextOverflowError)
    assert not isinstance(overflow, ImagePayloadTooLargeError)


def test_transcript_prune_oversized_images_replaces_only_offending_blocks() -> None:
    """``prune_oversized_images`` should rewrite *only* the oversized
    base64 block(s), leave under-limit images alone, and append a clear
    text marker so the model still sees an explanation."""
    from engine.session import TranscriptManager
    from engine.types import Message, TextBlock

    tm = TranscriptManager()
    small_data = "a" * 1024  # ~768 bytes binary
    big_data = "a" * 200_000  # ~150 KB binary
    tm.append_message(Message(role="assistant", content=[
        TextBlock(text="here are two images"),
        ImageBlock(source_type="base64", data=small_data, media_type="image/png"),
        ImageBlock(source_type="base64", data=big_data, media_type="image/png"),
    ]))

    stats = tm.prune_oversized_images(max_bytes=100_000)
    assert stats.changed
    assert stats.omitted_images == 1
    assert stats.modified_messages == 1

    msg = tm.get_messages()[0]
    assert isinstance(msg.content, list)
    image_blocks = [b for b in msg.content if isinstance(b, ImageBlock)]
    assert len(image_blocks) == 1
    assert image_blocks[0].data == small_data  # the small one survived
    text_blocks = [b for b in msg.content if isinstance(b, TextBlock)]
    assert any("omitted from model history" in b.text for b in text_blocks)


def test_compactor_allows_short_but_heavy_sessions() -> None:
    """The old strict ``len(messages) >= 20`` floor blocked manual
    compaction on heavy short sessions (a single huge image + a few
    replies). The new gate uses a token-size check so the user-pressed
    compact button has a chance to actually run."""
    from engine.compaction import SummaryCompaction
    from engine.session import TranscriptManager
    from engine.types import Message, TextBlock

    tm = TranscriptManager()
    # 11 messages — under the old MIN_MESSAGES_TO_COMPACT=20 floor.
    # Message 0 carries enough text to clear the MIN_TOKENS_TO_SUMMARIZE
    # threshold once it's split out from the kept tail.
    tm.append_message(Message(role="user", content="x" * 20_000))
    for _ in range(10):
        tm.append_message(Message(role="assistant", content="ok"))

    compactor = SummaryCompaction()

    class _FakeProvider:
        def complete(self, *args, **kwargs):
            class _R:
                content = "<summary>compressed</summary>"
                tool_calls: list = []
                usage = None
            return _R()

    result = compactor.compact(tm, _FakeProvider())
    # The gate should pass; whether the fake summary parse succeeds
    # downstream is a separate concern — at minimum we expect the
    # "Not enough messages" sentinel to be gone.
    assert (result.error or "") != "Not enough messages to compact"


def test_openai_provider_normalizes_cached_tokens_out_of_input() -> None:
    """OpenAI reports ``input_tokens`` as the *total* prompt token count
    with ``cached_tokens`` as a subset. The rest of the codebase
    (compute_cost, usage.UsageStats.total_tokens) assumes the disjoint
    Anthropic convention. The provider must subtract the cached slice
    from ``input_tokens`` before producing an ``APIUsage`` so cost isn't
    double-counted on cache hits."""
    from engine.openai_provider import _api_usage_from_response_usage

    class _InputDetails:
        cached_tokens = 194_000

    class _OutputDetails:
        reasoning_tokens = 320

    class _Usage:
        input_tokens = 195_000   # total — includes cached
        output_tokens = 57
        input_tokens_details = _InputDetails()
        output_tokens_details = _OutputDetails()

    usage = _api_usage_from_response_usage(_Usage())
    assert usage.input_tokens == 1_000  # 195k - 194k cached
    assert usage.cache_read_tokens == 194_000
    assert usage.output_tokens == 57
    assert usage.reasoning_tokens == 320

    # No cache hit → identity.
    class _UsageNoCache:
        input_tokens = 1_000
        output_tokens = 50
        input_tokens_details = None
        output_tokens_details = None

    plain = _api_usage_from_response_usage(_UsageNoCache())
    assert plain.input_tokens == 1_000
    assert plain.cache_read_tokens == 0


def test_openai_normalization_yields_correct_cost_against_anthropic_semantics() -> None:
    """End-to-end: after normalization, ``compute_cost`` agrees with the
    intuitive billing for OpenAI cache hits.

    Pre-fix: 195k input + 194k cache_read both billed at 5.0 / 0.50 per
    1M = ~$1.07 (10× too high). Post-fix: 1k fresh + 194k cache_read =
    ~$0.10."""
    from engine.openai_provider import _api_usage_from_response_usage
    from engine.providers import compute_cost

    class _InputDetails:
        cached_tokens = 194_000

    class _Usage:
        input_tokens = 195_000
        output_tokens = 57
        input_tokens_details = _InputDetails()
        output_tokens_details = None

    usage = _api_usage_from_response_usage(_Usage())
    cost = compute_cost(
        "gpt-5.5",
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=usage.cache_read_tokens,
    )
    # gpt-5.5 pricing: (5.0, 15.0, 0.50) per 1M.
    # Expected: 1k * 5 + 57 * 15 + 194k * 0.5 = 5000 + 855 + 97000 = ~$0.10286
    assert cost is not None
    assert 0.09 < cost < 0.12, f"expected ~$0.10, got ${cost:.4f}"


def test_iterative_compaction_extends_previous_summary() -> None:
    """Second compaction in a session takes the iterative update path.

    A fresh session uses the SUMMARY_PROMPT; once a compaction entry
    lives in the transcript the next compaction switches to
    SUMMARY_UPDATE_PROMPT so the model extends instead of re-derives.
    The CompactionResult exposes ``resumed_from_previous`` so callers
    can show this in telemetry / dashboards."""
    from engine.compaction import SummaryCompaction
    from engine.session import TranscriptManager
    from engine.types import Message

    tm = TranscriptManager()
    # Enough mass to clear the new size-based gate (~20k chars on one
    # message → ~5k tokens; threshold is 2k).
    tm.append_message(Message(role="user", content="x" * 20_000))
    for _ in range(10):
        tm.append_message(Message(role="assistant", content="ok"))

    compactor = SummaryCompaction()

    class _FakeProvider:
        def __init__(self):
            self.prompts: list[str] = []
        def complete(self, *args, **kwargs):
            prompt = kwargs.get("messages", [])[0].content
            self.prompts.append(prompt)
            class _R:
                content = "<summary>fake compressed summary that mentions x" * 5 + "</summary>"
                tool_calls: list = []
                usage = None
            return _R()

    p1 = _FakeProvider()
    res1 = compactor.compact(tm, p1)
    assert res1.success
    assert res1.resumed_from_previous is False
    assert "PREVIOUS SUMMARY:" not in p1.prompts[0]

    # Add more messages, compact again — should use the UPDATE prompt.
    for _ in range(15):
        tm.append_message(Message(role="user", content="y" * 4_000))
        tm.append_message(Message(role="assistant", content="z" * 4_000))
    p2 = _FakeProvider()
    res2 = compactor.compact(tm, p2)
    assert res2.success
    assert res2.resumed_from_previous is True, "expected iterative path"
    assert "PREVIOUS SUMMARY:" in p2.prompts[0]


def test_pinned_entry_excluded_from_compaction() -> None:
    """A pinned (compaction_excluded=True) entry must stay in the kept
    tail. The compactor pulls split_point back to before the earliest
    pin, even when that means summarizing nothing — F1 from the doc."""
    from engine.compaction import SummaryCompaction
    from engine.session import TranscriptManager
    from engine.types import Message

    tm = TranscriptManager()
    # 30 messages so split_point would naturally be ~20.
    for i in range(30):
        if i % 2 == 0:
            tm.append_message(Message(role="user", content=f"u{i} " + "x" * 1000))
        else:
            tm.append_message(Message(role="assistant", content=f"a{i} " + "y" * 1000))

    # Pin entry at message index 2 — well inside the to_summarize range.
    target = tm.entries[2]
    tm.set_entry_pinned(target.id, True)
    assert target.compaction_excluded is True

    compactor = SummaryCompaction()
    # split_point should fold back to index 2, leaving messages 0,1 in
    # the to_summarize range (or skipping entirely if that's empty after
    # safe-split walks). Either way the pinned entry must NOT land in
    # to_summarize.
    messages = tm.get_messages()
    split_point = len(messages) - compactor.keep_recent
    split_point = compactor._find_safe_split(messages, split_point)  # noqa: SLF001
    split_point = compactor._honor_pins(tm, split_point)  # noqa: SLF001
    assert split_point <= 2, (
        f"expected split_point ≤ pin index (2), got {split_point}"
    )
