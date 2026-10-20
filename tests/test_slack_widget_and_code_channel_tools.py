"""The two gateway-only tools: widgets into the live reply, and the code
channel lifecycle through a fake adapter."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bridge.gateway.platforms.base import MessageSource, Platform, SendResult
from bridge.tools.code_channel_tool import CodeChannelTool
from bridge.tools.slack_widget_tool import SlackWidgetTool


class _Consumer:
    def __init__(self, ok: bool = True):
        self.blocks: list[list[dict]] = []
        self.ok = ok
        self.finalized = False

    async def append_blocks(self, blocks, *, label=""):
        if not self.ok:
            return False
        self.blocks.append(blocks)
        return True


class _Session:
    id = "freyja:slack:T1:dm:D1"

    def __init__(self, consumer):
        self._active_stream_consumer = consumer


class _Adapter:
    def __init__(self):
        self.posted: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.sent: list[tuple[str, str, Any]] = []
        self.fail_create = ""

        class _B:
            code_channels: dict = {}
        self.bindings = _B()

    async def send_blocks(self, chat_id, blocks, *, thread_id=None, text="", metadata=None):
        self.posted.append({"chat_id": chat_id, "blocks": blocks, "thread_id": thread_id})
        return SendResult(ok=True, message_id="5.5")

    async def code_channel_create(self, name, **kw):
        self.calls.append(("create", {"name": name, **kw}))
        if self.fail_create:
            return SendResult(ok=False, error=self.fail_create)
        self.bindings.code_channels["C9"] = {"name": name, "origin_chat_id": kw.get("origin_chat_id"), "origin_ts": kw.get("origin_ts")}
        return SendResult(ok=True, message_id="C9", raw={"ok": True})

    async def set_session_status(self, chat_id, *, thread_id=None, status, title=None, **kw):
        self.calls.append(("status", {"chat_id": chat_id, "status": status, "title": title}))
        return SendResult(ok=True)

    async def code_channel_call(self, method, **kw):
        self.calls.append((method, kw))
        if method == "archive":
            self.bindings.code_channels.pop(kw.get("channel_id"), None)
        return SendResult(ok=True, message_id=kw.get("channel_id"), raw={"ok": True, "view_id": "V1"})

    async def send(self, chat_id, content, *, thread_id=None, **kw):
        self.sent.append((chat_id, content, thread_id))
        return SendResult(ok=True, message_id="7.7")


def _source(chat_type="dm") -> MessageSource:
    return MessageSource(
        platform=Platform.SLACK, workspace_id="T1", chat_id="D1" if chat_type == "dm" else "C9",
        chat_type=chat_type, user_id="U1", message_id="1.1", thread_id=None,
    )


@pytest.fixture
def adapter(monkeypatch):
    a = _Adapter()
    from bridge.gateway import approval

    monkeypatch.setattr(approval, "get_approval_adapter", lambda name: a)
    return a


def test_widget_goes_into_the_live_stream(adapter):
    consumer = _Consumer()
    tool = SlackWidgetTool(get_gateway_source=_source, get_session=lambda: _Session(consumer))
    res = asyncio.run(tool.execute("c1", {
        "kind": "table", "title": "PRs", "columns": ["Repo", "Open"], "rows": [["freyja", 3], ["evals", 12]],
    }))
    assert not res.is_error and "Rendered the table" in res.content
    assert consumer.blocks[0][0]["type"] == "data_table"
    assert adapter.posted == []


def test_widget_falls_back_to_a_thread_post_without_a_stream(adapter):
    tool = SlackWidgetTool(get_gateway_source=_source, get_session=lambda: _Session(_Consumer(ok=False)))
    res = asyncio.run(tool.execute("c1", {
        "kind": "chart", "title": "Turns", "chart_type": "line",
        "series": '[{"name": "Turns", "data": [{"label": "Mon", "value": 1}]}]',
    }))
    assert not res.is_error and "Posted the chart" in res.content
    assert adapter.posted[0]["blocks"][0]["type"] == "data_visualization"
    assert adapter.posted[0]["thread_id"] == "1.1"  # DM replies thread on the user's message


def test_widget_rejects_bad_input_with_the_limit(adapter):
    tool = SlackWidgetTool(get_gateway_source=_source, get_session=lambda: _Session(_Consumer()))
    res = asyncio.run(tool.execute("c1", {"kind": "table", "columns": ["a"] * 21, "rows": [[1] * 21]}))
    assert res.is_error and "20" in res.content
    res = asyncio.run(tool.execute("c1", {"kind": "cards", "cards": [{"title": "A"}, {"title": "B"}]}))
    assert not res.is_error and _Session  # carousel path builds


def test_widget_table_from_records_infers_columns(adapter):
    consumer = _Consumer()
    tool = SlackWidgetTool(get_gateway_source=_source, get_session=lambda: _Session(consumer))
    asyncio.run(tool.execute("c1", {"kind": "table", "rows": [{"name": "a", "n": 1}, {"name": "b", "n": 2}]}))
    header = consumer.blocks[0][0]["rows"][0]
    assert [c["text"] for c in header] == ["name", "n"]


def test_code_channel_open_sets_status_and_commands(adapter):
    tool = CodeChannelTool(get_gateway_source=_source, get_session=lambda: _Session(None))
    res = asyncio.run(tool.execute("c1", {"action": "open", "name": "Migrate Billing Cron!"}))
    assert not res.is_error and "C9" in res.content and "freyja:slack:T1:code:C9" in res.content
    create = adapter.calls[0][1]
    assert create["name"] == "migrate-billing-cron" and create["origin_chat_id"] == "D1" and create["origin_ts"] == "1.1"
    assert ("status", {"chat_id": "C9", "status": "processing", "title": "Migrate Billing Cron!"}) in adapter.calls
    assert adapter.calls[-1][0] == "setCommands" and len(adapter.calls[-1][1]["commands"]) == 4


def test_code_channel_open_explains_feature_disabled(adapter):
    adapter.fail_create = "feature_disabled: Slack Code is not turned on for this workspace or app."
    tool = CodeChannelTool(get_gateway_source=_source, get_session=lambda: _Session(None))
    res = asyncio.run(tool.execute("c1", {"action": "open", "name": "x"}))
    assert res.is_error and "feature_disabled" in res.content


def test_code_channel_views_context_commands_and_archive(adapter, tmp_path):
    tool = CodeChannelTool(get_gateway_source=_source, get_session=lambda: _Session(None))
    asyncio.run(tool.execute("c0", {"action": "open", "name": "job"}))
    report = tmp_path / "coverage.html"
    report.write_text("<!doctype html><p>ok</p>")
    res = asyncio.run(tool.execute("c1", {"action": "view", "channel_id": "C9", "view_type": "html", "path": str(report)}))
    assert not res.is_error and "V1" in res.content
    view = [c for c in adapter.calls if c[0] == "setView"][-1][1]
    assert view["type"] == "html" and view["view_key"] == "coverage.html" and view["name"] == "coverage"
    res = asyncio.run(tool.execute("c2", {"action": "view", "channel_id": "C9", "view_type": "diff", "content": "--- a\n+++ b\n"}))
    assert not res.is_error
    res = asyncio.run(tool.execute("c3", {"action": "context", "channel_id": "C9", "items": [
        {"key": "repo", "label": "sohamshah/freyja", "icon": "folder", "url": "https://github.com/sohamshah/freyja"},
        {"key": "bad"},
    ]}))
    assert not res.is_error and "sohamshah/freyja" in res.content
    props = [c for c in adapter.calls if c[0] == "setProperties"][-1][1]
    assert len(props["code_channel"]["context_bar_items"]) == 1
    res = asyncio.run(tool.execute("c4", {"action": "commands", "channel_id": "C9", "commands": [{"name": "/Deploy", "description": "ship it"}]}))
    assert not res.is_error and "/deploy" in res.content
    res = asyncio.run(tool.execute("c5", {"action": "archive", "channel_id": "C9", "summary": "All done."}))
    assert not res.is_error and "origin thread" in res.content
    archive = [c for c in adapter.calls if c[0] == "archive"][-1][1]
    assert archive["summary_message_ts"] == "7.7"
    assert adapter.sent[0] == ("C9", "All done.", None)
    assert adapter.sent[1][0] == "D1" and adapter.sent[1][2] == "1.1"
    assert "C9" not in adapter.bindings.code_channels


def test_code_channel_inside_the_channel_needs_no_id(adapter):
    adapter.bindings.code_channels["C9"] = {"name": "job"}
    tool = CodeChannelTool(get_gateway_source=lambda: _source("code"), get_session=lambda: _Session(None))
    res = asyncio.run(tool.execute("c1", {"action": "status", "status": "suspended", "title": "Waiting"}))
    assert not res.is_error
    assert adapter.calls[-1] == ("status", {"chat_id": "C9", "status": "suspended", "title": "Waiting"})
    res = asyncio.run(tool.execute("c2", {"action": "view", "view_type": "pull_request", "pr_url": "http://x"}))
    assert res.is_error and "https" in res.content
