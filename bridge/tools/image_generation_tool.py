"""Image generation tool backed by OpenAI's Images API."""

from __future__ import annotations

import base64
import inspect
import os
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from bridge.tools.base import ImageBlock, TextBlock, ToolDefinition, ToolResult, ToolTier
from bridge.tools.image_store import ImageAsset, SessionImageStore, strip_data_url

DEFAULT_IMAGE_MODEL = "gpt-image-2"
DEFAULT_IMAGE_OUTPUT_DIR = Path.home() / "Pictures" / "Freyja"
SUPPORTED_SIZES = {
    "auto",
    "1024x1024",
    "1536x1024",
    "1024x1536",
    "2048x2048",
    "2048x1152",
    "3840x2160",
    "2160x3840",
}
SUPPORTED_QUALITIES = {"auto", "low", "medium", "high"}
SUPPORTED_FORMATS = {"png", "jpeg", "webp"}
SUPPORTED_BACKGROUNDS = {"auto", "opaque"}
SUPPORTED_MODERATION = {"auto", "low"}
SUPPORTED_INPUT_FIDELITY = {"low", "high"}


def _timestamp_str() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _save_image_to_disk(
    b64: str,
    media_type: str,
    ref: str,
    *,
    save_path: str | None = None,
    default_output_dir: Path | None = None,
) -> Path:
    """Decode base64 image and write it to disk. Returns the resolved Path.

    If *save_path* is given it is used as-is (expanded). If it points to an
    existing or implied directory the image is auto-named inside it.
    Otherwise the image lands in ``FREYJA_IMAGE_OUTPUT_DIR`` (env), a
    session-scoped project image folder, or the default ``~/Pictures/Freyja/``.
    """
    raw = base64.b64decode(strip_data_url(b64))
    ext = _extension_for_media_type(media_type)

    if save_path:
        out = Path(save_path).expanduser().resolve()
        # If the caller gave a directory (or a path with no extension), auto-name
        if out.is_dir() or not out.suffix:
            out.mkdir(parents=True, exist_ok=True)
            out = out / f"freyja_{ref}_{_timestamp_str()}{ext}"
    else:
        env_dir = os.environ.get("FREYJA_IMAGE_OUTPUT_DIR", "").strip()
        out_dir = (
            Path(env_dir).expanduser().resolve()
            if env_dir
            else (default_output_dir or DEFAULT_IMAGE_OUTPUT_DIR)
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"freyja_{ref}_{_timestamp_str()}{ext}"

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw)
    return out


def _clean_enum(
    value: Any,
    *,
    allowed: set[str],
    default: str,
) -> str:
    normalized = str(value or default).strip().lower()
    return normalized if normalized in allowed else default


def _clean_compression(value: Any) -> int | None:
    if value in ("", None):
        return None
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return None


def _bytes_from_base64(data: str) -> bytes:
    return base64.b64decode(strip_data_url(data))


def _dimensions_from_image_bytes(raw: bytes) -> tuple[int, int] | None:
    """Best-effort image dimension parser for PNG/JPEG/WebP."""
    if len(raw) >= 24 and raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return int.from_bytes(raw[16:20], "big"), int.from_bytes(raw[20:24], "big")

    if len(raw) >= 10 and raw[:3] == b"\xff\xd8\xff":
        idx = 2
        while idx + 9 < len(raw):
            if raw[idx] != 0xFF:
                idx += 1
                continue
            marker = raw[idx + 1]
            idx += 2
            if marker in (0xD8, 0xD9):
                continue
            if idx + 2 > len(raw):
                return None
            seg_len = int.from_bytes(raw[idx : idx + 2], "big")
            if seg_len < 2:
                return None
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                if idx + 7 > len(raw):
                    return None
                height = int.from_bytes(raw[idx + 3 : idx + 5], "big")
                width = int.from_bytes(raw[idx + 5 : idx + 7], "big")
                return width, height
            idx += seg_len

    if len(raw) >= 30 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        chunk = raw[12:16]
        if chunk == b"VP8X" and len(raw) >= 30:
            width = 1 + int.from_bytes(raw[24:27], "little")
            height = 1 + int.from_bytes(raw[27:30], "little")
            return width, height
        if chunk == b"VP8L" and len(raw) >= 25:
            bits = int.from_bytes(raw[21:25], "little")
            width = 1 + (bits & 0x3FFF)
            height = 1 + ((bits >> 14) & 0x3FFF)
            return width, height
        if chunk == b"VP8 " and len(raw) >= 30:
            width = int.from_bytes(raw[26:28], "little") & 0x3FFF
            height = int.from_bytes(raw[28:30], "little") & 0x3FFF
            return width, height

    return None


def _get_response_item(response: Any) -> Any | None:
    data = response.get("data") if isinstance(response, dict) else getattr(response, "data", None)
    if not data:
        return None
    return data[0]


def _field(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


async def _download_image_url(url: str) -> tuple[str, str]:
    async with httpx.AsyncClient(timeout=None) as client:
        response = await client.get(url)
        response.raise_for_status()
        media_type = response.headers.get("content-type", "image/png").split(";", 1)[0]
        return base64.b64encode(response.content).decode("ascii"), media_type


def _media_type_from_path(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        return "image/jpeg"
    if suffix == ".webp":
        return "image/webp"
    return "image/png"


def _extension_for_media_type(media_type: str) -> str:
    if media_type == "image/jpeg":
        return ".jpg"
    if media_type == "image/webp":
        return ".webp"
    return ".png"


class GenerateImageTool:
    """Generate or edit one image and return it as a renderable ImageBlock."""

    def __init__(
        self,
        client_factory: Any | None = None,
        image_store: SessionImageStore | None = None,
        default_output_dir: Path | None = None,
    ):
        self._client_factory = client_factory
        self._image_store = image_store
        self._default_output_dir = default_output_dir

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="generate_image",
            summary="Generate or edit an image with OpenAI GPT Image 2",
            tier=ToolTier.WARM,
            description="""Generate or edit an image using OpenAI's Images API.

Use this when the user explicitly asks for an image, illustration, design reference,
texture, visual asset, mockup, or creative bitmap output. If input_images are
provided, this edits/transforms those images; otherwise it generates from text.
The tool returns a base64 image block so Freyja can render it inline in chat.

Guidelines:
- Write the prompt as the actual image brief, not conversational scaffolding.
- Include style, composition, aspect ratio, text constraints, and background needs.
- To edit a user attachment, set use_latest_user_image=true or pass
  input_images: [{"ref":"latest_user_image"}].
- To revise a previous output, pass input_images: [{"ref":"latest_generated_image"}].
- You may also pass local paths, URLs, or base64 data in input_images.
- Use a non-transparent background unless the user asks for cutouts/assets.
- Generate one image per call; call again only if the user asks for variants.""",
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed image prompt to send to GPT Image 2.",
                    },
                    "size": {
                        "type": "string",
                        "enum": sorted(SUPPORTED_SIZES),
                        "description": (
                            "Output size. GPT Image 2 supports these popular sizes "
                            "plus auto."
                        ),
                        "default": "1024x1024",
                    },
                    "quality": {
                        "type": "string",
                        "enum": sorted(SUPPORTED_QUALITIES),
                        "description": "Generation quality. Higher quality may cost more and take longer.",
                        "default": "auto",
                    },
                    "output_format": {
                        "type": "string",
                        "enum": sorted(SUPPORTED_FORMATS),
                        "description": "Image encoding returned to chat.",
                        "default": "png",
                    },
                    "background": {
                        "type": "string",
                        "enum": sorted(SUPPORTED_BACKGROUNDS),
                        "description": (
                            "Background mode. GPT Image 2 supports auto or opaque; "
                            "transparent backgrounds are not currently supported."
                        ),
                        "default": "auto",
                    },
                    "input_images": {
                        "type": "array",
                        "description": (
                            "Optional source images to edit or transform. Each item may "
                            "provide ref, path, url, or dataBase64/data_base64 plus "
                            "optional mimeType/mime_type. Refs include latest_user_image "
                            "and latest_generated_image when available."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "ref": {"type": "string"},
                                "path": {"type": "string"},
                                "url": {"type": "string"},
                                "dataBase64": {"type": "string"},
                                "data_base64": {"type": "string"},
                                "mimeType": {"type": "string"},
                                "mime_type": {"type": "string"},
                                "label": {"type": "string"},
                            },
                        },
                    },
                    "use_latest_user_image": {
                        "type": "boolean",
                        "description": "Use the most recent pasted/uploaded user image as an input image.",
                        "default": False,
                    },
                    "use_latest_generated_image": {
                        "type": "boolean",
                        "description": "Use the most recent generated image as an input image.",
                        "default": False,
                    },
                    "mask_image": {
                        "type": "object",
                        "description": "Optional mask image for image edits. Same shape as input_images items.",
                        "properties": {
                            "ref": {"type": "string"},
                            "path": {"type": "string"},
                            "url": {"type": "string"},
                            "dataBase64": {"type": "string"},
                            "data_base64": {"type": "string"},
                            "mimeType": {"type": "string"},
                            "mime_type": {"type": "string"},
                        },
                    },
                    "input_fidelity": {
                        "type": "string",
                        "enum": sorted(SUPPORTED_INPUT_FIDELITY),
                        "description": "How strongly to preserve input image details for edits.",
                        "default": "high",
                    },
                    "output_compression": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 100,
                        "description": "Compression level for jpeg/webp output. Ignored for png.",
                    },
                    "moderation": {
                        "type": "string",
                        "enum": sorted(SUPPORTED_MODERATION),
                        "description": "OpenAI image moderation strictness when supported.",
                        "default": "auto",
                    },
                    "save_path": {
                        "type": "string",
                        "description": (
                            "Optional file or directory path where the image should be saved on disk. "
                            "Supports ~ expansion. If a directory is given the image is auto-named "
                            "freyja_<ref>_<timestamp>.<ext> inside it. If omitted the image is "
                            "auto-saved to FREYJA_IMAGE_OUTPUT_DIR (env), the session project image "
                            "folder, or ~/Pictures/Freyja/."
                        ),
                    },
                },
                "required": ["prompt"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        prompt = str(arguments.get("prompt", "")).strip()
        if not prompt:
            return ToolResult(
                call_id=call_id,
                content="Error: prompt is required",
                is_error=True,
            )

        api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return ToolResult(
                call_id=call_id,
                content="Error: OPENAI_API_KEY is not configured",
                is_error=True,
            )

        model = os.environ.get("FREYJA_IMAGE_MODEL", DEFAULT_IMAGE_MODEL).strip() or DEFAULT_IMAGE_MODEL
        size = _clean_enum(arguments.get("size"), allowed=SUPPORTED_SIZES, default="1024x1024")
        quality = _clean_enum(arguments.get("quality"), allowed=SUPPORTED_QUALITIES, default="auto")
        output_format = _clean_enum(
            arguments.get("output_format"),
            allowed=SUPPORTED_FORMATS,
            default="png",
        )
        background = _clean_enum(
            arguments.get("background"),
            allowed=SUPPORTED_BACKGROUNDS,
            default="auto",
        )
        moderation = _clean_enum(
            arguments.get("moderation"),
            allowed=SUPPORTED_MODERATION,
            default="auto",
        )
        input_fidelity = _clean_enum(
            arguments.get("input_fidelity"),
            allowed=SUPPORTED_INPUT_FIDELITY,
            default="high",
        )
        output_compression = _clean_compression(arguments.get("output_compression"))
        if output_format == "png":
            output_compression = None

        try:
            input_images = await self._resolve_input_images(arguments.get("input_images"))
            if arguments.get("use_latest_user_image"):
                input_images.append(
                    await self._resolve_input_image({"ref": "latest_user_image"})
                )
            if arguments.get("use_latest_generated_image"):
                input_images.append(
                    await self._resolve_input_image({"ref": "latest_generated_image"})
                )
            mask_image = None
            if isinstance(arguments.get("mask_image"), dict):
                mask_image = await self._resolve_input_image(arguments["mask_image"])
            b64, media_type = await self._generate(
                api_key=api_key,
                model=model,
                prompt=prompt,
                size=size,
                quality=quality,
                output_format=output_format,
                background=background,
                output_compression=output_compression,
                moderation=moderation,
                input_images=input_images,
                mask_image=mask_image,
                input_fidelity=input_fidelity,
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                call_id=call_id,
                content=f"Error: image generation failed: {exc}",
                is_error=True,
            )

        image = ImageBlock.from_base64(b64, media_type)
        try:
            dims = _dimensions_from_image_bytes(_bytes_from_base64(b64))
            if dims:
                # Dynamic attrs are consumed by bridge tracing for richer UI
                # metadata; provider serialization ignores them.
                image.width, image.height = dims  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass

        saved_ref = None
        if self._image_store is not None:
            try:
                saved = self._image_store.add_base64(
                    b64,
                    media_type,
                    label="generated image",
                    source="generate_image",
                    aliases=("latest_image", "latest_generated_image"),
                )
                saved_ref = saved.ref
            except Exception:  # noqa: BLE001
                saved_ref = None

        # Auto-save to disk (non-fatal — never blocks the inline result)
        disk_path: Path | None = None
        try:
            disk_path = _save_image_to_disk(
                b64,
                media_type,
                saved_ref or "img",
                save_path=str(arguments.get("save_path") or "").strip() or None,
                default_output_dir=self._default_output_dir,
            )
        except Exception:  # noqa: BLE001
            disk_path = None

        mode_text = "Edited image" if input_images else "Generated image"
        ref_text = ""
        if saved_ref:
            ref_text += f" Saved as `{saved_ref}` (aliases: `latest_generated_image`, `latest_image`)."
        if disk_path:
            ref_text += f" File saved to `{disk_path}`."

        return ToolResult(
            call_id=call_id,
            content=[
                TextBlock(
                    text=(
                        f"{mode_text} with {model} "
                        f"({size}, quality={quality}, format={output_format})."
                        f"{ref_text}"
                    )
                ),
                image,
            ],
            is_error=False,
        )

    async def _generate(
        self,
        *,
        api_key: str,
        model: str,
        prompt: str,
        size: str,
        quality: str,
        output_format: str,
        background: str,
        output_compression: int | None,
        moderation: str,
        input_images: list[ImageAsset],
        mask_image: ImageAsset | None,
        input_fidelity: str,
    ) -> tuple[str, str]:
        client = self._make_client(api_key)

        kwargs: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "size": size,
            "quality": quality,
        }

        extras = {
            "output_format": output_format,
            "background": background,
            "output_compression": output_compression,
            "moderation": moderation,
        }
        if input_images:
            kwargs["image"] = [
                self._file_tuple(asset, index)
                for index, asset in enumerate(input_images, start=1)
            ]
            if mask_image is not None:
                extras["mask"] = self._file_tuple(mask_image, 0)
            extras["input_fidelity"] = input_fidelity
            self._merge_supported_kwargs(client.images.edit, kwargs, extras)
            response = await client.images.edit(**kwargs)
        else:
            self._merge_supported_kwargs(client.images.generate, kwargs, extras)
            response = await client.images.generate(**kwargs)

        item = _get_response_item(response)
        if item is None:
            raise RuntimeError("OpenAI image response did not include any image data")

        b64 = _field(item, "b64_json") or _field(item, "image_base64") or _field(item, "base64")
        media_type = f"image/{'jpeg' if output_format == 'jpeg' else output_format}"
        if b64:
            return str(b64), media_type

        url = _field(item, "url")
        if url:
            return await _download_image_url(str(url))

        raise RuntimeError("OpenAI image response had neither base64 data nor a URL")

    async def _resolve_input_images(self, value: Any) -> list[ImageAsset]:
        if value in ("", None):
            return []
        if not isinstance(value, list):
            raise ValueError("input_images must be an array")
        return [await self._resolve_input_image(item) for item in value]

    async def _resolve_input_image(self, item: Any) -> ImageAsset:
        if isinstance(item, str):
            item = {"ref": item}
        if not isinstance(item, dict):
            raise ValueError(
                "each input image must be an object, ref string, path, URL, or base64 data"
            )

        ref = str(item.get("ref") or "").strip()
        if ref:
            if self._image_store is None:
                raise ValueError(f"image ref {ref!r} is unavailable in this session")
            asset = self._image_store.resolve(ref)
            if asset is None:
                available = ", ".join(a.ref for a in self._image_store.recent())
                hint = f" Available refs: {available}" if available else ""
                raise ValueError(f"image ref {ref!r} was not found.{hint}")
            return asset

        data = str(item.get("dataBase64") or item.get("data_base64") or "").strip()
        media_type = str(item.get("mimeType") or item.get("mime_type") or "image/png")
        label = str(item.get("label") or "input image")
        if data:
            return ImageAsset(
                ref=label,
                data_base64=strip_data_url(data),
                media_type=media_type,
                label=label,
                source="inline",
            )

        path_value = str(item.get("path") or "").strip()
        if path_value:
            path = Path(path_value).expanduser()
            raw = path.read_bytes()
            return ImageAsset(
                ref=path.name,
                data_base64=base64.b64encode(raw).decode("ascii"),
                media_type=media_type
                if item.get("mimeType") or item.get("mime_type")
                else _media_type_from_path(path),
                label=label or path.name,
                source="path",
            )

        url = str(item.get("url") or "").strip()
        if url:
            b64, downloaded_media_type = await _download_image_url(url)
            return ImageAsset(
                ref=url,
                data_base64=b64,
                media_type=media_type
                if item.get("mimeType") or item.get("mime_type")
                else downloaded_media_type,
                label=label or url,
                source="url",
            )

        raise ValueError(
            "input image needs one of ref, path, url, dataBase64, or data_base64"
        )

    @staticmethod
    def _file_tuple(asset: ImageAsset, index: int) -> tuple[str, bytes, str]:
        stem = asset.ref.replace("/", "_").replace(" ", "_") or f"input_{index}"
        suffix = _extension_for_media_type(asset.media_type)
        return f"{stem}{suffix}", _bytes_from_base64(asset.data_base64), asset.media_type

    def _make_client(self, api_key: str) -> Any:
        if self._client_factory is not None:
            return self._client_factory(api_key)
        from openai import AsyncOpenAI  # noqa: PLC0415

        return AsyncOpenAI(api_key=api_key, timeout=None)

    @staticmethod
    def _merge_supported_kwargs(
        generate_fn: Any,
        kwargs: dict[str, Any],
        extras: dict[str, Any],
    ) -> None:
        """Use first-class SDK params when present, otherwise extra_body.

        The installed OpenAI SDK can lag the docs. `extra_body` keeps newer
        image parameters reachable without forcing a dependency bump.
        """
        try:
            parameters = set(inspect.signature(generate_fn).parameters)
        except Exception:  # noqa: BLE001
            parameters = set()

        extra_body: dict[str, Any] = {}
        for key, value in extras.items():
            if value in ("", None):
                continue
            if key in parameters:
                kwargs[key] = value
            else:
                extra_body[key] = value

        if extra_body:
            if not parameters or "extra_body" in parameters:
                kwargs["extra_body"] = extra_body
            else:
                kwargs.update(extra_body)
