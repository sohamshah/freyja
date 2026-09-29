"""Helpers shared by the external-suite builders (download, truncation, stats).

Not a package: each builder adds its own directory to sys.path and imports this.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx

BUILD_DIR = Path(__file__).resolve().parent
JEV_DIR = BUILD_DIR.parent
if str(JEV_DIR) not in sys.path:
    sys.path.insert(0, str(JEV_DIR))

from schema import CASES_DIR, RAW_DIR, Case, approx_tokens, state_text, write_cases  # noqa: E402

STATE_CHAR_BUDGET = 24_000
STATS_DIR = RAW_DIR / "_stats"
STATS_MD = CASES_DIR / "external_STATS.md"


# ----------------------------------------------------------------------------- download

def download(url: str, dest: Path, *, headers: dict[str, str] | None = None,
             retries: int = 4, timeout: float = 120.0, quiet: bool = False) -> Path:
    """Fetch `url` to `dest` unless it already exists. Raises on HTTP errors."""
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last: Exception | None = None
    for attempt in range(retries):
        try:
            with httpx.Client(follow_redirects=True, timeout=timeout, headers=headers or {}) as c:
                with c.stream("GET", url) as r:
                    r.raise_for_status()
                    with tmp.open("wb") as fh:
                        for chunk in r.iter_bytes(1 << 16):
                            fh.write(chunk)
            tmp.rename(dest)
            if not quiet:
                print(f"  downloaded {dest.name} ({dest.stat().st_size:,} bytes)")
            return dest
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (401, 403, 404):
                tmp.unlink(missing_ok=True)
                raise
            last = e
        except (httpx.HTTPError, OSError) as e:
            last = e
        time.sleep(1.5 * (attempt + 1))
    tmp.unlink(missing_ok=True)
    raise RuntimeError(f"failed to download {url}: {last}")


def download_many(pairs: Iterable[tuple[str, Path]], *, workers: int = 8,
                  headers: dict[str, str] | None = None) -> list[tuple[Path, Exception | None]]:
    pairs = list(pairs)
    todo = [(u, d) for u, d in pairs if not (d.exists() and d.stat().st_size > 0)]
    print(f"  {len(pairs) - len(todo)} cached, {len(todo)} to fetch")
    results: list[tuple[Path, Exception | None]] = []

    def one(u: str, d: Path):
        try:
            download(u, d, headers=headers, quiet=True)
            return d, None
        except Exception as e:  # noqa: BLE001
            return d, e

    with ThreadPoolExecutor(max_workers=workers) as ex:
        for i, res in enumerate(ex.map(lambda p: one(*p), todo), 1):
            results.append(res)
            if i % 25 == 0 or i == len(todo):
                print(f"  fetched {i}/{len(todo)}")
    failed = [(d, e) for d, e in results if e is not None]
    if failed:
        print(f"  {len(failed)} downloads failed; first: {failed[0][0].name}: {failed[0][1]}")
    return results


def hf_list_files(repo: str) -> list[str]:
    url = f"https://huggingface.co/api/datasets/{repo}"
    with httpx.Client(follow_redirects=True, timeout=60) as c:
        r = c.get(url)
        r.raise_for_status()
        return [s["rfilename"] for s in r.json().get("siblings", [])]


def hf_resolve(repo: str, path: str) -> str:
    return f"https://huggingface.co/datasets/{repo}/resolve/main/{path}"


def gh_raw(repo: str, path: str, ref: str = "main") -> str:
    return f"https://raw.githubusercontent.com/{repo}/{ref}/{path}"


def gh_tree(repo: str, ref: str = "main") -> list[dict[str, Any]]:
    url = f"https://api.github.com/repos/{repo}/git/trees/{ref}?recursive=1"
    with httpx.Client(follow_redirects=True, timeout=60) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.json()["tree"]


# ----------------------------------------------------------------------------- truncation

def truncate_middle(s: str, max_chars: int, marker: str = "\n[... {n} chars omitted ...]\n") -> str:
    if len(s) <= max_chars:
        return s
    m = marker.format(n=len(s) - max_chars)
    keep = max_chars - len(m)
    if keep <= 0:
        return s[:max_chars]
    head = keep * 3 // 4
    tail = keep - head
    return s[:head] + m + s[len(s) - tail:]


def _string_leaves(obj: Any, path: tuple = ()) -> list[tuple[tuple, str]]:
    out = []
    if isinstance(obj, str):
        out.append((path, obj))
    elif isinstance(obj, dict):
        for k, v in obj.items():
            out.extend(_string_leaves(v, path + (k,)))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.extend(_string_leaves(v, path + (i,)))
    return out


def _set_path(obj: Any, path: tuple, value: Any) -> None:
    for k in path[:-1]:
        obj = obj[k]
    obj[path[-1]] = value


def fit_state(state: Any, budget: int = STATE_CHAR_BUDGET) -> tuple[Any, dict[str, Any]]:
    """Shrink the longest string leaves (middle truncation) until the serialized
    state is under `budget` chars. Returns (state, meta_fields)."""
    text = state_text(state)
    original = len(text)
    meta: dict[str, Any] = {"truncated": False, "original_chars": original}
    if original <= budget:
        meta["state_chars"] = original
        meta["approx_tokens"] = approx_tokens(text)
        return state, meta
    if isinstance(state, str):
        state = truncate_middle(state, budget)
    else:
        state = json.loads(json.dumps(state))  # deep copy
        for _ in range(64):
            text = state_text(state)
            over = len(text) - budget
            if over <= 0:
                break
            leaves = sorted(_string_leaves(state), key=lambda kv: -len(kv[1]))
            path, longest = leaves[0]
            target = max(200, len(longest) - over - 64)
            _set_path(state, path, truncate_middle(longest, target))
    text = state_text(state)
    meta.update(truncated=True, state_chars=len(text), approx_tokens=approx_tokens(text))
    return state, meta


def finalize(case: Case, budget: int = STATE_CHAR_BUDGET) -> Case:
    """Apply the size budget and record size metadata on `case.meta`."""
    state, m = fit_state(case.state, budget)
    case.state = state
    case.meta.update(m)
    return case


# ----------------------------------------------------------------------------- stats

def percentile(xs: list[int], p: float) -> int:
    if not xs:
        return 0
    xs = sorted(xs)
    k = min(len(xs) - 1, max(0, int(round(p * (len(xs) - 1)))))
    return xs[k]


def summarize(suite: str, cases: list[Case], question: str) -> dict[str, Any]:
    chars = [c.meta.get("state_chars", len(state_text(c.state))) for c in cases]
    labels = Counter(str(c.labels[question]) for c in cases)
    truncated = sum(1 for c in cases if c.meta.get("truncated"))
    s = {
        "suite": suite,
        "question": question,
        "n": len(cases),
        "labels": dict(sorted(labels.items())),
        "median_chars": int(statistics.median(chars)) if chars else 0,
        "p95_chars": percentile(chars, 0.95),
        "max_chars": max(chars) if chars else 0,
        "truncated": truncated,
    }
    print(f"[{suite}] n={s['n']} labels={s['labels']} median_chars={s['median_chars']} "
          f"p95_chars={s['p95_chars']} max={s['max_chars']} truncated={truncated}")
    return s


def write_suite(suite: str, cases: list[Case], question: str, *, source: dict[str, Any]) -> dict[str, Any]:
    """Validate + write cases, record stats sidecar, regenerate external_STATS.md."""
    for c in cases:
        finalize(c)
    write_cases(cases, CASES_DIR / f"{suite}.jsonl")
    s = summarize(suite, cases, question)
    s["source"] = source
    STATS_DIR.mkdir(parents=True, exist_ok=True)
    (STATS_DIR / f"{suite}.json").write_text(json.dumps(s, indent=1))
    regenerate_stats_md()
    return s


def regenerate_stats_md() -> None:
    entries = []
    for p in sorted(STATS_DIR.glob("*.json")):
        entries.append(json.loads(p.read_text()))
    lines = [
        "# External expert-labeled suites: statistics",
        "",
        "Generated by `evals/jev_judge/build/{llmbar,judgebench,agentrewardbench,trail}.py`.",
        f"State budget: {STATE_CHAR_BUDGET:,} chars (middle truncation; `meta.truncated`, "
        "`meta.original_chars`). `meta.approx_tokens = len(state_text)//4`.",
        "",
        "| suite | question | cases | label balance | median chars | p95 chars | max chars | truncated |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for e in entries:
        bal = ", ".join(f"{k}: {v}" for k, v in e["labels"].items())
        lines.append(f"| `{e['suite']}` | `{e['question']}` | {e['n']} | {bal} | {e['median_chars']:,} "
                     f"| {e['p95_chars']:,} | {e['max_chars']:,} | {e['truncated']} |")
    lines += ["", "## Sources, licenses, downloads", ""]
    seen = set()
    for e in entries:
        src = e.get("source", {})
        key = src.get("name")
        if not key or key in seen:
            continue
        seen.add(key)
        lines.append(f"### {key}")
        lines.append("")
        for k in ("license", "paper", "availability"):
            if src.get(k):
                lines.append(f"- {k}: {src[k]}")
        for u in src.get("urls", []):
            lines.append(f"- {u}")
        for note in src.get("notes", []):
            lines.append(f"- {note}")
        lines.append("")
    STATS_MD.parent.mkdir(parents=True, exist_ok=True)
    STATS_MD.write_text("\n".join(lines) + "\n")
