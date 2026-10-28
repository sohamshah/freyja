import asyncio
import base64
import json
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI

import bridge.tools.image_generation_tool as image_generation_tool
from bridge.freyja_bridge import _tool_content_preview_and_images
from bridge.tools.image_generation_tool import GenerateImageTool
from bridge.tools.image_store import SessionImageStore
from bridge.tools.registry import build_desktop_registry
from engine.types import ImageBlock, TextBlock

PNG_1X1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg=="
)
MODELS_2_5 = (
    "gpt-image-2.5-sunburst",
    "gpt-image-2.5-sunburst-2026-09-08",
    "gpt-image-2.5-flare",
    "gpt-image-2.5-flare-2026-09-08",
)
MODERN_MODELS = (*MODELS_2_5, "gpt-image-2", "gpt-image-2-2026-04-21")
# GPT Image 1 shut down 2026-10-23; 1.5 and 1 Mini follow on 2026-12-01.
# They left with their fixed sizes and the input_fidelity knob.
ALL_MODELS = MODERN_MODELS


@pytest.fixture(autouse=True)
def isolate_image_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("FREYJA_IMAGE_OUTPUT_DIR", str(tmp_path / "generated"))
    monkeypatch.delenv("FREYJA_IMAGE_MODEL", raising=False)


@pytest.fixture
async def sdk_client():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"created": 1, "data": [{"b64_json": PNG_1X1_B64}]},
        )

    async with AsyncOpenAI(
        api_key="test-key",
        base_url="https://images.test/v1",
        max_retries=0,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    ) as client:
        yield client, requests


def _inline_png(label="source"):
    return {"dataBase64": PNG_1X1_B64, "mimeType": "image/png", "label": label}


def _fake_request_body(kwargs):
    assert kwargs is not None
    return {key: value for key, value in kwargs.items() if key != "extra_body"} | kwargs.get(
        "extra_body", {}
    )


def _multipart_parts(request):
    message = BytesParser(policy=policy.default).parsebytes(
        b"Content-Type: "
        + request.headers["content-type"].encode("ascii")
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
        + request.content
    )
    assert message.is_multipart()
    parts = list(message.iter_parts())
    names = [part.get_param("name", header="content-disposition") for part in parts]
    assert len(set(names)) == len(names)
    return dict(zip(names, parts, strict=True))


class FakeImages:
    def __init__(self) -> None:
        self.kwargs = None
        self.generate_kwargs = None
        self.edit_kwargs = None

    async def generate(self, **kwargs):
        self.kwargs = kwargs
        self.generate_kwargs = kwargs
        return SimpleNamespace(data=[SimpleNamespace(b64_json=PNG_1X1_B64)])

    async def edit(self, **kwargs):
        self.kwargs = kwargs
        self.edit_kwargs = kwargs
        return SimpleNamespace(data=[SimpleNamespace(b64_json=PNG_1X1_B64)])


class FakeClient:
    def __init__(self) -> None:
        self.images = FakeImages()


class SlowImages(FakeImages):
    async def generate(self, **kwargs):
        await asyncio.sleep(0.02)
        return await super().generate(**kwargs)


class SlowClient:
    def __init__(self) -> None:
        self.images = SlowImages()


@pytest.mark.asyncio
async def test_generate_image_returns_inline_image_block(monkeypatch, tmp_path) -> None:
    fake = FakeClient()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    tool = GenerateImageTool(client_factory=lambda _key: fake)
    result = await tool.execute(
        "call-1",
        {
            "prompt": "a tiny luminous topo map",
            "size": "1024x1024",
            "quality": "high",
            "output_format": "png",
        },
    )

    assert not result.is_error
    assert isinstance(result.content, list)
    assert isinstance(result.content[0], TextBlock)
    assert isinstance(result.content[1], ImageBlock)
    assert result.content[1].data == PNG_1X1_B64
    assert result.content[1].media_type == "image/png"
    assert getattr(result.content[1], "width") == 1
    assert getattr(result.content[1], "height") == 1
    assert fake.images.kwargs["model"] == "gpt-image-2.5-sunburst"
    assert fake.images.kwargs["prompt"] == "a tiny luminous topo map"
    assert fake.images.kwargs["size"] == "1024x1024"
    assert fake.images.kwargs["quality"] == "high"
    saved_files = list((tmp_path / "generated").glob("*.png"))
    assert len(saved_files) == 1
    assert saved_files[0].read_bytes() == base64.b64decode(PNG_1X1_B64)


@pytest.mark.asyncio
async def test_generate_image_uses_edit_api_for_image_refs(monkeypatch) -> None:
    fake = FakeClient()
    store = SessionImageStore()
    store.add_base64(
        PNG_1X1_B64,
        "image/png",
        label="user image",
        source="test",
        aliases=("latest_user_image",),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    tool = GenerateImageTool(client_factory=lambda _key: fake, image_store=store)
    result = await tool.execute(
        "call-edit",
        {
            "prompt": "make this feel like a luminous topo poster",
            "use_latest_user_image": True,
        },
    )

    assert not result.is_error
    assert fake.images.generate_kwargs is None
    assert fake.images.edit_kwargs is not None
    assert "input_fidelity" not in _fake_request_body(fake.images.edit_kwargs)
    assert fake.images.edit_kwargs["image"][0][0] == "img_001.png"
    assert "Edited image" in result.content[0].text
    assert "latest_generated_image" in result.content[0].text
    assert store.resolve("latest_generated_image") is not None


@pytest.mark.asyncio
async def test_generate_image_does_not_enforce_freyja_timeout(monkeypatch) -> None:
    fake = SlowClient()
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(image_generation_tool, "_env_timeout", lambda: 0.001, raising=False)

    tool = GenerateImageTool(client_factory=lambda _key: fake)
    result = await tool.execute("call-slow", {"prompt": "slow but valid"})

    assert not result.is_error


@pytest.mark.asyncio
async def test_generate_image_requires_openai_key(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    tool = GenerateImageTool(client_factory=lambda _key: FakeClient())
    result = await tool.execute("call-2", {"prompt": "anything"})

    assert result.is_error
    assert "OPENAI_API_KEY" in str(result.content)


def test_tool_preview_extracts_image_payload_for_renderer() -> None:
    preview, images = _tool_content_preview_and_images(
        [
            TextBlock(text="generated"),
            ImageBlock.from_base64(PNG_1X1_B64, "image/png"),
        ]
    )

    assert preview == "generated\n[Image: image/png, 1x1]"
    assert images == [
        {
            "id": "image-2",
            "dataBase64": PNG_1X1_B64,
            "mimeType": "image/png",
            "width": 1,
            "height": 1,
            "label": "image 1",
        }
    ]


def test_desktop_registry_exposes_generate_image_as_warm_tool(tmp_path) -> None:
    registry = build_desktop_registry(
        workspace=tmp_path,
        include_bash=False,
        include_web=False,
        include_subagents=False,
        include_computer=False,
    )

    entry = registry.get_catalog_entry("generate_image")
    assert entry is not None
    assert entry.summary_visible
    assert not entry.schema_visible
    assert "generate_image" in registry.list_summaries()


def test_generate_image_schema_describes_supported_models_and_options() -> None:
    definition = GenerateImageTool().definition
    properties = definition.parameters["properties"]

    assert image_generation_tool.DEFAULT_IMAGE_MODEL == "gpt-image-2.5-sunburst"
    # The OpenAI set plus Google's Nano Banana 2.1 (tests/test_image_generation_gemini.py).
    assert image_generation_tool.SUPPORTED_MODELS == set(ALL_MODELS) | {"gemini-nano-banana-2.1"}
    assert set(properties["model"]["enum"]) == set(ALL_MODELS) | {"gemini-nano-banana-2.1"}
    assert "Sunburst" in definition.summary
    assert "Flare" in definition.summary
    assert "Nano Banana" in definition.summary
    assert "gpt-image-2.5-sunburst" in definition.description
    assert "gpt-image-2.5-flare" in definition.description
    assert "explicit model overrides FREYJA_IMAGE_MODEL" in definition.description
    assert "snapshots" in definition.description
    assert "editing precision" in properties["model"]["description"]
    assert "fast everyday generation" in properties["model"]["description"]
    assert "FREYJA_IMAGE_MODEL" in properties["model"]["description"]
    assert "model" not in definition.parameters["required"]
    assert properties["size"]["type"] == "string"
    assert "enum" not in properties["size"]
    assert "WIDTHxHEIGHT" in properties["size"]["description"]
    assert "3840" in properties["size"]["description"]
    assert set(properties["quality"]["enum"]) == {"auto", "low", "medium", "high", "xhigh", "max"}
    assert "2.5" in properties["quality"]["description"]
    assert set(properties["background"]["enum"]) == {"auto", "opaque", "transparent"}
    assert set(properties["output_format"]["enum"]) == {"png", "jpeg", "webp"}
    assert set(properties["moderation"]["enum"]) == {"auto", "low"}
    # input_fidelity left with the GPT Image 1 line; modern models always
    # process references at high fidelity.
    assert "input_fidelity" not in properties
    assert properties["output_compression"]["type"] == "integer"
    assert properties["output_compression"]["minimum"] == 0
    assert properties["output_compression"]["maximum"] == 100


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("env_model", "explicit_model", "expected_model"),
    [
        (None, None, "gpt-image-2.5-sunburst"),
        ("   ", None, "gpt-image-2.5-sunburst"),
        (" gpt-image-2.5-flare ", None, "gpt-image-2.5-flare"),
        ("gpt-image-2", "gpt-image-2.5-flare", "gpt-image-2.5-flare"),
        ("invalid-environment-model", "gpt-image-2.5-sunburst", "gpt-image-2.5-sunburst"),
        ("gpt-image-2.5-flare", "   ", "gpt-image-2.5-flare"),
        (None, " gpt-image-2 ", "gpt-image-2"),
    ],
)
async def test_model_selection_precedence(
    monkeypatch, env_model, explicit_model, expected_model
) -> None:
    if env_model is not None:
        monkeypatch.setenv("FREYJA_IMAGE_MODEL", env_model)
    fake = FakeClient()
    tool = GenerateImageTool(client_factory=lambda _key: fake)
    arguments = {"prompt": "model selection"}
    if explicit_model is not None:
        arguments["model"] = explicit_model

    result = await tool.execute("model-selection", arguments)

    assert not result.is_error
    assert fake.images.generate_kwargs["model"] == expected_model


@pytest.mark.asyncio
async def test_unsupported_environment_model_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("FREYJA_IMAGE_MODEL", "dall-e-3")
    fake = FakeClient()
    tool = GenerateImageTool(client_factory=lambda _key: fake)

    result = await tool.execute("bad-env-model", {"prompt": "anything"})

    assert result.is_error
    assert "model must be one of" in str(result.content)
    assert fake.images.kwargs is None


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODERN_MODELS)
async def test_edits_never_send_input_fidelity(model) -> None:
    """The knob belonged to the GPT Image 1 line; a leftover argument from
    an older prompt is ignored rather than forwarded or rejected."""
    fake = FakeClient()
    tool = GenerateImageTool(client_factory=lambda _key: fake)

    result = await tool.execute(
        "fidelity",
        {
            "prompt": "preserve details",
            "model": model,
            "input_images": [_inline_png()],
            "input_fidelity": "high",
        },
    )

    assert not result.is_error
    assert fake.images.generate_kwargs is None
    assert "input_fidelity" not in _fake_request_body(fake.images.edit_kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODERN_MODELS)
@pytest.mark.parametrize(
    "size",
    [
        "auto",
        "1024x1024",
        "1536x1024",
        "1024x1536",
        "1536x864",
        "1024x640",
        "1536x512",
        "512x1536",
        "2048x2048",
        "3840x2160",
        "2160x3840",
    ],
)
async def test_modern_models_accept_standard_and_custom_sizes(model, size) -> None:
    fake = FakeClient()
    tool = GenerateImageTool(client_factory=lambda _key: fake)

    result = await tool.execute("valid-size", {"prompt": "anything", "model": model, "size": size})

    assert not result.is_error
    assert fake.images.generate_kwargs["size"] == size


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ["gpt-image-2", "gpt-image-2.5-sunburst", "gpt-image-2.5-flare"])
@pytest.mark.parametrize(
    "size",
    [
        "1537x864",  # Not a multiple of 16.
        "3856x2048",  # Edge exceeds 3840.
        "2048x640",  # Landscape aspect ratio exceeds 3:1.
        "640x2048",  # Portrait aspect ratio exceeds 1:3.
        "1024x624",  # Area below 655360.
        "3840x2176",  # Area above 8294400.
        "0x1024",
        "-1024x1024",
        "1024",
        "1024x1024x1024",
        "1024.0x1024",
        "１０２４x１０２４",
        "1000000000000000x1024",
    ],
)
async def test_modern_models_reject_invalid_custom_sizes(model, size) -> None:
    fake = FakeClient()
    tool = GenerateImageTool(client_factory=lambda _key: fake)

    result = await tool.execute("bad-size", {"prompt": "anything", "model": model, "size": size})

    assert result.is_error
    assert "size" in str(result.content)
    assert fake.images.kwargs is None


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ALL_MODELS)
@pytest.mark.parametrize("quality", ["auto", "low", "medium", "high", "xhigh", "max"])
async def test_quality_is_validated_for_selected_model(model, quality) -> None:
    fake = FakeClient()
    tool = GenerateImageTool(client_factory=lambda _key: fake)

    result = await tool.execute(
        "quality", {"prompt": "anything", "model": model, "quality": quality}
    )

    if quality in {"xhigh", "max"} and model not in MODELS_2_5:
        assert result.is_error
        assert "quality" in str(result.content)
        assert fake.images.kwargs is None
    else:
        assert not result.is_error
        assert fake.images.generate_kwargs["quality"] == quality


@pytest.mark.asyncio
@pytest.mark.parametrize("model", ALL_MODELS)
@pytest.mark.parametrize("output_format", ["png", "webp", "jpeg"])
async def test_transparency_requires_png_or_webp(model, output_format) -> None:
    fake = FakeClient()
    tool = GenerateImageTool(client_factory=lambda _key: fake)

    result = await tool.execute(
        "transparent",
        {
            "prompt": "a cutout asset",
            "model": model,
            "background": "transparent",
            "output_format": output_format,
        },
    )

    if output_format == "jpeg":
        assert result.is_error
        assert "transparent background requires output_format png or webp" in str(result.content)
        assert fake.images.kwargs is None
    else:
        assert not result.is_error
        body = _fake_request_body(fake.images.generate_kwargs)
        assert body["background"] == "transparent"
        assert body["output_format"] == output_format
        assert result.content[1].media_type == f"image/{output_format}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("option", "value"),
    [
        ("model", "dall-e-3"),
        ("quality", "ultra"),
        ("output_format", "jpg"),
        ("background", "checkerboard"),
        ("moderation", "off"),
    ],
)
async def test_unsupported_enum_options_are_rejected(option, value) -> None:
    fake = FakeClient()
    tool = GenerateImageTool(client_factory=lambda _key: fake)

    result = await tool.execute("bad-enum", {"prompt": "anything", option: value})

    assert result.is_error
    assert f"{option} must be one of" in str(result.content)
    assert fake.images.kwargs is None


@pytest.mark.asyncio
@pytest.mark.parametrize("output_format", ["png", "jpeg", "webp"])
@pytest.mark.parametrize("compression", [None, 0, 75, 100])
async def test_output_compression_is_only_serialized_for_jpeg_and_webp(
    output_format, compression
) -> None:
    fake = FakeClient()
    tool = GenerateImageTool(client_factory=lambda _key: fake)

    result = await tool.execute(
        "compression",
        {"prompt": "anything", "output_format": output_format, "output_compression": compression},
    )

    assert not result.is_error
    body = _fake_request_body(fake.images.generate_kwargs)
    if output_format == "png" or compression is None:
        assert "output_compression" not in body
    else:
        assert body["output_compression"] == compression
        assert type(body["output_compression"]) is int


@pytest.mark.asyncio
@pytest.mark.parametrize("output_format", ["png", "jpeg", "webp"])
@pytest.mark.parametrize("compression", [-1, 101, True, False, 75.5, "75", "invalid"])
async def test_invalid_compression_is_rejected_even_for_png(output_format, compression) -> None:
    fake = FakeClient()
    tool = GenerateImageTool(client_factory=lambda _key: fake)

    result = await tool.execute(
        "bad-compression",
        {"prompt": "anything", "output_format": output_format, "output_compression": compression},
    )

    assert result.is_error
    assert "output_compression must be an integer between 0 and 100" in str(result.content)
    assert fake.images.kwargs is None


@pytest.mark.asyncio
async def test_mask_without_input_image_is_rejected() -> None:
    fake = FakeClient()
    tool = GenerateImageTool(client_factory=lambda _key: fake)

    result = await tool.execute(
        "mask-without-input", {"prompt": "anything", "mask_image": _inline_png("mask")}
    )

    assert result.is_error
    assert "mask_image requires an input image" in str(result.content)
    assert fake.images.kwargs is None


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODERN_MODELS)
async def test_sdk_generation_sends_only_the_documented_fields(sdk_client, model) -> None:
    client, requests = sdk_client
    tool = GenerateImageTool(client_factory=lambda _key: client)
    arguments = {"prompt": "precise image", "model": model, "size": "1536x864"}

    result = await tool.execute("sdk-generation", arguments)

    assert not result.is_error, result.content
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/images/generations"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {
        "model": model,
        "prompt": "precise image",
        "n": 1,
        "size": "1536x864",
        "quality": "auto",
        "output_format": "png",
        "background": "auto",
        "moderation": "auto",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output_format", "compression", "background"),
    [
        ("png", 75, "transparent"),
        ("jpeg", 0, "opaque"),
        ("jpeg", 100, "auto"),
        ("webp", 75, "transparent"),
    ],
)
async def test_sdk_generation_serializes_output_options(
    sdk_client, output_format, compression, background
) -> None:
    client, requests = sdk_client
    tool = GenerateImageTool(client_factory=lambda _key: client)

    result = await tool.execute(
        "sdk-generation-options",
        {
            "prompt": "a production asset",
            "model": "gpt-image-2.5-flare",
            "size": "3840x2160",
            "quality": "max",
            "output_format": output_format,
            "background": background,
            "output_compression": compression,
            "moderation": "low",
        },
    )

    assert not result.is_error, result.content
    assert len(requests) == 1
    body = json.loads(requests[0].content)
    expected = {
        "model": "gpt-image-2.5-flare",
        "prompt": "a production asset",
        "n": 1,
        "size": "3840x2160",
        "quality": "max",
        "output_format": output_format,
        "background": background,
        "moderation": "low",
    }
    if output_format != "png":
        expected["output_compression"] = compression
    assert body == expected
    assert result.content[1].media_type == f"image/{output_format}"


@pytest.mark.asyncio
@pytest.mark.parametrize("model", MODERN_MODELS)
@pytest.mark.parametrize(("output_format", "compression"), [("png", 75), ("jpeg", 0)])
async def test_sdk_edit_serializes_files_mask_and_model_options(
    sdk_client, model, output_format, compression
) -> None:
    client, requests = sdk_client
    tool = GenerateImageTool(client_factory=lambda _key: client)
    arguments = {
        "prompt": "preserve this input outside the mask",
        "model": model,
        "size": "1536x864",
        "quality": "xhigh" if model in MODELS_2_5 else "high",
        "input_images": [_inline_png()],
        "mask_image": _inline_png("mask"),
        "output_format": output_format,
        "output_compression": compression,
        "background": "transparent" if output_format == "png" else "opaque",
        "moderation": "low",
    }

    result = await tool.execute("sdk-edit", arguments)

    assert not result.is_error, result.content
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/images/edits"
    assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
    parts = _multipart_parts(request)
    for name, filename in [("image[]", "source.png"), ("mask", "mask.png")]:
        assert parts[name].get_filename() == filename
        assert parts[name].get_content_type() == "image/png"
        assert parts[name].get_payload(decode=True) == base64.b64decode(PNG_1X1_B64)
    fields = {
        name: part.get_payload(decode=True).decode("utf-8")
        for name, part in parts.items()
        if part.get_filename() is None
    }
    expected = {
        "model": model,
        "prompt": "preserve this input outside the mask",
        "n": "1",
        "size": "1536x864",
        "quality": "xhigh" if model in MODELS_2_5 else "high",
        "output_format": output_format,
        "background": "transparent" if output_format == "png" else "opaque",
        "moderation": "low",
    }
    if output_format == "jpeg":
        expected["output_compression"] = str(compression)
    assert fields == expected
    assert set(parts) == set(expected) | {"image[]", "mask"}
    assert result.content[1].media_type == f"image/{output_format}"
