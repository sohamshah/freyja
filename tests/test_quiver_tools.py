import base64
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from bridge.tools.quiver_tools import GenerateSvgTool
from bridge.tools.image_store import SessionImageStore
from engine.types import ImageBlock, TextBlock


@pytest.mark.asyncio
async def test_quiver_generate_svg_definition():
    tool = GenerateSvgTool()
    definition = tool.definition
    assert definition.name == "generate_svg"
    assert definition.tier.value == "hot"
    assert "prompt" in definition.parameters["required"]


@pytest.mark.asyncio
async def test_quiver_generate_svg_missing_api_key():
    tool = GenerateSvgTool()
    
    with patch.dict(os.environ, {}, clear=True):
        result = await tool.execute(
            call_id="test_missing_key",
            arguments={"prompt": "draw a square"}
        )
        assert result.is_error
        assert "QUIVER_API_KEY environment variable is not set" in result.content


@pytest.mark.asyncio
async def test_quiver_generate_svg_missing_prompt():
    tool = GenerateSvgTool()
    
    with patch.dict(os.environ, {"QUIVER_API_KEY": "fake_key"}):
        result = await tool.execute(
            call_id="test_missing_prompt",
            arguments={}
        )
        assert result.is_error
        assert "Error: 'prompt' argument is required" in result.content


@pytest.mark.asyncio
async def test_quiver_generate_svg_successful_execution(tmp_path):
    image_store = SessionImageStore()
    tool = GenerateSvgTool(image_store=image_store, project_output_dir=tmp_path)

    # Mock response from Quiver AI API
    mock_svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"><rect width="100" height="100" fill="red"/></svg>'
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "data": [
            {
                "id": "svg_01J9AZ",
                "svg": mock_svg
            }
        ],
        "credits": 10
    }

    with patch.dict(os.environ, {"QUIVER_API_KEY": "fake_key"}):
        with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
            result = await tool.execute(
                call_id="test_success",
                arguments={
                    "prompt": "red square",
                    "instructions": "flat clean geometry",
                    "model": "arrow-1.1",
                    "n": 1
                }
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
            assert any(isinstance(block, TextBlock) and "Successfully generated 1 SVG" in block.text for block in result.content)
            assert any(isinstance(block, ImageBlock) and block.media_type == "image/svg+xml" for block in result.content)

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
    
    # Pre-populate image store with a reference image
    ref_b64 = base64.b64encode(b"dummy image bytes").decode("utf-8")
    ref_asset = image_store.add_base64(
        data_base64=ref_b64,
        media_type="image/png",
        label="ref logo"
    )
    assert ref_asset.ref == "img_001"

    tool = GenerateSvgTool(image_store=image_store, project_output_dir=tmp_path)

    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "data": [{"id": "svg_out", "svg": "<svg></svg>"}],
        "credits": 5
    }

    with patch.dict(os.environ, {"QUIVER_API_KEY": "fake_key"}):
        with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
            await tool.execute(
                call_id="test_ref_resolution",
                arguments={
                    "prompt": "matching design",
                    "references": ["img_001", "https://example.com/online.jpg"]
                }
            )

            mock_post.assert_called_once()
            kwargs = mock_post.call_args[1]
            payload_references = kwargs["json"]["references"]

            # Confirm "img_001" was successfully resolved to base64 and other references passed through
            assert len(payload_references) == 2
            assert payload_references[0]["base64"] == f"data:image/png;base64,{ref_b64}"
            assert payload_references[1]["url"] == "https://example.com/online.jpg"
