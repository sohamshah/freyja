"""Tiny in-memory image reference store for a Freyja session."""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class ImageAsset:
    ref: str
    data_base64: str
    media_type: str
    label: str = ""
    source: str = ""
    created_at: float = 0.0


def strip_data_url(data: str) -> str:
    raw = data.strip()
    if "," in raw and raw.split(",", 1)[0].startswith("data:"):
        raw = raw.split(",", 1)[1]
    return raw.strip()


class SessionImageStore:
    """Session-local image refs used by image generation/edit tools.

    This is intentionally process-local and boring: attachments and generated
    outputs are already in the transcript/UI. The store only gives tools stable
    names so agents can say "edit img_003" without carrying base64 in tool args.
    """

    def __init__(self) -> None:
        self._next = 0
        self._assets: dict[str, ImageAsset] = {}
        self._aliases: dict[str, str] = {}

    def add_base64(
        self,
        data_base64: str,
        media_type: str = "image/png",
        *,
        label: str = "",
        source: str = "",
        aliases: list[str] | tuple[str, ...] = (),
    ) -> ImageAsset:
        data = strip_data_url(data_base64)
        # Validate early so bad attachment payloads do not become refs.
        base64.b64decode(data, validate=False)

        self._next += 1
        ref = f"img_{self._next:03d}"
        asset = ImageAsset(
            ref=ref,
            data_base64=data,
            media_type=media_type or "image/png",
            label=label,
            source=source,
            created_at=time.time(),
        )
        self._assets[ref] = asset
        for alias in aliases:
            if alias:
                self._aliases[alias] = ref
        return asset

    def resolve(self, ref: str) -> ImageAsset | None:
        key = ref.strip()
        if not key:
            return None
        return self._assets.get(self._aliases.get(key, key))

    def recent(self, limit: int = 8) -> list[ImageAsset]:
        assets = sorted(self._assets.values(), key=lambda item: item.created_at)
        return assets[-limit:]

    def refs_line(self, refs: list[str]) -> str:
        concrete = [ref for ref in refs if self.resolve(ref)]
        if not concrete:
            return ""
        return "Available image refs for generate_image input_images: " + ", ".join(
            f"`{ref}`" for ref in concrete
        )
