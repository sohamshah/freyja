#!/usr/bin/env python3
"""Convert Freyja v3 JSON session exports to ATIF v1.6 format.

Usage:
    python scripts/convert-to-atif.py --input exports/*.json --output training/atif/
    python scripts/convert-to-atif.py --input session.json --output out.json
    python scripts/convert-to-atif.py --input exports/ --output atif/ --min-tool-success-rate 0.7
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def compute_tool_success_rate(tool_stats: dict) -> float:
    total = sum(s["count"] for s in tool_stats.values())
    if total == 0:
        return 1.0
    success = sum(s["success"] for s in tool_stats.values())
    return success / total


def ms_to_iso(ms: int | float | None) -> str | None:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except (ValueError, OSError):
        return None


def convert_to_atif(data: dict) -> dict:
    """Convert a single Freyja v3 export to ATIF v1.6."""

    session = data.get("session", {})
    messages = data.get("messages", [])
    tool_calls_list = data.get("toolCalls", [])
    tool_call_order = data.get("toolCallOrder", [])
    subagents = data.get("subagents", [])
    usage = data.get("usage", {})
    system_prompt = data.get("systemPrompt")

    # Index tool calls by ID for fast lookup
    tc_by_id: dict[str, dict] = {}
    for tc in tool_calls_list:
        tc_by_id[tc["id"]] = tc

    # Index subagents by ID
    sa_by_id: dict[str, dict] = {}
    for sa in subagents:
        sa_by_id[sa["id"]] = sa

    # Build ATIF steps
    steps: list[dict] = []
    step_id = 1

    # Step 1: system prompt
    if system_prompt:
        steps.append({
            "step_id": step_id,
            "source": "system",
            "message": system_prompt,
        })
        step_id += 1

    # Walk messages in order, converting each to ATIF steps
    for msg in messages:
        role = msg.get("role")
        parts = msg.get("parts", [])
        timestamp = ms_to_iso(msg.get("createdAt"))
        input_tokens = msg.get("inputTokens", 0)
        output_tokens = msg.get("outputTokens", 0)

        if role == "user":
            # Collect text parts
            text_parts = [p["text"] for p in parts if p.get("type") == "text" and p.get("text")]
            if not text_parts:
                continue
            message_content: str | list[dict] = "\n".join(text_parts)

            # Check for image attachments — use multimodal ContentPart
            image_parts = [p for p in parts if p.get("type") == "image"]
            if image_parts:
                content_parts: list[dict] = [{"type": "text", "text": message_content}]
                for img in image_parts:
                    src = img.get("url") or img.get("path") or img.get("data")
                    if src:
                        content_parts.append({
                            "type": "image",
                            "source": {
                                "media_type": img.get("mimeType", "image/png"),
                                "path": src,
                            },
                        })
                message_content = content_parts

            steps.append({
                "step_id": step_id,
                "source": "user",
                "message": message_content,
                **({"timestamp": timestamp} if timestamp else {}),
            })
            step_id += 1

        elif role == "assistant":
            # Gather text, thinking, and tool_call parts
            text_segments: list[str] = []
            reasoning_segments: list[str] = []
            tool_call_ids: list[str] = []

            for part in parts:
                ptype = part.get("type")
                if ptype == "text" and part.get("text"):
                    text_segments.append(part["text"])
                elif ptype == "thinking" and part.get("text"):
                    reasoning_segments.append(part["text"])
                elif ptype == "tool_call" and part.get("toolCallId"):
                    tool_call_ids.append(part["toolCallId"])

            message_text = "\n".join(text_segments) if text_segments else ""
            reasoning = "\n".join(reasoning_segments) if reasoning_segments else None

            # Build ATIF tool_calls and observations
            atif_tool_calls: list[dict] = []
            observation_results: list[dict] = []

            for tc_id in tool_call_ids:
                tc = tc_by_id.get(tc_id)
                if not tc:
                    continue

                atif_tool_calls.append({
                    "tool_call_id": tc["id"],
                    "function_name": tc["name"],
                    "arguments": tc.get("arguments") or {},
                })

                result_content = tc.get("result")
                if result_content is not None:
                    # Truncate very large tool results for training
                    if isinstance(result_content, str) and len(result_content) > 50000:
                        result_content = result_content[:50000] + "\n... [truncated]"
                    observation_results.append({
                        "source_call_id": tc["id"],
                        "content": result_content if isinstance(result_content, str)
                                   else json.dumps(result_content),
                    })

            # Check for subagent references in this message's tool calls
            # (sub-agent launches appear as tool_call parts referencing
            #  a subagent ID)
            for tc_id in tool_call_ids:
                tc = tc_by_id.get(tc_id)
                if not tc:
                    continue
                # Check if any subagent matches this tool call
                for sa in subagents:
                    if sa.get("id") == tc_id or sa.get("id") in (tc.get("arguments") or {}).values():
                        observation_results.append({
                            "source_call_id": tc["id"],
                            "content": sa.get("result", ""),
                            "subagent_trajectory_ref": [{
                                "session_id": sa["id"],
                                "extra": {
                                    "agent_type": sa.get("agentType", "general"),
                                    "label": sa.get("label", ""),
                                    "mode": sa.get("mode", ""),
                                    "state": sa.get("state", ""),
                                    "task": sa.get("task", ""),
                                    "artifact_path": sa.get("artifactPath"),
                                    "elapsed_ms": sa.get("elapsedMs"),
                                    "tokens_in": sa.get("tokensIn"),
                                    "tokens_out": sa.get("tokensOut"),
                                    "tools_called": sa.get("toolsCalled"),
                                },
                            }],
                        })

            step: dict = {
                "step_id": step_id,
                "source": "agent",
                "message": message_text,
                **({"timestamp": timestamp} if timestamp else {}),
                **({"reasoning_content": reasoning} if reasoning else {}),
            }

            if atif_tool_calls:
                step["tool_calls"] = atif_tool_calls
            if observation_results:
                step["observation"] = {"results": observation_results}

            # Per-step metrics (we have per-message token counts)
            if input_tokens or output_tokens:
                step["metrics"] = {
                    **({"prompt_tokens": input_tokens} if input_tokens else {}),
                    **({"completion_tokens": output_tokens} if output_tokens else {}),
                }

            # Model override (if available at message level)
            model = msg.get("model")
            if model:
                step["model_name"] = model

            steps.append(step)
            step_id += 1

    # Also emit standalone subagent refs that weren't linked to tool calls
    emitted_sa_ids = set()
    for step in steps:
        obs = step.get("observation", {})
        for r in obs.get("results", []):
            for ref in r.get("subagent_trajectory_ref", []):
                emitted_sa_ids.add(ref["session_id"])

    for sa in subagents:
        if sa["id"] not in emitted_sa_ids:
            steps.append({
                "step_id": step_id,
                "source": "system",
                "message": f"Sub-agent launched: {sa.get('label', sa['id'])}",
                **({"timestamp": ms_to_iso(sa.get("startedAt"))} if sa.get("startedAt") else {}),
                "observation": {
                    "results": [{
                        "content": sa.get("result", ""),
                        "subagent_trajectory_ref": [{
                            "session_id": sa["id"],
                            "extra": {
                                "agent_type": sa.get("agentType", "general"),
                                "label": sa.get("label", ""),
                                "mode": sa.get("mode", ""),
                                "state": sa.get("state", ""),
                                "task": sa.get("task", ""),
                                "artifact_path": sa.get("artifactPath"),
                                "elapsed_ms": sa.get("elapsedMs"),
                                "tokens_in": sa.get("tokensIn"),
                                "tokens_out": sa.get("tokensOut"),
                                "tools_called": sa.get("toolsCalled"),
                            },
                        }],
                    }],
                },
            })
            step_id += 1

    # Build final ATIF trajectory
    trajectory: dict = {
        "schema_version": "ATIF-v1.6",
        "session_id": session.get("id", "unknown"),
        "agent": {
            "name": "freyja",
            "version": data.get("version", 3),
            "model_name": session.get("model", "unknown"),
        },
        "steps": steps,
    }

    # Final metrics
    if usage:
        final_metrics: dict = {}
        if usage.get("totalInputTokens"):
            final_metrics["total_prompt_tokens"] = usage["totalInputTokens"]
        if usage.get("totalOutputTokens"):
            final_metrics["total_completion_tokens"] = usage["totalOutputTokens"]
        if usage.get("totalCacheReadTokens"):
            final_metrics["total_cached_tokens"] = usage["totalCacheReadTokens"]
        if usage.get("totalCost"):
            final_metrics["total_cost_usd"] = usage["totalCost"]
        final_metrics["total_steps"] = len(steps)
        if final_metrics:
            trajectory["final_metrics"] = final_metrics

    # Extra metadata from Freyja
    trajectory["extra"] = {
        "source_format": "freyja-v3",
        "exported_at": data.get("exportedAt"),
        "task_description": data.get("taskDescription", ""),
        "tool_stats": data.get("toolStats", {}),
        "workspace": session.get("workspace"),
    }

    # Notes
    thinking_traces = data.get("thinkingTraces", [])
    if thinking_traces:
        trajectory["notes"] = (
            f"Session has {len(thinking_traces)} thinking traces. "
            f"Reasoning coverage: {len(thinking_traces)} assistant turns with reasoning."
        )

    return trajectory


def process_file(
    input_path: Path,
    output_path: Path,
    *,
    min_tool_success_rate: float = 0.0,
) -> bool:
    """Process a single Freyja v3 JSON file. Returns True if converted."""
    with open(input_path) as f:
        data = json.load(f)

    # Validate it's a Freyja v3 export
    if data.get("version") != 3 or data.get("app") != "freyja":
        print(f"  SKIP {input_path.name}: not a Freyja v3 export", file=sys.stderr)
        return False

    # Quality filter: tool success rate
    tool_stats = data.get("toolStats", {})
    if tool_stats and min_tool_success_rate > 0:
        rate = compute_tool_success_rate(tool_stats)
        if rate < min_tool_success_rate:
            print(
                f"  SKIP {input_path.name}: tool success rate {rate:.1%} < {min_tool_success_rate:.1%}",
                file=sys.stderr,
            )
            return False

    atif = convert_to_atif(data)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(atif, f, indent=2, ensure_ascii=False)

    step_count = len(atif.get("steps", []))
    print(f"  OK   {input_path.name} → {output_path.name} ({step_count} steps)")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Convert Freyja v3 JSON exports to ATIF v1.6 format",
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
        help="Output file (single input) or directory (multiple inputs)",
    )
    parser.add_argument(
        "--min-tool-success-rate",
        type=float,
        default=0.0,
        help="Skip sessions with tool success rate below this threshold (0.0-1.0)",
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
            # Try glob expansion
            from glob import glob
            input_files.extend(Path(f) for f in sorted(glob(pattern)))

    if not input_files:
        print("No input files found.", file=sys.stderr)
        sys.exit(1)

    output = Path(args.output)
    converted = 0
    skipped = 0

    if len(input_files) == 1 and output.suffix == ".json":
        # Single file → single file
        if process_file(input_files[0], output, min_tool_success_rate=args.min_tool_success_rate):
            converted += 1
        else:
            skipped += 1
    else:
        # Multiple files → directory
        output.mkdir(parents=True, exist_ok=True)
        for f in input_files:
            out_name = f.stem + ".atif.json"
            if process_file(f, output / out_name, min_tool_success_rate=args.min_tool_success_rate):
                converted += 1
            else:
                skipped += 1

    print(f"\nDone: {converted} converted, {skipped} skipped.")


if __name__ == "__main__":
    main()
