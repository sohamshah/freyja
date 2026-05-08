import asyncio
from types import SimpleNamespace

import pytest

import bridge.tools.image_generation_tool as image_generation_tool
from bridge.tools.image_generation_tool import GenerateImageTool
from bridge.tools.image_store import SessionImageStore
from bridge.tools.registry import build_desktop_registry
from bridge.freyja_bridge import _tool_content_preview_and_images
from engine.types import ImageBlock, TextBlock


PNG_1X1_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9Q"
    "DwADhgGAWjR9awAAAABJRU5ErkJggg=="
)


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
async def test_generate_image_returns_inline_image_block(monkeypatch) -> None:
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
    assert fake.images.kwargs["model"] == "gpt-image-2"
    assert fake.images.kwargs["prompt"] == "a tiny luminous topo map"
    assert fake.images.kwargs["size"] == "1024x1024"
    assert fake.images.kwargs["quality"] == "high"


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
            "input_fidelity": "high",
        },
    )

    assert not result.is_error
    assert fake.images.generate_kwargs is None
    assert fake.images.edit_kwargs is not None
    assert fake.images.edit_kwargs["input_fidelity"] == "high"
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
