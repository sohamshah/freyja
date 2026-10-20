"""Slack thread bindings that survive a daemon restart.

The adapter decides whether a channel message is for Freyja by looking
at two sets: threads the bot was mentioned in, and messages the bot
sent. Both used to live in memory, so after a restart a reply in an old
thread was ignored until someone mentioned the bot again. ``ThreadBindings``
keeps the same sets, plus the code channels Freyja opened and the titles
operators gave their sessions, in one JSON file under ``~/.freyja/gateway/``.

Writes are debounced (one write per ``flush_delay`` seconds at most) and
atomic. Sets are bounded; the oldest entries go first.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from bridge.gateway.pid import freyja_home

logger = logging.getLogger(__name__)

_VERSION = 1


def bindings_path() -> Path:
    d = freyja_home() / "gateway"
    d.mkdir(parents=True, exist_ok=True)
    return d / "slack-bindings.json"


class ThreadBindings:
    """Bounded, persisted sets keyed by Slack ids."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        max_threads: int = 2000,
        max_message_ts: int = 5000,
        flush_delay: float = 2.0,
    ) -> None:
        self.path = path or bindings_path()
        self.max_threads = max_threads
        self.max_message_ts = max_message_ts
        self.flush_delay = flush_delay
        # Insertion-ordered dicts stand in for ordered sets.
        self.mentioned_threads: dict[str, float] = {}
        self.bot_message_ts: dict[str, float] = {}
        self.code_channels: dict[str, dict[str, Any]] = {}
        self.session_titles: dict[str, str] = {}   # "<channel>|<thread_ts>" → title
        self._dirty = False
        self._flush_handle: asyncio.TimerHandle | None = None
        self.load()

    # ── persistence ──

    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("[slack] bindings file unreadable, starting empty: %s", exc)
            return
        if not isinstance(raw, dict):
            return
        self.mentioned_threads = {str(k): float(v) for k, v in (raw.get("mentioned_threads") or {}).items()}
        self.bot_message_ts = {str(k): float(v) for k, v in (raw.get("bot_message_ts") or {}).items()}
        self.code_channels = {str(k): dict(v) for k, v in (raw.get("code_channels") or {}).items() if isinstance(v, dict)}
        self.session_titles = {str(k): str(v) for k, v in (raw.get("session_titles") or {}).items()}
        self._trim()

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": _VERSION,
            "mentioned_threads": self.mentioned_threads,
            "bot_message_ts": self.bot_message_ts,
            "code_channels": self.code_channels,
            "session_titles": self.session_titles,
        }

    def save(self) -> None:
        """Atomic write. Safe to call from any context; never raises."""
        self._dirty = False
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.to_dict(), indent=0), encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[slack] bindings save failed: %s", exc)

    def _touch(self) -> None:
        self._trim()
        self._dirty = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.save()
            return
        if self._flush_handle is not None and not self._flush_handle.cancelled():
            return
        self._flush_handle = loop.call_later(self.flush_delay, self._flush_now)

    def _flush_now(self) -> None:
        self._flush_handle = None
        if self._dirty:
            self.save()

    def _trim(self) -> None:
        for store, cap in ((self.mentioned_threads, self.max_threads), (self.bot_message_ts, self.max_message_ts)):
            if len(store) > cap:
                for key in list(store)[: len(store) - cap]:
                    store.pop(key, None)
        if len(self.session_titles) > self.max_threads:
            for key in list(self.session_titles)[: len(self.session_titles) - self.max_threads]:
                self.session_titles.pop(key, None)

    # ── mention gating ──

    def add_thread(self, thread_ts: str) -> None:
        if not thread_ts:
            return
        self.mentioned_threads[str(thread_ts)] = time.time()
        self._touch()

    def has_thread(self, thread_ts: str | None) -> bool:
        return bool(thread_ts) and str(thread_ts) in self.mentioned_threads

    def add_message_ts(self, ts: str) -> None:
        if not ts:
            return
        self.bot_message_ts[str(ts)] = time.time()
        self._touch()

    def has_message_ts(self, ts: str | None) -> bool:
        return bool(ts) and str(ts) in self.bot_message_ts

    # ── code channels ──

    def add_code_channel(self, channel_id: str, info: dict[str, Any]) -> None:
        self.code_channels[str(channel_id)] = dict(info)
        self._touch()

    def remove_code_channel(self, channel_id: str) -> None:
        if self.code_channels.pop(str(channel_id), None) is not None:
            self._touch()

    def is_code_channel(self, channel_id: str | None) -> bool:
        return bool(channel_id) and str(channel_id) in self.code_channels

    # ── titles ──

    @staticmethod
    def title_key(channel_id: str, thread_ts: str | None) -> str:
        return f"{channel_id}|{thread_ts or ''}"

    def set_title(self, channel_id: str, thread_ts: str | None, title: str, *, by_user: bool) -> None:
        key = self.title_key(channel_id, thread_ts)
        current = self.session_titles.get(key)
        if current and current.startswith("user:") and not by_user:
            return  # an operator's title is never overwritten by an automatic one
        self.session_titles[key] = ("user:" if by_user else "auto:") + title
        self._touch()

    def title_for(self, channel_id: str, thread_ts: str | None) -> str | None:
        raw = self.session_titles.get(self.title_key(channel_id, thread_ts))
        if not raw:
            return None
        return raw.split(":", 1)[1] if ":" in raw else raw

    def title_is_users(self, channel_id: str, thread_ts: str | None) -> bool:
        raw = self.session_titles.get(self.title_key(channel_id, thread_ts)) or ""
        return raw.startswith("user:")
