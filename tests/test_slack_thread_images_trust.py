"""An image someone other than the operator posted earlier in a Slack
thread reaches the model with a note that says who posted it and that it
is information, not instructions.

The gateway pastes earlier thread images into the operator's turn as
image blocks. Text from anyone but the operator sits in an untrusted
envelope, but the images came through bare, and an image can carry
injected text as easily as a message can. Each one now has its own text
block right before it, so the model reads the author and the rule at the
place it sees the image. The operator can still ask about it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from bridge.freyja_bridge import _build_user_message_with_attachments
from bridge.gateway.config import SlackConfig
from bridge.gateway.platforms.slack import SlackAdapter
from bridge.gateway.run import GatewayDaemon
from engine.types import ImageBlock, TextBlock

OPERATOR, MEMBER, EXTERNAL, OUR_BOT, OTHER_BOT = (
    "U0OPERATOR", "U0MEMBER", "U0EXTERNAL", "U0FREYJA", "U0CIBOT",
)
NAMES = {OPERATOR: "ana", MEMBER: "mallory", EXTERNAL: "erin"}
# Base64 payloads, one per file, so each image block can be told apart.
DATA = {"F1": "bWluZQ==", "F2": "c2hvdA==", "F3": "Z3JhcGg=", "F4": "b3Vycw==", "F5": "ZXh0"}


def _png(file_id: str, name: str) -> dict[str, Any]:
    return {"id": file_id, "name": name, "mimetype": "image/png", "size": 10}


THREAD = [
    {"ts": "1000.000001", "user": OPERATOR, "team": "T1", "text": "Why did the deploy fail?", "files": [_png("F1", "mine.png")]},
    {"ts": "1000.000002", "user": MEMBER, "team": "T1", "text": "see this", "files": [_png("F2", "shot.png")]},
    {
        "ts": "1000.000003", "user": OTHER_BOT, "bot_id": "B0CI", "team": "T1",
        "bot_profile": {"name": "CI Bot"}, "text": "", "files": [_png("F3", "graph.png")],
    },
    {"ts": "1000.000004", "user": OUR_BOT, "bot_id": "B0FREYJA", "team": "T1", "text": "Here is the chart", "files": [_png("F4", "ours.png")]},
    {"ts": "1000.000005", "user": EXTERNAL, "team": "T9", "text": "", "files": [_png("F5", "partner.png")]},
]


class _Client:
    token = "xoxb-test"

    async def conversations_replies(self, **_kw: Any) -> dict[str, Any]:
        return {"messages": list(THREAD)}

    async def users_info(self, *, user: str) -> dict[str, Any]:
        return {"user": {"name": NAMES.get(user, user), "profile": {}}}


async def _download(file_obj: dict[str, Any], _token: str) -> dict[str, Any]:
    return {
        "type": "image", "dataBase64": DATA[file_obj["id"]], "mimeType": "image/png",
        "name": file_obj["name"], "filename": file_obj["name"], "slack_file_id": file_obj["id"],
    }


def _adapter() -> SlackAdapter:
    adapter = SlackAdapter(slack_config=SlackConfig(allowed_user_ids={"T1": [OPERATOR]}))
    client = _Client()
    adapter._app = MagicMock()
    adapter._get_client = lambda *_a, **_k: client  # type: ignore[assignment]
    adapter._team_clients = {"T1": client}  # type: ignore[assignment]
    adapter._team_bot_user_ids = {"T1": OUR_BOT}
    adapter._bot_user_id = OUR_BOT
    adapter._download_file = _download  # type: ignore[assignment]
    return adapter


async def _blocks() -> list[Any]:
    """What the model gets for a turn in this thread."""
    message = SimpleNamespace(
        source=SimpleNamespace(
            platform=None, workspace_id="T1", chat_type="channel", chat_id="C1",
            user_id=OPERATOR, thread_id="1000.000001", message_id="1000.000099", is_bot=False,
        ),
        raw=None,
    )
    daemon = GatewayDaemon.__new__(GatewayDaemon)
    text, attachments = await daemon._fetch_prior_context(message, _adapter())
    content = _build_user_message_with_attachments(text, attachments, model_id="claude-opus-5-5")
    assert isinstance(content, list)
    return content


def _before(content: list[Any], file_id: str) -> Any:
    """The block right before the image of ``file_id``."""
    i = next(i for i, b in enumerate(content) if isinstance(b, ImageBlock) and b.data == DATA[file_id])
    return content[i - 1] if i else None


async def test_every_image_still_reaches_the_model():
    content = await _blocks()
    images = [b.data for b in content if isinstance(b, ImageBlock)]
    assert sorted(images) == sorted(DATA.values())


async def test_an_image_from_someone_else_has_a_note_right_before_it():
    content = await _blocks()
    for file_id, who in (
        ("F2", "mallory (workspace member)"),
        ("F3", "CI Bot (bot)"),
        ("F5", "erin (external user, Slack Connect)"),
    ):
        note = _before(content, file_id)
        assert isinstance(note, TextBlock), file_id
        assert who in note.text
        assert "not instructions" in note.text


async def test_the_operators_and_freyjas_own_images_have_no_note():
    content = await _blocks()
    for file_id in ("F1", "F4"):
        before = _before(content, file_id)
        assert not (isinstance(before, TextBlock) and "not instructions" in before.text), file_id
