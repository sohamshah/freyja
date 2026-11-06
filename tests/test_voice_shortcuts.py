"""Shortcuts adapter behavior: list cache, exact/fuzzy/ambiguous/unknown resolution, run_exec argv, and text-input handling.

`mac.run_exec` is the single subprocess seam — every test monkeypatches it; no test shells out to the real `shortcuts` binary.
"""

import os

import pytest

from bridge.voice.adapters import mac, shortcuts
from bridge.voice.verbs import build_default_registry

_NAMES = [
    "Open Crossword",
    "Open Notion",
    "Open Hacker News",
    "Open NYTimes",
    "Play Focus Meditation",
    "Take a Break",
    "Text Last Image",
]
_LIST_OUT = "\n".join(_NAMES)


class ExecRecorder:
    """Stands in for mac.run_exec; replays canned (ok, out) replies by call order, defaulting to a successful empty run."""

    def __init__(self, replies=None):
        self.calls = []
        self.replies = list(replies or [])

    async def __call__(self, argv, timeout=6.0):
        self.calls.append(list(argv))
        return self.replies.pop(0) if self.replies else (True, "")


@pytest.fixture
def run_exec(monkeypatch):
    rec = ExecRecorder()
    monkeypatch.setattr(mac, "run_exec", rec)
    return rec


@pytest.fixture(autouse=True)
def _reset_list_cache():
    shortcuts._reset_cache()
    yield
    shortcuts._reset_cache()


@pytest.fixture
def reg():
    return build_default_registry()


@pytest.mark.asyncio
async def test_list_caches_names_and_surfaces_failure(reg, run_exec):
    run_exec.replies = [(True, _LIST_OUT)]
    res = await reg.get("shortcuts.list").run({})
    assert res.ok and res.summary == "7 shortcuts"
    assert res.data["shortcuts"] == _NAMES
    # A second list inside the TTL reuses the cache — no second shell-out.
    await reg.get("shortcuts.list").run({})
    assert run_exec.calls == [["shortcuts", "list"]]

    shortcuts._reset_cache()
    run_exec.replies = [(False, "shortcuts: command not found")]
    res = await reg.get("shortcuts.list").run({})
    assert not res.ok and res.summary == "couldn't list Shortcuts"


@pytest.mark.asyncio
async def test_run_exact_or_unique_fuzzy_name_and_failure(reg, run_exec):
    run_exec.replies = [(True, _LIST_OUT), (True, "done"), (True, ""), (False, "shortcut errored")]
    res = await reg.get("shortcuts.run").run({"name": "Open Notion"})
    assert res.ok and res.summary == "▷ ran Open Notion"
    assert res.data["output"] == "done"
    assert run_exec.calls[-1] == ["shortcuts", "run", "Open Notion"]

    # "hacker news" is a unique substring of "Open Hacker News".
    res = await reg.get("shortcuts.run").run({"name": "hacker news"})
    assert res.ok and res.summary == "▷ ran Open Hacker News"
    assert run_exec.calls[-1] == ["shortcuts", "run", "Open Hacker News"]

    res = await reg.get("shortcuts.run").run({"name": "Open Notion"})
    assert not res.ok and res.summary == "Open Notion failed"
    assert res.error == "shortcut errored"
    # The list was fetched once and cached across all three runs.
    assert run_exec.calls.count(["shortcuts", "list"]) == 1


@pytest.mark.asyncio
async def test_run_ambiguous_or_unknown_name_runs_nothing(reg, run_exec):
    run_exec.replies = [(True, _LIST_OUT)]
    # "open" is a substring of four shortcuts → ask, don't run.
    res = await reg.get("shortcuts.run").run({"name": "open"})
    assert not res.ok
    assert "Open Notion" in res.data["suggestions"]
    # "focus" overlaps "Play Focus Meditation" → offered as a near-miss.
    res = await reg.get("shortcuts.run").run({"name": "focus session"})
    assert not res.ok
    assert "Play Focus Meditation" in res.data["suggestions"]
    res = await reg.get("shortcuts.run").run({"name": "zzqwidget"})
    assert not res.ok and res.summary == "no shortcut named zzqwidget"
    # Only the list ran — nothing was executed.
    assert run_exec.calls == [["shortcuts", "list"]]


@pytest.mark.asyncio
async def test_run_with_input_uses_input_path(reg, run_exec):
    run_exec.replies = [(True, _LIST_OUT), (True, "")]
    res = await reg.get("shortcuts.run").run(
        {"name": "Text Last Image", "input": "hello there"}
    )
    assert res.ok
    argv = run_exec.calls[-1]
    assert argv[:3] == ["shortcuts", "run", "Text Last Image"]
    assert "--input-path" in argv
    idx = argv.index("--input-path")
    input_path = argv[idx + 1]
    # The temp file is cleaned up in finally — it must be gone after the run.
    assert not os.path.exists(input_path)
