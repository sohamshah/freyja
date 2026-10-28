"""generate_image on Google's Nano Banana 2.1 (gemini-nano-banana-2.1).

The model runs through the Gemini API, not OpenAI's Images API, so the tool
picks the key, the size vocabulary and the response parsing by model. Every
test here is hermetic: a fake client stands in for google.genai. What the
fakes mimic was probed live on 2026-10-28: the model answers in image/jpeg,
accepts 1K/2K/4K (not the documented 512px), and takes reference images as
inline parts.
"""

from __future__ import annotations

import base64
import io
from types import SimpleNamespace

import pytest
from PIL import Image

import bridge.tools.image_generation_tool as image_generation_tool
from bridge.tools.image_generation_tool import GenerateImageTool

PNG_1X1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
PNG_1X1 = base64.b64decode(PNG_1X1_B64)
MODEL = "gemini-nano-banana-2.1"


def _jpeg_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buffer, format="JPEG")
    return buffer.getvalue()


JPEG_4X4 = _jpeg_bytes()


@pytest.fixture(autouse=True)
def isolate_image_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test-key")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
    monkeypatch.setenv("FREYJA_IMAGE_OUTPUT_DIR", str(tmp_path / "generated"))
    monkeypatch.delenv("FREYJA_IMAGE_MODEL", raising=False)


def _part(data=PNG_1X1, mime_type="image/png", *, thought=False, text=None):
    inline = SimpleNamespace(data=data, mime_type=mime_type) if data is not None else None
    return SimpleNamespace(inline_data=inline, text=text, thought=thought)


def _response(parts, finish_reason="STOP"):
    return SimpleNamespace(
        candidates=[
            SimpleNamespace(content=SimpleNamespace(parts=parts), finish_reason=finish_reason)
        ]
    )


class FakeGeminiModels:
    def __init__(self, response) -> None:
        self.response = response
        self.kwargs = None

    async def generate_content(self, **kwargs):
        self.kwargs = kwargs
        return self.response


class FakeGeminiClient:
    def __init__(self, response=None) -> None:
        self.models = FakeGeminiModels(response or _response([_part()]))
        self.aio = SimpleNamespace(models=self.models)


class FakeOpenAIImages:
    def __init__(self) -> None:
        self.kwargs = None

    async def generate(self, **kwargs):
        self.kwargs = kwargs
        raise AssertionError("the OpenAI path must not run for a Gemini model")

    edit = generate


class FakeOpenAIClient:
    def __init__(self) -> None:
        self.images = FakeOpenAIImages()


def _tool(response=None):
    gemini = FakeGeminiClient(response)
    openai = FakeOpenAIClient()
    tool = GenerateImageTool(
        client_factory=lambda _key: openai,
        gemini_client_factory=lambda _key: gemini,
    )
    return tool, gemini, openai


# ── routing ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_nano_banana_generates_through_the_gemini_api() -> None:
    tool, gemini, openai = _tool()

    result = await tool.execute(
        "nb",
        {"prompt": "a lighthouse at dusk", "model": MODEL, "size": "1536x864", "quality": "high"},
    )

    assert not result.is_error, result.content
    kwargs = gemini.models.kwargs
    assert kwargs["model"] == MODEL
    assert kwargs["contents"] == ["a lighthouse at dusk"]
    config = kwargs["config"]
    assert "IMAGE" in config.response_modalities
    assert config.image_config.aspect_ratio == "16:9"
    assert config.image_config.image_size == "2K"
    assert openai.images.kwargs is None
    assert result.content[1].media_type == "image/png"
    assert result.content[1].data == PNG_1X1_B64
    assert "Generated image with gemini-nano-banana-2.1 (16:9 at 2K" in result.content[0].text


@pytest.mark.asyncio
async def test_env_default_model_may_be_nano_banana(monkeypatch) -> None:
    monkeypatch.setenv("FREYJA_IMAGE_MODEL", MODEL)
    tool, gemini, openai = _tool()

    result = await tool.execute("nb", {"prompt": "x"})

    assert not result.is_error, result.content
    assert gemini.models.kwargs["model"] == MODEL
    assert openai.images.kwargs is None


@pytest.mark.asyncio
async def test_nano_banana_needs_the_gemini_key_not_the_openai_one(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY")
    tool, gemini, _ = _tool()
    result = await tool.execute("nb", {"prompt": "x", "model": MODEL})
    assert not result.is_error, result.content

    monkeypatch.delenv("GEMINI_API_KEY")
    tool, gemini, _ = _tool()
    result = await tool.execute("nb", {"prompt": "x", "model": MODEL})
    assert result.is_error
    assert "GEMINI_API_KEY" in str(result.content)
    assert gemini.models.kwargs is None


def test_schema_lists_nano_banana() -> None:
    definition = GenerateImageTool().definition
    assert MODEL in image_generation_tool.SUPPORTED_MODELS
    assert MODEL in definition.parameters["properties"]["model"]["enum"]
    assert "Nano Banana" in definition.summary
    assert "GEMINI_API_KEY" in definition.description


# ── size and quality → aspect ratio and tier ────────────────────────────


@pytest.mark.parametrize(
    ("size", "quality", "aspect", "tier"),
    [
        ("auto", "auto", None, "1K"),
        ("auto", "low", None, "1K"),
        ("auto", "medium", None, "1K"),
        ("auto", "high", None, "2K"),
        ("auto", "xhigh", None, "4K"),
        ("auto", "max", None, "4K"),
        ("16:9", "auto", "16:9", "1K"),
        ("9:16", "max", "9:16", "4K"),
        # An explicit WIDTHxHEIGHT picks the tier itself; quality is ignored.
        ("1024x1024", "high", "1:1", "1K"),
        ("500x500", "auto", "1:1", "1K"),
        ("1536x864", "auto", "16:9", "2K"),
        ("3840x2160", "low", "16:9", "4K"),
        ("2000x1000", "auto", "16:9", "2K"),
        ("1080x1920", "auto", "9:16", "2K"),
        # The tier is a ceiling: the output is never smaller than asked for.
        ("2048x878", "auto", "21:9", "2K"),
        ("2100x900", "auto", "21:9", "4K"),
        ("1200x1500", "auto", "4:5", "2K"),
    ],
)
def test_size_and_quality_map_to_ratio_and_tier(size, quality, aspect, tier) -> None:
    cleaned = image_generation_tool._clean_gemini_size(size)
    assert image_generation_tool._gemini_render_plan(cleaned, quality) == (aspect, tier)


@pytest.mark.parametrize(
    "size", ["4:7", "big", "0x100", "1024", "1024x1024x1024", "1537x", "1024.0x768"]
)
@pytest.mark.asyncio
async def test_bad_sizes_are_rejected_before_any_request(size) -> None:
    tool, gemini, _ = _tool()

    result = await tool.execute("nb", {"prompt": "x", "model": MODEL, "size": size})

    assert result.is_error
    assert "size" in str(result.content)
    assert gemini.models.kwargs is None


@pytest.mark.asyncio
async def test_xhigh_and_max_quality_are_accepted() -> None:
    tool, gemini, _ = _tool()
    result = await tool.execute("nb", {"prompt": "x", "model": MODEL, "quality": "max"})
    assert not result.is_error, result.content
    assert gemini.models.kwargs["config"].image_config.image_size == "4K"


# ── output format ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_models_own_format_stands_unless_one_is_asked_for() -> None:
    tool, _, _ = _tool(_response([_part(JPEG_4X4, "image/jpeg")]))

    result = await tool.execute("nb", {"prompt": "x", "model": MODEL})

    assert not result.is_error, result.content
    assert result.content[1].media_type == "image/jpeg"
    assert base64.b64decode(result.content[1].data) == JPEG_4X4
    assert "format=jpeg" in result.content[0].text


@pytest.mark.asyncio
async def test_a_requested_format_is_produced_locally() -> None:
    tool, _, _ = _tool(_response([_part(JPEG_4X4, "image/jpeg")]))

    result = await tool.execute("nb", {"prompt": "x", "model": MODEL, "output_format": "png"})

    assert not result.is_error, result.content
    assert result.content[1].media_type == "image/png"
    assert base64.b64decode(result.content[1].data).startswith(b"\x89PNG")
    assert "format=png" in result.content[0].text


@pytest.mark.asyncio
async def test_jpeg_with_compression_is_produced_locally() -> None:
    tool, _, _ = _tool(_response([_part(PNG_1X1, "image/png")]))

    result = await tool.execute(
        "nb", {"prompt": "x", "model": MODEL, "output_format": "jpeg", "output_compression": 50}
    )

    assert not result.is_error, result.content
    assert result.content[1].media_type == "image/jpeg"
    assert base64.b64decode(result.content[1].data).startswith(b"\xff\xd8\xff")


# ── editing with reference images ────────────────────────────────────────


@pytest.mark.asyncio
async def test_reference_images_travel_as_inline_parts() -> None:
    tool, gemini, _ = _tool()

    result = await tool.execute(
        "nb",
        {
            "prompt": "make it noon",
            "model": MODEL,
            "input_images": [{"dataBase64": PNG_1X1_B64, "mimeType": "image/png"}],
        },
    )

    assert not result.is_error, result.content
    contents = gemini.models.kwargs["contents"]
    assert contents[0] == "make it noon"
    assert contents[1].inline_data.mime_type == "image/png"
    assert contents[1].inline_data.data == PNG_1X1
    assert result.content[0].text.startswith("Edited image with gemini-nano-banana-2.1")


@pytest.mark.asyncio
async def test_more_than_fourteen_references_are_rejected() -> None:
    tool, gemini, _ = _tool()
    refs = [{"dataBase64": PNG_1X1_B64, "mimeType": "image/png"}] * 15

    result = await tool.execute("nb", {"prompt": "x", "model": MODEL, "input_images": refs})

    assert result.is_error
    assert "14" in str(result.content)
    assert gemini.models.kwargs is None


@pytest.mark.parametrize(
    ("arguments", "word"),
    [
        ({"background": "transparent"}, "background"),
        (
            {
                "input_images": [{"dataBase64": PNG_1X1_B64, "mimeType": "image/png"}],
                "mask_image": {"dataBase64": PNG_1X1_B64, "mimeType": "image/png"},
            },
            "mask_image",
        ),
    ],
)
@pytest.mark.asyncio
async def test_openai_only_options_are_refused_up_front(arguments, word) -> None:
    tool, gemini, _ = _tool()

    result = await tool.execute("nb", {"prompt": "x", "model": MODEL, **arguments})

    assert result.is_error
    assert word in str(result.content)
    assert gemini.models.kwargs is None


# ── response parsing ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_image_reports_the_finish_reason_and_the_models_words() -> None:
    response = _response(
        [_part(None, text="I can't draw that one.")],
        finish_reason=SimpleNamespace(name="IMAGE_SAFETY"),
    )
    tool, _, _ = _tool(response)

    result = await tool.execute("nb", {"prompt": "x", "model": MODEL})

    assert result.is_error
    assert "IMAGE_SAFETY" in str(result.content)
    assert "I can't draw that one." in str(result.content)


@pytest.mark.asyncio
async def test_an_empty_candidate_is_an_error_not_a_crash() -> None:
    response = SimpleNamespace(
        candidates=[
            SimpleNamespace(content=None, finish_reason=SimpleNamespace(name="IMAGE_RECITATION"))
        ]
    )
    tool, _, _ = _tool(response)

    result = await tool.execute("nb", {"prompt": "x", "model": MODEL})

    assert result.is_error
    assert "IMAGE_RECITATION" in str(result.content)


@pytest.mark.asyncio
async def test_a_thought_parts_image_is_not_the_answer() -> None:
    response = _response([_part(b"draft-not-the-answer", thought=True), _part()])
    tool, _, _ = _tool(response)

    result = await tool.execute("nb", {"prompt": "x", "model": MODEL})

    assert not result.is_error, result.content
    assert result.content[1].data == PNG_1X1_B64


@pytest.mark.asyncio
async def test_base64_inline_data_is_accepted_too() -> None:
    """Older SDKs hand inline_data.data back as a base64 string, not bytes."""
    response = _response([_part(PNG_1X1_B64, "image/png")])
    tool, _, _ = _tool(response)

    result = await tool.execute("nb", {"prompt": "x", "model": MODEL})

    assert not result.is_error, result.content
    assert result.content[1].data == PNG_1X1_B64
