"""Per-session permission-request → Slack Block Kit bridge.

Distinct from ``SlackStreamConsumer`` (which is per-TURN): a
``SlackPermissionListener`` is registered once when a gateway session
first receives a message and stays registered for the life of the
daemon. This matters for **background-mode sub-agents** — they keep
running after their parent's turn completes, and any
``permission_request`` they emit fires AFTER the parent's consumer is
already finalized + unregistered.

With this listener in place, the permission flow on the Slack side
looks identical regardless of whether the request comes from the
parent's foreground tool call or a background child's:

  · daemon emits ``permission_request``
  · listener posts a Block Kit Approve/Deny message in the originating
    thread
  · operator clicks → ``slack.py:_handle_approval_click`` →
    ``approval.resolve_approval`` → external resolver →
    ``session.permission_handler.resolve``

Permission events scoped to the same ``sessionId`` route here exactly
once (no duplicate Block Kit posts) because the consumer no longer
handles ``permission_request`` / ``permission_resolved`` — those event
types were removed from its ``on_event`` filter.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


class SlackPermissionListener:
    """Owns the permission-prompt lifecycle for one gateway session.

    Hold-once semantics: register a single instance per session via
    ``bridge.freyja_bridge.register_session_listener`` and don't
    unregister. The daemon process is the listener's lifetime; the
    closure costs a few hundred bytes.

    The listener captures the originating ``MessageSource`` from the
    first inbound message in the session and uses it for every future
    Block Kit post. Slack threads don't move, and a session's
    chat_id / thread_ts pair is stable for the session's lifetime, so
    the captured source stays valid even as new messages arrive.
    """

    def __init__(
        self,
        adapter: Any,
        session_ref: Any,
        source: Any,
        reply_thread_id: str | None,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.adapter = adapter
        self.session_ref = session_ref
        self.source = source
        self.reply_thread_id = reply_thread_id
        self._loop = loop

    # ── sync entry point fired by emit() ──

    def on_event(self, event: dict[str, Any]) -> None:
        etype = event.get("type")
        if etype == "permission_request":
            try:
                asyncio.run_coroutine_threadsafe(
                    self._handle_request(event), self._loop,
                )
            except RuntimeError:
                # Loop is shut down; nothing we can do.
                pass
        elif etype == "permission_resolved":
            # Synchronous: just drop the resolver entry — no Slack
            # round-trip needed. The Block Kit message was already
            # replaced with an approved/denied footer by the click
            # handler in slack.py.
            self._on_resolved(event)

    # ── implementation ──

    async def _handle_request(self, event: dict[str, Any]) -> None:
        request_id = str(event.get("requestId") or "")
        if not request_id:
            return
        prompt = str(event.get("prompt") or "permission required")
        reason = str(event.get("reason") or "") or str(event.get("details") or "")
        level = str(event.get("level") or "medium")
        handler = getattr(self.session_ref, "permission_handler", None)
        if handler is None:
            logger.warning(
                "permission_request %s arrived for session with no handler",
                request_id,
            )
            return
        from bridge.gateway.approval import register_external_resolver
        register_external_resolver(
            request_id,
            lambda approved: bool(handler.resolve(
                request_id, approved,
                "slack-approve" if approved else "slack-deny",
            )),
        )
        try:
            result = await self.adapter.send_approval_request(
                chat_id=self.source.chat_id,
                request_id=request_id,
                tool_name=f"permission · {level}",
                command_preview=prompt,
                reason=reason,
                thread_id=self.reply_thread_id,
            )
            if not getattr(result, "ok", False):
                logger.warning(
                    "Block Kit approval post failed for %s: %s",
                    request_id, getattr(result, "error", "?"),
                )
        except Exception:  # noqa: BLE001
            logger.exception("send_approval_request raised for %s", request_id)

    def _on_resolved(self, event: dict[str, Any]) -> None:
        request_id = str(event.get("requestId") or "")
        if not request_id:
            return
        from bridge.gateway.approval import unregister_external_resolver
        unregister_external_resolver(request_id)
