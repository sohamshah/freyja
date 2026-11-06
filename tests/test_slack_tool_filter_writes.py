"""With `slack.enable_tool_filter` on, `write_file`, `edit_file` and
`edit_json` write only inside the session's project output directory.

They took any absolute path, so a filtered session could rewrite
`~/.freyja/gateway.yaml` to turn the filter off, edit a job file, or
append to a shell rc file or a LaunchAgent plist.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bridge.gateway.config import config_path
from bridge.gateway.platforms.base import MessageSource, Platform
from engine.types import ToolCall

SLACK = "freyja:slack:T1:channel:C1:5.5"
FILTER_ON = "slack:\n  enable_tool_filter: true\n"


async def _slack_session(monkeypatch, tmp_path, *, filter_on: bool = True):
    import bridge.freyja_bridge as fb

    monkeypatch.setattr(fb, "emit", lambda _e: None)
    config_path().write_text(f"slack:\n  enable_tool_filter: {str(filter_on).lower()}\n")
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    sess = fb._BridgeSession(
        SLACK, workspace=str(tmp_path), model_id="claude-sonnet-5-5",
        reasoning_level="auto", coordination_strategy="bus", state=state,
    )
    sess.gateway_source = MessageSource(
        platform=Platform.SLACK, workspace_id="T1", chat_type="channel", chat_id="C1",
        user_id="U0OPERATOR", thread_id="5.5", message_id="5.5",
    )
    await sess.initialize()
    return sess


async def _run(sess, name: str, arguments: dict):
    return await sess.tool_registry.execute(ToolCall(id="c1", name=name, arguments=arguments))


def _outside(tmp_path) -> dict[str, Path]:
    """Files outside the project dir: gateway.yaml, a job, a shell rc."""
    job = Path.home() / ".freyja" / "scheduler" / "jobs" / "job_1.json"
    job.parent.mkdir(parents=True, exist_ok=True)
    job.write_text(json.dumps({"id": "job_1", "permission_snapshot": "low"}))
    rc = Path.home() / ".zshrc"
    rc.write_text("export PATH=/usr/bin\n")
    return {"gateway": config_path(), "job": job, "rc": rc, "workspace": tmp_path / "notes.md"}


@pytest.mark.parametrize("target", ["gateway", "rc", "workspace"])
async def test_write_file_cannot_write_outside_the_project_dir(monkeypatch, tmp_path, target):
    sess = await _slack_session(monkeypatch, tmp_path)
    path = _outside(tmp_path)[target]
    before = path.read_text() if path.exists() else None
    result = await _run(sess, "write_file", {
        "path": str(path), "content": "slack:\n  enable_tool_filter: false\n",
    })
    assert result.is_error and "project output directory" in result.content, result.content
    assert (path.read_text() if path.exists() else None) == before


async def test_edit_file_cannot_edit_outside_the_project_dir(monkeypatch, tmp_path):
    sess = await _slack_session(monkeypatch, tmp_path)
    result = await _run(sess, "edit_file", {
        "path": str(config_path()), "old_string": "true", "new_string": "false",
    })
    assert result.is_error and "project output directory" in result.content, result.content
    assert config_path().read_text() == FILTER_ON


async def test_edit_json_cannot_edit_a_job(monkeypatch, tmp_path):
    sess = await _slack_session(monkeypatch, tmp_path)
    job = _outside(tmp_path)["job"]
    result = await _run(sess, "edit_json", {
        "path": str(job), "json_path": "permission_snapshot", "value": "yolo",
    })
    assert result.is_error and "project output directory" in result.content, result.content
    assert json.loads(job.read_text())["permission_snapshot"] == "low"


async def test_a_relative_path_cannot_climb_out_of_the_project_dir(monkeypatch, tmp_path):
    sess = await _slack_session(monkeypatch, tmp_path)
    escape = "../../gateway.yaml"
    assert (sess.project_output_dir / escape).resolve() == config_path().resolve()
    result = await _run(sess, "write_file", {"path": escape, "base": "project", "content": "x"})
    assert result.is_error
    assert config_path().read_text() == FILTER_ON


async def test_call_tool_and_a_direct_call_are_confined_too(monkeypatch, tmp_path):
    sess = await _slack_session(monkeypatch, tmp_path)
    result = await _run(sess, "call_tool", {
        "tool_name": "write_file", "arguments": {"path": str(config_path()), "content": "x"},
    })
    assert result.is_error
    result = await sess.tool_registry.get("write_file").execute(
        "c2", {"path": str(config_path()), "content": "x"},
    )
    assert result.is_error
    assert config_path().read_text() == FILTER_ON


async def test_a_sub_agent_is_confined_to_the_same_dir(monkeypatch, tmp_path):
    import engine.runner as runner_module

    sess = await _slack_session(monkeypatch, tmp_path)
    seen = {}

    class _Stop(Exception):
        pass

    class _Runner:
        def __init__(self, *, tool_registry, **_kw):
            seen["registry"] = tool_registry
            raise _Stop

    monkeypatch.setattr(runner_module, "AsyncAgentRunner", _Runner)
    tool = sess.tool_registry.get("sub_agent")
    record = tool._spec.registry.register(  # noqa: SLF001
        id="sub_writes_1", label="helper", task="write", mode="background",
    )
    record.task_id = "t1"
    with pytest.raises(_Stop):
        await tool._run_child(record)  # noqa: SLF001
    child = seen["registry"]
    result = await child.execute(ToolCall(id="c1", name="write_file", arguments={
        "path": str(config_path()), "content": "x",
    }))
    assert result.is_error
    assert config_path().read_text() == FILTER_ON
    result = await child.execute(ToolCall(id="c2", name="write_file", arguments={
        "path": "child.md", "content": "x",
    }))
    assert not result.is_error, result.content
    assert (Path(sess.project_output_dir) / "child.md").read_text() == "x"


async def test_it_still_writes_and_edits_in_the_project_dir(monkeypatch, tmp_path):
    sess = await _slack_session(monkeypatch, tmp_path)
    result = await _run(sess, "write_file", {"path": "report.md", "content": "draft"})
    assert not result.is_error, result.content
    report = Path(sess.project_output_dir) / "report.md"
    assert report.read_text() == "draft"
    result = await _run(sess, "edit_file", {
        "path": str(report), "old_string": "draft", "new_string": "final",
    })
    assert not result.is_error, result.content
    assert report.read_text() == "final"


async def test_with_the_filter_off_it_writes_anywhere(monkeypatch, tmp_path):
    sess = await _slack_session(monkeypatch, tmp_path, filter_on=False)
    rc = _outside(tmp_path)["rc"]
    result = await _run(sess, "write_file", {"path": str(rc), "content": "x"})
    assert not result.is_error, result.content
    assert rc.read_text() == "x"
