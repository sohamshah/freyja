"""Send images to the Anthropic API by file ID instead of inline base64.

Every request resends the whole history, so a session with dozens of
screenshots uploads the same megabytes on every turn. The Files API takes
an image once and lets requests refer to it as
``{"type": "image", "source": {"type": "file", "file_id": ...}}``, which
also works inside ``tool_result`` content.

Prompt caching keys on the exact request bytes, so an image should switch
from inline to file reference once and then stay put:

- An image in the newest message goes inline the first time; its upload
  runs in the background, and the next request refers to it by ID.
- An image deeper in history with no upload yet (a restored session) is
  uploaded before the request, with a time limit, so the whole backlog
  switches in one step instead of several.
- A failed upload pins that image inline for the rest of the process.

Uploads are keyed by the SHA-256 of the bytes actually sent (a resize makes
a new upload) and recorded in ``$FREYJA_HOME/media/anthropic-files.jsonl``,
shared by processes and sub-agents using the same API key (stored only as
a fingerprint). Files expire after 30 days; entries are treated as expired
a day early and a 404 drops the IDs involved, so a stale ID costs one
retry, not a broken session.

Only for the first-party API: Bedrock and Vertex have no Files API.
The Files API is not ZDR-eligible; set FREYJA_ANTHROPIC_FILE_REFS=0 to
keep every image inline.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from typing import Any
from urllib.parse import urlparse

from engine.media_store import image_ext, image_sha256, media_root
from engine.types import ImageBlock, Message, ToolResultBlock

logger = logging.getLogger(__name__)

FILE_TTL_SECONDS = 30 * 24 * 3600
_EXPIRY_MARGIN_SECONDS = 24 * 3600
# Below this the upload round-trip isn't worth it.
_MIN_B64_CHARS = 16_384
_BACKLOG_WAIT_SECONDS = 20.0
_MAX_CONCURRENT_UPLOADS = 4


def file_refs_enabled(base_url: str | None) -> bool:
    """File references only go to the first-party API, and can be turned off."""
    setting = os.environ.get("FREYJA_ANTHROPIC_FILE_REFS", "1").strip().lower()
    if setting in ("0", "false", "no", "off"):
        return False
    return base_url is None or urlparse(base_url).hostname == "api.anthropic.com"


def _iter_images(messages: list[Message]):
    """(message_index, ImageBlock) for every image, nested tool results included."""
    for i, msg in enumerate(messages):
        if not isinstance(msg.content, list):
            continue
        for block in msg.content:
            if isinstance(block, ImageBlock):
                yield i, block
            elif isinstance(block, ToolResultBlock) and isinstance(block.content, list):
                for sub in block.content:
                    if isinstance(sub, ImageBlock):
                        yield i, sub


class AnthropicImageFiles:
    """Upload cache and per-request choice of file reference vs inline."""

    def __init__(self, async_client: Any, api_key: str) -> None:
        self._client = async_client
        self._key = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]
        self._ids: dict[str, tuple[str, float]] = {}  # sha -> (file_id, expires_at)
        self._pending: dict[str, asyncio.Task] = {}
        self._inline_only: set[str] = set()
        self._index_mtime: float | None = None
        self._semaphore: asyncio.Semaphore | None = None

    # ── lookup ────────────────────────────────────────────────────────────

    def source_for(self, block: ImageBlock) -> dict[str, Any] | None:
        """The file source for an uploaded image, else None (send inline)."""
        if not self._eligible(block):
            return None
        entry = self._lookup(image_sha256(block))
        return {"type": "file", "file_id": entry} if entry else None

    def _eligible(self, block: ImageBlock) -> bool:
        return block.source_type == "base64" and len(block.data) >= _MIN_B64_CHARS

    def _lookup(self, sha: str) -> str | None:
        entry = self._ids.get(sha)
        if entry is None and self._reload_index():
            entry = self._ids.get(sha)
        if entry is None or entry[1] - _EXPIRY_MARGIN_SECONDS <= time.time():
            return None
        return entry[0]

    # ── per-request preparation ───────────────────────────────────────────

    async def prepare(self, messages: list[Message]) -> None:
        """Start uploads for images not yet uploaded; wait (bounded) for the
        ones below the newest message, see the module docstring."""
        if not messages:
            return
        last = len(messages) - 1
        backlog: list[asyncio.Task] = []
        for index, block in _iter_images(messages):
            if not self._eligible(block):
                continue
            sha = image_sha256(block)
            if sha in self._inline_only or self._lookup(sha):
                continue
            task = self._pending.get(sha)
            if task is None:
                task = asyncio.get_running_loop().create_task(
                    self._upload(sha, block.data, block.media_type)
                )
                self._pending[sha] = task
                task.add_done_callback(lambda _t, s=sha: self._pending.pop(s, None))
            if index < last:
                backlog.append(task)
        if backlog:
            done, _ = await asyncio.wait(backlog, timeout=_BACKLOG_WAIT_SECONDS)
            if len(done) < len(backlog):
                logger.info(
                    "image uploads: %d of %d still running after %.0fs; sending those inline",
                    len(backlog) - len(done), len(backlog), _BACKLOG_WAIT_SECONDS,
                )

    async def _upload(self, sha: str, data: str, media_type: str) -> None:
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(_MAX_CONCURRENT_UPLOADS)
        name = f"{sha[:16]}.{image_ext(media_type)}"
        try:
            async with self._semaphore:
                meta = await self._client.beta.files.upload(
                    file=(name, base64.b64decode(data), media_type),
                    extra_body={"expires_in_seconds": FILE_TTL_SECONDS},
                )
        except Exception as exc:  # noqa: BLE001 — any failure means "stay inline"
            self._inline_only.add(sha)
            logger.warning("image upload failed; keeping it inline: %s", exc)
            return
        expires_at = time.time() + FILE_TTL_SECONDS
        self._ids[sha] = (meta.id, expires_at)
        self._append_index(
            {"key": self._key, "sha": sha, "file_id": meta.id, "expires_at": expires_at}
        )

    # ── invalidation ──────────────────────────────────────────────────────

    def forget_request_files(self, request_kwargs: dict[str, Any]) -> int:
        """Drop every file ID a failed request referenced (the API said one
        is gone; its neighbours may be too). The next request sends those
        images inline and re-uploads them."""
        file_ids: set[str] = set()

        def _walk(obj: Any) -> None:
            if isinstance(obj, list):
                for item in obj:
                    _walk(item)
            elif isinstance(obj, dict):
                source = obj.get("source")
                if (
                    obj.get("type") == "image"
                    and isinstance(source, dict)
                    and source.get("type") == "file"
                ):
                    file_ids.add(str(source.get("file_id")))
                for value in obj.values():
                    if isinstance(value, (dict, list)):
                        _walk(value)

        _walk(request_kwargs.get("messages"))
        gone = [sha for sha, (fid, _) in self._ids.items() if fid in file_ids]
        for sha in gone:
            del self._ids[sha]
            self._append_index({"key": self._key, "sha": sha, "gone": True})
        return len(gone)

    # ── shared index ──────────────────────────────────────────────────────

    def _index_path(self):
        return media_root() / "anthropic-files.jsonl"

    def _reload_index(self) -> bool:
        """Re-read the shared index if another process appended to it."""
        path = self._index_path()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False
        if mtime == self._index_mtime:
            return False
        self._index_mtime = mtime
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("key") != self._key or not row.get("sha"):
                continue
            if row.get("gone"):
                self._ids.pop(row["sha"], None)
            elif row.get("file_id"):
                self._ids[row["sha"]] = (row["file_id"], float(row.get("expires_at") or 0))
        return True

    def _append_index(self, row: dict[str, Any]) -> None:
        """One short line per append (O_APPEND), safe across processes."""
        path = self._index_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(row, separators=(",", ":")) + "\n")
        except OSError as exc:
            logger.warning("image upload index not written: %s", exc)
