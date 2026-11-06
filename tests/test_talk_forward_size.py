"""A talk() message too long for the control channel is refused, not lost.

A message for a session another process owns goes there as one line in
a control file. The reader there skips a line over MAX_LINE_BYTES (it
reads that much at a time), so a longer message never arrived, while
the sender was told it was forwarded and waited for a reply.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from bridge.gateway.control_channel import MAX_LINE_BYTES, commands_path, desktop_commands_path
from bridge.inbox import InboxMessage, new_message_id
from bridge.tools import talk_tool as tt
from bridge.tools.talk_tool import TalkRouter

SLACK = "freyja:slack:T1:channel:C1:1.0"


def _router() -> TalkRouter:
    return TalkRouter(
        bridge_state=SimpleNamespace(sessions={}),
        get_running_sessions=lambda: {},
        resolve_archived_sub=lambda _sid: None,
        wake_archived_sub=lambda _sid, _msg: None,
    )


def _msg(content: str) -> InboxMessage:
    return InboxMessage(
        id=new_message_id(), from_session="sender", from_label="sender", from_role="agent", content=content,
    )


@pytest.mark.parametrize(
    "where, target, archived, owner_is_me",
    [
        # To a Slack thread, from a process that is not the gateway.
        ("gateway", SLACK, None, False),
        # To an archived sub-agent of a Slack thread, likewise.
        ("gateway", "sub_1_1", {"parentSessionId": SLACK}, False),
        # From the gateway, to a session the desktop bridge runs.
        ("desktop", "session-abc", None, True),
    ],
)
def test_a_message_too_long_for_the_control_channel_is_refused(monkeypatch, where, target, archived, owner_is_me):
    me = os.getpid()
    monkeypatch.setattr(tt, "_gateway_owner_pid", lambda: me if owner_is_me else me + 1)
    # The gateway sends to the app's bridge only while it runs.
    monkeypatch.setattr(tt, "_desktop_bridge_runs", lambda: True)
    path = commands_path() if where == "gateway" else desktop_commands_path()

    status = _router().forward_cross_process(
        target, _msg("x" * MAX_LINE_BYTES), live_local=False, archived_state=archived,
    )

    # "failed" tells the talk tool not to wait for a reply.
    assert status is not None and "failed" in status and "MiB" in status
    assert not path.exists() or path.read_text() == ""


def test_a_message_that_fits_is_still_forwarded(monkeypatch):
    monkeypatch.setattr(tt, "_gateway_owner_pid", lambda: os.getpid() + 1)
    status = _router().forward_cross_process(SLACK, _msg("x" * 1000), live_local=False, archived_state=None)
    assert status is not None and status.startswith("forwarded")
    assert len(commands_path().read_text().splitlines()) == 1
