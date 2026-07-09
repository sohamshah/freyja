"""File + clipboard reach verbs (P2): files.list/open_latest/reveal/organize,
clipboard.read/write.

The morning-session gaps these close: "what's in my Downloads", "open the
last screenshot", "show that file in Finder", "sort my screenshots into
dated folders", and the two ends of the clipboard ("what did I just copy",
"copy this down"). Everything here stays INSIDE the operator's home tree —
a spoken path can never reach outside `~` — and the file reads/moves run in
Python (``os.scandir`` / ``shutil`` in a thread), not AppleScript, so
Finder's Automation TCC grant is never needed for the listing/organizing.

Only `files.reveal` and `files.open_latest` shell out — to plain `open`
(`open -R <path>` / `open <path>`), which needs no TCC and just hands the
file to Finder/the default app. `files.organize` is the one mutating verb:
it is confirm-tier, moves files with ``shutil.move`` (never overwriting an
existing destination, never leaving the home tree), and returns an undo
closure that moves each file back.

Spoken directory names ("downloads", "desktop", "documents", "home",
"trash") resolve to the matching home folder; anything else is treated as a
literal path UNDER the home tree (a bare "reports" → ``~/reports``), and any
path that resolves outside ``~`` is refused. Tests use real ``tmp_path``
dirs so ``os.scandir``/``shutil`` are exercised for real; the clipboard
verbs monkeypatch ``mac.run_exec`` (pbpaste) and the small pbcopy helper.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from pathlib import Path
from typing import Any, Optional

from bridge.voice.adapters import mac
from bridge.voice.verbs import Verb, VerbRegistry, VerbResult

_LIST_CAP = 25
# Spoken dir names → path relative to home (or home itself). "trash" is the
# real ~/.Trash; everything else is a plain visible folder.
_NAMED_DIRS = {
    "downloads": "Downloads",
    "download": "Downloads",
    "desktop": "Desktop",
    "documents": "Documents",
    "docs": "Documents",
    "home": "",
    "trash": ".Trash",
    "pictures": "Pictures",
    "movies": "Movies",
    "music": "Music",
}
_DEFAULT_ORGANIZE_DIR = "Desktop"

# kind → predicate over a lower-cased filename, for open_latest's filter.
_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".heic", ".webp", ".tiff", ".bmp")
_DOC_EXTS = (".pdf", ".doc", ".docx", ".pages", ".txt", ".md", ".rtf", ".key", ".numbers")


def _home() -> Path:
    """The operator's home, resolved. A single seam so tests that set
    ``HOME``/monkeypatch ``Path.home`` steer every path decision."""
    return Path.home().resolve()


def _within_home(path: Path) -> bool:
    """True when ``path`` (already absolute) is the home dir or below it.
    The home-tree guard on EVERY file op — a spoken path can name
    ``../../etc`` and we refuse rather than touch it."""
    home = _home()
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return resolved == home or home in resolved.parents


def _expand_path(raw: str) -> Path:
    """A literal spoken path → absolute Path. Expands ``~``/``~/…`` against
    OUR home (``_home()``, not ``os.path.expanduser``'s ``$HOME`` — they
    agree in production but the seam keeps home a single source of truth),
    and makes any other relative name home-relative (voice has no cwd)."""
    s = raw.strip()
    if s == "~":
        return _home()
    if s.startswith("~/"):
        return _home() / s[2:]
    path = Path(s)
    if path.is_absolute():
        return path
    return _home() / s


def _resolve_dir(spoken: Optional[str]) -> tuple[Optional[Path], Optional[str]]:
    """Spoken directory name → (absolute Path under home, error).

    "downloads"/"desktop"/… map to the named home folders; "home"/""/None
    is the home dir itself; anything else is a literal path. A leading ``~``
    expands to home; a bare relative name ("reports") is taken as
    ``~/reports`` (NEVER the process cwd — voice has no meaningful cwd). Any
    result outside the home tree is refused with an error string."""
    raw = (spoken or "").strip()
    if not raw:
        return _home(), None
    key = " ".join(raw.lower().split())
    if key in _NAMED_DIRS:
        rel = _NAMED_DIRS[key]
        return (_home() / rel if rel else _home()), None
    path = _expand_path(raw)
    if not _within_home(path):
        return None, f"{raw} is outside your home folder"
    return path, None


def _kind_of(name: str) -> str:
    low = name.lower()
    _, ext = os.path.splitext(low)
    if ext in _IMAGE_EXTS:
        return "image"
    if ext in _DOC_EXTS:
        return "doc"
    return "file"


def _matches_kind(name: str, kind: str) -> bool:
    """Filter predicate for open_latest's optional ``kind``."""
    low = name.lower()
    _, ext = os.path.splitext(low)
    if kind == "pdf":
        return ext == ".pdf"
    if kind == "image":
        return ext in _IMAGE_EXTS
    if kind == "doc":
        return ext in _DOC_EXTS
    if kind == "screenshot":
        return _is_screenshot(name)
    return True


def _is_screenshot(name: str) -> bool:
    """macOS screenshots are "Screenshot 2026-07-09 at ….png" (current) or
    "Screen Shot 2026-07-09 at ….png" (older) — match both, PNG only."""
    low = name.lower()
    return low.endswith(".png") and (
        low.startswith("screenshot") or "screen shot" in low
    )


def _scan(dir_path: str) -> list[dict[str, Any]]:
    """``os.scandir`` a directory into entry dicts, newest-first, capped.
    Synchronous (filesystem) — callers run it in a thread. Skips dot-files
    and anything that vanishes mid-scan (stat race)."""
    entries: list[dict[str, Any]] = []
    with os.scandir(dir_path) as it:
        for de in it:
            if de.name.startswith("."):
                continue
            try:
                st = de.stat()
                is_dir = de.is_dir()
            except OSError:
                continue
            entries.append(
                {
                    "name": de.name,
                    "is_dir": is_dir,
                    "size": int(st.st_size),
                    "mtime": float(st.st_mtime),
                }
            )
    entries.sort(key=lambda e: e["mtime"], reverse=True)
    return entries


def _when(mtime: float) -> str:
    """Compact human timestamp for a listing row."""
    try:
        return time.strftime("%b %d %H:%M", time.localtime(mtime))
    except (OverflowError, OSError, ValueError):
        return ""


def _dir_label(path: Path) -> str:
    """How a directory reads in a summary: "~" for home, "~/Downloads"
    below it, else its basename."""
    home = _home()
    if path == home:
        return "~"
    try:
        rel = path.relative_to(home)
        return f"~/{rel}"
    except ValueError:
        return path.name or str(path)


# ── files.list ──────────────────────────────────────────────────────────────


async def _list(args: dict[str, Any]) -> VerbResult:
    path, err = _resolve_dir(args.get("dir"))
    if path is None:
        return VerbResult(ok=False, summary=err or "bad directory", error=err)
    if not path.exists():
        return VerbResult(ok=False, summary=f"no folder at {_dir_label(path)}", error="not_found")
    if not path.is_dir():
        return VerbResult(ok=False, summary=f"{_dir_label(path)} isn't a folder", error="not_a_dir")
    try:
        rows = await asyncio.to_thread(_scan, str(path))
    except OSError as exc:
        return VerbResult(ok=False, summary=f"couldn't read {_dir_label(path)}", error=str(exc))
    total = len(rows)
    entries = [
        {
            "name": r["name"],
            "kind": "folder" if r["is_dir"] else _kind_of(r["name"]),
            "when": _when(r["mtime"]),
        }
        for r in rows[:_LIST_CAP]
    ]
    label = _dir_label(path)
    # Report the TRUE total, not the truncated slice — "25 of 1321" is
    # honest; saying "25 items" for a folder of 1321 is not. The model
    # gets `total` + `shown` so it can say either.
    if total > len(entries):
        summary = f"showing {len(entries)} of {total} items in {label}"
    else:
        summary = f"{total} item{'s' if total != 1 else ''} in {label}"
    return VerbResult(
        ok=True,
        summary=summary,
        data={"dir": label, "entries": entries, "total": total, "shown": len(entries)},
    )


# ── files.open_latest ────────────────────────────────────────────────────────


async def _open_latest(args: dict[str, Any]) -> VerbResult:
    path, err = _resolve_dir(args.get("dir"))
    if path is None:
        return VerbResult(ok=False, summary=err or "bad directory", error=err)
    if not path.is_dir():
        return VerbResult(ok=False, summary=f"no folder at {_dir_label(path)}", error="not_found")
    kind = str(args.get("kind") or "").strip().lower()
    try:
        rows = await asyncio.to_thread(_scan, str(path))
    except OSError as exc:
        return VerbResult(ok=False, summary=f"couldn't read {_dir_label(path)}", error=str(exc))
    # Files only (never open a folder as "the latest file"), newest first,
    # applying the optional kind filter.
    for r in rows:
        if r["is_dir"]:
            continue
        if kind and not _matches_kind(r["name"], kind):
            continue
        target = path / r["name"]
        if not _within_home(target):
            continue
        ok, out = await mac.run_exec(["open", str(target)])
        if not ok:
            return VerbResult(ok=False, summary=f"couldn't open {r['name']}", error=out)
        return VerbResult(ok=True, summary=f"opened {r['name']}", data={"name": r["name"]})
    what = f" {kind}" if kind else ""
    return VerbResult(
        ok=False,
        summary=f"no{what} files in {_dir_label(path)}",
        error="empty",
    )


# ── files.reveal ─────────────────────────────────────────────────────────────


def _resolve_within(dir_path: Path, name: str) -> tuple[Optional[Path], list[str]]:
    """Find ``name`` inside ``dir_path`` fuzzily: exact → prefix → substring
    (case-insensitive), returning (match, candidates). A unique fuzzy hit
    wins; several matches return candidate names so the caller asks."""
    try:
        names = [
            e["name"]
            for e in _scan(str(dir_path))
        ]
    except OSError:
        return None, []
    low = name.strip().lower()
    if not low:
        return None, []
    exact = [n for n in names if n.lower() == low]
    if len(exact) == 1:
        return dir_path / exact[0], []
    prefix = [n for n in names if n.lower().startswith(low)]
    if len(prefix) == 1:
        return dir_path / prefix[0], []
    if not prefix:
        subs = [n for n in names if low in n.lower()]
        if len(subs) == 1:
            return dir_path / subs[0], []
        pool = subs
    else:
        pool = prefix
    return None, sorted(pool, key=len)[:5]


async def _reveal(args: dict[str, Any]) -> VerbResult:
    # An explicit path wins; otherwise resolve a name within a directory.
    raw_path = str(args.get("path") or "").strip()
    if raw_path:
        target = _expand_path(raw_path)
        if not _within_home(target):
            return VerbResult(
                ok=False, summary=f"{raw_path} is outside your home folder", error="escape"
            )
        if not target.exists():
            return VerbResult(ok=False, summary=f"nothing at {raw_path}", error="not_found")
        ok, out = await mac.run_exec(["open", "-R", str(target)])
        if not ok:
            return VerbResult(ok=False, summary=f"couldn't reveal {target.name}", error=out)
        return VerbResult(ok=True, summary=f"revealed {target.name}", data={"name": target.name})

    dir_path, err = _resolve_dir(args.get("dir"))
    if dir_path is None:
        return VerbResult(ok=False, summary=err or "bad directory", error=err)
    if not dir_path.is_dir():
        return VerbResult(
            ok=False, summary=f"no folder at {_dir_label(dir_path)}", error="not_found"
        )
    name = str(args.get("name") or "").strip()
    if not name:
        return VerbResult(ok=False, summary="reveal what?", error="missing_name")
    match, candidates = _resolve_within(dir_path, name)
    if match is None:
        if candidates:
            listing = ", ".join(candidates)
            return VerbResult(
                ok=False,
                summary=f"which {name}? {len(candidates)} match",
                data={"candidates": candidates},
                error=f"multiple files match {name}: {listing}. Ask which one.",
            )
        return VerbResult(ok=False, summary=f"no file matching {name}", error="not_found")
    ok, out = await mac.run_exec(["open", "-R", str(match)])
    if not ok:
        return VerbResult(ok=False, summary=f"couldn't reveal {match.name}", error=out)
    return VerbResult(ok=True, summary=f"revealed {match.name}", data={"name": match.name})


# ── files.organize (confirm-tier, mutating) ──────────────────────────────────
# The "move my screenshots into dated folders" ask. Default target is the
# Desktop (fall back to Downloads if the Desktop has no screenshots), the
# grouping key is each file's mtime date (YYYY-MM-DD), and the default
# filter is screenshots. Moves are real ``shutil.move`` in a thread; we
# never overwrite an existing destination and never leave the home tree.
# The undo closure moves each file back to where it came from.


def _plan_organize(dir_path: Path, screenshots_only: bool) -> list[tuple[str, str]]:
    """(src, dest) pairs for the move, computed synchronously. Skips a file
    when its dated destination already exists (no overwrite) and when either
    endpoint would fall outside the home tree."""
    plan: list[tuple[str, str]] = []
    try:
        rows = _scan(str(dir_path))
    except OSError:
        return plan
    for r in rows:
        if r["is_dir"]:
            continue
        name = r["name"]
        if screenshots_only and not _is_screenshot(name):
            continue
        src = dir_path / name
        try:
            date = time.strftime("%Y-%m-%d", time.localtime(r["mtime"]))
        except (OverflowError, OSError, ValueError):
            continue
        dest_dir = dir_path / date
        dest = dest_dir / name
        if not _within_home(src) or not _within_home(dest_dir):
            continue
        if dest.exists():
            # Never overwrite — skip and let the operator handle the clash.
            continue
        plan.append((str(src), str(dest)))
    return plan


def _do_moves(plan: list[tuple[str, str]]) -> list[list[str]]:
    """Execute the planned moves, creating date subfolders. Returns the
    moves that actually happened as ``[src, dest]`` lists (JSON-friendly).
    Runs in a thread; a per-file failure is skipped, not fatal."""
    moved: list[list[str]] = []
    for src, dest in plan:
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            if os.path.exists(dest):
                continue  # a race created it since planning — never overwrite
            shutil.move(src, dest)
            moved.append([src, dest])
        except OSError:
            continue
    return moved


def _undo_moves(moved: list[list[str]]) -> list[list[str]]:
    """Reverse a move batch: dest → src, skipping any that would overwrite
    a file re-created at the source. Runs in a thread. Returns the pairs
    put back."""
    restored: list[list[str]] = []
    for src, dest in moved:
        try:
            if not os.path.exists(dest):
                continue
            if os.path.exists(src):
                continue  # something took the old name back — don't clobber
            os.makedirs(os.path.dirname(src), exist_ok=True)
            shutil.move(dest, src)
            restored.append([src, dest])
        except OSError:
            continue
    return restored


async def _organize(args: dict[str, Any]) -> VerbResult:
    by = str(args.get("by") or "date").strip().lower() or "date"
    if by != "date":
        # Only date grouping is wired; anything else is a model error.
        return VerbResult(
            ok=False, summary=f"can't organize by {by} yet", error="unsupported_by"
        )
    raw_dir = args.get("dir")
    screenshots_only = True

    async def _plan_for(path: Path) -> list[tuple[str, str]]:
        return await asyncio.to_thread(_plan_organize, path, screenshots_only)

    if raw_dir:
        path, err = _resolve_dir(raw_dir)
        if path is None:
            return VerbResult(ok=False, summary=err or "bad directory", error=err)
        if not path.is_dir():
            return VerbResult(
                ok=False, summary=f"no folder at {_dir_label(path)}", error="not_found"
            )
        plan = await _plan_for(path)
    else:
        # Default: Desktop, falling back to Downloads if the Desktop has no
        # screenshots to move (the operator most often means one or the other).
        desktop = _home() / _DEFAULT_ORGANIZE_DIR
        path = desktop
        plan = await _plan_for(desktop) if desktop.is_dir() else []
        if not plan:
            downloads = _home() / "Downloads"
            if downloads.is_dir():
                dl_plan = await _plan_for(downloads)
                if dl_plan:
                    path, plan = downloads, dl_plan

    if not plan:
        return VerbResult(
            ok=True,
            summary=f"no screenshots to sort in {_dir_label(path)}",
            data={"moved": []},
        )

    moved = await asyncio.to_thread(_do_moves, plan)
    if not moved:
        return VerbResult(
            ok=False, summary="couldn't move any screenshots", error="all_moves_failed"
        )

    async def undo() -> VerbResult:
        restored = await asyncio.to_thread(_undo_moves, moved)
        n = len(restored)
        return VerbResult(
            ok=True,
            summary=f"put {n} screenshot{'s' if n != 1 else ''} back",
            data={"restored": restored},
        )

    n = len(moved)
    return VerbResult(
        ok=True,
        summary=f"moved {n} screenshot{'s' if n != 1 else ''} into dated folders",
        data={"moved": moved, "dir": _dir_label(path)},
        undo=undo,
    )


# ── clipboard ────────────────────────────────────────────────────────────────
# Two ends of the pasteboard. `clipboard.read` runs `pbpaste` through the
# shared run_exec seam; `clipboard.write` needs stdin (run_exec has none),
# so it uses a tiny asyncio-subprocess helper that pipes text into `pbcopy`.
# Pasting (cmd+v) is deliberately NOT a verb — it's `computer.press cmd+v`,
# which keeps the keystroke behind the same computer-control gate rather
# than coupling this always-on adapter to the computer tools.


async def _pbcopy(text: str, timeout: float = 5.0) -> tuple[bool, str]:
    """Pipe ``text`` into `pbcopy` via stdin (run_exec has no stdin).
    Module-level so tests monkeypatch this seam instead of the real
    pasteboard."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "pbcopy",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return False, "pbcopy: command not found"
    except OSError as exc:
        return False, f"pbcopy failed to start: {exc}"
    try:
        _, stderr = await asyncio.wait_for(
            proc.communicate(text.encode("utf-8")), timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, "pbcopy timeout"
    if proc.returncode == 0:
        return True, ""
    return False, stderr.decode("utf-8", "replace").strip() or "pbcopy failed"


async def _clipboard_read(args: dict[str, Any]) -> VerbResult:
    ok, out = await mac.run_exec(["pbpaste"])
    if not ok:
        return VerbResult(ok=False, summary="couldn't read clipboard", error=out)
    if not out:
        return VerbResult(ok=True, summary="clipboard is empty", data={"text": ""})
    # The realtime model reads data.text; the summary is just a peek.
    return VerbResult(ok=True, summary=out[:60], data={"text": out})


async def _clipboard_write(args: dict[str, Any]) -> VerbResult:
    text = args.get("text")
    if text is None:
        return VerbResult(ok=False, summary="copy what?", error="missing_text")
    text = str(text)
    if not text:
        return VerbResult(ok=False, summary="nothing to copy", error="empty_text")
    ok, err = await _pbcopy(text)
    if not ok:
        return VerbResult(ok=False, summary="couldn't copy to clipboard", error=err)
    # Never echo the copied text verbatim into a long summary/receipt line.
    return VerbResult(ok=True, summary="copied to clipboard", data={"length": len(text)})


def register(registry: VerbRegistry) -> None:
    registry.register(
        Verb(
            name="files.list",
            description="List a folder's contents, newest first (Downloads, Desktop, etc.)",
            params={
                "dir": {
                    "type": "string",
                    "description": "downloads/desktop/documents/home/trash or a path",
                }
            },
            required=[],
            tier="auto",
            run=_list,
        )
    )
    registry.register(
        Verb(
            name="files.open_latest",
            description="Open the most recent file in a folder (optional kind filter)",
            params={
                "dir": {"type": "string", "description": "folder name or path"},
                "kind": {
                    "type": "string",
                    "enum": ["pdf", "image", "doc", "screenshot"],
                    "description": "restrict to this kind of file",
                },
            },
            required=[],
            tier="auto",
            run=_open_latest,
        )
    )
    registry.register(
        Verb(
            name="files.reveal",
            description="Reveal a file in Finder by name (within a folder) or by path",
            params={
                "name": {"type": "string", "description": "file name (fuzzy)"},
                "dir": {"type": "string", "description": "folder to search in"},
                "path": {"type": "string", "description": "exact path (skips name search)"},
            },
            required=[],
            tier="auto",
            run=_reveal,
        )
    )
    registry.register(
        Verb(
            name="files.organize",
            description="Sort a folder's screenshots into dated subfolders (Desktop/Downloads)",
            params={
                "dir": {"type": "string", "description": "folder name or path (default Desktop)"},
                "by": {"type": "string", "enum": ["date"], "description": "grouping (date)"},
            },
            required=[],
            tier="confirm",
            run=_organize,
        )
    )
    registry.register(
        Verb(
            name="clipboard.read",
            description="Read the current clipboard text",
            params={},
            required=[],
            tier="auto",
            run=_clipboard_read,
        )
    )
    registry.register(
        Verb(
            name="clipboard.write",
            description="Copy text to the clipboard",
            params={"text": {"type": "string"}},
            required=["text"],
            tier="auto",
            run=_clipboard_write,
        )
    )
