"""Fit inline images to the provider's per-side pixel limits.

Anthropic caps each image at 8000px per side, and at 2000px per side once
a single request carries more than 20 image blocks (earlier turns and
images nested in tool results count). A long session crosses 20 images
without anyone noticing, and from then on one tall screenshot rejects
every request with "image dimensions exceed max allowed size for
many-image requests: 2000 pixels" until the image is shrunk. See
https://platform.claude.com/docs/en/build-with-claude/vision#request-limits

The model never sees more than ~2576px on the long edge anyway (the API
downscales past that), so shrinking to 2000px costs little fidelity.
"""

from __future__ import annotations

import base64
import binascii
import logging
from io import BytesIO

logger = logging.getLogger(__name__)

MAX_IMAGE_DIM = 8000
"""Per-side pixel cap for any image."""

MANY_IMAGE_THRESHOLD = 20
"""More image blocks than this in one request tightens the per-side cap."""

MANY_IMAGE_MAX_DIM = 2000
"""Per-side pixel cap once a request is over ``MANY_IMAGE_THRESHOLD``."""

# Bedrock and Vertex cap each image at 5 MB of base64 (the direct API
# allows 10 MB). Stay under the stricter one with ~10% headroom.
_MAX_B64_BYTES = 4_700_000

# Enough base64 to cover the PNG/GIF/WebP header and most JPEG SOF markers
# without decoding a multi-MB payload on every request.
_HEADER_B64_CHARS = 64 * 1024


def request_image_dim_limit(image_count: int) -> int:
    """The per-side pixel cap that applies to a request with ``image_count`` images."""
    return MANY_IMAGE_MAX_DIM if image_count > MANY_IMAGE_THRESHOLD else MAX_IMAGE_DIM


def image_size(data: str) -> tuple[int, int] | None:
    """``(width, height)`` of a base64 image, or None if it can't be read.

    Parses only the header from a prefix of the payload, falling back to a
    full decode when the header sits deeper (e.g. JPEGs with large EXIF).
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    for chunk in (data[:_HEADER_B64_CHARS], data):
        try:
            with Image.open(BytesIO(base64.b64decode(chunk, validate=False))) as img:
                return img.size
        except (OSError, ValueError, binascii.Error, SyntaxError):
            if len(chunk) == len(data):
                return None
    return None


def shrink_image(data: str, media_type: str, max_dim: int) -> tuple[str, str] | None:
    """Re-encode a base64 image so neither side exceeds ``max_dim``.

    Keeps PNG as PNG (screenshots of text stay sharp) and JPEG/WebP in
    their own format, falling back to JPEG if the result would be over the
    per-image byte cap. Returns ``(new_data, new_media_type)``, or None
    when the image can't be decoded — callers must then drop the block,
    since sending it would be rejected.
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        img = Image.open(BytesIO(base64.b64decode(data, validate=False)))
        img.load()
    except (OSError, ValueError, binascii.Error, SyntaxError) as exc:
        logger.warning("image shrink: cannot decode image: %s", exc)
        return None

    w, h = img.size
    scale = max_dim / max(w, h)
    if scale < 1.0:
        img = img.resize(
            (max(1, int(w * scale)), max(1, int(h * scale))),
            Image.Resampling.LANCZOS,
        )

    fmt = {"image/jpeg": "JPEG", "image/webp": "WEBP"}.get(media_type, "PNG")
    out = _encode(img, fmt)
    if len(out) > _MAX_B64_BYTES and fmt != "JPEG":
        fmt = "JPEG"
        out = _encode(img, fmt)
    return out, f"image/{fmt.lower()}"


def _encode(img, fmt: str) -> str:
    if fmt == "JPEG" and img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    elif fmt == "PNG" and img.mode not in ("RGB", "RGBA", "L", "LA", "P"):
        img = img.convert("RGBA")
    buf = BytesIO()
    if fmt == "JPEG":
        img.save(buf, format="JPEG", quality=88, optimize=True)
    else:
        img.save(buf, format=fmt, optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")
