"""Content-addressed store for image bytes, shared by every session.

Transcripts, raw message logs, compaction snapshots and the per-session
event mirror used to carry each image as inline base64, often in several
of those files at once, so images were most of ``~/.freyja/sessions``.
At those persistence boundaries an image's bytes now go here once, keyed
by their SHA-256, and the file records ``{"sha256": ...}`` instead.
Loading resolves the hash back to the same base64 string, so the
in-memory model (``ImageBlock.data``) is unchanged.

Layout: ``$FREYJA_HOME/media/images/<sha[:2]>/<sha>.<ext>``. Blobs are
never deleted: sessions (including forks, which share hashes) and the
training corpus refer to them.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_EXT_FOR_MEDIA_TYPE = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}


def media_root() -> Path:
    """Root of the media store. Read per call so tests (and FREYJA_HOME)
    redirect it."""
    home = os.environ.get("FREYJA_HOME") or os.path.expanduser("~/.freyja")
    return Path(home) / "media"


def image_ext(media_type: str) -> str:
    return _EXT_FOR_MEDIA_TYPE.get(media_type, "bin")


def image_path(sha256: str, media_type: str) -> Path:
    return media_root() / "images" / sha256[:2] / f"{sha256}.{image_ext(media_type)}"


def canonical_bytes(data: str) -> bytes | None:
    """Decoded bytes of ``data`` if it is canonical base64 (re-encoding
    reproduces it exactly), else None. Only canonical payloads are stored,
    so loading returns the identical string and prompt-cache prefixes
    don't shift across a save/restore."""
    try:
        raw = base64.b64decode(data, validate=True)
    except (binascii.Error, ValueError):
        return None
    return raw if base64.b64encode(raw).decode("ascii") == data else None


def put_image(data: str, media_type: str) -> str | None:
    """Store a base64 image; return its SHA-256, or None if it can't be
    stored losslessly (non-canonical base64, disk error)."""
    raw = canonical_bytes(data)
    if raw is None:
        return None
    sha = hashlib.sha256(raw).hexdigest()
    path = image_path(sha, media_type)
    if path.exists():
        return sha
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(raw)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    except OSError as exc:
        logger.warning("media store: could not write %s: %s", path, exc)
        return None
    return sha


def get_image(sha256: str, media_type: str) -> str | None:
    """Base64 for a stored image, or None if its blob is missing."""
    try:
        return base64.b64encode(image_path(sha256, media_type).read_bytes()).decode("ascii")
    except OSError:
        return None


def image_sha256(block: Any) -> str:
    """SHA-256 of an ImageBlock's decoded bytes, cached on the block for as
    long as its ``data`` string is the same object (a resize assigns a new
    string, which invalidates the cache)."""
    data = block.data
    cached = block.__dict__.get("_sha256_cache")
    if cached is not None and cached[0] is data:
        return cached[1]
    sha = hashlib.sha256(base64.b64decode(data, validate=False)).hexdigest()
    block.__dict__["_sha256_cache"] = (data, sha)
    return sha


def externalize_images(obj: Any) -> Any:
    """Move inline base64 out of serialized content blocks, in place.

    Walks dicts and lists; every ``{"type": "image", "source_type":
    "base64", "data": ...}`` whose bytes are safely stored becomes
    ``{..., "sha256": ...}`` without ``data``. Anything that can't be
    stored keeps its inline data, so nothing is ever dropped. Returns
    ``obj`` for chaining.
    """
    if isinstance(obj, list):
        for item in obj:
            externalize_images(item)
    elif isinstance(obj, dict):
        if (
            obj.get("type") == "image"
            and obj.get("source_type", "base64") == "base64"
            and isinstance(obj.get("data"), str)
            and obj["data"]
        ):
            sha = put_image(obj["data"], obj.get("media_type", "image/png"))
            if sha is not None:
                del obj["data"]
                obj["sha256"] = sha
        else:
            for value in obj.values():
                if isinstance(value, (dict, list)):
                    externalize_images(value)
    return obj
