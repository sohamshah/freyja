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
        # Per stream: the chunks in order and the blocks on the seal, so
        # read_message hands back the message the way Slack stores it
        # (prose as rich_text, the cards as task_card blocks merged by id).
        self.stream_chunks: dict[str, list[Any]] = {}
        self.stop_blocks: dict[str, list[dict[str, Any]]] = {}
        # What update_message_blocks wrote, per message ts.
        self.updated: dict[str, dict[str, Any]] = {}
        # Errors for update_message_blocks to answer, one per call.
        self.update_errors: list[str] = []
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
        self.stream_chunks.setdefault(ts, []).extend(chunks or [])
        return SendResult(ok=True)

    async def stop_stream(self, chat_id: str, ts: str, **kw: Any) -> SendResult:
        self._record(kw.get("chunks"))
        self.stream_chunks.setdefault(ts, []).extend(kw.get("chunks") or [])
        self.stop_blocks[ts] = list(kw.get("blocks") or [])
        self.stop_calls.append(dict(kw))
        self.streams_stopped.append(ts)
        return SendResult(ok=True)

    def stored_message(self, ts: str) -> dict[str, Any]:
        """The message as Slack would store it after the seal."""
        blocks: list[dict[str, Any]] = []
        cards: dict[str, dict[str, Any]] = {}
        prose: dict[str, Any] | None = None
        for chunk in self.stream_chunks.get(ts, []):
            if hasattr(chunk, "status"):
                prose = None
                card = cards.get(chunk.id)
                if card is None:
                    card = {
                        "type": "task_card", "block_id": f"task-{chunk.id}",
                        "task_id": chunk.id, "title": chunk.title, "status": chunk.status,
                    }
                    cards[chunk.id] = card
                    blocks.append(card)
                else:
                    card["title"] = chunk.title
                    card["status"] = chunk.status
                if getattr(chunk, "output", None):
                    out = card.setdefault("output", {
                        "type": "rich_text", "block_id": "o",
                        "elements": [{"type": "rich_text_section", "elements": []}],
                    })
                    out["elements"][0]["elements"].append(
                        {"type": "text", "text": str(chunk.output)},
                    )
            elif hasattr(chunk, "blocks"):
                prose = None
                blocks.extend(dict(b) for b in chunk.blocks)
            elif hasattr(chunk, "text"):
                if prose is None:
                    prose = {
                        "type": "rich_text", "block_id": f"p{len(blocks)}",
                        "elements": [{
                            "type": "rich_text_section",
                            "elements": [{"type": "text", "text": ""}],
                        }],
                    }
                    blocks.append(prose)
                prose["elements"][0]["elements"][0]["text"] += chunk.text
        blocks.extend(dict(b) for b in self.stop_blocks.get(ts, []))
        parts: list[str] = []
        for b in blocks:
            if b.get("type") == "rich_text":
                parts.append(b["elements"][0]["elements"][0]["text"])
            elif b.get("type") == "task_card":
                parts.append(str(b.get("title") or ""))
        text = " ".join(parts)
        return {"ts": ts, "blocks": blocks, "text": text}

    async def read_message(self, chat_id: str, ts: str) -> SendResult:
        if ts in self.updated:
            return SendResult(ok=True, message_id=ts, raw={"ts": ts, **self.updated[ts]})
        if ts not in self.stream_chunks:
            return SendResult(ok=False, error="message_not_found")
        return SendResult(ok=True, message_id=ts, raw=self.stored_message(ts))

    async def update_message_blocks(
        self, chat_id: str, ts: str, *, blocks: list[dict[str, Any]], text: str,
        metadata: dict[str, Any] | None = None,
    ) -> SendResult:
        if self.update_errors:
            return SendResult(ok=False, error=self.update_errors.pop(0))
        self.updated[ts] = {"blocks": blocks, "text": text, "metadata": metadata}
        return SendResult(ok=True, message_id=ts)

    async def set_session_status(self, chat_id: str, *, thread_id: str | None = None, status: str, **kw: Any) -> SendResult:
        self.statuses.append(status)
        return SendResult(ok=True)

    async def send_typing(self, *a: Any, **kw: Any) -> None:
        self.statuses.append("processing")

    async def stop_typing(self, *a: Any, **kw: Any) -> None:
        self.statuses.append("active")
