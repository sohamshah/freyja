"""Files + clipboard adapter behavior.

For the ``files.*`` verbs we use REAL ``tmp_path`` directories as the operator's home (monkeypatching ``Path.home``) so ``os.scandir`` and ``shutil.move`` are exercised for real — the whole point of these verbs is that the moves actually happen and the undo actually reverses them. The only mocked seams are the subprocess ones: ``mac.run_exec`` (``open`` / ``pbpaste``) and the module's ``_pbcopy`` helper.
"""

import os
import time
from pathlib import Path

import pytest

from bridge.voice.adapters import files, mac
from bridge.voice.verbs import build_default_registry


class ExecRecorder:
    """Stands in for mac.run_exec; replays canned (ok, out) replies by call order, defaulting to a successful empty run and recording every argv."""

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


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point the adapter's home at a real temp dir. Every path decision in files.py flows through ``_home()`` → ``Path.home()``."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path.resolve()


@pytest.fixture
def reg():
    return build_default_registry()


def _touch(path: Path, mtime: float, content: str = "x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    os.utime(path, (mtime, mtime))


def _day(stamp):
    return time.mktime(time.strptime(stamp, "%Y-%m-%d %H:%M"))


# ── files.list ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_newest_first_with_kinds_skipping_dotfiles(reg, home):
    dl = home / "Downloads"
    _touch(dl / "old.txt", 1000.0)
    _touch(dl / "photo.png", 3000.0)
    _touch(dl / "paper.pdf", 2000.0)
    _touch(dl / ".hidden", 5000.0)
    (dl / "sub").mkdir()
    os.utime(dl / "sub", (4000.0, 4000.0))
    res = await reg.get("files.list").run({"dir": "downloads"})
    assert res.ok
    names = [e["name"] for e in res.data["entries"]]
    # Newest first: the folder (4000) then png (3000), pdf (2000), txt (1000).
    assert names == ["sub", "photo.png", "paper.pdf", "old.txt"]
    kinds = {e["name"]: e["kind"] for e in res.data["entries"]}
    assert kinds["sub"] == "folder"
    assert kinds["photo.png"] == "image"
    assert kinds["paper.pdf"] == "doc"
    assert res.data["dir"] == "~/Downloads"
    assert res.summary == "4 items in ~/Downloads"


@pytest.mark.asyncio
async def test_list_caps_at_25(reg, home):
    d = home / "Desktop"
    for i in range(40):
        _touch(d / f"f{i:02d}.txt", 1000.0 + i)
    res = await reg.get("files.list").run({"dir": "desktop"})
    assert res.ok and len(res.data["entries"]) == 25
    # Cap keeps the newest 25 — f39 (mtime 1039) first.
    assert res.data["entries"][0]["name"] == "f39.txt"
    # The summary reports the TRUE total, not the truncated slice.
    assert res.data["total"] == 40 and res.data["shown"] == 25
    assert res.summary == "showing 25 of 40 items in ~/Desktop"


@pytest.mark.asyncio
async def test_list_missing_folder(reg, home):
    res = await reg.get("files.list").run({"dir": "nope-not-here"})
    assert not res.ok and "no folder" in res.summary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "verb,args",
    [
        ("files.list", {"dir": "../../../../etc"}),
        ("files.reveal", {"path": "/etc/hosts"}),
        ("files.organize", {"dir": "../../tmp"}),
    ],
)
async def test_paths_outside_home_are_refused_before_any_action(reg, home, run_exec, verb, args):
    res = await reg.get(verb).run(args)
    assert not res.ok and "outside your home folder" in res.summary
    assert run_exec.calls == []


# ── files.open_latest ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_latest_opens_newest(reg, home, run_exec):
    d = home / "Downloads"
    _touch(d / "a.txt", 1000.0)
    _touch(d / "b.txt", 3000.0)
    _touch(d / "c.txt", 2000.0)
    res = await reg.get("files.open_latest").run({"dir": "downloads"})
    assert res.ok and res.summary == "opened b.txt"
    assert run_exec.calls == [["open", str(d / "b.txt")]]


@pytest.mark.asyncio
async def test_open_latest_kind_filters(reg, home, run_exec):
    d = home / "Desktop"
    _touch(d / "newest.txt", 5000.0)  # newest but not an image
    _touch(d / "random.png", 4000.0)  # newest image, but not a screenshot
    _touch(d / "Screenshot 2026-07-09 at 10.00.00.png", 3000.0)
    _touch(d / "older.jpg", 1000.0)
    res = await reg.get("files.open_latest").run({"dir": "desktop", "kind": "image"})
    assert res.ok and res.summary == "opened random.png"
    assert run_exec.calls[-1] == ["open", str(d / "random.png")]
    res = await reg.get("files.open_latest").run({"dir": "desktop", "kind": "screenshot"})
    assert res.ok and "Screenshot" in res.summary


@pytest.mark.asyncio
async def test_open_latest_refuses_when_nothing_matches(reg, home, run_exec):
    (home / "Downloads").mkdir()
    res = await reg.get("files.open_latest").run({"dir": "downloads"})
    assert not res.ok and "no files" in res.summary
    _touch(home / "Downloads" / "note.txt", 1000.0)
    res = await reg.get("files.open_latest").run({"dir": "downloads", "kind": "pdf"})
    assert not res.ok and "no pdf files" in res.summary
    assert run_exec.calls == []  # never shelled out to `open`


# ── files.reveal ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reveal_by_fuzzy_name_or_path(reg, home, run_exec):
    d = home / "Downloads"
    _touch(d / "Quarterly Report Final.pdf", 1000.0)
    res = await reg.get("files.reveal").run({"name": "quarterly", "dir": "downloads"})
    assert res.ok and res.summary == "revealed Quarterly Report Final.pdf"
    # open -R argv (reveal in Finder).
    assert run_exec.calls == [["open", "-R", str(d / "Quarterly Report Final.pdf")]]

    _touch(home / "Documents" / "thing.txt", 1000.0)
    res = await reg.get("files.reveal").run({"path": "~/Documents/thing.txt"})
    assert res.ok and res.summary == "revealed thing.txt"
    assert run_exec.calls[-1] == ["open", "-R", str(home / "Documents" / "thing.txt")]


@pytest.mark.asyncio
async def test_reveal_ambiguous_or_missing_reveals_nothing(reg, home, run_exec):
    d = home / "Downloads"
    _touch(d / "report-a.pdf", 1000.0)
    _touch(d / "report-b.pdf", 2000.0)
    res = await reg.get("files.reveal").run({"name": "report", "dir": "downloads"})
    assert not res.ok
    assert set(res.data["candidates"]) == {"report-a.pdf", "report-b.pdf"}
    res = await reg.get("files.reveal").run({"name": "ghost", "dir": "downloads"})
    assert not res.ok and "no file matching ghost" in res.summary
    assert run_exec.calls == []


# ── files.organize ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_organize_moves_into_dated_folders_and_undo(reg, home):
    d = home / "Desktop"
    # Two screenshots on different mtime dates + a non-screenshot left alone.
    _touch(d / "Screenshot 2026-07-01 at 10.00.00.png", _day("2026-07-01 10:00"))
    _touch(d / "Screen Shot 2026-07-02 at 11.00.00.png", _day("2026-07-02 11:00"))
    _touch(d / "keep.txt", _day("2026-07-01 10:00"))
    res = await reg.get("files.organize").run({"dir": "desktop"})
    assert res.ok and res.summary == "moved 2 screenshots into dated folders"
    # Dated subfolders created and files moved in.
    assert (d / "2026-07-01" / "Screenshot 2026-07-01 at 10.00.00.png").exists()
    assert (d / "2026-07-02" / "Screen Shot 2026-07-02 at 11.00.00.png").exists()
    assert not (d / "Screenshot 2026-07-01 at 10.00.00.png").exists()
    # The non-screenshot is untouched.
    assert (d / "keep.txt").exists()
    assert len(res.data["moved"]) == 2
    # Undo puts every file back.
    assert res.undo is not None
    undo_res = await res.undo()
    assert undo_res.ok
    assert (d / "Screenshot 2026-07-01 at 10.00.00.png").exists()
    assert (d / "Screen Shot 2026-07-02 at 11.00.00.png").exists()
    assert not (d / "2026-07-01" / "Screenshot 2026-07-01 at 10.00.00.png").exists()


@pytest.mark.asyncio
async def test_organize_no_overwrite_guard(reg, home):
    d = home / "Desktop"
    name = "Screenshot 2026-07-01 at 10.00.00.png"
    _touch(d / name, _day("2026-07-01 10:00"), content="new")
    # A destination file already exists at the dated path → must NOT overwrite.
    _touch(d / "2026-07-01" / name, _day("2026-07-01 10:00"), content="pre-existing")
    res = await reg.get("files.organize").run({"dir": "desktop"})
    # Nothing moved (the only candidate had a dest clash).
    assert res.ok and res.data["moved"] == []
    # The source is untouched and the pre-existing dest is preserved.
    assert (d / name).read_text() == "new"
    assert (d / "2026-07-01" / name).read_text() == "pre-existing"


@pytest.mark.asyncio
async def test_organize_default_desktop_then_downloads(reg, home):
    # Desktop exists but has no screenshots; Downloads has one → default
    # picks Downloads.
    (home / "Desktop").mkdir()
    dl = home / "Downloads"
    _touch(dl / "Screenshot 2026-07-01 at 10.00.00.png", _day("2026-07-01 10:00"))
    res = await reg.get("files.organize").run({})
    assert res.ok and res.summary == "moved 1 screenshot into dated folders"
    assert (dl / "2026-07-01" / "Screenshot 2026-07-01 at 10.00.00.png").exists()


@pytest.mark.asyncio
async def test_organize_nothing_to_sort(reg, home):
    d = home / "Desktop"
    _touch(d / "just-a-doc.txt", 1000.0)
    res = await reg.get("files.organize").run({"dir": "desktop"})
    assert res.ok and "no screenshots to sort" in res.summary
    assert res.data["moved"] == []
    assert res.undo is None


# ── clipboard ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clipboard_read_truncates_summary_and_handles_empty(reg, run_exec):
    run_exec.replies = [(True, "hello from the clipboard"), (True, "z" * 200), (True, "")]
    res = await reg.get("clipboard.read").run({})
    assert res.ok and res.data["text"] == "hello from the clipboard"
    assert res.summary == "hello from the clipboard"
    assert run_exec.calls == [["pbpaste"]]
    res = await reg.get("clipboard.read").run({})
    assert res.ok and res.data["text"] == "z" * 200
    assert res.summary == "z" * 60  # summary peeks at 60 chars
    res = await reg.get("clipboard.read").run({})
    assert res.ok and res.summary == "clipboard is empty"
    assert res.data["text"] == ""


@pytest.mark.asyncio
async def test_clipboard_write_pipes_stdin_and_surfaces_failure(reg, monkeypatch):
    captured = []
    outcome = {"result": (True, "")}

    async def fake_pbcopy(text, timeout=5.0):
        captured.append(text)
        return outcome["result"]

    monkeypatch.setattr(files, "_pbcopy", fake_pbcopy)
    res = await reg.get("clipboard.write").run({"text": "copy me down"})
    assert res.ok and res.summary == "copied to clipboard"
    assert captured == ["copy me down"]
    # The copied text never rides verbatim in a long summary.
    assert "copy me down" not in res.summary

    outcome["result"] = (False, "pbcopy timeout")
    res = await reg.get("clipboard.write").run({"text": "x"})
    assert not res.ok and res.summary == "couldn't copy to clipboard"


@pytest.mark.asyncio
async def test_pbcopy_helper_roundtrips_via_cat(monkeypatch):
    # Swap `pbcopy` for `cat` (present everywhere) so the stdin-piping helper
    # is exercised for real without touching the pasteboard: cat echoes stdin
    # to stdout and exits 0, proving the write path plumbs stdin correctly.
    import asyncio as _asyncio

    orig = _asyncio.create_subprocess_exec

    async def patched(program, *argv, **kwargs):
        if program == "pbcopy":
            return await orig("cat", *argv, **kwargs)
        return await orig(program, *argv, **kwargs)

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", patched)
    ok, err = await files._pbcopy("piped text")
    assert ok and err == ""
