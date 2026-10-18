# Trajectory Training & Session Data Architecture

How Freyja captures, exports, and plans to use agent interaction
trajectories for model training and self-improvement.

> **Status:** v5 export format shipping. ShareGPT converter and batch
> runner planned for next phase.

---

## 1. What we capture today

Every Freyja session is persisted to `~/.freyja/sessions/<id>.json`
and can be downloaded as one training-ready JSON file via the sidebar
context menu (right-click → Download). The main process writes the file.
It reads the raw transcript and the full tool outputs from disk, and it
streams the result, so a session larger than one JavaScript string still
downloads.

### Export format (v5)

```json
{
  "version": 5,
  "exportedAt": "2026-04-12T...",
  "app": "freyja",

  "systemPrompt": "You are running inside Freyja...",

  "taskDescription": "the first user message (for RFT pairing)",

  "toolStats": {
    "bash": { "count": 6, "success": 5, "failure": 1 },
    "read_file": { "count": 3, "success": 3, "failure": 0 }
  },

  "thinkingTraces": [
    { "messageId": "msg_1", "thinking": "I should check the file..." }
  ],

  "session": {
    "id": "session-local",
    "title": "Debug auth flow",
    "model": "claude-sonnet-4-6",
    "workspace": "/Users/.../project",
    "createdAt": 1775960801821,
    "updatedAt": 1775973426125,
    "parentSessionId": null,
    "childSessionIds": ["sub_1", "comp_1"],
    "messageCount": 12,
    "totalInputTokens": 45000,
    "totalOutputTokens": 3200,
    "cacheReadTokens": 38000
  },

  "messages": [
    {
      "id": "msg_1",
      "role": "user",
      "parts": [{ "type": "text", "text": "fix the auth bug" }],
      "createdAt": 1775960802000,
      "inputTokens": 500,
      "outputTokens": 0,
      "attachments": null
    },
    {
      "id": "msg_2",
      "role": "assistant",
      "parts": [
        { "type": "thinking", "text": "Let me look at the auth module..." },
        { "type": "text", "text": "I'll check the auth flow." },
        { "type": "tool_call", "toolCallId": "call_1" }
      ],
      "createdAt": 1775960803000,
      "inputTokens": 0,
      "outputTokens": 150
    }
  ],

  "toolCalls": [
    {
      "id": "call_1",
      "name": "read_file",
      "arguments": { "path": "src/auth.py" },
      "argumentsSource": "transcript",
      "executedArguments": { "path": "src/auth.py", "resolvedPath": "/Users/.../src/auth.py" },
      "status": "success",
      "result": "# auth.py\ndef login(user, pwd):...",
      "resultSource": "transcript",
      "resultComplete": true,
      "resultChars": 4210,
      "isError": false,
      "durationMs": 45,
      "startedAt": 1775960803100,
      "groupId": "grp_1"
    },
    {
      "id": "call_2",
      "name": "call_tool",
      "arguments": {
        "tool_name": "bash",
        "arguments": { "command": "pytest -q", "summary": "Run the tests" }
      },
      "argumentsSource": "transcript",
      "invokes": "bash",
      "executedArguments": { "command": "pytest -q", "summary": "Run the tests" },
      "status": "success",
      "result": "3 passed",
      "resultSource": "transcript",
      "resultComplete": true,
      "resultChars": 8,
      "isError": false,
      "durationMs": 1200,
      "startedAt": 1775960804000,
      "groupId": "grp_1"
    }
  ],
  "toolCallOrder": ["call_1", "call_2"],

  "subagents": [
    {
      "id": "sub_1",
      "label": "research auth patterns",
      "mode": "background",
      "state": "done",
      "task": "Find OAuth2 best practices for Python",
      "startedAt": 1775960810000,
      "elapsedMs": 12000,
      "tokensIn": 8000,
      "tokensOut": 1200,
      "toolsCalled": 5,
      "result": "Found 3 patterns..."
    }
  ],

  "usage": {
    "totalInputTokens": 45000,
    "totalOutputTokens": 3200,
    "totalCacheReadTokens": 38000,
    "totalCacheWriteTokens": 0,
    "totalCost": 0.156,
    "lastTurnInputTokens": 5000,
    "lastTurnOutputTokens": 200,
    "contextWindow": 1000000
  },

  "systemEvents": [
    { "id": "sys_1", "subtype": "compaction_complete", "message": "...", "at": 1775960900000 }
  ],

  "rawTranscript": [
    { "ts": 1775960803.1, "session_id": "session-local", "turn_id": "turn-1",
      "message": { "role": "assistant", "content": "...", "tool_calls": [{ "id": "call_1", "name": "read_file", "arguments": { "path": "src/auth.py" } }],
                   "input_tokens": 500, "output_tokens": 150, "cache_read_tokens": 400, "cache_write_tokens": 0 } }
  ],
  "telemetry": {
    "llm_call_metric": [{ "model": "claude-sonnet-4-6", "input_tokens": 500, "output_tokens": 150, "cost_usd": 0.01, "duration_ms": 2100 }],
    "tool_call_metric": [{ "tool_call_id": "call_1", "tool_name": "read_file", "result_bytes": 4210, "ok": true }]
  },
  "compactions": [
    { "ts": 1775960900, "subtype": "compaction_complete", "summary_text": "…the full summary…" }
  ]
}
```

### What an export records

An export records what happened. It does not tidy it, and it does not cut it.

Two logs hold the exact record. The app's own record (what the UI shows) is not exact: the bridge cuts a result's preview to 2000 characters, and the app stores a call's arguments after path normalization. So the main process reads the logs the engine writes.

- `~/.freyja/projects/<id>/raw_messages.jsonl` has every message as the engine appended it: each call as the model sent it, each result as the model received it. Compaction never touches it.
- `~/.freyja/truncated/*.txt` has the whole output of a tool result the engine cut to fit the model's context. The text the model saw ends with `[Content truncated -- … Full output saved to: <path>]`.

**Arguments.** `name` and `arguments` are what the model sent. A `call_tool` call is `call_tool({tool_name, arguments})`. A model trained on `call_tool` with the inner arguments alone would learn to leave out `tool_name`. The app's own copy is not the same: across three real sessions, 609 of 1852 direct calls had app arguments that differed from the model's. The app adds `resolvedPath`, and in one case it resolved `AGENTS.md` to `CLAUDE.md`.

`argumentsSource` says where `arguments` came from. `transcript` is exact. `app` is the app's record: exact for a `call_tool` call, but for a direct call it may be after path normalization. `rebuilt` means the app lost the model's wrapper args, so the export rebuilt them from `invokes` and the executed arguments. The `call_tool` shape is fixed, so that is exact except for path normalization.

`executedArguments` are the arguments the tool ran with. It is present for a wrapped call, and for any call whose executed arguments differ from the ones the model sent. `invokes` is the tool a `call_tool` call ran.

**Results.** `result` is the whole result as the model received it, as text. A result with several text blocks has them joined by a newline. If the engine cut the result for the model, the export reads the whole output back from the file the engine saved, and checks that the cut text is the start of that file before it uses it. It reads only files under `~/.freyja/truncated/`, because the path comes from text a tool returned.

`resultSource` says how whole the result is:

| `resultSource` | Meaning | `resultComplete` |
|---|---|---|
| `transcript` | The transcript text is the whole result. | `true` |
| `original` | The engine cut it for the model. This is the whole output, read back from the saved file. | `true` |
| `transcript-truncated` | The engine cut it and the saved file is gone. Only the cut text remains. `resultOriginalChars` has the size the engine reported. | `false` |
| `preview` | There is no transcript entry. This is the bridge's preview, cut to 2000 characters with a `… [truncated N chars] …` marker if it was longer. | `false` if the marker is there |

This was checked on real sessions. Every restored result had the same byte length as `result_bytes` in the telemetry, which the engine records before it cuts the result.

**The rest.**

- `toolStats` counts a `call_tool` call for the tool behind it. A call that never reached its tool counts for the tool it asked for.
- `groupId` is the same for tool calls that came from one model response.
- Images are not in `toolCalls`. A `frame` or `resultImages` entry keeps the size, type and label. The images the model saw are in `rawTranscript`, inline.
- `systemEvents` is the app's rolling buffer of the last 100. It is the one cap left. The compaction, pressure and tool-call records are whole in `telemetry` and `compactions`, and `goalLoop` keeps the goal state out of the buffer.
- `call_tool` calls recorded before commit `f71f478` have the inner arguments under `name: "call_tool"` in the app's record. The export recovers `invokes` and the wrapper arguments from the raw transcript.
- A sub-agent is a session of its own with its own raw transcript. Export it by its own id. `childSessionIds` lists them.

### The download

The download is one file. It has everything above, then the whole record from disk:

| Key | Holds |
|---|---|
| `rawTranscript` | Every message as the model sent and received it, one per line of `raw_messages.jsonl`, with stored images put back inline so the file stands on its own. Each result is as the model received it, so one the engine cut is cut here. Read `toolCalls[].result` for the whole result. |
| `telemetry` | The session's telemetry rows, by row type. `llm_call_metric` has one row per model call: model, input and output tokens, cache read and write tokens, cost and duration. `tool_call_metric` has one row per tool call, with the size of the full result. Rows have a `turn_id`, so they join to `rawTranscript`. Rows for sub-agents are in their own sessions' exports. |
| `compactions` | Every compaction, runtime and agent driven, with its full summary text. |

### The `/export` command

`/export` writes three files for the active session. None is cut.

| File | Holds |
|---|---|
| `<name>.json` | The saved session, as the app stored it, with each tool result whole. Each tool call record gains `resultSource`, `resultComplete` and `resultChars`. A `call_tool` record from before `f71f478` gains `invokes` and `wrapperArguments`. Its `arguments` stay what the tool ran with. |
| `<name>.bundle.json` (v4) | `raw_transcript`: every message as the model sent and received it. `full_tool_results`: each result the engine cut, whole. `live_transcript`: what the app shows. `telemetry`: as above. `compactions`: as above. |
| `<name>.trace.txt` | The session as text to read. Each call shows its arguments as the model sent them, then `ran with:` when the executed arguments differ. A `call_tool` line reads `TOOL call_tool -> bash`. Each result is whole and indented, with a note on where it came from. Images are left out. |

Every file is written a piece at a time, and appears only when it is whole.

### What each field enables

| Field | Training use |
|---|---|
| `systemPrompt` | SFT: the model needs to know what instructions it was following |
| `taskDescription` | RFT/DPO: identifies the task for pairing trajectories |
| `messages` with `parts` | SFT: the core training signal (user→assistant→tool→assistant) |
| `messages[].parts[type=thinking]` | ATLaS: training on reasoning traces is 3x more effective |
| `toolCalls` with `arguments` + `result` | SFT: learning tool usage patterns. `arguments` is what the model sent and `result` is whole, so the pair is valid. |
| `toolStats` | Quality filtering: reject trajectories with high tool failure rates. Counted by the tool that ran. |
| `thinkingTraces` | Critical step selection: identify which steps matter most |
| `subagents` | Multi-agent training: how to decompose and delegate |
| `usage` | Cost analysis and token budget optimization |
| `session.model` | Provider-specific fine-tuning |
| `groupId` | Which tool calls one model response made |
| `rawTranscript` + `telemetry` | Exact per-model-call record: tokens, cache use, cost, duration, and every message as sent |

---

## 2. What we learned from the research

### Training approaches and what they need

| Approach | What it needs beyond our v5 export | Effort to add |
|---|---|---|
| **SFT** (supervised fine-tuning on expert trajectories) | Nothing — v5 has everything | Ready now |
| **RFT** (rejection sampling) | Binary outcome signal per session, multiple rollouts per task | Add outcome field + batch runner |
| **DPO** (preference optimization) | Paired (chosen/rejected) trajectories for same task | Manual annotation or LLM judge |
| **RL** (reinforcement learning) | Per-trajectory scalar reward, reference answers | Custom reward functions |
| **Trajectory synthesis** | Prompt datasets + automated verification | APIGen-MT pipeline |

### Key papers and projects that informed the design

- **ATLaS** (ACL 2025) — training on the critical 30% of steps
  outperforms full trajectories. Our `thinkingTraces` field enables
  this filtering.
- **Agent Data Protocol** (ICLR 2026) — the gold standard unified
  trajectory format. Our export is compatible with their schema.
- **AgentOhana** — quality filtering via LLM scoring (4.0+ threshold).
  Our `toolStats` enables automated quality filtering.
- **Hermes Agent** (Nous Research) — dual storage (SQLite for live +
  ShareGPT JSONL for training). Their format uses `<think>` blocks
  and `<tool_call>` XML, which our converter will target.
- **FireAct** — SFT on ReAct trajectories from multiple prompting
  methods. Our multi-model support (13 models) means we naturally
  generate diverse trajectories.
- **APIGen-MT** — simulated human-agent interplay with quality gates.
  Their verification pipeline (format → execution → policy →
  semantic alignment) is the template for our future batch runner.
- **Harbor Framework** (Laude Institute) — the emerging standard
  for agent evaluation and optimization in sandboxed containers.
  Defines ATIF v1.6 (Agent Trajectory Interchange Format), the
  most comprehensive trajectory spec available. See §2a below.

### Harbor Framework & ATIF v1.6

[harborframework.com](https://harborframework.com) · Apache-2.0 ·
by the Terminal-Bench team at Laude Institute.

Harbor is a framework for evaluating and optimizing agents in
container environments. Its architecture:

```
Task (instruction.md + Dockerfile + tests/)
  → Trial (task × agent × environment × verifier)
    → Job (many trials with concurrency control)
      → Results (ATIF trajectories + rewards + metrics)
```

**Why it matters for Freyja:**

1. **ATIF v1.6** is the trajectory interchange standard. It supports
   per-step reasoning, tool calls with observation linking,
   multimodal content, subagent trajectory refs, and per-step
   token IDs + logprobs for RL. Our v5 export maps cleanly to it.

2. **22+ agent adapters** — Claude Code, OpenHands, Codex, Aider,
   Cursor, SWE-Agent, etc. Each adapter converts the agent's native
   output to ATIF. We can make Freyja a Harbor-compatible agent.

3. **RewardKit** — lightweight verifier system with programmatic
   criteria (`criteria.file_exists()`) and LLM-as-judge via TOML
   config. Aggregation modes: weighted_mean, all_pass, any_pass,
   threshold. This replaces our need to build custom reward functions.

4. **Cloud sandboxing** — Daytona, Modal, E2B, Runloop for
   horizontal scaling of evaluation trials.

5. **Rollout details** — Harbor captures full `prompt_token_ids`,
   `completion_token_ids`, and `logprobs` per step for RL training.
   This answers our open question #1 (see §6): yes, capture them.

**ATIF v1.6 schema (condensed):**

```json
{
  "schema_version": "ATIF-v1.6",
  "session_id": "unique-run-id",
  "agent": {
    "name": "freyja",
    "version": "0.1.0",
    "model_name": "claude-sonnet-4-6",
    "tool_definitions": [{ "type": "function", "function": {...} }]
  },
  "steps": [
    {
      "step_id": 1,
      "source": "system",
      "message": "You are running inside Freyja..."
    },
    {
      "step_id": 2,
      "source": "user",
      "message": "Fix the auth bug"
    },
    {
      "step_id": 3,
      "source": "agent",
      "message": "I'll check the auth flow.",
      "reasoning_content": "Let me look at the auth module...",
      "tool_calls": [{
        "tool_call_id": "call_1",
        "function_name": "read_file",
        "arguments": { "path": "src/auth.py" }
      }],
      "observation": {
        "results": [{
          "source_call_id": "call_1",
          "content": "# auth.py\ndef login(user, pwd):..."
        }]
      },
      "metrics": {
        "prompt_tokens": 5000,
        "completion_tokens": 150,
        "cached_tokens": 3800,
        "cost_usd": 0.012
      }
    }
  ],
  "final_metrics": {
    "total_prompt_tokens": 45000,
    "total_completion_tokens": 3200,
    "total_cached_tokens": 38000,
    "total_cost_usd": 0.156,
    "total_steps": 12
  }
}
```

**Freyja v5 → ATIF field mapping:**

| Freyja v5 field | ATIF v1.6 field |
|---|---|
| `systemPrompt` | Step with `source: "system"` |
| `messages[role=user]` | Steps with `source: "user"` |
| `messages[role=assistant].parts[type=text]` | Steps with `source: "agent"`, `message` |
| `messages[role=assistant].parts[type=thinking]` | `reasoning_content` on agent steps |
| `messages[role=assistant].parts[type=tool_call]` | `tool_calls` array on agent steps |
| `toolCalls[id].result` | `observation.results[].content` |
| `toolCalls[id].name` | `tool_calls[].function_name` |
| `toolCalls[id].arguments` | `tool_calls[].arguments` |
| `subagents[id]` | `observation.results[].subagent_trajectory_ref` |
| `usage.totalInputTokens` | `final_metrics.total_prompt_tokens` |
| `usage.totalOutputTokens` | `final_metrics.total_completion_tokens` |
| `usage.totalCacheReadTokens` | `final_metrics.total_cached_tokens` |
| `usage.totalCost` | `final_metrics.total_cost_usd` |
| `session.model` | `agent.model_name` |

### Hermes Agent patterns we should adopt

From the `/Users/sohamshah/work/services/hermes-agent/` repo:

1. **ShareGPT JSONL format** — the de facto standard for training:
   ```json
   {
     "conversations": [
       {"from": "system", "value": "You are..."},
       {"from": "human", "value": "Fix the bug"},
       {"from": "gpt", "value": "<think>...</think>\n<tool_call>{...}</tool_call>"},
       {"from": "tool", "value": "<tool_response>{...}</tool_response>"},
       {"from": "gpt", "value": "Done."}
     ]
   }
   ```

2. **Per-tool success/failure tracking** — we now have this in
   `toolStats`. Hermes normalizes the schema to ensure all possible
   tools are present with count=0 for HuggingFace dataset compat.

3. **Reasoning coverage metrics** — Hermes tracks:
   ```json
   {
     "total_assistant_turns": 8,
     "turns_with_reasoning": 6,
     "turns_without_reasoning": 2,
     "has_any_reasoning": true
   }
   ```
   We should add this to our export.

4. **Batch trajectory generation** — Hermes has a `batch_runner.py`
   that runs N prompts through the agent, collects ShareGPT
   trajectories, and checkpoints progress. We should build the same.

5. **RL environment integration** — Hermes subclasses `HermesAgentBaseEnv`
   from Atropos for reward computation. Each environment defines
   `compute_reward()` and `evaluate()`. We should design our tools
   and session format to be compatible with this pattern.

---

## 3. What we should build next

### Phase 1: Trajectory converters (DONE)

Two converters at `scripts/`:

**ATIF v1.6** — the primary format for Harbor eval/optimization:

```bash
python scripts/convert-to-atif.py \
  --input exports/*.json \
  --output training/atif/ \
  --min-tool-success-rate 0.7
```

Produces one ATIF JSON per session with full step sequence,
tool call → observation linking, reasoning traces, subagent refs,
and final metrics. Validates against ATIF v1.6 schema.

**ShareGPT JSONL** — for Hermes/Axolotl SFT training:

```bash
python scripts/convert-to-sharegpt.py \
  --input exports/*.json \
  --output training/trajectories.jsonl \
  --min-tool-success-rate 0.7 \
  --require-thinking
```

Produces one JSONL line per session with `<think>` blocks and
`<tool_call>` XML wrapping. Strips image attachments.

### Phase 2: Outcome annotation (1 day)

Add a "rate this session" UI element:
- Thumbs up / thumbs down on the session header
- Optional 1-5 star rating
- Stored in the session snapshot and included in exports
- Used for RFT filtering (keep only thumbs-up sessions)

### Phase 3: Batch runner (3-5 days)

A CLI tool that:
- Takes a JSONL file of prompts
- Runs each through Freyja's engine (headless, no UI)
- Collects trajectories
- Applies quality filters (tool success rate, reasoning coverage)
- Outputs ShareGPT JSONL ready for training

```bash
freyja-batch \
  --prompts tasks/coding-challenges.jsonl \
  --model claude-sonnet-4-6 \
  --workers 4 \
  --output data/batch_1.jsonl \
  --resume
```

### Phase 4: DPO pair construction (2-3 days)

Run the same prompt twice (or with different models), compare
trajectories, and construct (chosen, rejected) pairs:

```bash
freyja-dpo \
  --prompts tasks/refactoring.jsonl \
  --model-chosen claude-opus-4-6 \
  --model-rejected claude-haiku-4-5 \
  --output data/dpo_pairs.jsonl
```

### Phase 5: Harbor integration (5-7 days)

Build a Harbor agent adapter for Freyja so it can participate
in Harbor's evaluation and optimization pipelines:

1. **Freyja agent adapter** — implement `BaseInstalledAgent` that
   wraps Freyja's headless engine, sets `SUPPORTS_ATIF = True`,
   and writes ATIF trajectory.json after each run.

2. **RewardKit verifiers** — define task-specific programmatic
   criteria and LLM-as-judge configs for Freyja's tool set.

3. **Task definitions** — create Harbor-compatible task specs
   (instruction.md + Dockerfile + tests/) for common Freyja
   workflows (code editing, debugging, computer-use, etc.).

4. **Cloud scaling** — test horizontal scaling via Daytona/Modal
   for batch evaluation (replaces our custom batch runner with
   Harbor's existing `TrialQueue` + concurrency control).

This replaces the earlier plan for a custom Atropos RL environment.
Harbor already has the trial queue, sandboxing, reward pipeline,
and rollout detail capture that we would have built from scratch.

---

## 4. Quality filtering pipeline

Based on the research, the filtering stack should be:

```
Raw trajectories
  │
  ├─ Filter 1: Tool success rate ≥ 70% (via toolStats)
  │
  ├─ Filter 2: Has reasoning traces (via thinkingTraces)
  │
  ├─ Filter 3: No partial/cancelled sessions
  │
  ├─ Filter 4: User rating ≥ 4/5 (when available)
  │
  ├─ Filter 5: LLM judge score ≥ 4.0 (AgentRater pattern)
  │
  └─ Output: high-quality ShareGPT JSONL for SFT
```

For DPO, we skip the binary filters and instead rank trajectories
for the same task, using the scoring pipeline to determine
chosen vs rejected.

---

## 5. Format compatibility matrix

| Target | Compatible? | Notes |
|---|---|---|
| **ATIF v1.6** (Harbor) | **Via converter** | `scripts/convert-to-atif.py` — primary target |
| **Hermes/Axolotl** (ShareGPT) | **Via converter** | `scripts/convert-to-sharegpt.py` — `<think>` + `<tool_call>` XML |
| **Harbor eval pipeline** | Via ATIF | Freyja sessions → ATIF → Harbor trials + RewardKit |
| **OpenAI SFT** (messages JSONL) | Via converter | Need `tool_calls` array + `tools` definitions |
| **OpenAI RFT** | Via converter + grader | Need `reference_answer` field |
| **Anthropic Bedrock** | Via converter | Messages format, no tool_use support in current FT |
| **Agent Data Protocol** | Direct | Our schema maps cleanly to ADP actions/observations |
| **AgentOhana** | Via converter | Need `steps[].input/output/next_observation` format |

---

## 6. Open questions (updated)

1. ~~**Should we capture token IDs and logprobs?**~~ **RESOLVED: Yes.**
   ATIF v1.6 includes per-step `prompt_token_ids`,
   `completion_token_ids`, and `logprobs`. Harbor's entire RL pipeline
   depends on them. Add to the bridge's token tracking when we build
   the RL pipeline (Phase 5). Storage cost is acceptable — Harbor
   stores them for every agent run.

2. **How to handle image attachments in training data?** **RESOLVED:**
   ATIF v1.6 uses `ContentPart` with `type: "image"` and
   `ImageSource` referencing files by path. Store images in an
   `images/` subdirectory alongside the trajectory JSON. Our ATIF
   converter implements option (c): store separately, reference by
   relative path.

3. **Cross-session memory as training context?** Still open. Hermes
   includes MEMORY.md in the system prompt. Our `systemPrompt` field
   already captures the full system prompt including any injected
   memory. For ATIF, this goes in the first step (`source: "system"`).

4. ~~**Multi-agent trajectories?**~~ **RESOLVED:** ATIF v1.6 has
   `subagent_trajectory_ref` in observation results. Each sub-agent
   is referenced by `session_id` and optional `trajectory_path`.
   Our ATIF converter maps `subagents[]` to these refs, with the
   sub-agent summary as the observation `content`.

5. **Should Freyja become a Harbor agent adapter?** New question.
   Harbor already wraps 22+ agents. Building a Freyja adapter would
   let us use Harbor's trial queue, cloud sandboxing, and RewardKit
   for evaluation without building our own batch infrastructure.
   Effort: ~2 days (implement `BaseInstalledAgent`, write ATIF in
   `populate_context_post_run()`).

---

*Built during the Freyja migration session, April 2026.
Research sources: ATLaS, Agent Data Protocol, AgentOhana,
Agent-FLAN, FireAct, AgentBank, APIGen-MT, Hermes Agent,
ASTRA, AgentHER, DiaTool-DPO, RAGEN, AgentGym, TopoCurate,
Harbor Framework (Laude Institute), ATIF v1.6 specification.*
