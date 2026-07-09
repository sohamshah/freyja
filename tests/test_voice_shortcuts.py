"""Shortcuts adapter behavior: list cache, exact/fuzzy/ambiguous/unknown
resolution, run_exec argv, and text-input handling.

`mac.run_exec` is the single subprocess seam — every test monkeypatches
it; no test shells out to the real `shortcuts` binary.
"""

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
    """Stands in for mac.run_exec; replays canned (ok, out) replies by call
    order, defaulting to a successful empty run."""

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


# ── list + cache ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_argv_and_summary(reg, run_exec):
    run_exec.replies = [(True, _LIST_OUT)]
    res = await reg.get("shortcuts.list").run({})
    assert res.ok and res.summary == "7 shortcuts"
    assert res.data["shortcuts"] == _NAMES
    assert run_exec.calls == [["shortcuts", "list"]]


@pytest.mark.asyncio
async def test_list_caches_names(reg, run_exec):
    run_exec.replies = [(True, _LIST_OUT)]
    await reg.get("shortcuts.list").run({})
    # A second list inside the TTL reuses the cache — no second shell-out.
    await reg.get("shortcuts.list").run({})
    assert run_exec.calls == [["shortcuts", "list"]]


@pytest.mark.asyncio
async def test_list_failure_surfaces(reg, run_exec):
    run_exec.replies = [(False, "shortcuts: command not found")]
    res = await reg.get("shortcuts.list").run({})
    assert not res.ok and res.summary == "couldn't list Shortcuts"


# ── run: exact / fuzzy / ambiguous / unknown ───────────────────────────────


@pytest.mark.asyncio
async def test_run_exact_name(reg, run_exec):
    run_exec.replies = [(True, _LIST_OUT), (True, "done")]
    res = await reg.get("shortcuts.run").run({"name": "Open Notion"})
    assert res.ok and res.summary == "▷ ran Open Notion"
    assert res.data["output"] == "done"
    assert run_exec.calls[-1] == ["shortcuts", "run", "Open Notion"]


@pytest.mark.asyncio
async def test_run_fuzzy_unique_substring(reg, run_exec):
    run_exec.replies = [(True, _LIST_OUT), (True, "")]
    # "hacker news" is a unique substring of "Open Hacker News".
    res = await reg.get("shortcuts.run").run({"name": "hacker news"})
    assert res.ok and res.summary == "▷ ran Open Hacker News"
    assert run_exec.calls[-1] == ["shortcuts", "run", "Open Hacker News"]


@pytest.mark.asyncio
async def test_run_ambiguous_asks(reg, run_exec):
    run_exec.replies = [(True, _LIST_OUT)]
    # "open" is a substring of four shortcuts → ask, don't run.
    res = await reg.get("shortcuts.run").run({"name": "open"})
    assert not res.ok
    assert "Open Notion" in res.data["suggestions"]
    # Only the list ran — nothing was executed.
    assert run_exec.calls == [["shortcuts", "list"]]


@pytest.mark.asyncio
async def test_run_unknown_suggests_close(reg, run_exec):
    run_exec.replies = [(True, _LIST_OUT)]
    # "meditate" shares the word "meditation"? no — shares no word; but
    # "focus meditate" would. Use a query that shares a word for the
    # did-you-mean path.
    res = await reg.get("shortcuts.run").run({"name": "focus session"})
    assert not res.ok
    # "focus" overlaps "Play Focus Meditation" → offered as a near-miss.
    assert "Play Focus Meditation" in res.data["suggestions"]
    assert run_exec.calls == [["shortcuts", "list"]]


@pytest.mark.asyncio
async def test_run_totally_unknown(reg, run_exec):
    run_exec.replies = [(True, _LIST_OUT)]
    res = await reg.get("shortcuts.run").run({"name": "zzqwidget"})
    assert not res.ok and res.summary == "no shortcut named zzqwidget"
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
    import os

    assert not os.path.exists(input_path)


@pytest.mark.asyncio
async def test_run_failure_surfaces(reg, run_exec):
    run_exec.replies = [(True, _LIST_OUT), (False, "shortcut errored")]
    res = await reg.get("shortcuts.run").run({"name": "Open Notion"})
    assert not res.ok and res.summary == "Open Notion failed"
    assert res.error == "shortcut errored"


@pytest.mark.asyncio
async def test_run_requires_name(reg, run_exec):
    res = await reg.get("shortcuts.run").run({})
    assert not res.ok and res.summary == "which shortcut?"
    assert run_exec.calls == []
