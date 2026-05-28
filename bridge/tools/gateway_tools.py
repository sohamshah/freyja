"""Tools that only make sense in gateway sessions (Slack, etc.).

A gateway session has a stashed ``gateway_source`` describing where
its messages came from — workspace, channel/DM, thread, sender. These
tools use that context to act on the external platform: send files,
react with emoji, set status, etc. They're conditionally registered
in ``_BridgeSession.initialize()`` only when ``gateway_source`` is
present, so the desktop session never sees them.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable

from bridge.tools.base import ToolDefinition, ToolResult

logger = logging.getLogger(__name__)


# Sensible upload caps — Slack's per-file limit is 1 GB but anything
# over ~50 MB is impractical to send back through a chat surface and
# Slack frequently rejects with rate-limit / size errors before then.
_MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024
_MAX_FILES_PER_CALL = 10


def _build_send_attachment_definition() -> ToolDefinition:
    description = (
        "Send one or more local files (images, documents, etc.) "
        "back to the user via the active gateway chat (Slack, etc.). "
        "Use this when the user asks you to share a file, show an "
        "image you found on disk, or attach a generated artifact. "
        "Files are uploaded as native chat attachments (one upload "
        "group, optional caption above). Up to 10 files per call; "
        "split into multiple calls for larger batches. Each file "
        "must already exist on disk — use other tools (read_file, "
        "glob, generate_image) to produce or locate the file first."
    )
    return ToolDefinition(
        name="send_attachment",
        description=description,
        summary="Send local files back to the chat (Slack DM / channel).",
        parameters={
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Absolute filesystem paths of files to attach. "
                        "Tilde + relative paths get expanded but must "
                        "resolve to a real file."
                    ),
                },
                "caption": {
                    "type": "string",
                    "description": (
                        "Optional one-paragraph caption that appears "
                        "above the attachments. Keep brief."
                    ),
                },
            },
            "required": ["paths"],
        },
    )


class SendAttachmentTool:
    """Wraps the active platform adapter's ``upload_files`` so the
    agent can deliberately send files back to the chat.

    Bound at session-init time with a CALLABLE that returns the
    session's current gateway_source. We can't snapshot the source
    at init time because chat_id/thread_id change as the user moves
    between top-level DMs and threads — snapshotting would always
    send to whichever thread the session was first created in
    (classic "image arrives in the wrong place" bug).

    Implements the duck-typed Tool protocol (``definition`` property
    + ``execute(call_id, arguments)``).
    """

    def __init__(
        self,
        *,
        get_gateway_source: Callable[[], Any],
    ) -> None:
        self._get_gateway_source = get_gateway_source
        self._defn = _build_send_attachment_definition()

    @property
    def definition(self) -> ToolDefinition:
        return self._defn

    async def execute(
        self,
        call_id: str,
        arguments: dict[str, Any],
    ) -> ToolResult:
        import json as _json

        args = arguments or {}
        paths_raw = args.get("paths")
        caption = args.get("caption") or None

        # The LLM occasionally emits `paths` as a JSON-encoded STRING
        # ("[\"/path/foo.png\"]") instead of a real array. Anthropic
        # tool-use can serialize array fields as strings under load /
        # when the model is uncertain. Be defensive: if we get a
        # string that looks like a JSON array, parse it. If we get a
        # bare string that looks like a path, wrap it in a list. Both
        # are forgiving paths that recover from real-world LLM quirks
        # without losing the call.
        if isinstance(paths_raw, str):
            stripped = paths_raw.strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                try:
                    parsed = _json.loads(stripped)
                    if isinstance(parsed, list):
                        paths_raw = parsed
                except (ValueError, TypeError):
                    pass
            if isinstance(paths_raw, str):
                # Single path emitted as bare string — wrap.
                paths_raw = [paths_raw]
        if paths_raw is None:
            paths_raw = []

        if not isinstance(paths_raw, list) or not paths_raw:
            return ToolResult(
                call_id=call_id,
                content=(
                    "send_attachment: `paths` must be a non-empty list "
                    "of filesystem paths. Got: "
                    + repr(args.get("paths"))[:200]
                ),
                is_error=True,
            )
        if len(paths_raw) > _MAX_FILES_PER_CALL:
            return ToolResult(
                call_id=call_id,
                content=(
                    f"send_attachment: too many files in one call "
                    f"({len(paths_raw)}). Max {_MAX_FILES_PER_CALL} "
                    f"per call — split into batches."
                ),
                is_error=True,
            )

        # Validate every path before touching the network.
        valid_paths: list[Path] = []
        errors: list[str] = []
        for raw in paths_raw:
            try:
                p = Path(str(raw)).expanduser().resolve()
            except Exception:
                errors.append(f"unparseable path: {raw!r}")
                continue
            if not p.exists():
                errors.append(f"file not found: {p}")
                continue
            if not p.is_file():
                errors.append(f"not a regular file: {p}")
                continue
            try:
                size = p.stat().st_size
            except OSError as exc:
                errors.append(f"stat failed for {p}: {exc}")
                continue
            if size > _MAX_FILE_SIZE_BYTES:
                errors.append(
                    f"file too large for chat send "
                    f"({size // (1024 * 1024)} MB > 50 MB): {p}"
                )
                continue
            valid_paths.append(p)
        if not valid_paths:
            return ToolResult(
                call_id=call_id,
                content=(
                    "send_attachment: no valid files to send. "
                    + " · ".join(errors)
                ),
                is_error=True,
            )

        # Resolve the CURRENT gateway_source from the session — not
        # whatever was captured at session-init time. The session
        # attribute is overwritten on every inbound message via
        # session_router.route(), so reading it at call time gives
        # us the chat_id + thread_id for the message that actually
        # triggered the agent's current turn. Snapshotting at init
        # caused image uploads to always land in the FIRST thread
        # the session was opened in, even when the user later moved
        # to a different thread.
        gateway_source = self._get_gateway_source()
        if gateway_source is None:
            return ToolResult(
                call_id=call_id,
                content=(
                    "send_attachment: no active gateway source on this "
                    "session. The session may have been created by a "
                    "non-gateway code path; cannot route attachments."
                ),
                is_error=True,
            )

        # Find the adapter for this session's platform via the
        # approval module's registry (same registry doubles as a
        # platform-name → live adapter lookup).
        from bridge.gateway.approval import get_approval_adapter

        platform_obj = getattr(gateway_source, "platform", None)
        platform_name = (
            getattr(platform_obj, "value", None)
            or str(platform_obj or "")
        )
        adapter = get_approval_adapter(platform_name)
        if adapter is None:
            return ToolResult(
                call_id=call_id,
                content=(
                    f"send_attachment: no live adapter for platform "
                    f"`{platform_name}`. The gateway daemon may not be "
                    f"connected. Files were NOT sent."
                ),
                is_error=True,
            )

        from bridge.gateway.platforms.base import UploadItem
        items = [
            UploadItem(path=str(p), filename=p.name)
            for p in valid_paths
        ]
        chat_id = str(getattr(gateway_source, "chat_id", "") or "")
        # Mirror the stream consumer's reply-threading logic so
        # attachments land in the same thread as the text response
        # they accompany. If the inbound has a real thread_id, use
        # it. Else for DMs with reply_in_thread on (default), anchor
        # to the user's message so the file is grouped with the
        # text response under the same thread.
        explicit_thread = getattr(gateway_source, "thread_id", None)
        if explicit_thread:
            thread_id: str | None = explicit_thread
        else:
            chat_type = getattr(gateway_source, "chat_type", None)
            message_id = getattr(gateway_source, "message_id", None)
            cfg = getattr(adapter, "slack_config", None)
            reply_in_thread = getattr(cfg, "reply_in_thread", True)
            if chat_type == "dm" and reply_in_thread and message_id:
                thread_id = str(message_id)
            else:
                thread_id = None
        if not chat_id:
            return ToolResult(
                call_id=call_id,
                content=(
                    "send_attachment: gateway_source has no chat_id — "
                    "cannot route attachments anywhere. This is a "
                    "session-routing bug; report it."
                ),
                is_error=True,
            )

        try:
            res = await adapter.upload_files(
                chat_id,
                items,
                thread_id=thread_id,
                initial_comment=caption,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("send_attachment upload_files raised")
            return ToolResult(
                call_id=call_id,
                content=f"send_attachment: upload raised: {exc}",
                is_error=True,
            )

        if not getattr(res, "ok", False):
            return ToolResult(
                call_id=call_id,
                content=(
                    "send_attachment: upload failed: "
                    + str(getattr(res, "error", "unknown error"))
                ),
                is_error=True,
            )

        names = ", ".join(p.name for p in valid_paths)
        warn = (
            "\n\n(note: " + " · ".join(errors) + " — these were skipped)"
            if errors else ""
        )
        return ToolResult(
            call_id=call_id,
            content=(
                f"Sent {len(valid_paths)} file(s) to the chat: {names}."
                + warn
            ),
            is_error=False,
        )
