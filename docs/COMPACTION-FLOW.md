# Compaction flow — end-to-end stocktaking

Last updated: 2026-05-14

This is a reference doc for the complete LLM-summary compaction lifecycle in Freyja PLUS the agent-facing `session_memory` tool that lives alongside it. It complements `COMPACTION-DECISION-DRAFT.md` (the design / decision log) by describing what the code actually does today — every trigger path, every emit, every artifact, every metric field. Use it when:

- Debugging a session that compacted unexpectedly.
- Adding a new compaction trigger and making sure it fires the same signals every other trigger does.
- Building a new dashboard surface and figuring out which JSONL field to read.
- Wondering whether the agent's notes in `session_memory` survive a session restore (yes), are in the export bundle (no — Gap G4), or get cleaned up on session deletion (no — Gap G6).
- Onboarding to the compaction subsystem.

Sections 1–9 cover LLM-summary compaction itself. Section 10 covers `session_memory` (the agent's compaction-aware scratchpad). Section 11 is the implementation history.

## 1 — Trigger paths

There are four ways an LLM-summary compaction can happen. All four eventually call `engine/compaction.py:SummaryCompaction.compact()` through `engine/runner.py:_attempt_compaction()` or directly.

| Path | Source | Code site | Fires when |
|---|---|---|---|
| **A** Pre-request safety | Runtime, before each provider call | `engine/runner.py:_ensure_context_room` | `ratio > CONTEXT_COMPACTION_THRESHOLD` (0.80) |
| **B** Overflow cascade | Runtime, after provider returns `ContextOverflowError` | `engine/runner.py:_handle_overflow_cascade` | Provider rejected request |
| **C** Agent-driven | `summarize_context` tool with LLM scopes (`since_last_compaction`/`all`/`early`) | `bridge/tools/summarize_context_tool.py` | Agent chose to call the tool |
| **D** Manual | User clicks compact / `/compact` slash command | `bridge/freyja_bridge.py:force_compact` | Explicit user action |

The non-LLM scopes of the `summarize_context` tool (`tool_results_only`, `exploration_only`) take their own path inside `SummarizeContextTool._compact_tool_results_only` / `_compact_exploration_only`. They mutate tool-result message bodies in place without an LLM call, emit `context_pruning` system events, and don't carry a summary text. They're not covered in detail in this doc — search for "context_pruning" in the codebase for the full story.

## 2 — Universal canonical fields

Every successful LLM-summary compaction produces a `CompactionResult` and a system_event whose `details` dict carries the same canonical field set, regardless of which trigger fired it. This was not always true — the four paths used to disagree on `tokens_before` (engine transcript total vs request total vs effective-window-relative), only the manual path wrote snapshot files, and the summarizer LLM call wasn't tracked at all. The 2026-05-14 audit closed those gaps.

### Canonical CompactionResult fields

```python
success: bool
summary: str | None
tokens_before: int           # transcript.estimate_tokens() before append_compaction
tokens_after: int            # transcript.estimate_tokens() after append_compaction
error: str | None
entries_removed: int         # number of pre-split entries deleted
messages_before: int         # entries_count before
messages_after: int          # entries_count after (always entries_before - entries_removed + 1)
images_before: int
images_after: int
resumed_from_previous: bool  # true on the iterative path (SUMMARY_UPDATE_PROMPT)
summary_tokens: int          # estimate of the produced summary's own size, len // 4
summarizer_input_tokens: int   # input tokens charged for the summarizer LLM call
summarizer_output_tokens: int  # output tokens charged
summarizer_duration_ms: int    # wall-clock of the summarizer call
summarizer_model: str | None   # model id that produced the summary
summarizer_cost_usd: float | None  # estimated cost, None if model isn't priced
```

### Canonical details dict (`_compaction_event_details`)

```jsonc
{
  // Tokens — all paths use transcript.estimate_tokens() for the
  // engine view AND optionally pass a caller-perspective measure
  // (e.g. _current_context_tokens). Both are exposed.
  "tokens_before": <int>,                   // caller perspective (or transcript)
  "tokens_after": <int>,
  "transcript_tokens_before": <int>,        // canonical engine measure
  "transcript_tokens_after": <int>,
  "context_tokens_before": <int>,           // back-compat alias
  "context_tokens_after": <int>,
  // Effective-window pressure pct — same denominator everywhere.
  "effective_window": <int> | null,         // context_window - max_tokens_per_turn
  "pressure_pct_before": <float> | null,    // (tokens_before / effective_window) * 100
  "pressure_pct_after": <float> | null,
  // Counts
  "entries_removed": <int>,
  "messages_before": <int>,
  "messages_after": <int>,
  "images_before": <int>,
  "images_after": <int>,
  // Summary content
  "summary_chars": <int>,
  "summary_preview": "<first 700 chars>",
  "summary_text": "<full text>",
  "summary_tokens": <int>,                  // estimate of summary's own size
  // Summarizer call (Gap 4)
  "summarizer_input_tokens": <int>,
  "summarizer_output_tokens": <int>,
  "summarizer_duration_ms": <int>,
  "summarizer_model": "<model id>" | null,
  "summarizer_cost_usd": <float> | null,
  // Lineage
  "resumed_from_previous": <bool>,           // iterative path
  "trigger": "<path label>",                 // "pre_request" | "overflow_cascade"
                                             //   | "agent_summarize_context" | "manual"
  "strategy": "llm_summary"
}
```

## 3 — Per-trigger event/persistence matrix

Read across each row to see what fires on each trigger:

| Signal | A: pre_request | B: overflow_cascade | C: agent | D: manual |
|---|---|---|---|---|
| `compaction_start` system_event → renderer | ✅ | ✅ | ✅ | ✅ |
| `compaction_complete` system_event → renderer | ✅ | ✅ | ✅ | ✅ |
| `compaction_skipped` (on failure) | ✅ | ✅ | ✅ | ✅ |
| `compaction_event` JSONL (cross-session telemetry) | ✅ | ✅ | ✅ | ✅ |
| Per-session `compactions.jsonl` (full summary) | ✅ | ✅ | ✅ | ✅ |
| Snapshot pair (`.md` + `.json` before/after) | ✅ | ✅ | ✅ | ✅ |
| `llm_call_metric` JSONL with `call_kind: "summarizer"` | ✅ | ✅ | ✅ | ✅ |
| `summarize_context_call` JSONL (decision corpus) | ❌ | ❌ | ✅ | ❌ |
| `usage_snapshot` event | (next call) | (next call) | (next call) | ✅ |
| Renderer inline `system` message-part | ✅ | ✅ | ✅ | ✅ |
| `_save_transcript()` immediate (Gap 6 fix) | ✅ (on complete) | ✅ (on complete) | ✅ (on complete) | ✅ (on complete) |

The asymmetric rows worth understanding:

- **`summarize_context_call`** only fires on agent-driven calls because it captures Dataset 1 (the trigger-decision corpus — scope, reason, pressure_pct_at_call, preserve_facts_missing, pinned_ordinals). The runtime + manual paths don't have an agent-driver to attribute the decision to.
- **`usage_snapshot`** only fires immediately on manual because the user is staring at a "compact now" button and expects the spend meter to refresh immediately. Runtime + agent paths refresh naturally on the next call's `usage` event.

## 4 — Detailed lifecycle of one LLM-summary compaction

### 4.1 Trigger detection

**Path A (pre_request):** `_ensure_context_room` runs at the top of every iteration. Calls `_ensure_media_room` first (image trimming), then computes `usage_fraction = used_tokens / effective_window`.
- If `ratio > CONTEXT_PRESSURE_THRESHOLD (0.25)`: calls `prune_old_tool_results` (cheap halving, no LLM). Emits `context_pruning` system_event. Updates `used_tokens`.
- If `ratio > CONTEXT_COMPACTION_THRESHOLD (0.80)`: enters compaction.

**Path B (overflow_cascade):** Caught in `_handle_provider_error` when `is_context_overflow_error(str(error))` is true or the error is a `ContextOverflowError` instance. Forwards to `_handle_overflow_cascade` which is now async (changed 2026-05-14).
- Bails immediately if `ctx.compaction_attempts >= max_compaction_attempts`.

**Path C (agent):** Agent invokes `summarize_context` with one of the LLM-summary scopes. `SummarizeContextTool.execute`:
- Validates scope + level.
- Applies `pin_entries` ordinals to the transcript BEFORE the compactor runs (so `_honor_pins` sees them).
- Reads pressure_pct from the runner (for telemetry).

**Path D (manual):** `_BridgeSession.force_compact()` called via the `/compact` slash command IPC.
- Bails if `session is None` or `pending_task` is in progress.
- Captures snapshots BEFORE compaction.

### 4.2 `compaction_start` emit

All four paths emit a `compaction_start` system_event with details including:
- `tokens_before` (caller perspective)
- `transcript_tokens_before`
- `effective_window`, `pressure_pct_before`
- `trigger` (path label)
- For path C: `scope`, `reason`, `pressure_pct_at_call`
- For path D: `before_snapshot_path`, `before_snapshot_json_path`, `before_preview`

On the bridge side, `_BridgeSession._on_system_event` mirrors this to:
- The cross-session `~/.freyja/telemetry/compaction.jsonl`.
- The per-session `~/.freyja/projects/<sid>/compactions.jsonl`.
- A snapshot pair: `_write_compaction_snapshot(phase="before", ...)` writes a `.md` (human-readable transcript dump) + `.json` (machine-readable transcript) under `~/.freyja/sessions/compactions/<sid>-<ts>-before.{md,json}`.

The manual path also calls `_mirror_compaction_event` directly to make sure these mirrors land — it bypasses the runner so the `on_system_event` callback doesn't fire on its behalf.

### 4.3 `SummaryCompaction.compact()` core

In order:
1. `messages = transcript.get_messages()` — walks entries, injects a synthetic `[Previous conversation summary]` system message right after any prior compaction entry.
2. `tokens_before = transcript.estimate_tokens()` — canonical transcript size measure.
3. Gate: if `len(messages) <= keep_recent (10)`, bail with "Nothing to summarize". If `< min_messages` AND `summarizable_tokens < MIN_TOKENS_TO_SUMMARIZE`, bail. The second gate lets short-but-heavy sessions (huge image, few replies) compact when the simple count gate wouldn't.
4. `split_point = len(messages) - keep_recent`.
5. `_find_safe_split` — walks split_point back past any orphan tool_use / tool_result groups so `to_keep` doesn't start with a tool_result missing its tool_use.
6. `_honor_pins(transcript, split_point)` — accounts for the sys_summary inject and walks `transcript.entries` (which has different indexing than `messages`) to find any `compaction_excluded=True` entries that land in `to_summarize`. Pulls split_point back to before the earliest pin so pinned entries stay in the kept tail.
7. `_ensure_latest_user_message_kept` — anchors the newest user message in `to_keep` (prevents the summarizer from swallowing the active task into a section the post-compaction system prompt tells the model NOT to act on).
8. `previous_summary = _find_previous_summary(transcript)` — newest-first walk for a prior `is_compaction=True` entry. Sets `resumed_from_previous = True` if found.
9. If iterative path, strips the sys_summary inject from `to_summarize` so the prior summary isn't fed in twice (it's already in the `SUMMARY_UPDATE_PROMPT`).
10. `conversation_text = _format_conversation(to_summarize)`.
11. `_generate_summary(conversation_text, provider, previous_summary=...)`: a. `redact_sensitive_text` on the input. b. Build either `SUMMARY_PROMPT` or `SUMMARY_UPDATE_PROMPT`. c. **Call provider.complete(...)** — this is the summarizer LLM call. Time it. d. Capture usage (`input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`), model id, duration_ms, estimated cost via `compute_cost`. e. Stash all of the above into `stats_out`. f. Fire `on_summarizer_call(payload)` with the stats + `call_kind: "summarizer"` so the runner's `on_llm_call` hook can mirror it to the dashboard as compaction overhead (Gap 4). g. Parse `<summary>...</summary>` out of the response. h. `redact_sensitive_text` on the output.
12. `first_kept_id = transcript.entries[split_point].id`.
13. `transcript.append_compaction(summary, first_kept_id, tokens_before)`:
- Walks entries to find `first_kept_id`.
- **Deletes** every entry before it from `_entries` (in-memory).
- Prepends a single `is_compaction=True` entry with `compaction_summary=<text>`.
- Recomputes `tokens_after` if not provided.
14. `tokens_after = transcript.estimate_tokens()`.
15. Compute `summary_tokens = len(summary) // 4`.
16. Return `CompactionResult` populated with all canonical fields.

### 4.4 `compaction_complete` emit

All four paths emit a `compaction_complete` system_event with details from `_compaction_event_details(result, trigger=..., tokens_before=..., tokens_after=...)` — the canonical builder. The runtime paths use the runner's `_compaction_event_details`; the manual path assembles its own equivalent (which now matches).

On the bridge side, `_on_system_event` mirror runs:
- `_mirror_compaction_event("compaction_complete", details)`:
- Writes a `compaction_event` row to `~/.freyja/telemetry/compaction.jsonl` (cross-session).
- Writes a row to `~/.freyja/projects/<sid>/compactions.jsonl` (per-session, FULL summary text).
- `_write_compaction_snapshot(phase="after", ...)` writes the after-snapshot pair.
- `_save_transcript()` — persists the just-compacted transcript to `~/.freyja/sessions/<sid>/transcript.json` IMMEDIATELY (Gap 6 fix). Without this, a crash between compaction and turn-end would leave the disk transcript in the pre-compaction state, disagreeing with the just-logged JSONL row.

### 4.5 What the model sees on the next call

After `compact()` returns, the engine transcript is in this shape:

```
entries = [
  TranscriptEntry(is_compaction=True, compaction_summary="<full text>"),
  TranscriptEntry(message=<msg_at_first_kept_id>),
  TranscriptEntry(message=<next_msg>),
  ...
  TranscriptEntry(message=<latest_msg>)
]
```

When `transcript.get_messages()` runs on the next iteration:
- Compaction entries are processed by stashing `last_compaction_summary` for the next real message to consume.
- The next real entry triggers a synthetic `Message(role="system", content="[Previous conversation summary]\n<text>")` to be prepended.

So the messages list sent to the provider is:

```
[
  Message(role="system", content="[Previous conversation summary]\n<...>"),
  Message(role=msg_at_first_kept.role, content=...),
  ...
  Message(role=latest_msg.role, content=...)
]
```

The Anthropic provider rewrites the system-role inject as user-role `[System context]: [Previous conversation summary] ...` because Anthropic's API doesn't support system-role inside `messages`. It also stamps `cache_control: ephemeral` on this message (the 3rd cache breakpoint) so the entire prefix through system + tools + summary is cached.

If Channel 2 is active (ratio ≥ 40%), the `_augment_messages_with_pressure_note` adds a `<system-reminder>` block to the tail of the last user message — this is after every cache breakpoint, so cache validity is preserved.

### 4.6 What the renderer sees

The renderer's view diverges from the engine's:
- The renderer NEVER truncates its own message list. Pre-compaction messages stay visible in the conversation scroll.
- The renderer inserts a synthetic `system` MessagePart into the conversation showing the compaction marker ("Pre-request compaction complete: X → Y tokens" or "Manual compaction complete: ..." etc.).
- Clicking the system marker opens the structured detail panel (in `ActivityView.tsx`) which surfaces:
- SCOPE / MECHANISM / TRIGGER chips
- Tokens before → after with delta + percent
- Reason (agent rationale)
- Full summary text (scrollable, copyable)

## 5 — Persistence surfaces

After a successful LLM-summary compaction, these on-disk artifacts exist:

| Path | Source | Purpose |
|---|---|---|
| `~/.freyja/sessions/<sid>/transcript.json` | `_save_transcript` | Engine view (entries with the compaction entry + kept tail). Source of truth for session resume. |
| `~/.freyja/projects/<sid>/raw_messages.jsonl` | `Session.create(on_message_appended=...)` | Append-only log of every Message ever appended. Survives compaction. Source for export bundle `raw_transcript[]`. |
| `~/.freyja/projects/<sid>/compactions.jsonl` | `_BridgeSession._append_compaction_log` via `_mirror_compaction_event` | One row per compaction event with FULL summary text + canonical fields. Source for export bundle `compactions[]`. |
| `~/.freyja/sessions/compactions/<sid>-<ts>-{before,after}.{md,json}` | `_BridgeSession._write_compaction_snapshot` | Transcript snapshot pair around each compaction. Source of truth for "what did the model see before vs after?" |
| `~/.freyja/telemetry/compaction.jsonl` | `bridge/compaction_telemetry.append_telemetry` via `_mirror_compaction_event` | Cross-session log used by the metrics dashboard. Contains `compaction_event` + `llm_call_metric` + `pressure_signal` + `tool_call_metric` + `summarize_context_call` + `profile_invocation` + `profile_completion` rows. |
| Renderer session payload (debounced via `sessionSave` IPC) | renderer state | UI view: visible message history + system parts + tool_calls + frames. |

The export bundle (`<sid>.bundle.json`) combines `raw_messages.jsonl`
+ `transcript.json` + `compactions.jsonl` into three views:
`raw_transcript[]` + `live_transcript[]` + `compactions[]`.

## 6 — Metric fields by surface

### `compaction_event` row in cross-session JSONL

Fields (every successful compaction has these populated):

```
type                       "compaction_event"
session_id                 <sid>
agent_type                 <profile name or null for root>
parent_session_id          <parent sid or null>
subtype                    "compaction_start" | "compaction_complete"
                           | "compaction_skipped" | "context_pruning"
                           | "media_pruning" | "thrash_skip"
model                      <model id>
tokens_before              <int>
tokens_after               <int>
effective_window           <int>
pressure_pct_before        <float>
pressure_pct_after         <float>
summary_tokens             <int>
mechanism                  "summary" | "summary_iterative" | "tool_halve"
                           | "image_prune" | "thrash_skip"
                           | "tool_results_only" | "exploration_only"
trigger                    "pre_request" | "overflow_cascade"
                           | "agent_summarize_context" | "manual"
                           | "thrash_detector"
scope                      <agent's scope choice, agent path only>
reason                     <agent's free-text rationale, agent path only>
resumed_from_previous      <bool>
summarizer_input_tokens    <int>
summarizer_output_tokens   <int>
summarizer_duration_ms     <int>
summarizer_cost_usd        <float | null>
summary_excerpt            <first 240 chars>
summary_text               <full text, or null for non-summary subtypes>
ts                         <unix epoch float>
```

### `llm_call_metric` row — summarizer flavor

When `call_kind == "summarizer"`, the row represents a summarizer LLM call. Fields:

```
type                       "llm_call_metric"
session_id                 <sid>
turn_id                    <turn id at time of call>
agent_type                 <profile or null>
parent_session_id          <parent or null>
model                      <model id>
input_tokens               <int>
output_tokens              <int>
cache_read_tokens          <int>
cache_write_tokens         <int>
cost_usd                   <float | null>
duration_ms                <int>
call_kind                  "summarizer"
iterative_summarizer       <bool>     # true iff SUMMARY_UPDATE_PROMPT
ts                         <unix epoch float>
```

### `summarize_context_call` row — agent decision corpus

Only the agent path produces these. The dashboard's "Agent compaction decisions" panel reads them. Fields:

```
type                       "summarize_context_call"
session_id                 <sid>
turn_id                    <turn id>
agent_type                 <profile or null>
parent_session_id          <parent or null>
scope                      "since_last_compaction" | "all" | "early"
                           | "tool_results_only" | "exploration_only"
level_requested            "auto" | "episode" | "chapter"
level_used                 "episode" | "chapter"
preserve_facts_count       <int>
preserve_facts_missing     [<string>, ...]      # facts not in produced summary
pinned_ordinals            [<int>, ...]         # ordinals pinned by this call
reason                     <agent rationale>
pressure_pct_at_call       <float>
tokens_before              <int>
tokens_after               <int>
resumed_from_previous      <bool>
entries_removed            <int>
success                    <bool>
error                      <string | null>
elapsed_ms                 <int>
model                      <model id>
ts                         <unix epoch float>
```

## 7 — Aggregator → dashboard surface mapping

Where each metric ends up in the metrics dashboard:

| Dashboard element | Source rows | Aggregation |
|---|---|---|
| Total spend stat tile | `llm_call_metric` (all `call_kind`s) | sum of `cost_usd` |
| Compactions stat tile | `compaction_event` rows with `subtype == "compaction_complete"` | count |
| Tokens-saved stat tile | `compaction_event` complete rows | sum of `(tokens_before - tokens_after)` |
| Cache reuse stat tile | `llm_call_metric` rows | `(cache_read + cache_write) / (input + cache_read + cache_write)` aggregated |
| Cooperation effectiveness tile | `compaction_event` complete rows | `count(trigger==agent_summarize_context) / total` |
| Trigger source RankedBars | `compaction_event` rows | groupby `trigger` |
| Savings trend SVG | `compaction_event` rows with `tokens_before > 0` | `(tokens_before - tokens_after) / tokens_before` over time |
| Per-session compaction count | `compaction_event` rows | groupby `session_id` |
| Per-session compaction log in drawer | `compaction_event` rows for that session | sorted by ts |
| Click row → full summary panel | `compaction_event.summary_text` | direct render |
| Agent compaction decisions panel | `summarize_context_call` rows | newest-first list + scope mix |
| Summarizer overhead (TODO surface) | `llm_call_metric` rows with `call_kind == "summarizer"` | sum of `cost_usd` |

## 8 — Channel signaling (cooperative protocol)

The cooperative protocol's three channels surface pressure cues to the agent so it has a chance to drive compaction itself before the runtime fallback at 80%. Briefly:

- **Channel 1:** Per-tool-result tag `[ctx: NN% · advisory]` appended at `_build_pressure_tag` in `bridge/freyja_bridge.py`. Fires at `ratio ≥ 0.25`.
- **Channel 2:** `<system-reminder>` block tail-appended to the last user message in the request via `_augment_messages_with_pressure_note` in `engine/runner.py`. Fires at `ratio ≥ 0.40`. Lives in the user message (not system prompt) to preserve the system-block cache marker.
- **Channel 3:** `[!CTX PRESSURE: window crossed X-Y%]` prepended to the next tool result body when pressure escalates into ≥ strong band during a turn. Set by `mark_channel3_crossing`, consumed by the bridge's tool-result wrapper via `consume_channel3_advisory`. One-shot.

These do NOT trigger compaction directly. They give the agent visibility so it can call `summarize_context` itself. The actual trigger thresholds are:

- `CONTEXT_PRESSURE_THRESHOLD` (0.25) — runtime cheap pruning.
- `CONTEXT_COMPACTION_THRESHOLD` (0.80) — runtime fallback LLM compaction. Was 0.40 in Phase 1, restored to 0.80 in Phase 2 so the agent has a real 40–80% cooperation window.

## 9 — Known caveats and design notes

- **Multiple compactions in one turn are possible.** Path B can fire mid-turn after an overflow error, and Path A can fire on the next iteration if pressure stays high. Each produces its own `compaction_event` row.
- **The transcript mutation is destructive in memory.** Pre-split entries are deleted from `session.transcript.entries`. They only survive in `raw_messages.jsonl`, the per-session `compactions.jsonl` summary text, the renderer's separate message slice, and the snapshot pair files.
- **`append_compaction` accumulates compactions in the entries list.** After two compactions the entries list is `[compaction_2, kept_tail_2, ...]` — `compaction_1` was deleted when `compaction_2`'s `first_kept_id` walk truncated entries before it. Only the most-recent compaction entry survives in the engine view. Old summaries are in `compactions.jsonl` only.
- **The iterative path strips the sys_summary inject from `to_summarize`.** Without this the prior summary would be fed to the summarizer twice — once via the `PREVIOUS SUMMARY:` section of `SUMMARY_UPDATE_PROMPT`, once embedded in the conversation transcript.
- **`_honor_pins` accounts for the inject offset.** Pre-2026-05-12 it walked entries while counting `message_idx` only for non-None message entries — but `get_messages()` adds a sys_summary pseudo-message after every compaction entry. The walker now tracks `inject_pending` and increments `message_idx` for the inject when the next real message follows.
- **Anti-thrash protection.** `_attempt_compaction` checks `ctx.ineffective_compaction_count >= 2` (last two compactions each saved < 10%) and bails with a `thrash_skip` JSONL row + CompactionResult error. The dashboard counts these as a separate subtype.
- **Sub-agent isolation.** When a subagent's child registry is built in `_run_child`, the `summarize_context` and `session_memory` tool instances are REPLACED with subagent-scoped ones whose closures point at the child session/runner. Otherwise the subagent would compact the PARENT's transcript.
- **`force_compact` does NOT route through the runner.** It calls `compactor.compact()` directly via `asyncio.to_thread` with its own `on_summarizer_call` closure that forwards to the runner's `on_llm_call` (when a runner exists). The bridge emits its own start/complete system events and calls `_mirror_compaction_event` directly because the runner's `on_system_event` callback doesn't fire.
- **Sync runner is legacy.** The sync `AgentRunner` in `engine/runner.py` (line ~235) is the CLI path. The bridge always uses `AsyncAgentRunner`. The async path is the authoritative one for everything in this doc.

## 10 — The `session_memory` tool

`session_memory` is the agent-facing escape hatch that lets it carry notes across compaction boundaries. It's a sibling to `summarize_context` — together they're how the agent participates in its own memory management.

### 10.1 What it is

A markdown file per session at ``~/.freyja/projects/<safe_sid>/memory.md``, exposed to the agent as a tool with four actions:

| Action | Behavior |
|---|---|
| `read` | Return current contents (empty string if file doesn't exist). |
| `write` | REPLACE the file with `content`. |
| `append` | Add `content` to the file under a `## <timestamp>` header. Preferred for incremental notes. |
| `clear` | Delete the file. |

Source: `bridge/tools/session_memory_tool.py`. Constructor takes just `session_id`; the file path is derived via `bridge/project_paths.py:project_output_dir(session_id) / "memory.md"`.

Defensive cap: `_MAX_BYTES = 64 * 1024`. If a write or append would exceed it, the file is truncated **from the head** — oldest entries get dropped so the tail (most recent notes) survives. This is the right shape for a scratchpad where recency matters more than completeness.

### 10.2 Expected workflow

The system prompt's `_CONTEXT_DISCIPLINE_BLOCK` tells the agent:

> Before compacting:
> - Stash anything you'll need to remember into `session_memory` —
>   the file lives outside the transcript and survives every future
>   compaction.

And the tool's own description doubles down:

> Anything you write here SURVIVES compaction by construction — the
> file is never summarized. Use it to stash facts, plans, or notes
> that you need to keep retrievable even after the conversation
> history is condensed.

The intended workflow is:

1. **During a task**, agent appends notes: `session_memory(action="append", content="<finding / decision / file path>")`
2. **Before calling `summarize_context`**, agent appends a summary of what just got accomplished and what's still pending.
3. **After a compaction lands** (next iteration the agent sees the `[Previous conversation summary]` injected message), the agent *should* read `session_memory` to recover anything the summary may have paraphrased away.
4. **For load-bearing literal strings** (credentials, exact file paths, error messages, API tokens), the agent can either:
- Write them to `session_memory` so they survive verbatim, OR
- Pass them as `preserve_facts` to `summarize_context` (the compactor auto-appends them to the summary if missing).

Both options are honest about the safety story; `session_memory` is the lower-friction choice for ongoing work.

### 10.3 What actually happens in code

**Registration.** `bridge/tools/registry.py:152-155` — when `subagent_parent_session_id` is non-empty (which it always is for real bridge sessions), one `SessionMemoryTool(session_id=_mem_session)` gets registered. `_mem_session = subagent_parent_session_id or ""` — the caller passes the current session id here for both root sessions and the registry-build-time call for subagents.

**Subagent isolation.** `bridge/tools/sub_agent_tool.py:1319-1324` — the child registry inherits the parent's tool instances by default, but `_run_child` overwrites two specifically:

```python
if "summarize_context" in child_registry._tools:
    child_registry._tools["summarize_context"] = sub_summarize_tool
if "session_memory" in child_registry._tools:
    child_registry._tools["session_memory"] = SessionMemoryTool(
        session_id=record.id,
    )
```

Without this, a subagent calling `session_memory` would write to the PARENT's `memory.md` file. With it, every subagent has its own per-subagent memory file.

**Concurrency.** `execute()` calls `run_in_executor(None, ...)` — so two parallel tool calls (a possible state during the runner's `_handle_tool_calls_parallel`) race in the thread pool. The `append` action is read-then-write which is the most racy. The `_MAX_BYTES` truncation is also done at write time, so a race could land with one side's content silently overwritten. In practice the agent rarely fires parallel `session_memory` calls in the same turn, but the race exists.

**Persistence.** The file is written eagerly on every successful write/append — no debouncing, no batching. Reading is also direct filesystem I/O. Survives bridge restart, app restart, and every compaction (because it lives outside the transcript).

**Restore.** On session resume, the rebuilt `SessionMemoryTool` points at the same `~/.freyja/projects/<sid>/memory.md` path (since session id is stable). The agent has to remember to read it — there's no auto-injection on restore.

### 10.4 Telemetry / observability

Every `session_memory` invocation produces a `tool_call_metric` JSONL row via the runner's `on_tool_metric` hook in `_execute_single_tool`:

```jsonc
{
  "type": "tool_call_metric",
  "session_id": "<sid>",
  "turn_id": "<turn>",
  "agent_type": "<profile or null>",
  "tool_name": "session_memory",
  "duration_ms": <int>,
  "ok": <bool>,
  "result_bytes": <int>,    // size of the JSON response, not memory file
  "ts": <unix epoch>
}
```

The dashboard's per-profile top-tools histogram counts these. But **the row does NOT carry the action** — read / write / append / clear all lump together. The histogram can't tell you "this profile reads memory 80% of the time" vs "writes 80% of the time". For a tool with mode-specific semantics that's information loss.

The tool also returns a structured JSON body (`{action, path, bytes, content, ...}`) in its `ToolResult` content. The renderer treats this as a normal tool_result string — so the agent sees the body in the next iteration and the user sees it in the conversation timeline.

### 10.5 Surfaces it touches

| Surface | Behavior |
|---|---|
| Conversation timeline | Renders `session_memory` calls + results inline like any other tool. The full file content shows up in the `read` action's result. |
| Activity panel | Same. No special chip / category. |
| Metrics dashboard | `tool_call_metric` rows aggregated into the top-tools histogram. No action breakdown. No file-size tracking. |
| Session export bundle (`<sid>.bundle.json`) | **memory.md content is NOT included.** The bundle has raw_transcript + live_transcript + compactions; the memory file is a separate artifact under projects/. |
| `~/.freyja/sessions/<sid>/transcript.json` | Doesn't contain memory.md content. The file is outside the engine transcript by design. |
| Session deletion (`delete_session` IPC) | Removes only `transcript.json`. The entire `~/.freyja/projects/<sid>/` directory — including memory.md, raw_messages.jsonl, compactions.jsonl, artifacts — is left orphaned on disk. |

### 10.6 Gaps and limitations

This section is the audit. Each item is a real shortcoming worth fixing in a future pass.

**G1 — Tool calls don't carry the action.** `tool_call_metric` rows for `session_memory` lump read/write/append/clear together. The dashboard can't break down memory use by mode. Fix: have the runner's `on_tool_metric` hook receive the parsed `arguments` preview (it already does — `arguments_preview`) AND expose it on the row. Or have the tool emit its own `session_memory_event` telemetry row when an action runs.

**G2 — No auto-inject after compaction or restore.** The agent must explicitly call `read` to recover what it wrote. After a compaction, the `[Previous conversation summary]` inject doesn't mention session_memory. After a session restore, nothing in the fresh system prompt prompts the agent to check. We rely on the system prompt's "Before compacting:" guidance and the agent's own memory of having written. The right fix is probably one of:

- Append a one-liner to the `[Previous conversation summary]` inject when memory.md exists and is non-empty: *"There are agent notes in session_memory (N bytes) — call session_memory(action='read') if relevant."*
- OR include the current memory.md size in the `[FREYJA CONTEXT PRESSURE]` `<system-reminder>` so the agent has continuous awareness.
- OR add a short header to the system prompt at session restore time noting that memory.md exists from a prior session.

**G3 — No content surface in the UI.** The user can't see what's in memory.md without watching the agent's tool calls scroll by. A dedicated panel in the session detail drawer (alongside the compaction log) would be useful for debugging "why does the agent think X" questions. Similarly, the activity panel could show `session_memory` calls in their own category instead of lumping them with generic tool calls.

**G4 — Not in the export bundle.** `<sid>.bundle.json` includes `raw_transcript[]`, `live_transcript[]`, and `compactions[]`, but not memory.md content. For a session you're archiving or sharing for review, the agent's working notes are a meaningful artifact — they should land in the bundle. Trivial fix: add a `session_memory: { exists: bool, bytes: int, content: string }` field in `loadSessionExportBundle` reading from `~/.freyja/projects/<sid>/memory.md`.

**G5 — Concurrency race on parallel writes.** Two parallel tool calls can interleave on the read-modify-write of `append`. A `threading.Lock` keyed on the file path would close the race definitively. Practical impact is low (agents rarely parallelize memory writes) but worth noting.

**G6 — Orphan files on session deletion.** `delete_session` unlinks only `transcript.json`. The `~/.freyja/projects/<sid>/` directory containing memory.md, raw_messages.jsonl, compactions.jsonl, and artifacts is orphaned. Over many deleted sessions this accumulates. Fix: have `delete_transcript` (or the bridge's `delete_session` handler) also `shutil.rmtree(project_output_dir(sid))` after the transcript file is removed. Should be guarded so it skips sessions that share a project_session_id (subagent / branch clones might).

**G7 — No write/read latency or size tracked over time.** We track per-call duration via tool_call_metric but never the memory.md file's size over the session lifetime. A growth trend would tell us "this session's agent is being verbose in its notes" — useful for budgeting.

**G8 — 64KB cap is shared across the session, not per-task.** A long session naturally accumulates notes; eventually old notes (from completed phases) get truncated from the head. The agent has no way to "checkpoint" or "rotate" memory when a phase finishes. Possible fix: a `rotate` action that moves the current file to `memory.<timestamp>.md` and starts fresh, with the agent able to `list_archives` and `read_archive`.

**G9 — Cross-session is impossible by design, but that sometimes hurts.** The user might want to continue a prior session's notes in a new session (e.g. branching for a related follow-up task). Today they'd have to manually copy the file. A `session_memory(action="import", from_session_id=...)` would help, but it's a UX feature gap, not a correctness gap.

### 10.7 Relationship to other tools

| Tool | What it's for | When to use over session_memory |
|---|---|---|
| `memory` (action-based) | Persistent cross-session user/project preferences | Save anything that should outlive THIS session |
| `record_user_preference` | Append-only shorthand for the above | Save a clearly-stated user preference |
| `task_board` / `kanban` | Multi-agent task ledger | Track work items, assign to subagents |
| `write_file` to project_output_dir | Long-form artifacts | When the content is a deliverable (report, code, doc) the user wants on disk |
| `summarize_context(preserve_facts=...)` | Force-include literal strings in compaction summary | When you trust the summarizer to keep your facts but want belt-and-suspenders |
| `pin_entries` on summarize_context | Keep specific messages verbatim across compactions | When a tool_result is the source of truth and you don't want to risk paraphrase |

### 10.8 In one line

`session_memory` is the agent's in-session scratchpad. It's a markdown file on disk, outside the transcript, capped at 64KB, with read/write/append/clear actions. It survives every compaction by construction, gets rebuilt per-subagent so subagents don't clobber parent state, but is not visible in the UI or export bundle, has no auto-injection on compaction or restore, and orphans its file on session deletion.

## 11 — Implementation history

See `COMPACTION-DECISION-DRAFT.md` Implementation log section for the full timeline. The major commits that shaped the current flow:

- Phase 1 (2026-04-30): telemetry instrumentation, Channel 1, cheap pruning at 25%.
- Metrics dashboard v2 (2026-05-07): squeeze drawer, sparklines, 6 stat tiles.
- Cooperative Phase 2 (2026-05-12): `summarize_context` tool, three channels, iterative summary path.
- Profiles view + raw transcript log + 3-view export (2026-05-12).
- Cache normalization + cooperation tile (2026-05-12).
- Channel 2 wire-level fix (2026-05-14): moved to last-user-message + `<system-reminder>` framing for cache friendliness.
- Compaction flow audit + 7-gap fix (2026-05-14): unified emit surface, summarizer tagging, snapshot universality, immediate persistence, canonical token measure, summary_tokens. This doc.
