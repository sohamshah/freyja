"""Search tools must terminate, and must not report a cut-short scan as absence.

`GrepTool` and `GlobTool` both began with `list(path.rglob("*"))` — the whole
tree materialized before a single file was examined, then `.is_file()` (and for
glob, `.stat()`) on every entry. Pointed at a monorepo checkout the walk alone
never finishes, and a pattern with no match never trips the `max_results`
early-exit, so nothing stops it.

A Slack turn on 2026-09-09 did exactly that: iteration 29 called
`grep(path="~/work/services", pattern="<a uuid>")` against 118 GB / 2.36M files.
Six minutes later the tool had still not returned and the whole agent loop was
blocked behind it, with the gateway pinned at ~48% CPU.

The second half is as important as the bound. A search that stops early and
reports "No matches found" hands the model a false negative it cannot detect —
worse than being slow, because the agent then reasons confidently from it.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

from bridge.tools.search_tools import (
    MAX_FILE_BYTES,
    PRUNE_DIRS,
    GlobTool,
    GrepTool,
    iter_files,
)


def _run(tool, args):
    return asyncio.run(tool.execute("call-1", args))


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    """A small repo-shaped tree with the usual junk directories."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("needle_in_src = 1\n")
    (tmp_path / "src" / "notes.md").write_text("needle_in_src markdown\n")

    for junk in ("node_modules", ".git", ".venv", "dist"):
        d = tmp_path / junk
        d.mkdir()
        (d / "buried.py").write_text("needle_in_src = 'should not be found'\n")

    return tmp_path


# ── pruning ──────────────────────────────────────────────────────────

def test_junk_directories_are_not_walked(tree: Path):
    found = {p.name for p in iter_files(tree, deadline=time.monotonic() + 30)}
    assert "app.py" in found
    assert "buried.py" not in found


@pytest.mark.parametrize("junk", ["node_modules", ".git", ".venv", "dist"])
def test_each_junk_dir_is_pruned(junk):
    assert junk in PRUNE_DIRS


def test_grep_skips_pruned_dirs(tree: Path):
    out = _run(GrepTool(), {"path": str(tree), "pattern": "needle_in_src"}).content
    assert "app.py" in out
    assert "buried.py" not in out


def test_symlink_loop_does_not_hang(tmp_path: Path):
    """followlinks=False — a self-referential link would otherwise spin."""
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "f.txt").write_text("x")
    try:
        os.symlink(tmp_path, tmp_path / "real" / "loop")
    except OSError:
        pytest.skip("symlinks unavailable")
    files = list(iter_files(tmp_path, deadline=time.monotonic() + 10))
    assert any(f.name == "f.txt" for f in files)


# ── bounds ───────────────────────────────────────────────────────────

def test_iter_files_respects_an_expired_deadline(tree: Path):
    assert list(iter_files(tree, deadline=time.monotonic() - 1)) == []


def test_grep_stops_and_says_so_when_the_file_cap_is_hit(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("bridge.tools.search_tools.MAX_FILES_SCANNED", 5)
    for i in range(50):
        (tmp_path / f"f{i}.txt").write_text("nothing here\n")

    out = _run(GrepTool(), {"path": str(tmp_path), "pattern": "absent_pattern"}).content
    assert "INCOMPLETE" in out or "NOT a confirmed absence" in out
    assert "file limit" in out


def test_oversized_files_are_skipped_not_read(tmp_path: Path):
    big = tmp_path / "big.txt"
    big.write_text("needle\n" + ("x" * (MAX_FILE_BYTES + 1024)))
    (tmp_path / "small.txt").write_text("needle\n")

    out = _run(GrepTool(), {"path": str(tmp_path), "pattern": "needle"}).content
    assert "small.txt" in out
    assert "big.txt" not in out


# ── honesty about partial results ────────────────────────────────────

def test_genuine_absence_is_still_reported_plainly(tree: Path):
    """The scary wording must NOT appear on a complete search."""
    out = _run(GrepTool(), {"path": str(tree), "pattern": "definitely_not_here"}).content
    assert "No matches found" in out
    assert "INCOMPLETE" not in out
    assert "NOT a confirmed absence" not in out


def test_truncated_absence_is_never_called_absence(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("bridge.tools.search_tools.MAX_FILES_SCANNED", 2)
    for i in range(20):
        (tmp_path / f"f{i}.txt").write_text("nothing\n")
    out = _run(GrepTool(), {"path": str(tmp_path), "pattern": "absent"}).content
    assert "NOT a confirmed absence" in out


# ── behaviour preserved ──────────────────────────────────────────────

def test_grep_still_finds_and_reports_matches(tree: Path):
    out = _run(GrepTool(), {"path": str(tree), "pattern": "needle_in_src"}).content
    assert "Found" in out and "match(es)" in out


def test_file_pattern_filters_by_name(tree: Path):
    out = _run(
        GrepTool(),
        {"path": str(tree), "pattern": "needle_in_src", "file_pattern": "*.md"},
    ).content
    assert "notes.md" in out
    assert "app.py" not in out


def test_grep_on_a_single_file_still_works(tree: Path):
    out = _run(
        GrepTool(), {"path": str(tree / "src" / "app.py"), "pattern": "needle_in_src"}
    ).content
    assert "Found 1 match(es)" in out


def test_glob_recursive_finds_files_and_skips_junk(tree: Path):
    out = _run(GlobTool(), {"path": str(tree), "pattern": "**/*.py"}).content
    assert "app.py" in out
    assert "buried.py" not in out


def test_glob_flat_pattern_is_unchanged(tree: Path):
    out = _run(GlobTool(), {"path": str(tree / "src"), "pattern": "*.py"}).content
    assert "app.py" in out


def test_glob_sorts_newest_first(tmp_path: Path):
    old, new = tmp_path / "old.py", tmp_path / "new.py"
    old.write_text("a")
    new.write_text("b")
    os.utime(old, (1_000_000, 1_000_000))
    out = _run(GlobTool(), {"path": str(tmp_path), "pattern": "**/*.py"}).content
    assert out.index("new.py") < out.index("old.py")
