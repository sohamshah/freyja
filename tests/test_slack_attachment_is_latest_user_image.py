"""The image the operator attaches to a turn is that turn's
`latest_user_image`, even when the thread holds an older image of theirs.

The gateway handed a turn its own attachments first and the earlier thread
images after them. Each image of the operator's takes the
`latest_user_image` alias as it is registered, so the last one won: an
older image the operator had posted in the thread, not the one they had
just attached. `generate_image use_latest_user_image=true` then edited the
wrong picture. The earlier images now come first, in the order they were
posted, and the turn's own attachments last.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import bridge.gateway.run as run_mod
from bridge.gateway.config import SlackConfig
from bridge.gateway.platforms.base import IncomingMessage, MessageSource, Platform
from bridge.gateway.platforms.slack import SlackAdapter
from bridge.gateway.run import GatewayDaemon
from bridge.tools.image_store import SessionImageStore

OPERATOR, OUR_BOT = "U0OPERATOR", "U0FREYJA"
DATA = {"F0": "bm93", "F1": "b2xk", "F2": "b2xkZXI="}


def _png(file_id: str) -> dict[str, Any]:
    return {"id": file_id, "name": f"{file_id}.png", "mimetype": "image/png", "size": 10}


async def _download(file_obj: dict[str, Any], _token: str) -> dict[str, Any]:
    return {
        "type": "image", "dataBase64": DATA[file_obj["id"]], "mimeType": "image/png",
        "name": file_obj["name"], "filename": file_obj["name"], "slack_file_id": file_obj["id"],
    }


class _Client:
    token = "xoxb-test"

    def __init__(self, thread: list[dict[str, Any]]) -> None:
        self.thread = thread

    async def conversations_replies(self, **_kw: Any) -> dict[str, Any]:
        return {"messages": list(self.thread)}

    async def users_info(self, *, user: str) -> dict[str, Any]:
        return {"user": {"name": user, "profile": {}}}


async def _turn_attachments(monkeypatch, thread: list[dict[str, Any]], attached: list[str]) -> list[dict]:
    """The attachments the gateway hands the bridge for the operator's turn."""
    import bridge.freyja_bridge as fb

    adapter = SlackAdapter(slack_config=SlackConfig(allowed_user_ids={"T1": [OPERATOR]}))
    client = _Client(thread)
    adapter._app = MagicMock()
    adapter._get_client = lambda *_a, **_k: client  # type: ignore[assignment]
    adapter._team_clients = {"T1": client}  # type: ignore[assignment]
    adapter._team_bot_user_ids = {"T1": OUR_BOT}
    adapter._bot_user_id = OUR_BOT
    adapter._download_file = _download  # type: ignore[assignment]

    session = SimpleNamespace(talk_wake_hook=object(), _slack_perm_listener=object(), session=None)
    handed: list[list[dict]] = []

    async def _route(_message: Any, _state: Any, **_kw: Any) -> tuple[str, Any]:
        return "freyja:slack:T1:channel:C1:1000.000001", session

    def _schedule(_session: Any, _text: str, attachments: Any, **_kw: Any) -> None:
        handed.append(list(attachments or []))

    async def _nothing(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(run_mod, "route_message", _route)
    monkeypatch.setattr(fb, "_schedule_or_queue_turn", _schedule)
    daemon = GatewayDaemon.__new__(GatewayDaemon)
    daemon.state = object()
    daemon.adapters = [adapter]
    daemon._recent_inbound = {}
    daemon._title_and_welcome = _nothing  # type: ignore[assignment]
    daemon._maybe_send_catching_up_notice = _nothing  # type: ignore[assignment]

    message = IncomingMessage(
        source=MessageSource(
            platform=Platform.SLACK, workspace_id="T1", chat_type="channel", chat_id="C1",
            user_id=OPERATOR, thread_id="1000.000001", message_id="1000.000099",
        ),
        text="make this one blue",
        attachments=[await _download(_png(f), "") for f in attached],
    )
    await daemon._on_inbound(message)
    assert len(handed) == 1
    return handed[0]


def _latest_user_image(attachments: list[dict]) -> str | None:
    from bridge.freyja_bridge import _BridgeSession

    session = SimpleNamespace(image_store=SessionImageStore())
    _BridgeSession._register_user_image_refs(session, attachments)  # type: ignore[arg-type]
    asset = session.image_store.resolve("latest_user_image")
    return asset.data_base64 if asset else None


THREAD = [
    {"ts": "1000.000001", "user": OPERATOR, "team": "T1", "text": "the chart", "files": [_png("F2")]},
    {"ts": "1000.000002", "user": OPERATOR, "team": "T1", "text": "and this", "files": [_png("F1")]},
]


async def test_the_image_just_attached_wins_over_the_operators_older_ones(monkeypatch):
    attachments = await _turn_attachments(monkeypatch, THREAD, ["F0"])
    assert _latest_user_image(attachments) == DATA["F0"]


async def test_earlier_images_come_first_in_the_order_they_were_posted(monkeypatch):
    attachments = await _turn_attachments(monkeypatch, THREAD, ["F0"])
    assert [a["dataBase64"] for a in attachments] == [DATA["F2"], DATA["F1"], DATA["F0"]]


async def test_with_nothing_attached_the_operators_newest_earlier_image_is_latest(monkeypatch):
    attachments = await _turn_attachments(monkeypatch, THREAD, [])
    assert _latest_user_image(attachments) == DATA["F1"]
