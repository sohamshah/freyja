"""Quiver AI integration tool for high-fidelity SVG generation."""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any

import httpx

from bridge.tools.base import ImageBlock, TextBlock, ToolDefinition, ToolResult, ToolTier
from bridge.tools.image_store import strip_data_url

logger = logging.getLogger(__name__)


class GenerateSvgTool:
    """Generate high-quality SVGs from descriptive text prompts using Arrow 1.1."""

    def __init__(self, image_store: Any | None = None, project_output_dir: Path | str | None = None) -> None:
        self._image_store = image_store
        self._output_dir = Path(project_output_dir or ".").resolve()

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="generate_svg",
            summary="Generate high-quality vector SVGs from text prompts",
            tier=ToolTier.HOT,  # Set as HOT tier so it is immediately available to the model
            description="""Generate production-ready Scalable Vector Graphics (SVG) from text prompts.

Use this tool whenever you need high-fidelity graphic assets, logos, minimalist icons, 
illustrated backgrounds, wordmarks, or technical flats. You can specify style guidelines via instructions 
and provide visual inspiration via reference images (which can be base64 strings or existing session image references like 'img_001').

Requires the QUIVER_API_KEY environment variable.""",
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Descriptive text prompt outlining the target visual subject (e.g., 'minimalist cute kitten logo').",
                    },
                    "instructions": {
                        "type": "string",
                        "description": "Additional formatting or aesthetic guidance (e.g., 'flat monochrome vector, perfect geometric circles, no gradient').",
                    },
                    "model": {
                        "type": "string",
                        "enum": ["arrow-1.1", "arrow-1.1-max"],
                        "default": "arrow-1.1",
                        "description": "Use 'arrow-1.1' for fast, cost-efficient assets. Use 'arrow-1.1-max' for high-precision layouts, symmetrical shapes, or technical mechanical sketches.",
                    },
                    "n": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 16,
                        "default": 1,
                        "description": "Number of alternative designs/variations to output in parallel.",
                    },
                    "references": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "description": "Style/reference image as a URL, local path, base64 data string, or session image reference code (e.g., 'img_001')."
                        },
                        "description": "Optional list of reference/style images to direct visual layout."
                    }
                },
                "required": ["prompt"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        api_key = os.environ.get("QUIVER_API_KEY")
        if not api_key:
            return ToolResult(
                call_id=call_id,
                content="Error: QUIVER_API_KEY environment variable is not set. Please set it in your .env or session environment.",
                is_error=True
            )

        prompt = str(arguments.get("prompt", "")).strip()
        instructions = arguments.get("instructions")
        model = str(arguments.get("model") or "arrow-1.1")
        n = int(arguments.get("n") or 1)
        raw_references = arguments.get("references") or []

        if not prompt:
            return ToolResult(call_id=call_id, content="Error: 'prompt' argument is required.", is_error=True)

        # Process references (resolve from image store, URLs, paths, or base64)
        formatted_refs = []
        for ref in raw_references:
            if not isinstance(ref, str):
                continue
            ref_str = ref.strip()
            if not ref_str:
                continue

            # Case A: Session Image Reference
            if ref_str.startswith("img_") and self._image_store is not None:
                asset = self._image_store.resolve(ref_str)
                if asset:
                    formatted_refs.append({"base64": f"data:{asset.media_type};base64,{asset.data_base64}"})
                    continue

            # Case B: Base64 String
            if ref_str.startswith("data:image/") or ";base64," in ref_str:
                formatted_refs.append({"base64": ref_str})
                continue

            # Case C: URL
            if ref_str.startswith("http://") or ref_str.startswith("https://"):
                formatted_refs.append({"url": ref_str})
                continue

            # Case D: Local Path
            local_path = Path(ref_str).expanduser().resolve()
            if local_path.is_file():
                try:
                    file_bytes = local_path.read_bytes()
                    b64_str = base64.b64encode(file_bytes).decode("utf-8")
                    mime_type = "image/png" if local_path.suffix.lower() == ".png" else "image/jpeg"
                    formatted_refs.append({"base64": f"data:{mime_type};base64,{b64_str}"})
                except Exception as e:
                    logger.error("Failed to read local reference image: %s", e)

        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": n,
            "stream": False,
        }
        if instructions:
            payload["instructions"] = instructions
        if formatted_refs:
            payload["references"] = formatted_refs

        url = "https://api.quiver.ai/v1/svgs/generations"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }

        # Arrow renders (especially -max and multi-variation requests) can sit
        # in Quiver's queue well past a minute under load. Give the read phase a
        # generous 5-minute budget; keep connect/write/pool short so a genuinely
        # dead endpoint still fails fast.
        timeout = httpx.Timeout(connect=10.0, read=300.0, write=30.0, pool=10.0)

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(url, json=payload, headers=headers, timeout=timeout)

            if response.status_code != 200:
                error_msg = f"API Error {response.status_code}: "
                try:
                    err_json = response.json()
                    error_msg += f"{err_json.get('message', response.text)}"
                except Exception:
                    error_msg += response.text
                return ToolResult(call_id=call_id, content=error_msg, is_error=True)

            data = response.json()
            outputs = data.get("data", [])
            credits_spent = data.get("credits", 0)

            results_blocks: list[Any] = [
                TextBlock(text=f"Successfully generated {len(outputs)} SVG variation(s). Spent {credits_spent} credits.\n")
            ]

            # Save outputs locally and register them in Freyja's ImageStore
            quiver_dir = self._output_dir / "quiver_outputs"
            quiver_dir.mkdir(parents=True, exist_ok=True)

            for idx, out in enumerate(outputs):
                svg_markup = out.get("svg", "")
                output_id = out.get("id", f"svg_{idx}")

                # Save raw SVG XML file to disk
                file_name = f"quiver_{call_id}_{idx}.svg"
                file_path = quiver_dir / file_name
                file_path.write_text(svg_markup, encoding="utf-8")

                # Register SVG with image store
                saved_ref = f"img_{idx}"
                if self._image_store is not None:
                    try:
                        b64_svg = base64.b64encode(svg_markup.encode("utf-8")).decode("utf-8")
                        saved = self._image_store.add_base64(
                            b64_svg,
                            media_type="image/svg+xml",
                            label=f"quiver generated svg {idx+1}",
                            source="generate_svg",
                            aliases=("latest_image", "latest_generated_image") if idx == 0 else (),
                        )
                        saved_ref = saved.ref
                    except Exception as e:
                        logger.error("Failed to register SVG in ImageStore: %s", e)

                results_blocks.append(
                    TextBlock(
                        text=f"\n### Variation {idx+1} (Ref: `{saved_ref}`)\nSaved to: `{file_path}`\n\n```xml\n{svg_markup[:300]}...\n```"
                    )
                )
                b64_svg_embed = base64.b64encode(svg_markup.encode("utf-8")).decode("utf-8")
                results_blocks.append(ImageBlock.from_base64(b64_svg_embed, "image/svg+xml"))

            return ToolResult(call_id=call_id, content=results_blocks)

        except httpx.TimeoutException:
            return ToolResult(
                call_id=call_id,
                content="Quiver render timed out after 300s (5 min). The renderer is likely overloaded — retry shortly.",
                is_error=True,
            )
        except Exception as exc:
            return ToolResult(call_id=call_id, content=f"Network or processing failure: {exc}", is_error=True)
