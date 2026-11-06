"""Only the operator's own images become `latest_user_image`.

The gateway handed the operator's turn their own attachments first, then
up to eight earlier thread images, and each image registered took the
`latest_user_image` and `latest_image` aliases. So the newest earlier
image won, whoever posted it, and `generate_image
use_latest_user_image=true` edited a channel member's image as if the
operator had sent it. An earlier image someone else posted still gets a
ref the model can name, but no alias.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from bridge.gateway.config import SlackConfig
from bridge.gateway.platforms.slack import SlackAdapter
from bridge.gateway.run import GatewayDaemon
from bridge.tools.image_store import SessionImageStore

OPERATOR, MEMBER, EXTERNAL, OUR_BOT = "U0OPERATOR", "U0MEMBER", "U0EXTERNAL", "U0FREYJA"
DATA = {"F0": "bm93", "F1": "bWluZQ==", "F2": "c2hvdA==", "F4": "b3Vycw==", "F5": "ZXh0"}


def _png(file_id: str) -> dict[str, Any]:
    return {"id": file_id, "name": f"{file_id}.png", "mimetype": "image/png", "size": 10}


class _Client:
    token = "xoxb-test"

    def __init__(self, thread: list[dict[str, Any]]) -> None:
        self.thread = thread

    async def conversations_replies(self, **_kw: Any) -> dict[str, Any]:
        return {"messages": list(self.thread)}

    async def users_info(self, *, user: str) -> dict[str, Any]:
        return {"user": {"name": user, "profile": {}}}


async def _download(file_obj: dict[str, Any], _token: str) -> dict[str, Any]:
    return {
        "type": "image", "dataBase64": DATA[file_obj["id"]], "mimeType": "image/png",
        "name": file_obj["name"], "filename": file_obj["name"], "slack_file_id": file_obj["id"],
    }


async def _latest_user_image(thread: list[dict[str, Any]], attached: list[str]) -> str | None:
    """The data `latest_user_image` names after the operator's turn in
    ``thread``, attaching the files in ``attached``."""
    from bridge.freyja_bridge import _BridgeSession

    adapter = SlackAdapter(slack_config=SlackConfig(allowed_user_ids={"T1": [OPERATOR]}))
    client = _Client(thread)
    adapter._app = MagicMock()
    adapter._get_client = lambda *_a, **_k: client  # type: ignore[assignment]
    adapter._team_clients = {"T1": client}  # type: ignore[assignment]
    adapter._team_bot_user_ids = {"T1": OUR_BOT}
    adapter._bot_user_id = OUR_BOT
    adapter._download_file = _download  # type: ignore[assignment]
    message = SimpleNamespace(
        source=SimpleNamespace(
            platform=None, workspace_id="T1", chat_type="channel", chat_id="C1",
            user_id=OPERATOR, thread_id="1000.000001", message_id="1000.000099", is_bot=False,
        ),
        raw=None,
    )
    daemon = GatewayDaemon.__new__(GatewayDaemon)
    _text, prior = await daemon._fetch_prior_context(message, adapter)
    # The turn's attachments, in the order the gateway hands them over.
    attachments = prior + [await _download(_png(f), "") for f in attached]
    session = SimpleNamespace(image_store=SessionImageStore())
    _BridgeSession._register_user_image_refs(session, attachments)  # type: ignore[arg-type]
    asset = session.image_store.resolve("latest_user_image")
    return asset.data_base64 if asset else None


def _thread(*posts: dict[str, Any]) -> list[dict[str, Any]]:
    root = {"ts": "1000.000001", "user": OPERATOR, "team": "T1", "text": "Make the deploy chart blue"}
    return [root, *posts]


MEMBERS_IMAGE = {"ts": "1000.000002", "user": MEMBER, "team": "T1", "text": "use this", "files": [_png("F2")]}
EXTERNALS_IMAGE = {"ts": "1000.000005", "user": EXTERNAL, "team": "T9", "text": "", "files": [_png("F5")]}
FREYJAS_IMAGE = {"ts": "1000.000004", "user": OUR_BOT, "bot_id": "B0F", "team": "T1", "text": "chart", "files": [_png("F4")]}
OPERATORS_IMAGE = {"ts": "1000.000003", "user": OPERATOR, "team": "T1", "text": "mine", "files": [_png("F1")]}


async def test_the_operators_attachment_stays_latest_after_a_members_earlier_image():
    assert await _latest_user_image(_thread(MEMBERS_IMAGE), ["F0"]) == DATA["F0"]


async def test_no_image_from_someone_else_becomes_latest():
    for post in (MEMBERS_IMAGE, EXTERNALS_IMAGE, FREYJAS_IMAGE):
        assert await _latest_user_image(_thread(post), []) is None, post["user"]


async def test_the_operators_own_earlier_image_is_still_latest():
    assert await _latest_user_image(_thread(OPERATORS_IMAGE, MEMBERS_IMAGE), []) == DATA["F1"]


async def test_an_image_from_someone_else_still_has_a_ref():
    from bridge.freyja_bridge import _BridgeSession

    session = SimpleNamespace(image_store=SessionImageStore())
    note = _BridgeSession._register_user_image_refs(  # type: ignore[arg-type]
        session, [{"type": "image", "dataBase64": DATA["F2"], "mimeType": "image/png", "from_operator": False}],
    )
    assert session.image_store.resolve("img_001").data_base64 == DATA["F2"]
    assert "`img_001`" in note
    assert "latest_user_image` and" not in note
