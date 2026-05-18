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


def test_honor_pins_after_prior_compaction_no_off_by_one() -> None:
    """After a prior compaction, get_messages injects a sys_summary
    pseudo-message at messages[0]. The pin walker must account for it,
    else any pin near the start blocks compaction entirely (Gap D)."""
    from engine.compaction import SummaryCompaction
    from engine.session import TranscriptManager
    from engine.types import Message

    tm = TranscriptManager()
    # Build a transcript: 5 messages, then a compaction-summary entry,
    # then 25 more messages. Pin the FIRST message after the compaction.
    for i in range(5):
        tm.append_message(Message(role="user", content=f"early u{i} " + "x" * 800))
    tm.append_compaction(
        summary="prior summary covering the first five user turns",
        first_kept_id="",
        tokens_before=10_000,
    )
    # The append_compaction with empty first_kept_id wipes everything
    # — but post-call, the transcript has just the compaction entry.
    # Re-add 25 messages.
    for i in range(25):
        tm.append_message(Message(role="user", content=f"new u{i} " + "y" * 800))

    # Pin the FIRST real message after the compaction (most-likely
    # bug-trigger case).
    target_entry = next(e for e in tm.entries if e.message is not None)
    tm.set_entry_pinned(target_entry.id, True)

    compactor = SummaryCompaction()
    messages = tm.get_messages()
    # messages[0] = sys_summary inject, messages[1] = the pinned entry
    assert messages[0].role == "system"
    split_point = len(messages) - compactor.keep_recent
    split_point = compactor._find_safe_split(messages, split_point)  # noqa: SLF001
    pinned_split = compactor._honor_pins(tm, split_point)  # noqa: SLF001
    # With the fix, pinned_split == 1 (the pin is at messages[1]).
    # Without the fix it would be 0 (the bug).
    assert pinned_split == 1, (
        f"expected pinned_split=1 (pin at messages[1] after sys_summary inject), "
        f"got {pinned_split}"
    )


def test_iterative_path_strips_sys_summary_inject_from_to_summarize() -> None:
    """The second compaction must not feed the prior summary to the
    summarizer twice (once via SUMMARY_UPDATE_PROMPT's PREVIOUS SUMMARY
    section, once via the injected sys_summary message in to_summarize)
    — Gap F."""
    from engine.compaction import SummaryCompaction
    from engine.session import TranscriptManager
    from engine.types import Message

    tm = TranscriptManager()
    # Seed a transcript and immediately add a prior compaction entry.
    for i in range(15):
        tm.append_message(Message(role="user", content=f"early {i} " + "x" * 200))
    tm.append_compaction(
        summary="UNIQUE_PRIOR_SUMMARY_MARKER_42",
        first_kept_id="",
        tokens_before=8_000,
    )
    # Add fresh content to summarize.
    for i in range(15):
        tm.append_message(Message(role="user", content=f"fresh {i} " + "z" * 4_000))

    compactor = SummaryCompaction()

    class _CapturingProvider:
        def __init__(self):
            self.prompt_text = ""
        def complete(self, *args, **kwargs):
            self.prompt_text = kwargs["messages"][0].content
            class _R:
                content = "<summary>updated summary text</summary>"
                tool_calls: list = []
                usage = None
            return _R()

    provider = _CapturingProvider()
    res = compactor.compact(tm, provider)
    assert res.success and res.resumed_from_previous

    # The prior summary appears once — in the PREVIOUS SUMMARY: section
    # of the update prompt. It must NOT appear a second time in the
    # NEW TURNS section.
    prior_marker = "UNIQUE_PRIOR_SUMMARY_MARKER_42"
    occurrences = provider.prompt_text.count(prior_marker)
    assert occurrences == 1, (
        f"prior summary should appear exactly once in the iterative "
        f"prompt; found {occurrences} occurrence(s)"
    )


def test_session_on_message_appended_callback_fires_for_every_message() -> None:
    """The Session.create on_message_appended hook is what drives the
    bridge's raw_messages.jsonl log. Must fire for every append, never
    for compaction entries (those go through append_compaction)."""
    from engine.session import Session
    from engine.types import Message

    seen: list[Message] = []
    sess = Session.create(
        system_prompt="",
        on_message_appended=lambda m: seen.append(m),
    )
    sess.add_user_message("hello")
    sess.add_assistant_message("hi back")
    sess.transcript.append_compaction(
        summary="should NOT trigger callback",
        first_kept_id="",
        tokens_before=10,
    )
    sess.add_user_message("after compaction")

    # 3 message appends; compaction skipped.
    assert len(seen) == 3
    assert seen[0].role == "user" and seen[0].content == "hello"
    assert seen[1].role == "assistant"
    assert seen[2].content == "after compaction"


def test_preserve_facts_auto_repair_appends_missing() -> None:
    """If the summarizer paraphrases a preserve_facts entry, the tool
    auto-appends a Preserved Facts section to the just-written summary
    so the verbatim string is literally present (Gap E)."""
    from bridge.tools.summarize_context_tool import SummarizeContextTool
    from engine.compaction import SummaryCompaction
    from engine.session import Session, TranscriptManager
    from engine.types import Message
    import asyncio

    tm = TranscriptManager()
    for i in range(15):
        tm.append_message(Message(role="user", content=f"u{i} " + "x" * 2_000))

    class _SuperficialProvider:
        model_id = "fake"
        def complete(self, *args, **kwargs):
            class _R:
                # Crucially does NOT contain the api_key string.
                content = "<summary>generic summary without secrets</summary>"
                tool_calls: list = []
                usage = None
            return _R()

    session = Session(id="test-sess", transcript=tm, system_prompt="")
    provider = _SuperficialProvider()
    compactor = SummaryCompaction()
    tool = SummarizeContextTool(
        get_session=lambda: session,
        get_provider=lambda: provider,
        get_compactor=lambda: compactor,
        on_summarize_call=None,
        get_current_pressure_pct=None,
    )

    secret = "api_key=SECRET_TOKEN_DEADBEEF"
    result = asyncio.new_event_loop().run_until_complete(
        tool.execute("call-1", {
            "scope": "all",
            "preserve_facts": [secret],
            "reason": "testing preserve_facts repair",
        })
    )
    # Tool result reports the auto-repair.
    assert not result.is_error
    assert "preserve_facts_missing" in result.content
    # The summary in the transcript now contains the verbatim string.
    last_compaction = next(
        e for e in reversed(tm.entries) if e.is_compaction
    )
    assert secret in last_compaction.compaction_summary


def test_channel2_pressure_note_attaches_to_last_user_message_not_system() -> None:
    """Channel 2 must not touch the system prompt — that would invalidate
    Anthropic's cache_control breakpoint on the static system block
    every band crossing. Instead the note tail-appends to the last
    user-role message, preserving the cached prefix."""
    from engine.runner import AsyncAgentRunner
    from engine.types import Message, TextBlock

    class _StubProvider:
        name = "stub"
        model_id = "fake"
        context_window = 100_000

    class _StubConfig:
        max_tokens_per_turn = 4_000

    class _StubUsage:
        def effective_context_tokens(self) -> int:
            return 50_000  # 50% of effective window (96k) → soft band

    runner = AsyncAgentRunner.__new__(AsyncAgentRunner)
    runner.provider = _StubProvider()
    runner.fallback_chain = None
    runner.config = _StubConfig()
    runner.usage = _StubUsage()
    runner._turn_start_pressure_band = None  # noqa: SLF001

    base_system = "You are a helpful assistant. " + ("x" * 500)
    messages = [
        Message(role="user", content="initial prompt"),
        Message(role="assistant", content="ack"),
        Message(role="user", content=[TextBlock(text="follow-up question")]),
    ]
    augmented = runner._augment_messages_with_pressure_note(messages)  # noqa: SLF001

    # System prompt is untouched (no Channel 2 method anymore for it).
    # Verify the augmentation only affected the last message.
    assert len(augmented) == len(messages)
    assert augmented[0] is messages[0]
    assert augmented[1] is messages[1]
    # Last message is a CLONE, not the original — transcript stays clean.
    assert augmented[-1] is not messages[-1]
    # Content is a list with the original block plus a new TextBlock
    # containing the pressure note.
    last_blocks = augmented[-1].content
    assert isinstance(last_blocks, list)
    assert len(last_blocks) == 2
    assert isinstance(last_blocks[1], TextBlock)
    # New framing: <system-reminder> wrapper that Anthropic-family
    # models are trained to attend to as runtime guidance (not user
    # voice, not assistant thought).
    assert "<system-reminder>" in last_blocks[1].text
    assert "</system-reminder>" in last_blocks[1].text
    assert "50-59%" in last_blocks[1].text  # quantized band
    # Original list reference is unmodified.
    assert len(messages[-1].content) == 1


def test_channel2_no_op_below_soft_band() -> None:
    """Below 40% the note must not attach — keeps awareness band quiet."""
    from engine.runner import AsyncAgentRunner
    from engine.types import Message

    class _StubProvider:
        name = "stub"
        model_id = "fake"
        context_window = 100_000

    class _StubConfig:
        max_tokens_per_turn = 4_000

    class _StubUsage:
        def effective_context_tokens(self) -> int:
            return 20_000  # ~20% — below 40% soft threshold

    runner = AsyncAgentRunner.__new__(AsyncAgentRunner)
    runner.provider = _StubProvider()
    runner.fallback_chain = None
    runner.config = _StubConfig()
    runner.usage = _StubUsage()
    runner._turn_start_pressure_band = None  # noqa: SLF001

    messages = [Message(role="user", content="hi")]
    augmented = runner._augment_messages_with_pressure_note(messages)
    assert augmented is messages  # same list, untouched


def test_summarize_context_system_event_carries_full_summary_text() -> None:
    """The compaction_complete system event must include the FULL
    summary text in details so the in-session ActivityView's expandable
    compaction card and the dashboard's clickable compaction-log rows
    can render the actual content (not just a 240-char excerpt)."""
    from bridge.tools.summarize_context_tool import SummarizeContextTool
    from engine.compaction import SummaryCompaction
    from engine.session import Session, TranscriptManager
    from engine.types import Message
    import asyncio

    tm = TranscriptManager()
    for i in range(15):
        tm.append_message(Message(role="user", content=f"u{i} " + "x" * 2_000))

    # Summarizer that returns a long, distinctive summary so we can
    # verify the FULL text (not just the first 240 chars) made it into
    # the system event details.
    long_summary = "<summary>" + ("A" * 1_500) + " UNIQUE_TAIL_MARKER_77 </summary>"

    class _Provider:
        model_id = "fake"
        def complete(self, *args, **kwargs):
            class _R:
                content = long_summary
                tool_calls: list = []
                usage = None
            return _R()

    session = Session(id="test-full-summary", transcript=tm, system_prompt="")
    compactor = SummaryCompaction()
    events: list[dict] = []
    tool = SummarizeContextTool(
        get_session=lambda: session,
        get_provider=lambda: _Provider(),
        get_compactor=lambda: compactor,
        on_system_event=lambda e: events.append(e),
    )
    asyncio.new_event_loop().run_until_complete(
        tool.execute("call-full", {"scope": "all", "reason": "test"})
    )
    complete = next(e for e in events if e.get("subtype") == "compaction_complete")
    details = complete["details"]
    # Excerpt is capped, but summary_text must be the FULL produced text.
    assert "summary_excerpt" in details
    assert len(details["summary_excerpt"]) <= 240
    assert "summary_text" in details
    assert "UNIQUE_TAIL_MARKER_77" in details["summary_text"]
    assert len(details["summary_text"]) > 1_000
    # Other detail fields the renderer expects.
    assert details["scope"] == "all"
    assert details["trigger"] == "agent_summarize_context"
    assert details["reason"] == "test"


def test_summarize_context_emits_system_event_on_success() -> None:
    """Agent-driven compactions must surface as a compaction_start +
    compaction_complete pair so the conversation timeline shows the
    same inline marker the runtime path produces (Gap N)."""
    from bridge.tools.summarize_context_tool import SummarizeContextTool
    from engine.compaction import SummaryCompaction
    from engine.session import Session, TranscriptManager
    from engine.types import Message
    import asyncio

    tm = TranscriptManager()
    for i in range(15):
        tm.append_message(Message(role="user", content=f"u{i} " + "x" * 2_000))

    class _Provider:
        model_id = "fake"
        def complete(self, *args, **kwargs):
            class _R:
                content = "<summary>fake summary content</summary>"
                tool_calls: list = []
                usage = None
            return _R()

    session = Session(id="test-emit", transcript=tm, system_prompt="")
    compactor = SummaryCompaction()
    events: list[dict] = []
    tool = SummarizeContextTool(
        get_session=lambda: session,
        get_provider=lambda: _Provider(),
        get_compactor=lambda: compactor,
        on_system_event=lambda e: events.append(e),
    )
    result = asyncio.new_event_loop().run_until_complete(
        tool.execute("call-N", {"scope": "all"})
    )
    assert not result.is_error
    subtypes = [e.get("subtype") for e in events]
    assert "compaction_start" in subtypes
    assert "compaction_complete" in subtypes
    complete = next(e for e in events if e.get("subtype") == "compaction_complete")
    assert complete["details"]["trigger"] == "agent_summarize_context"


def test_summarize_context_pin_entries_notifies_renderer() -> None:
    """Agent pin_entries fires an entry_pin_changed event for each
    successfully-pinned ordinal so the renderer's pin badge appears
    without a session reload (Gap M)."""
    from bridge.tools.summarize_context_tool import SummarizeContextTool
    from engine.compaction import SummaryCompaction
    from engine.session import Session, TranscriptManager
    from engine.types import Message
    import asyncio

    tm = TranscriptManager()
    for i in range(20):
        tm.append_message(Message(role="user", content=f"u{i} " + "z" * 1_000))

    class _Provider:
        model_id = "fake"
        def complete(self, *args, **kwargs):
            class _R:
                content = "<summary>fake</summary>"
                tool_calls: list = []
                usage = None
            return _R()

    session = Session(id="test-pin", transcript=tm, system_prompt="")
    compactor = SummaryCompaction()
    pin_events: list[dict] = []
    tool = SummarizeContextTool(
        get_session=lambda: session,
        get_provider=lambda: _Provider(),
        get_compactor=lambda: compactor,
        on_pin_changed=lambda e: pin_events.append(e),
    )
    asyncio.new_event_loop().run_until_complete(
        tool.execute("call-pin", {
            "scope": "all",
            "pin_entries": [3, 5, 18],
        })
    )
    pinned_ords = {e["messageOrdinal"] for e in pin_events}
    # The compactor's pinning step may not pin every requested ord if
    # the transcript shape doesn't match (e.g. ordinals beyond range).
    # Here all are valid → all three fired.
    assert pinned_ords == {3, 5, 18}
    for e in pin_events:
        assert e["pinned"] is True
        assert e["source"] == "agent_summarize_context"


def test_compaction_result_carries_summary_tokens_and_summarizer_stats() -> None:
    """Gap 7 + Gap 4: CompactionResult exposes a summary_tokens
    estimate AND the summarizer call's own input/output/duration/cost
    so the bridge can mirror them to telemetry. Without this the
    summarizer's spend is invisible AND the dashboard can't forecast
    future prompt-prefix size."""
    from engine.compaction import SummaryCompaction
    from engine.session import TranscriptManager
    from engine.providers import APIUsage
    from engine.types import Message

    tm = TranscriptManager()
    for i in range(15):
        tm.append_message(Message(role="user", content=f"u{i} " + "x" * 2_000))

    class _StubResponse:
        def __init__(self, text: str, in_tok: int, out_tok: int):
            self.content = text
            self.usage = APIUsage(
                input_tokens=in_tok,
                output_tokens=out_tok,
                cache_read_tokens=0,
                cache_write_tokens=0,
            )
            self.model = "claude-opus-4-7"
            self.stop_reason = "end_turn"

    class _Provider:
        name = "anthropic"
        model_id = "claude-opus-4-7"
        def complete(self, *args, **kwargs):
            return _StubResponse(
                "<summary>" + ("A" * 800) + "</summary>",
                in_tok=12_345,
                out_tok=2_222,
            )

    compactor = SummaryCompaction()
    seen: list[dict] = []
    result = compactor.compact(
        tm, _Provider(), on_summarizer_call=seen.append,
    )
    assert result.success
    # Gap 7: summary_tokens estimate.
    assert result.summary_tokens > 0
    assert result.summary_tokens == len(result.summary) // 4
    # Gap 4: summarizer telemetry on result.
    assert result.summarizer_input_tokens == 12_345
    assert result.summarizer_output_tokens == 2_222
    assert result.summarizer_model == "claude-opus-4-7"
    assert result.summarizer_cost_usd is not None
    assert result.summarizer_cost_usd > 0
    assert result.summarizer_duration_ms >= 0
    # Callback was fired with the call_kind tag.
    assert len(seen) == 1
    assert seen[0]["call_kind"] == "summarizer"
    assert seen[0]["input_tokens"] == 12_345


def test_compaction_event_details_emit_canonical_pressure_fields() -> None:
    """Gap 5: the runner's canonical details builder emits
    effective_window + pressure_pct_before/after so every compaction
    event downstream uses the same denominator."""
    from engine.compaction import CompactionResult
    from engine.runner import AsyncAgentRunner

    runner = AsyncAgentRunner.__new__(AsyncAgentRunner)

    class _StubProvider:
        context_window = 100_000

    class _StubConfig:
        max_tokens_per_turn = 4_000

    runner.provider = _StubProvider()
    runner.fallback_chain = None
    runner.config = _StubConfig()
    result = CompactionResult(
        success=True,
        summary="x" * 800,
        tokens_before=60_000,
        tokens_after=10_000,
        entries_removed=20,
        summary_tokens=200,
        summarizer_input_tokens=8_000,
        summarizer_output_tokens=400,
        summarizer_cost_usd=0.05,
        summarizer_model="claude-opus-4-7",
    )
    details = runner._compaction_event_details(  # noqa: SLF001
        result, trigger="overflow_cascade",
    )
    # effective_window = 100k - 4k = 96k.
    assert details["effective_window"] == 96_000
    # 60_000 / 96_000 → 62.5%
    assert details["pressure_pct_before"] == 62.5
    # 10_000 / 96_000 → 10.4%
    assert details["pressure_pct_after"] == 10.4
    assert details["summary_tokens"] == 200
    assert details["summarizer_input_tokens"] == 8_000
    assert details["summarizer_cost_usd"] == 0.05
    assert details["trigger"] == "overflow_cascade"


def test_bridge_mirror_helper_writes_compaction_event_telemetry(tmp_path, monkeypatch) -> None:
    """Gap 2: the bridge's _mirror_compaction_event helper writes both
    cross-session compaction.jsonl AND per-session compactions.jsonl —
    so the manual force_compact path (which used to bypass the runner's
    on_system_event mirror) now lands in both stores."""
    import json
    from pathlib import Path
    from bridge.freyja_bridge import _BridgeSession, _BridgeState

    # Redirect HOME so the JSONL writes land in tmp_path.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    state = _BridgeState(workspace=str(tmp_path), default_model="claude-opus-4-7")
    sess = _BridgeSession(
        "test-mirror",
        workspace=str(tmp_path),
        model_id="claude-opus-4-7",
        reasoning_level="auto",
        coordination_strategy="bus",
        state=state,
    )
    sess.agent_type = None
    sess.parent_session_id = None
    sess.current_turn_id = "turn-1"

    sess._mirror_compaction_event(  # noqa: SLF001
        "compaction_complete",
        {
            "trigger": "manual",
            "tokens_before": 50_000,
            "tokens_after": 8_000,
            "summary_text": "Sample produced summary for the test.",
            "summary_tokens": 9,
            "effective_window": 96_000,
            "pressure_pct_before": 52.1,
            "pressure_pct_after": 8.3,
            "summarizer_input_tokens": 5_000,
            "summarizer_output_tokens": 400,
            "summarizer_cost_usd": 0.018,
            "scope": None,
            "reason": None,
            "resumed_from_previous": False,
        },
    )
    # Per-session compactions.jsonl
    per_session_path = (
        tmp_path / ".freyja" / "projects" / "test-mirror" / "compactions.jsonl"
    )
    assert per_session_path.exists(), "per-session compactions log missing"
    rows = [json.loads(line) for line in per_session_path.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["trigger"] == "manual"
    assert rows[0]["summary_text"].startswith("Sample produced")
    assert rows[0]["effective_window"] == 96_000

    # Cross-session compaction.jsonl
    cross_path = tmp_path / ".freyja" / "telemetry" / "compaction.jsonl"
    assert cross_path.exists(), "cross-session compaction telemetry missing"
    rows = [json.loads(line) for line in cross_path.read_text().splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["type"] == "compaction_event"
    assert rows[0]["summary_tokens"] == 9
    assert rows[0]["summarizer_cost_usd"] == 0.018
