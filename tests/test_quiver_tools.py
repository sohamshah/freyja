"""generate_svg (Quiver AI): the request it sends, the SVG it stores and saves, and the image refs it resolves."""

import base64
import os
from unittest.mock import MagicMock, patch

import httpx
import pytest

from bridge.tools.image_store import SessionImageStore
from bridge.tools.quiver_tools import GenerateSvgTool
from engine.types import ImageBlock, TextBlock


def _response(svgs: list[str]) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.json.return_value = {
        "data": [{"id": f"svg_{i}", "svg": svg} for i, svg in enumerate(svgs)],
        "credits": 10,
    }
    return response


@pytest.mark.asyncio
async def test_missing_key_or_prompt_is_refused():
    tool = GenerateSvgTool()

    with patch.dict(os.environ, {}, clear=True):
        result = await tool.execute(call_id="c", arguments={"prompt": "draw a square"})
    assert result.is_error
    assert "QUIVER_API_KEY environment variable is not set" in result.content

    with patch.dict(os.environ, {"QUIVER_API_KEY": "fake_key"}):
        result = await tool.execute(call_id="c", arguments={})
    assert result.is_error
    assert "Error: 'prompt' argument is required" in result.content


@pytest.mark.asyncio
async def test_quiver_generate_svg_successful_execution(tmp_path):
    image_store = SessionImageStore()
    tool = GenerateSvgTool(image_store=image_store, project_output_dir=tmp_path)
    mock_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        '<rect width="100" height="100" fill="red"/></svg>'
    )

    with patch.dict(os.environ, {"QUIVER_API_KEY": "fake_key"}):
        with patch("httpx.AsyncClient.post", return_value=_response([mock_svg])) as mock_post:
            result = await tool.execute(
                call_id="test_success",
                arguments={
                    "prompt": "red square",
                    "instructions": "flat clean geometry",
                    "model": "arrow-1.1",
                    "n": 1,
                },
            )

    # Verify API call structure
    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "https://api.quiver.ai/v1/svgs/generations"
    assert kwargs["headers"]["Authorization"] == "Bearer fake_key"
    assert kwargs["json"]["prompt"] == "red square"
    assert kwargs["json"]["instructions"] == "flat clean geometry"
    assert kwargs["json"]["model"] == "arrow-1.1"

    # Verify ToolResult content and ImageStore state
    assert not result.is_error
    assert isinstance(result.content, list)
    assert any(
        isinstance(block, TextBlock) and "Successfully generated 1 SVG" in block.text
        for block in result.content
    )
    assert any(
        isinstance(block, ImageBlock) and block.media_type == "image/svg+xml"
        for block in result.content
    )

    # Check local file write
    saved_file_path = tmp_path / "quiver_outputs" / "quiver_test_success_0.svg"
    assert saved_file_path.is_file()
    assert saved_file_path.read_text(encoding="utf-8") == mock_svg

    # Check ImageStore asset reference
    asset = image_store.resolve("img_001")
    assert asset is not None
    assert asset.media_type == "image/svg+xml"
    assert base64.b64decode(asset.data_base64).decode("utf-8") == mock_svg


@pytest.mark.asyncio
async def test_quiver_generate_svg_resolves_image_references(tmp_path):
    image_store = SessionImageStore()
    ref_b64 = base64.b64encode(b"dummy image bytes").decode("utf-8")
    ref_asset = image_store.add_base64(data_base64=ref_b64, media_type="image/png", label="ref logo")
    assert ref_asset.ref == "img_001"

    tool = GenerateSvgTool(image_store=image_store, project_output_dir=tmp_path)

    with patch.dict(os.environ, {"QUIVER_API_KEY": "fake_key"}):
        with patch("httpx.AsyncClient.post", return_value=_response(["<svg></svg>"])) as mock_post:
            await tool.execute(
                call_id="test_ref_resolution",
                arguments={
                    "prompt": "matching design",
                    "references": ["img_001", "https://example.com/online.jpg"],
                },
            )

    mock_post.assert_called_once()
    payload_references = mock_post.call_args[1]["json"]["references"]
    # "img_001" was resolved to base64 and the URL reference passed through.
    assert len(payload_references) == 2
    assert payload_references[0]["base64"] == f"data:image/png;base64,{ref_b64}"
    assert payload_references[1]["url"] == "https://example.com/online.jpg"
