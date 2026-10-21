"""Shared adapter double for the Slack stream consumer tests.

Records what the consumer streams (markdown chunks on append and stop,
task cards, block chunks) and what it posts with ``send``. ``body`` is
the prose as Slack would show it: the streamed chunks in order, then
any fallback post.
"""

from __future__ import annotations

from typing import Any

from bridge.gateway.platforms.base import SendResult


class StreamingFakeAdapter:
    max_message_chars = 39_000

    def __init__(self, *, start_ok: bool = True) -> None:
        self.sent: list[str] = []
        self.streamed: list[str] = []
        self.cards: list[tuple[str, str, str]] = []
        # (card id, output) for every task chunk that carried one.
        self.card_outputs: list[tuple[str, str]] = []
        self.plans: list[str] = []
        self.block_chunks: list[list[dict[str, Any]]] = []
        self.stop_calls: list[dict[str, Any]] = []
        # Kind of every chunk in the order it reached the stream.
        self.order: list[str] = []
        self.statuses: list[str] = []
        self.streams_started = 0
        self.streams_stopped: list[str] = []
        self._start_ok = start_ok

    def format_content(self, content: str) -> str:
        return content

    @property
    def body(self) -> str:
        return "".join(self.streamed) + "".join(self.sent)

    def _record(self, chunks: list[Any] | None) -> None:
        for chunk in chunks or []:
            if hasattr(chunk, "status"):
                self.order.append("card")
                self.cards.append((chunk.id, chunk.title, chunk.status))
                if getattr(chunk, "output", None):
                    self.card_outputs.append((chunk.id, str(chunk.output)))
            elif hasattr(chunk, "blocks"):
                self.order.append("blocks")
                self.block_chunks.append(list(chunk.blocks))
            elif hasattr(chunk, "title"):
                self.plans.append(chunk.title)
            elif hasattr(chunk, "text"):
                self.order.append("text")
                self.streamed.append(chunk.text)

    async def send(self, chat_id: str, content: str, **kwargs: Any) -> SendResult:
        self.sent.append(content)
        return SendResult(ok=True, message_id="1.0")

    async def start_stream(self, *a: Any, **kw: Any) -> SendResult:
        if not self._start_ok:
            return SendResult(ok=False, error="not_allowed")
        self.streams_started += 1
        ts = "stream-ts" if self.streams_started == 1 else f"stream-ts-{self.streams_started}"
        return SendResult(ok=True, message_id=ts)

    async def append_stream(self, chat_id: str, ts: str, *, chunks: list[Any] | None = None, **kw: Any) -> SendResult:
        self._record(chunks)
        return SendResult(ok=True)

    async def stop_stream(self, chat_id: str, ts: str, **kw: Any) -> SendResult:
        self._record(kw.get("chunks"))
        self.stop_calls.append(dict(kw))
        self.streams_stopped.append(ts)
        return SendResult(ok=True)

    async def set_session_status(self, chat_id: str, *, thread_id: str | None = None, status: str, **kw: Any) -> SendResult:
        self.statuses.append(status)
        return SendResult(ok=True)

    async def send_typing(self, *a: Any, **kw: Any) -> None:
        self.statuses.append("processing")

    async def stop_typing(self, *a: Any, **kw: Any) -> None:
        self.statuses.append("active")
