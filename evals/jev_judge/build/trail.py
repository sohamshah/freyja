"""TRAIL (PatronusAI/TRAIL) -> suite `trail_error`.

148 OpenTelemetry-style agent traces (smolagents on GAIA and SWE-bench) with
human-annotated errors located at span ids. The Hugging Face dataset is gated
(403 without approved access), so this uses the MIT-licensed GitHub mirror
patronus-ai/trail-benchmark, which carries the same traces and annotations.

Per trace, up to 2 annotated-error spans (label true) and 2 matched non-error
spans of the same kind (label false); capped at 300 cases, balanced.

Run: .venv/bin/python evals/jev_judge/build/trail.py [--cap 300]
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import RAW_DIR, Case, download_many, gh_raw, gh_tree, truncate_middle, write_suite  # noqa: E402
from schema import Question  # noqa: E402

REPO = "patronus-ai/trail-benchmark"
RAW = RAW_DIR / "trail"
SPLITS = {"gaia": ("GAIA", "processed_annotations_gaia"),
          "swe_bench": ("SWE Bench", "processed_annotations_swe_bench")}
CANDIDATE_KINDS = {"LLM", "TOOL"}
CONTEXT_BEFORE, CONTEXT_AFTER = 1500, 500
SPAN_MAX_CHARS = 9000
PER_TRACE = 2  # positives per trace; negatives matched 1:1

QUESTION = Question(
    name="contains_error",
    type="noul",
    instructions=(
        "Does the highlighted step contain an error (wrong tool use, hallucinated result, formatting "
        "failure, task-orchestration mistake, or violation of instructions)?"
    ),
)
SOURCE = {
    "name": "TRAIL",
    "license": "MIT (both the HF dataset card and github.com/patronus-ai/trail-benchmark LICENSE)",
    "paper": "Deshpande et al. 2025, arXiv:2505.08638",
    "availability": "HF repo PatronusAI/TRAIL is gated (403 for this machine's token); the ungated GitHub "
                    "mirror has all 148 traces (117 GAIA + 31 SWE-bench, ~178 MB) and annotations, all used",
    "urls": [gh_raw(REPO, "benchmarking/data/GAIA/<trace_id>.json"),
             gh_raw(REPO, "benchmarking/data/SWE Bench/<trace_id>.json"),
             gh_raw(REPO, "benchmarking/processed_annotations_gaia/<trace_id>.json"),
             gh_raw(REPO, "benchmarking/processed_annotations_swe_bench/<trace_id>.json")],
    "notes": [
        "Spans are flattened in chronological DFS order; only LLM and TOOL spans are candidates (all but 2 of "
        "the 841 annotated locations are LLM/TOOL spans). LLM spans render the last input message and the "
        "model output (content or tool calls); TOOL spans render tool name, input and output; Step spans "
        "render their execution log after their children. One annotation file has a trailing comma and is "
        "parsed leniently.",
        "trace_excerpt = ~1,500 chars before the highlighted span, the span (<=9,000 chars, middle-truncated), "
        "~500 chars after. step_index is the span's position in the flattened trace.",
    ],
}

CATEGORY_CANON = {
    "context handling failure": "Context Handling Failures",
    "language-only": "Language-only",
    "task orchestration errors": "Task Orchestration",
    "task orchestration error": "Task Orchestration",
    "instruction non-compliance": "Instruction Non-compliance",
    "instruction non complience": "Instruction Non-compliance",
    "formatting error": "Formatting Errors",
    "poor information retrieval": "Poor Information Retrieval",
    "goal deviation": "Goal Deviation",
    "tool selection": "Tool Selection Errors",
}


def canon_category(c: str) -> str:
    c = " ".join(c.split())
    return CATEGORY_CANON.get(c.lower(), c)


# ----------------------------------------------------------------------------- download

def fetch_raw() -> None:
    tree = gh_tree(REPO)
    pairs = []
    for t in tree:
        p = t["path"]
        if t["type"] != "blob":
            continue
        if p.startswith("benchmarking/data/") or p.startswith("benchmarking/processed_annotations_"):
            pairs.append((gh_raw(REPO, p), RAW / p[len("benchmarking/"):]))
        elif p in ("LICENSE", "README.md"):
            pairs.append((gh_raw(REPO, p), RAW / p))
    download_many(pairs, workers=8)


def load_json_lenient(path: Path) -> Any:
    text = path.read_text()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return json.loads(re.sub(r",(\s*[\]}])", r"\1", text))


# ----------------------------------------------------------------------------- span rendering

def _text_of(content: Any) -> str:
    """Message content may be a string, or a JSON-encoded list of {type,text} parts."""
    if content is None:
        return ""
    if isinstance(content, list):
        return "\n".join(_text_of(p) for p in content)
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or json.dumps(content))
    s = str(content)
    if s[:1] in "[{":
        try:
            return _text_of(json.loads(s))
        except (json.JSONDecodeError, RecursionError):
            pass
    return s


def llm_last_input(attrs: dict) -> tuple[str, str]:
    idx = [int(k.split(".")[2]) for k in attrs if k.startswith("llm.input_messages.") and k.endswith(".message.content")]
    if not idx:
        return "", ""
    i = max(idx)
    return (str(attrs.get(f"llm.input_messages.{i}.message.role", "")),
            _text_of(attrs.get(f"llm.input_messages.{i}.message.content")))


def llm_output(attrs: dict) -> str:
    content = attrs.get("llm.output_messages.0.message.content")
    if content:
        return _text_of(content)
    calls = []
    j = 0
    while f"llm.output_messages.0.message.tool_calls.{j}.tool_call.function.name" in attrs:
        name = attrs[f"llm.output_messages.0.message.tool_calls.{j}.tool_call.function.name"]
        args = attrs.get(f"llm.output_messages.0.message.tool_calls.{j}.tool_call.function.arguments", "")
        calls.append(f"tool_call {name}({args})")
        j += 1
    if calls:
        return "\n".join(calls)
    return _text_of(attrs.get("output.value"))


def render_span(s: dict, kind: str | None) -> str | None:
    """Text block for one span, or None if the span carries nothing worth showing."""
    a = s.get("span_attributes", {}) or {}
    name = s["span_name"]
    if kind == "LLM":
        role, last_in = llm_last_input(a)
        out = llm_output(a)
        body = (f"last input message ({role}):\n{truncate_middle(last_in.strip(), 1500)}\n\n"
                f"model output:\n{truncate_middle(out.strip(), 6000)}")
    elif kind == "TOOL":
        body = (f"tool: {a.get('tool.name', name)}\n"
                f"input: {truncate_middle(_text_of(a.get('input.value')).strip(), 1500)}\n"
                f"output: {truncate_middle(_text_of(a.get('output.value')).strip(), 3000)}")
    elif kind == "CHAIN":
        out = _text_of(a.get("output.value")).strip()
        if not out:
            return None
        body = f"execution log / observation:\n{truncate_middle(out, 2000)}"
    elif kind == "AGENT":
        inp = _text_of(a.get("input.value")).strip()
        body = f"agent run input:\n{truncate_middle(inp, 2500)}"
    else:
        return None
    return f"{body}\n"


def flatten(trace: dict) -> list[dict]:
    """Chronological DFS. CHAIN (Step) spans are emitted after their children,
    because their output is the observation produced by the step."""
    out: list[dict] = []
    seen: set[str] = set()

    def walk(s: dict) -> None:
        if s["span_id"] in seen:
            return
        seen.add(s["span_id"])
        kind = (s.get("span_attributes") or {}).get("openinference.span.kind")
        entry = {"span": s, "kind": kind}
        if kind == "CHAIN":
            for c in sorted(s.get("child_spans", []), key=lambda x: x["timestamp"]):
                walk(c)
            out.append(entry)
        else:
            out.append(entry)
            for c in sorted(s.get("child_spans", []), key=lambda x: x["timestamp"]):
                walk(c)

    for s in sorted(trace["spans"], key=lambda x: x["timestamp"]):
        walk(s)
    rendered = []
    for e in out:
        text = render_span(e["span"], e["kind"])
        if text is None:
            continue
        rendered.append({**e, "text": text})
    for i, e in enumerate(rendered):
        e["index"] = i
        e["header"] = f"=== step {i}: {e['span']['span_name']} [{e['kind']}] ===\n"
    return rendered


def extract_task(entries: list[dict]) -> str:
    for e in entries:
        if e["kind"] == "AGENT":
            a = e["span"].get("span_attributes", {})
            raw = a.get("smolagents.task") or a.get("input.value") or ""
            try:
                d = json.loads(raw)
                if isinstance(d, dict) and d.get("task"):
                    return str(d["task"])
            except (json.JSONDecodeError, TypeError):
                pass
            if raw:
                return str(raw)
    for e in entries:
        if e["kind"] == "LLM":
            a = e["span"].get("span_attributes", {})
            for i in range(0, 8):
                if a.get(f"llm.input_messages.{i}.message.role") == "user":
                    t = _text_of(a.get(f"llm.input_messages.{i}.message.content"))
                    return re.sub(r"^\s*New task:\s*", "", t)
    return ""


def excerpt(entries: list[dict], idx: int) -> str:
    before = "".join(e["header"] + e["text"] + "\n" for e in entries[:idx])
    after = "".join(e["header"] + e["text"] + "\n" for e in entries[idx + 1:])
    target = entries[idx]
    block = target["header"] + truncate_middle(target["text"], SPAN_MAX_CHARS)
    if len(before) > CONTEXT_BEFORE:
        before = "[... earlier steps omitted ...]\n" + before[-CONTEXT_BEFORE:]
    if len(after) > CONTEXT_AFTER:
        after = after[:CONTEXT_AFTER] + "\n[... later steps omitted ...]"
    return (before + f">>>>> HIGHLIGHTED STEP {idx} BEGIN >>>>>\n" + block
            + f"<<<<< HIGHLIGHTED STEP {idx} END <<<<<\n" + after)


# ----------------------------------------------------------------------------- build

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cap", type=int, default=300)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    fetch_raw()
    pos_pool: list[dict] = []
    neg_pool: list[dict] = []
    n_traces = n_err = n_missing = 0
    for split, (trace_dir, ann_dir) in SPLITS.items():
        for ann_path in sorted((RAW / ann_dir).glob("*.json")):
            trace_path = RAW / "data" / trace_dir / ann_path.name
            if not trace_path.exists():
                print(f"  no trace for {ann_path.name}")
                continue
            n_traces += 1
            ann = load_json_lenient(ann_path)
            trace = json.loads(trace_path.read_text())
            entries = flatten(trace)
            by_id = {e["span"]["span_id"]: e for e in entries}
            task = extract_task(entries)
            errors_by_span: dict[str, list[dict]] = defaultdict(list)
            for err in ann.get("errors", []):
                n_err += 1
                if err.get("location") in by_id:
                    errors_by_span[err["location"]].append(err)
                else:
                    n_missing += 1
            pos_ids = [sid for sid in errors_by_span if by_id[sid]["kind"] in CANDIDATE_KINDS]
            neg_entries = [e for e in entries if e["kind"] in CANDIDATE_KINDS and e["span"]["span_id"] not in errors_by_span]
            k = min(PER_TRACE, len(pos_ids), len(neg_entries))
            if k == 0:
                continue
            rng.shuffle(pos_ids)
            chosen_pos = pos_ids[:k]
            # negatives: match the kind of each chosen positive when possible
            rng.shuffle(neg_entries)
            chosen_neg: list[dict] = []
            for sid in chosen_pos:
                want = by_id[sid]["kind"]
                pick = next((e for e in neg_entries if e["kind"] == want and e not in chosen_neg), None) \
                    or next((e for e in neg_entries if e not in chosen_neg), None)
                if pick is not None:
                    chosen_neg.append(pick)
            common = {"trace_id": trace["trace_id"], "split": split, "task": task, "n_steps": len(entries),
                      "n_errors_in_trace": len(ann.get("errors", []))}
            for sid in chosen_pos:
                e = by_id[sid]
                errs = errors_by_span[sid]
                pos_pool.append({**common, "entry": e, "entries": entries, "label": True,
                                 "error_category": canon_category(errs[0]["category"]),
                                 "error_categories": [canon_category(x["category"]) for x in errs],
                                 "error_impact": [x.get("impact") for x in errs],
                                 "error_evidence": [truncate_middle(str(x.get("evidence", "")), 400) for x in errs]})
            for e in chosen_neg:
                neg_pool.append({**common, "entry": e, "entries": entries, "label": False})

    print(f"traces={n_traces} annotated_errors={n_err} unresolvable_locations={n_missing} "
          f"candidates: pos={len(pos_pool)} neg={len(neg_pool)}")

    half = min(args.cap // 2, len(pos_pool), len(neg_pool))
    # subsample per trace pairs to keep the trace mix; simple random subsample of each pool
    rng.shuffle(pos_pool)
    rng.shuffle(neg_pool)
    picked = pos_pool[:half] + neg_pool[:half]
    picked.sort(key=lambda r: (r["trace_id"], r["entry"]["index"]))

    cases: list[Case] = []
    for r in picked:
        e = r["entry"]
        meta = {"source": "TRAIL", "split": r["split"], "trace_id": r["trace_id"], "span_id": e["span"]["span_id"],
                "span_name": e["span"]["span_name"], "span_kind": e["kind"], "n_steps": r["n_steps"],
                "n_errors_in_trace": r["n_errors_in_trace"],
                "error_category": r.get("error_category"),
                "error_categories": r.get("error_categories", []),
                "error_impact": r.get("error_impact", []),
                "error_evidence": r.get("error_evidence", [])}
        cases.append(Case(
            id=f"trail_error-{len(cases) + 1:04d}",
            suite="trail_error",
            category="compute",
            state={"task": truncate_middle(r["task"], 3000),
                   "trace_excerpt": excerpt(r["entries"], e["index"]),
                   "step_index": e["index"]},
            questions=[QUESTION],
            labels={"contains_error": r["label"]},
            label_source="trail_human_error_annotation",
            meta=meta,
        ))
    write_suite("trail_error", cases, "contains_error", source=SOURCE)
    print("  by split:", dict(Counter(c.meta["split"] for c in cases)))
    print("  by span kind:", dict(Counter(c.meta["span_kind"] for c in cases)))
    print("  error categories (positives):",
          dict(Counter(c.meta["error_category"] for c in cases if c.labels["contains_error"]).most_common(12)))


if __name__ == "__main__":
    main()
