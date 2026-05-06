---
name: analyze-session
type: workflow
description: Analyze exported Freyja, browser-agent, or harness session JSON for timelines, failures, performance issues, and behavioral patterns.
triggers:
  - analyze session export
  - session postmortem
  - why did this run take so long
  - inspect session logs
  - summarize agent behavior from json
tags:
  - sessions
  - logs
  - performance
  - postmortem
source: "~/.claude/skills/analyze-session/SKILL.md"
confidence: unvalidated
---

# Analyze Session

Use this when the user points at session JSON, asks what happened during a run, or wants a performance or behavior postmortem.

## Workflow

1. Inventory the data first.
   - Identify file paths, export timestamps, session ids, status, model, task count, message count, tool calls, screenshots, and attachments.
   - Use structured JSON parsing. Do not skim huge exports as raw text.
   - Preserve exact timestamps and session ids in notes.

2. Build the timeline from events, not metadata guesses.
   - Prefer explicit event timestamps such as `turn_start`, `task_start`, `tool_call_started`, `tool_result`, `message_stop`, and `turn_complete`.
   - For older browser-agent exports, use `task_start` message timestamps for task timing. Metadata `created_at` may be session creation time, not task execution time.
   - Deduplicate repeated start/complete pairs that occur within the same minute before pairing durations.

3. Quantify the run.
   - Wall time, active time, long idle gaps, model usage, estimated context size, tool-call counts, errors, retries, screenshot count, attachment volume, and subagent concurrency.
   - Flag tasks or turns that are more than 2x the median duration.
   - Identify loops: repeated navigation, repeated tool errors, repeated read attempts, repeated screenshots without changed state, or re-verification creep.

4. Separate symptom from cause.
   - UI friction: clicks not registering, slow rendering, dropdown state, stale screenshots, unavailable controls.
   - Agent behavior: wrong plan, repeated checks, insufficient decomposition, missing instruction.
   - System behavior: long context, large media payloads, high subagent fanout, tool latency, bridge retries, renderer lag.

5. Produce the right artifact for the user.
   - Quick status: concise bullet findings with concrete timestamps.
   - Engineering postmortem: tables, timelines, issue taxonomy, root-cause hypotheses, and recommended changes.
   - Client/team summary: only the important narrative, impact, and next actions.

## Useful Queries

```bash
python3 - <<'PY'
import json
from pathlib import Path

path = Path("session.json")
data = json.loads(path.read_text())
messages = data.get("messages", [])
print(path.name, len(messages), "messages")
for key in ("session_info", "metadata", "export_timestamp"):
    if key in data:
        print(key, type(data[key]).__name__)
PY
```

## Output Shape

```markdown
# Session Analysis

## Executive Summary
- [Finding with impact]

## Timeline
| Time | Event | Notes |
| --- | --- | --- |

## Performance Signals
| Signal | Value | Interpretation |
| --- | ---: | --- |

## Failure Patterns
| Pattern | Evidence | Likely Cause | Fix |
| --- | --- | --- | --- |

## Recommended Changes
1. [Actionable change]
```

## Quality Bar

- Include concrete timestamps, counts, and file names.
- Avoid blaming the model when the evidence points to UI, tooling, or context pressure.
- Note confidence level for hypotheses that cannot be proven from the export.
- Keep raw excerpts short and only quote lines that prove a point.
