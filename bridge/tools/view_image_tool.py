"""Load one or more images into the model's context window.

Without this tool, an agent that produced an image via generate_image,
captured a screenshot via browser_screenshot, or referenced an image
file on disk has no way to actually *see* it on subsequent turns —
the tool result's text alone can describe a path or a ref, but the
model can't OCR a path. ``view_image`` materializes the bytes as
``ImageBlock`` content blocks in the tool result so the next provider
call carries the actual pixels.

Inputs are a unified ``sources`` array — each item provides exactly
one of ``ref`` (a SessionImageStore ref or alias such as
``latest_image`` / ``latest_generated_image`` / ``latest_user_image``),
``path`` (local image file), or ``url`` (public HTTP(S) URL). Up to
six per call.

The tool is intentionally provider-aware: ``ToolResult`` content with
``ImageBlock`` entries is supported natively by the Anthropic provider,
and by Freyja's OpenAI Responses + Google Gemini providers via the
``function_call_output.output`` array shape and ``FunctionResponse.parts``
list respectively. Cerebras / Fireworks paths still text-coerce, so
those models will see the summary text but not the pixels — a known
limitation flagged in the provider notes.
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any

from bridge.tools.base import (
    ImageBlock,
    TextBlock,
    ToolDefinition,
    ToolResult,
    ToolTier,
)
from bridge.tools.image_store import ImageAsset, SessionImageStore, strip_data_url

logger = logging.getLogger(__name__)

# Hard ceiling on sources per call so a single view_image can't
# slam the context window with 30 high-res images at once. The
# common patterns (one screenshot; a/b/c/d critique) are well
# under this.
_MAX_SOURCES = 6

# Hard size cap per raw image, pre-downscale. Anything bigger is
# unlikely to be a real image the model needs to see; either it's
# a misclick on a non-image file or a corrupt download. The
# bridge's session-wide oversize-image sanitizer handles the
# Anthropic 5MiB inline-content cap downstream.
_MAX_RAW_BYTES = 50 * 1024 * 1024

# Default URL fetch timeout. Long-tail slow CDNs would block agent
# progress; 30s is plenty for a normal CDN image and short enough
# to fail fast on a wedged endpoint.
_URL_FETCH_TIMEOUT_S = 30.0

_IMAGE_EXTENSION_TO_MEDIA: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".heic": "image/heic",
    ".heif": "image/heif",
}


class ViewImageTool:
    """Materialize images into the model's next provider call.

    The session-local ``SessionImageStore`` is used to:
      · resolve ``ref`` sources (already-known images), and
      · register fresh bytes loaded from ``path``/``url`` so the agent
        can reference them in subsequent turns without re-fetching.
    """

    def __init__(self, image_store: SessionImageStore | None = None):
        self._image_store = image_store

    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="view_image",
            summary="Load one or more images into your own context window",
            tier=ToolTier.HOT,
            description="""View one or more images in your own context window. Use this BEFORE discussing, comparing, critiquing, or referencing any image — without viewing first, you literally cannot see the pixels.

Common patterns:
- After `generate_image`: in later turns, reload the produced image via its ref so you can re-inspect it after compaction.
- After `browser_screenshot`: the screenshot already lands in your context directly, but you can reload it later by ref.
- To pull a public image URL into context for analysis.
- To inspect a PNG/JPG/WebP/etc. saved on disk.

Each source must specify exactly one of:
- `ref`: a session image ref like `img_002` or an alias like `latest_image`, `latest_generated_image`, `latest_user_image`.
- `path`: an absolute path (with optional `~` expansion) to an image file. Supported types: png/jpg/jpeg/gif/webp/bmp/tiff/heic/heif.
- `url`: a public HTTP(S) URL to an image.

Limits: up to 6 sources per call; each source ≤ 50 MB raw. Loaded path/URL bytes are also registered in the session image store, so you get a fresh ref back (visible in the result text) for later reuse.""",
            parameters={
                "type": "object",
                "properties": {
                    "sources": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": _MAX_SOURCES,
                        "description": (
                            "Image sources to load. Each item must contain "
                            "exactly one of `ref`, `path`, or `url`."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "ref": {
                                    "type": "string",
                                    "description": (
                                        "Session image ref or alias. Examples: "
                                        "`img_002`, `latest_image`, "
                                        "`latest_generated_image`, "
                                        "`latest_user_image`."
                                    ),
                                },
                                "path": {
                                    "type": "string",
                                    "description": (
                                        "Absolute path to an image file. Supports "
                                        "`~` expansion."
                                    ),
                                },
                                "url": {
                                    "type": "string",
                                    "description": "Public HTTP(S) URL to an image.",
                                },
                            },
                        },
                    },
                    "note": {
                        "type": "string",
                        "description": (
                            "Optional context for why you're loading these "
                            "images (e.g. `comparing a/b/c morning-room "
                            "concepts`). Surfaced in the result summary."
                        ),
                    },
                },
                "required": ["sources"],
            },
        )

    async def execute(self, call_id: str, arguments: dict[str, Any]) -> ToolResult:
        sources = arguments.get("sources")
        if not isinstance(sources, list) or not sources:
            return ToolResult(
                call_id=call_id,
                content="Error: `sources` must be a non-empty array.",
                is_error=True,
            )
        if len(sources) > _MAX_SOURCES:
            return ToolResult(
                call_id=call_id,
                content=(
                    f"Error: at most {_MAX_SOURCES} sources per call "
                    f"(received {len(sources)})."
                ),
                is_error=True,
            )
        note = str(arguments.get("note") or "").strip()

        loaded: list[_LoadedImage] = []
        failures: list[str] = []
        for index, item in enumerate(sources, start=1):
            try:
                loaded.append(await self._load(item, index))
            except _LoadError as exc:
                failures.append(f"  · source {index}: {exc}")
            except Exception as exc:  # noqa: BLE001
                logger.exception("view_image source %d crashed", index)
                failures.append(f"  · source {index}: unexpected error: {exc}")

        if not loaded:
            text = "Loaded 0 images.\n\nFailures:\n" + "\n".join(failures)
            if note:
                text = f"Note: {note!r}\n\n{text}"
            return ToolResult(
                call_id=call_id,
                content=text,
                is_error=True,
            )

        summary_lines = [f"Loaded {len(loaded)} image(s):"]
        for img in loaded:
            summary_lines.append(f"  · {img.summary_line()}")
        if failures:
            summary_lines.append("")
            summary_lines.append(f"{len(failures)} failure(s):")
            summary_lines.extend(failures)
        if note:
            summary_lines.append("")
            summary_lines.append(f"Note: {note!r}")
        summary_text = "\n".join(summary_lines)

        # ImageBlocks ride alongside the summary text in the
        # ToolResult content. Each provider's _convert_messages
        # path is responsible for surfacing them to the wire format
        # the API expects (Anthropic native; OpenAI input_image array
        # items; Gemini FunctionResponsePart.inline_data).
        content_blocks: list[Any] = [TextBlock(text=summary_text)]
        for img in loaded:
            content_blocks.append(
                ImageBlock.from_base64(img.data_base64, img.media_type)
            )

        return ToolResult(
            call_id=call_id,
            content=content_blocks,
            is_error=False,
        )

    async def _load(self, item: Any, index: int) -> "_LoadedImage":
        if not isinstance(item, dict):
            raise _LoadError(
                "must be an object with exactly one of `ref`, `path`, or `url`"
            )

        provided = [k for k in ("ref", "path", "url") if str(item.get(k) or "").strip()]
        if not provided:
            raise _LoadError("missing one of `ref`, `path`, or `url`")
        if len(provided) > 1:
            raise _LoadError(
                f"provide exactly one of `ref`, `path`, or `url` "
                f"(got {', '.join(provided)})"
            )
        source_kind = provided[0]

        if source_kind == "ref":
            return self._load_from_ref(str(item["ref"]).strip())
        if source_kind == "path":
            return self._load_from_path(str(item["path"]).strip())
        return await self._load_from_url(str(item["url"]).strip())

    def _load_from_ref(self, ref: str) -> "_LoadedImage":
        if self._image_store is None:
            raise _LoadError(
                f"image ref {ref!r} cannot be resolved — no image store "
                "is attached to this session."
            )
        asset = self._image_store.resolve(ref)
        if asset is None:
            recent = ", ".join(a.ref for a in self._image_store.recent())
            hint = f" Recent refs: {recent}." if recent else ""
            raise _LoadError(f"ref {ref!r} not found.{hint}")
        return _LoadedImage(
            data_base64=asset.data_base64,
            media_type=asset.media_type or "image/png",
            assigned_ref=asset.ref,
            origin=f"ref:{ref}",
            aliases=self._aliases_for(asset.ref),
            byte_size=len(asset.data_base64) * 3 // 4,
        )

    def _load_from_path(self, raw_path: str) -> "_LoadedImage":
        if not raw_path:
            raise _LoadError("empty path")
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            raise _LoadError(
                "path must be absolute (use the full /Users/.../file.png form)"
            )
        if not path.exists():
            raise _LoadError(f"no such file: {path}")
        if not path.is_file():
            raise _LoadError(f"not a regular file: {path}")
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise _LoadError(f"could not stat {path}: {exc}") from exc
        if size <= 0:
            raise _LoadError(f"empty file: {path}")
        if size > _MAX_RAW_BYTES:
            raise _LoadError(
                f"file is {size} bytes; cap is {_MAX_RAW_BYTES} for "
                "view_image. Pre-downscale and retry."
            )
        media_type = _media_type_for_path(path)
        if media_type is None:
            raise _LoadError(
                f"unsupported image extension: {path.suffix}. Supported: "
                + ", ".join(sorted(_IMAGE_EXTENSION_TO_MEDIA))
            )
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise _LoadError(f"read failed for {path}: {exc}") from exc
        data_b64 = base64.b64encode(raw).decode("ascii")
        assigned_ref = self._register(
            data_b64,
            media_type,
            label=f"view_image path:{path.name}",
            source="view_image_path",
        )
        return _LoadedImage(
            data_base64=data_b64,
            media_type=media_type,
            assigned_ref=assigned_ref,
            origin=f"path:{path}",
            aliases=self._aliases_for(assigned_ref) if assigned_ref else [],
            byte_size=size,
        )

    async def _load_from_url(self, url: str) -> "_LoadedImage":
        if not url:
            raise _LoadError("empty url")
        lowered = url.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            raise _LoadError("url must start with http:// or https://")
        try:
            import httpx  # noqa: PLC0415
        except ImportError as exc:
            raise _LoadError("httpx not available — cannot fetch URL") from exc
        try:
            async with httpx.AsyncClient(
                timeout=_URL_FETCH_TIMEOUT_S, follow_redirects=True
            ) as client:
                response = await client.get(url)
                response.raise_for_status()
                raw = response.content
                content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
        except Exception as exc:  # noqa: BLE001
            raise _LoadError(f"fetch failed: {exc}") from exc
        if not raw:
            raise _LoadError("server returned an empty body")
        if len(raw) > _MAX_RAW_BYTES:
            raise _LoadError(
                f"response is {len(raw)} bytes; cap is {_MAX_RAW_BYTES}."
            )
        media_type = content_type if content_type.startswith("image/") else _media_type_from_magic(raw)
        if not media_type or not media_type.startswith("image/"):
            raise _LoadError(
                f"response is not an image (content-type={content_type!r})"
            )
        data_b64 = base64.b64encode(raw).decode("ascii")
        assigned_ref = self._register(
            data_b64,
            media_type,
            label=f"view_image url:{url[:80]}",
            source="view_image_url",
        )
        return _LoadedImage(
            data_base64=data_b64,
            media_type=media_type,
            assigned_ref=assigned_ref,
            origin=f"url:{url}",
            aliases=self._aliases_for(assigned_ref) if assigned_ref else [],
            byte_size=len(raw),
        )

    def _register(
        self,
        data_b64: str,
        media_type: str,
        *,
        label: str,
        source: str,
    ) -> str | None:
        """Register a freshly-loaded image in the session store and
        bump `latest_image`. Returns the assigned ref, or None if no
        store is wired up."""
        if self._image_store is None:
            return None
        try:
            asset = self._image_store.add_base64(
                strip_data_url(data_b64),
                media_type,
                label=label,
                source=source,
                aliases=("latest_image",),
            )
        except Exception:  # noqa: BLE001
            logger.exception("view_image: image_store registration failed")
            return None
        return asset.ref

    def _aliases_for(self, ref: str) -> list[str]:
        if self._image_store is None or not ref:
            return []
        # SessionImageStore doesn't expose this directly; reach into
        # the internal alias dict. Safe because the store is local to
        # this process and we're a sibling module.
        aliases = [
            alias
            for alias, target in self._image_store._aliases.items()  # noqa: SLF001
            if target == ref
        ]
        return sorted(aliases)


class _LoadError(Exception):
    """Per-source failure that doesn't abort the whole tool call."""


class _LoadedImage:
    """Successfully resolved image with metadata for the summary."""

    __slots__ = (
        "data_base64",
        "media_type",
        "assigned_ref",
        "origin",
        "aliases",
        "byte_size",
    )

    def __init__(
        self,
        *,
        data_base64: str,
        media_type: str,
        assigned_ref: str | None,
        origin: str,
        aliases: list[str],
        byte_size: int,
    ) -> None:
        self.data_base64 = data_base64
        self.media_type = media_type
        self.assigned_ref = assigned_ref
        self.origin = origin
        self.aliases = aliases
        self.byte_size = byte_size

    def summary_line(self) -> str:
        bits = []
        if self.assigned_ref:
            bits.append(f"ref:{self.assigned_ref}")
        bits.append(self.origin)
        if self.aliases:
            bits.append("aliases: " + ", ".join(self.aliases))
        bits.append(self.media_type)
        bits.append(_format_byte_size(self.byte_size))
        return " | ".join(bits)


def _media_type_for_path(path: Path) -> str | None:
    return _IMAGE_EXTENSION_TO_MEDIA.get(path.suffix.lower())


def _media_type_from_magic(raw: bytes) -> str | None:
    """Sniff the mime type from the first few bytes. Used as a
    fallback when an HTTP response doesn't set a useful content-type."""
    head = raw[:16]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"BM"):
        return "image/bmp"
    return None


def _format_byte_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KiB"
    return f"{n / (1024 * 1024):.2f} MiB"


# `os` is imported above for future use (e.g. env-gated debugging);
# keep the reference live so the linter doesn't strip it.
_ = os
