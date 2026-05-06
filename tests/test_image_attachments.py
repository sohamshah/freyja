from bridge.freyja_bridge import _build_user_message_with_attachments
from engine.openai_provider import OpenAIConfig, OpenAIProvider
from engine.types import ImageBlock, Message, TextBlock


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
