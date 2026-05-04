#!/usr/bin/env python3
"""Convert Freyja v3 JSON session exports to ShareGPT JSONL format.

Produces Hermes/Axolotl-compatible training data with <think> blocks
and <tool_call>/<tool_response> XML wrapping.

Usage:
    python scripts/convert-to-sharegpt.py --input exports/*.json --output training.jsonl
    python scripts/convert-to-sharegpt.py --input exports/ --output training.jsonl --require-thinking
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def compute_tool_success_rate(tool_stats: dict) -> float:
    total = sum(s["count"] for s in tool_stats.values())
    if total == 0:
        return 1.0
    success = sum(s["success"] for s in tool_stats.values())
    return success / total


def has_thinking_traces(data: dict) -> bool:
    return bool(data.get("thinkingTraces"))


def convert_to_sharegpt(data: dict) -> dict | None:
    """Convert a single Freyja v3 export to ShareGPT format.

    Returns a dict with "conversations" key, or None if the session
    has no meaningful content.
    """
    messages = data.get("messages", [])
    tool_calls_list = data.get("toolCalls", [])
    system_prompt = data.get("systemPrompt")
    session = data.get("session", {})

    # Index tool calls by ID
    tc_by_id: dict[str, dict] = {}
    for tc in tool_calls_list:
        tc_by_id[tc["id"]] = tc

    conversations: list[dict] = []

    # System message
    if system_prompt:
        conversations.append({"from": "system", "value": system_prompt})

    for msg in messages:
        role = msg.get("role")
        parts = msg.get("parts", [])

        if role == "user":
            text_parts = [p["text"] for p in parts if p.get("type") == "text" and p.get("text")]
            if text_parts:
                conversations.append({"from": "human", "value": "\n".join(text_parts)})

        elif role == "assistant":
            # Build the assistant message with <think> and <tool_call> blocks
            segments: list[str] = []
            tool_call_ids: list[str] = []

            for part in parts:
                ptype = part.get("type")
                if ptype == "thinking" and part.get("text"):
                    segments.append(f"<think>{part['text']}</think>")
                elif ptype == "text" and part.get("text"):
                    segments.append(part["text"])
                elif ptype == "tool_call" and part.get("toolCallId"):
                    tc_id = part["toolCallId"]
                    tool_call_ids.append(tc_id)
                    tc = tc_by_id.get(tc_id)
                    if tc:
                        call_obj = {
                            "name": tc["name"],
                            "arguments": tc.get("arguments") or {},
                        }
                        segments.append(f"<tool_call>{json.dumps(call_obj)}</tool_call>")

            if segments:
                conversations.append({"from": "gpt", "value": "\n".join(segments)})

            # Emit tool responses
            for tc_id in tool_call_ids:
                tc = tc_by_id.get(tc_id)
                if tc and tc.get("result") is not None:
                    result = tc["result"]
                    if not isinstance(result, str):
                        result = json.dumps(result)
                    # Truncate very large results
                    if len(result) > 30000:
                        result = result[:30000] + "\n... [truncated]"
                    conversations.append({
                        "from": "tool",
                        "value": f"<tool_response>{result}</tool_response>",
                    })

    if len(conversations) < 2:
        return None

    # Compute reasoning coverage metrics (Hermes pattern)
    assistant_turns = [c for c in conversations if c["from"] == "gpt"]
    turns_with_reasoning = sum(1 for c in assistant_turns if "<think>" in c["value"])

    return {
        "conversations": conversations,
        # Metadata for filtering and analysis
        "metadata": {
            "source": "freyja-v3",
            "session_id": session.get("id", ""),
            "model": session.get("model", ""),
            "task_description": data.get("taskDescription", ""),
            "tool_stats": data.get("toolStats", {}),
            "reasoning_coverage": {
                "total_assistant_turns": len(assistant_turns),
                "turns_with_reasoning": turns_with_reasoning,
                "turns_without_reasoning": len(assistant_turns) - turns_with_reasoning,
                "has_any_reasoning": turns_with_reasoning > 0,
            },
        },
    }


def process_file(
    input_path: Path,
    *,
    min_tool_success_rate: float = 0.0,
    require_thinking: bool = False,
) -> dict | None:
    """Process a single file. Returns ShareGPT dict or None if filtered."""
    with open(input_path) as f:
        data = json.load(f)

    if data.get("version") != 3 or data.get("app") != "freyja":
        print(f"  SKIP {input_path.name}: not a Freyja v3 export", file=sys.stderr)
        return None

    # Quality filters
    tool_stats = data.get("toolStats", {})
    if tool_stats and min_tool_success_rate > 0:
        rate = compute_tool_success_rate(tool_stats)
        if rate < min_tool_success_rate:
            print(
                f"  SKIP {input_path.name}: tool success rate {rate:.1%} < {min_tool_success_rate:.1%}",
                file=sys.stderr,
            )
            return None

    if require_thinking and not has_thinking_traces(data):
        print(f"  SKIP {input_path.name}: no thinking traces", file=sys.stderr)
        return None

    result = convert_to_sharegpt(data)
    if result is None:
        print(f"  SKIP {input_path.name}: no meaningful content", file=sys.stderr)
        return None

    turns = len(result["conversations"])
    print(f"  OK   {input_path.name} ({turns} turns)")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert Freyja v3 JSON exports to ShareGPT JSONL",
    )
    parser.add_argument(
        "--input", "-i",
        required=True,
        nargs="+",
        help="Input Freyja v3 JSON file(s) or directory",
    )
    parser.add_argument(
        "--output", "-o",
        required=True,
        help="Output JSONL file",
    )
    parser.add_argument(
        "--min-tool-success-rate",
        type=float,
        default=0.0,
        help="Skip sessions with tool success rate below this threshold (0.0-1.0)",
    )
    parser.add_argument(
        "--require-thinking",
        action="store_true",
        help="Skip sessions without thinking traces",
    )
    args = parser.parse_args()

    # Collect input files
    input_files: list[Path] = []
    for pattern in args.input:
        p = Path(pattern)
        if p.is_dir():
            input_files.extend(sorted(p.glob("*.json")))
        elif p.exists():
            input_files.append(p)
        else:
            from glob import glob
            input_files.extend(Path(f) for f in sorted(glob(pattern)))

    if not input_files:
        print("No input files found.", file=sys.stderr)
        sys.exit(1)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    converted = 0
    skipped = 0

    with open(output, "w") as out:
        for f in input_files:
            result = process_file(
                f,
                min_tool_success_rate=args.min_tool_success_rate,
                require_thinking=args.require_thinking,
            )
            if result:
                out.write(json.dumps(result, ensure_ascii=False) + "\n")
                converted += 1
            else:
                skipped += 1

    print(f"\nDone: {converted} converted, {skipped} skipped → {output}")


if __name__ == "__main__":
    main()
