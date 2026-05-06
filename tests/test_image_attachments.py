from bridge.freyja_bridge import _build_user_message_with_attachments, _thinking_config_for_model
from engine.anthropic_provider import AnthropicConfig, AnthropicProvider
from engine.openai_provider import OpenAIConfig, OpenAIProvider
from engine.types import ImageBlock, Message, TextBlock, ThinkingConfig


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
