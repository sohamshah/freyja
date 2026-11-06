# Compaction flow — end-to-end stocktaking

Last updated: 2026-11-06

This doc says what the code does today to keep a session's context inside the model's window. It covers how the runtime measures the context, the cheap pruning it does first, every path to an LLM summary, what each path writes, and the telemetry fields. `COMPACTION-DECISION-DRAFT.md` is the old design log. Where the two disagree, this doc follows the code.

Use it when:

- A session compacted and you do not know why.
- You add a compaction trigger and want it to write the same signals as the others.
- You build a dashboard surface and need to know which JSONL field to read.
- You onboard to the compaction subsystem.

Sections 1–9 cover compaction. Section 10 covers `session_memory`, the agent's notes file, which compaction never touches. Section 11 is the history.

## 1 — How the runtime measures the context

- The usable window is the model's context window minus `max_tokens_per_turn`, the output reserved for the reply. A provider rejects a request whose input plus `max_tokens` is larger than the window. Every threshold in this doc is a fraction of the usable window.
- The measured size is the last call's input, cache-read, cache-write and output tokens (`UsageAccumulator.effective_context_tokens` in `engine/usage.py`).
- The pre-request check reads `_current_context_tokens`: the larger of the measured size and the tokenizer's count. The tokenizer's count (`Session.estimate_tokens`) adds up the system prompt, the transcript and the tool definitions (estimated, times 2.5). On Claude it reads about two thirds of the measured size.
- The pressure readers read the measured size only: the Channel 1 tag, the Channel 2 note, the Channel 3 crossing check and the bridge's `pressure_signal` telemetry.
- A call's usage goes into the accumulator before the call is reported to `on_llm_call`. So each `pressure_signal` row shows the call that wrote it.
- A new turn keeps the last call's measure while the transcript still starts with what that call sent (`_continues_sent_prompt`), or with what a manual compaction or a restore left (`_continues_compacted`). After an edit or a delete the turn starts from zero, and the tokenizer's count stands until the first call reports. Between turns `context_outlook`, which the restore check and the Slack notice read, applies the same rule.
- After a compaction the measure is the tokenizer's count of the compacted request (`_mark_context_compacted`). A manual compaction calls `mark_context_compacted`, which also records the transcript it left, so the next turn keeps that measure. A restore does the same with the tokenizer's count of what it restored, so its first turn does not read 0%.
- After halving, the measure is lowered by the tokenizer's count of what halving removed (section 2).

Thresholds:

| Constant | Value | What happens past it |
|---|---|---|
| `CONTEXT_AWARENESS_THRESHOLD` | 0.25 | The Channel 1 tag starts. |
| `CONTEXT_PRESSURE_THRESHOLD` (`AgentConfig.compaction_threshold`) | 0.40 | On a cold cache, old tool results are halved and old appshot text is removed. |
| `CONTEXT_SOFT_SUGGEST_THRESHOLD` | 0.40 | The Channel 2 note starts. The tag suggests `summarize_context` at the next break. |
| `CONTEXT_STRONG_SUGGEST_THRESHOLD` | 0.55 | The tag and the note recommend `summarize_context` before more tool calls. A rise into this band during a turn raises the Channel 3 advisory. |
| `CONTEXT_COMPACTION_THRESHOLD` | 0.70 | The runtime compacts before its next request (path A). `CONTEXT_FALLBACK_THRESHOLD` has the same value. |

## 2 — Cheap pruning before a summary

`_ensure_context_room` in `engine/runner.py` runs before every provider call. In order:

1. `_ensure_media_room` drops old computer-use screenshots beyond the newest 4 (`KEEP_RECENT_COMPUTER_IMAGES`), 8 at a time (`SCREENSHOT_PRUNE_BATCH`). It also fits images to the provider's size limit. It emits `media_pruning`.
2. Halving, when the context is past 40% and the last call's prompt is not in the provider's cache (`_cached_prompt` returns None). On a warm cache a rewrite of history makes the provider write everything after it to the cache again, which costs more than halving saves. So on a warm cache only compaction relieves the pressure.
3. Compaction (path A), when the context is past 70%.

How halving works (`TranscriptManager.prune_old_tool_results` in `engine/session.py`):

- Every tool result except the newest 10 (`KEEP_RECENT_TOOL_RESULTS`) that is text of 2,000 characters or more (`MIN_TOOL_RESULT_CHARS_TO_HALVE`) is halved once. It keeps about the first 35% and the last 12.5%.
- Before it halves a result, it saves the whole text through the truncator (`ToolResultTruncator.write_full_content` in `engine/tools.py`) to `~/.freyja/truncated/<tool>_<time>.txt`. The marker in the halved text names that file and says to read the removed part from it with grep or sed.
- A result whose text could not be saved stays whole. A result that the marker would not make shorter stays whole.
- A result that already carries a halving marker, the old one or the new one, is not halved again.
- The text of old appshots goes too. The newest 2 keep their text, and bodies under 1,500 characters stay.
- It emits `context_pruning` with `pruned_count`, `tokens_before`, `tokens_after`, `effective_window` and both pressure percentages. The size after is the measured size less the tokenizer's count of what was removed. The 70% check that follows uses that size.

Halving rewrites only the live transcript. `raw_messages.jsonl` holds each result as it was appended, so exports still have the whole result.

Why these numbers (2026-11-06). Until then halving started at 25%, kept the newest 3 results and halved anything over 500 characters. Before fcb23f1 a turn's first check read the tokenizer's count, about 0.67 of the measured size, so halving effectively started near 37% measured; that commit made it 25% measured. Over 21 days of transcripts, halving cut 2,587 results, median 1,200 characters, and removed about 1.9M tokens. It cut about 11 results per pass, at a median pressure of 34-40%. Results of 2,000 characters or more were 54% of the cuts but 92% of the characters removed. The savings were at most tens of dollars over the three weeks. So halving now starts at 40% measured, back near where it effectively started, keeps the newest 10 results whole, and leaves results under 2,000 characters alone.

The sync `AgentRunner` (`_handle_context_pressure`) halves after each call past 40%, with no cache check, and compacts past 70%. The app does not use it.

## 3 — Paths to an LLM summary

| Path | Trigger label | Code | Fires when |
|---|---|---|---|
| **A** Pre-request | `pre_request` | `engine/runner.py:_ensure_context_room` | The context is past 70% before a call. |
| **B** Overflow | `overflow_cascade` | `engine/runner.py:_handle_overflow_cascade` | The provider rejected a request as too long. It first removes the oldest images when they hold the bytes (trigger `overflow_cascade_image_elision`), then summarizes, then moves down the fallback chain. Removing images runs no summary, so it reports `media_pruning` (strategy `cumulative_image_prune`), as the other image recoveries do. |
| **C** Agent | `agent_summarize_context` | `bridge/tools/summarize_context_tool.py` | The agent calls `summarize_context` with scope `since_last_compaction`, `all` or `early`. |
| **D** Manual | `manual` | `bridge/freyja_bridge.py:force_compact` | The user runs `/compact`. It is skipped while a turn runs. |
| **E** Restore | `restore` | `bridge/freyja_bridge.py:try_restore_transcript` | A transcript is loaded from disk and the runner's measure (`context_outlook`) is past 70% of the usable window (reason `pressure`). Or a Slack session was idle 6 hours or more and has more than 50 messages (reason `slack_idle_revisit`). |

The agent's cheap scopes, `tool_results_only` and `exploration_only`, make no model call. They replace each matching tool result with a one-line preview and end with `context_pruning`. Like halving, they first save each result whole through the truncator to `~/.freyja/truncated`, and the preview names that file and says to read it with grep or sed. A result that could not be saved, that the preview would not make shorter, or that was condensed before stays as it is. The file holds only a result's text: when the result also held images (or documents, videos), they leave the context with it, and the preview says how many and that the raw message log keeps them.

Every path runs `SummaryCompaction.compact()` in a worker thread through `run_off_loop` (`engine/runner.py`), so the event loop keeps serving while the summary call blocks. A stop waits for the worker, because the worker rewrites the transcript when the summary returns.

Between turns the runner keeps the cached prompt alive: it re-reads it 60 s before it expires (`CACHE_KEEPALIVE_LEAD_S`), at most twice (`CACHE_KEEPALIVE_PINGS`). A turn start, a reset and a manual compaction stop this keep-alive. During a manual compaction a ping would read the transcript while the worker rewrites it.

## 4 — Inside `SummaryCompaction.compact()`

1. It reads the messages and the tokenizer's count of the transcript (`tokens_before`).
2. It stops when there are 10 messages or fewer (`KEEP_RECENT_MESSAGES`, the tail it keeps word for word). It also stops when there are fewer than 20 messages (`MIN_MESSAGES_TO_COMPACT`), the older part holds under 2,000 tokens (`MIN_TOKENS_TO_SUMMARIZE`, at 4 characters per token) and the older part holds under 512 KiB of images.
3. It picks the split. `_find_safe_split` keeps each tool call with its result. `_honor_pins` keeps pinned entries in the kept tail. `_ensure_latest_user_message_kept` keeps the newest user message.
4. If the transcript already has a summary, the new summary extends it (`resumed_from_previous`).
5. It asks the runner for the last call's prompt (`prompt_source`, bound to `AsyncAgentRunner.cached_prompt_prefix`). The runner returns it while the provider still caches it and the summary request fits: the current size plus 4,096 tokens (`SUMMARY_REQUEST_HEADROOM_TOKENS`) inside the usable window. After an overflow there is none.
6. Call A writes the summary.
   - With the cached prompt, it sends that prompt unchanged, with the instruction (`CONTINUATION_SUMMARY_PROMPT`) as a new user turn after it. Tools are off (`tool_choice` none) and the effort is at most medium. The provider reads the prompt from its cache.
   - Without it, it sends a digest of the older messages (`_format_conversation`) with `SUMMARY_PROMPT`, or `SUMMARY_UPDATE_PROMPT` when it extends a summary. A message over 2,000 characters keeps its first 1,200 and last 600. A digest over 400,000 characters keeps its start and its end.
   - When the caller passes the action ledger's confirmed actions (`ground_truth`), the instruction lists them.
7. Call B extracts working memory (a summary, the actions completed, the entities) at the same time, when the caller passes a sink (`on_working_memory_upserts`). It is best effort and has 90 s (`WORKING_MEMORY_CALL_TIMEOUT_S`).
8. Call A has `summary_call_timeout_s(tokens)`: 180 s plus 1 s per 1,000 tokens, at most 600 s. If it fails with an overflow error, a plain fallback summary is used. Any other error fails the compaction.
9. `transcript.append_compaction` deletes the entries before the split from the live transcript and puts one compaction entry in front of the kept tail.
10. It returns a `CompactionResult` (section 5).

Each summary and working-memory call goes to `on_summarizer_call`, with `call_kind` `summarizer` or `working_memory_extraction`. Every path forwards it to the session's `on_llm_call`, so it lands in `llm_call_metric` and in the session's spend. A report from a worker thread moves to the event loop first: the parent session's since 795a5ad, a sub-agent's since 2026-11-06. A sub-agent's own `summarize_context` forwards its calls to `_on_sub_llm_call` since 2026-11-06; before, they were not costed.

## 5 — Canonical fields

### `CompactionResult`

```python
success: bool
summary: str | None
tokens_before: int                  # transcript.estimate_tokens() before append_compaction
tokens_after: int                   # transcript.estimate_tokens() after it
error: str | None
entries_removed: int                # entries before the split, now deleted
messages_before: int                # entry count before
messages_after: int                 # entry count after
images_before: int
images_after: int
resumed_from_previous: bool         # the summary extends the previous one
summary_tokens: int                 # len(summary) // 4
summarizer_input_tokens: int
summarizer_output_tokens: int
summarizer_duration_ms: int
summarizer_model: str | None
summarizer_cost_usd: float | None   # None when the model has no price
summarizer_cache_read_tokens: int   # what the summary call read from the cache
summary_continued_prompt: bool      # Call A continued the cached prompt
```

### The details dict (`AsyncAgentRunner._compaction_event_details`)

Paths A and B build their events with it. Their `tokens_before` and `tokens_after` are both the tokenizer's count of the request, taken just before and just after the summary, so the saving is like for like. The reading that set the compaction off (`_current_context_tokens`, after halving) is `trigger_tokens`, with `trigger_pressure_pct`: it says why the compaction ran. Before 2026-11-06 `tokens_before` was that reading, set against the tokenizer's count after, so on Claude the gap between the two measures showed as saved (90.2% recorded where the next call's measured size gives 88.1%). Path C builds its own in the tool. Its `tokens_before` and `tokens_after` are the tokenizer's count of the request too: the system prompt, the transcript and the tools. Before 2026-11-06 they counted the transcript only, which left out what every request carries and no compaction removes, so the share saved read too high (one Opus 5.5 row read 86.8% where the measured sizes of the calls just before and after it give 79.7%). Its start and outcome events carry `effective_window` (the bridge's `_effective_window`, the window less the reply's reserve) with the pressure before and after; a sub-agent's tool reads its runner's window the same way (`_sub_effective_window` in `bridge/tools/sub_agent_tool.py`), and before 2026-11-06 its rows had none. Path D builds an equivalent dict in `force_compact`, with extra fields: `request_tokens_before/after` (the tokenizer's count of the request), `last_provider_context_tokens` (the measured size), `transcript_tokens_before/after` and the snapshot paths. Its `tokens_before` and `tokens_after` are both the tokenizer's count of the request: no call has measured the compacted transcript, and the measured size set against the count after it would show the gap between the two measures as saved. Path E builds a short one: `trigger`, `reason`, `tokens_before`, `tokens_after` (the tokenizer's count of the request), their `context_tokens_*` aliases, `effective_window` (the outlook's usable window) with both pressure percentages, `entries_removed`, the summary text and the summarizer fields. Before 2026-11-06 it had no aliases, window or pressure, so its card in the chat showed no size change and its rows had no pressure.

```jsonc
{
  "tokens_before": <int>,              // the caller's measure, else the transcript's
  "tokens_after": <int>,               // the same measure as tokens_before
  "trigger_tokens": <int> | null,      // the reading that set the compaction off (A, B)
  "trigger_pressure_pct": <float> | null,
  "transcript_tokens_before": <int>,   // transcript.estimate_tokens()
  "transcript_tokens_after": <int>,
  "context_tokens_before": <int>,      // alias the renderer's inline marker reads
  "context_tokens_after": <int>,
  "effective_window": <int> | null,    // context_window - max_tokens_per_turn
  "pressure_pct_before": <float> | null,
  "pressure_pct_after": <float> | null,
  "entries_removed": <int>,
  "messages_before": <int>,
  "messages_after": <int>,
  "images_before": <int>,
  "images_after": <int>,
  "summary_chars": <int>,
  "summary_preview": "<first 700 chars>",
  "summary_text": "<full text>",
  "summary_tokens": <int>,
  "summarizer_input_tokens": <int>,
  "summarizer_output_tokens": <int>,
  "summarizer_duration_ms": <int>,
  "summarizer_model": "<model id>" | null,
  "summarizer_cost_usd": <float> | null,
  "summarizer_cache_read_tokens": <int>,
  "summary_continued_prompt": <bool>,
  "resumed_from_previous": <bool>,
  "trigger": "pre_request" | "overflow_cascade",
  "strategy": "llm_summary"
}
```

## 6 — What each path writes

| Signal | A | B | C | D | E |
|---|---|---|---|---|---|
| `compaction_start` event to the renderer | yes | yes | yes | yes | no (the gateway posts its own notice) |
| `compaction_complete` event to the renderer | yes | yes | yes | yes | yes |
| Event on failure | `compaction_skipped` | `compaction_skipped` | `compaction_skipped` | `compaction_skipped` | `compaction_failed` |
| `compaction_event` rows in telemetry | yes | yes | yes | yes | yes |
| `compactions.jsonl` row with the full summary | yes | yes | yes | yes | yes |
| Snapshot pair (`.md` and `.json`, before and after) | yes | yes | yes | yes | yes |
| `llm_call_metric` rows for the summary calls | yes | yes | yes | yes | yes |
| `summarize_context_call` row | no | no | yes | no | no |
| `usage_snapshot` event at once | no | no | no | yes | no |
| Transcript saved at once | yes | yes | yes | yes | yes |

Paths A and B reach the telemetry through the runner's `on_system_event`: `_BridgeSession._on_system_event` calls `_mirror_compaction_event`, writes the snapshot pair and saves the transcript on `compaction_complete`. Path C reaches it through `_emit_summarize_event`, which writes the snapshot pair and saves the transcript the same way. Paths D and E call `_mirror_compaction_event` themselves, and write the pair and save the transcript themselves.

`_mirror_compaction_event` also mirrors `context_pruning` and `media_pruning`, so halving and image pruning have telemetry rows.

## 7 — Where things land

| Path | Written by | What it holds |
|---|---|---|
| `~/.freyja/sessions/<sid>.transcript.json` | `_save_transcript` | The live transcript: the compaction entry, the kept tail, halved results. A resume reads it. |
| `~/.freyja/projects/<sid>/raw_messages.jsonl` | `_append_raw_message_log`, on every append | Every message as it was appended. Compaction and halving never touch it. A restored session keeps appending to it; before 2026-11-06 a restore stopped the log, so older logs end at the session's first restart. A fork that copies the project dir copies this log and its recovered rows, cut at the anchor (`clone_project_dir`). |
| `~/.freyja/sessions/<sub_id>.raw_messages.jsonl` | `append_subagent_raw_message` (`bridge/transcript_persistence.py`), on every append | The same log for a sub-agent, beside its other sidecars. Sub-agents run before 2026-11-06 have none. A branch that copies the sub-agent copies this log, its compaction log and its recovered rows, cut at the anchor (`clone_session_sidecars`). A session whose project dir is another session's (a sub-agent saved without a `.subagent.json`, opened as a root) writes its raw log and its compaction log here too (`_own_log_path`). |
| `~/.freyja/projects/<sid>/raw_messages.recovered.jsonl`, `~/.freyja/sessions/<sub_id>.raw_messages.recovered.jsonl` | `scripts/backfill_raw_messages.py --apply` (run by hand) | The messages a raw log is missing (a restore stopped the log, or a sub-agent had none), read from the transcript and the compaction snapshots. Each row has `recovered: true`, `recovered_from` and `whole`. A halved or condensed result is put back whole from `~/.freyja/truncated` when its marker names a file that matches it (`restored_from`); otherwise the row keeps the compacted text and has `whole: false`. The raw logs themselves are never rewritten. Exports, `recall` and the recall drawer read these rows among the live ones by time; a live row with the same call or result wins. |
| `~/.freyja/projects/<sid>/compactions.jsonl` | `_append_compaction_log` | One row per compaction event, with the full summary. |
| `~/.freyja/sessions/<sub_id>.compactions.jsonl` | `append_subagent_compaction` (`bridge/transcript_persistence.py`) | The same rows for a sub-agent (and a computer_use child): its runner's compactions and the ones it asks for with `summarize_context`. Not under `projects/`, which the briefing scans. Sub-agents run before 2026-11-06 have none, and wrote no `compaction_event` telemetry. |
| `~/.freyja/sessions/compactions/<sid>-<ms>-{before,after}.{md,json}` | `_write_compaction_snapshot` | The transcript before and after a compaction (every path). A fork at a message a summary folded in is cut from the newest `-before.json` that still holds it. |
| `~/.freyja/telemetry/compaction.jsonl` | `bridge/compaction_telemetry.append_telemetry` | The cross-session rows the metrics dashboard reads (section 8). |
| `~/.freyja/truncated/*.txt` | `ToolResultTruncator.write_full_content` | The whole text of each result the truncator cut to fit, each result halving cut and each result a cheap `summarize_context` scope condensed. The cut text names its file. |

An export (`exportSessionToFile` in `src/main/persistence.ts`) writes three files. `<dest>.json` is the saved session with each tool result whole. `<dest>.bundle.json` holds `raw_transcript` (the raw log), `full_tool_results`, `live_transcript` (the renderer's messages), `telemetry` (the session's rows by type) and `compactions`. `<dest>.trace.txt` is the session as text. A result the truncator cut is read back from its file (`src/main/sessionRecord.ts`). A halved result needs no read-back: the raw log has it whole. A sub-agent's export reads its raw log and its compaction log from beside its sidecars; without a raw log, each result is the app's 2,000-character preview. The export reads recovered rows among the live ones, by time; a live row always wins, and a recovered result with only its compacted text is marked incomplete.

## 8 — Telemetry rows

### `compaction_event`

```
type                       "compaction_event"
session_id                 <sid>
agent_type                 <profile name, or null for a root session>
parent_session_id          <parent sid or null>
turn_id                    <the bridge's turn id, a sub-agent's "<sub id>-t<n>", or null>
subtype                    "compaction_start" | "compaction_complete" | "compaction_skipped"
                           | "compaction_failed" | "context_pruning" | "media_pruning"
                           | "thrash_skip"
model                      <model id>
tokens_before              <int>
tokens_after               <int>
effective_window           <int | null>
pressure_pct_before        <float | null>
pressure_pct_after         <float | null>
trigger_tokens             <int | null>     the reading that set a path A or B compaction off
trigger_pressure_pct       <float | null>
omitted_images             <int | null>     an image prune's count of the images that left the context
summary_tokens             <int>
mechanism                  the details' "strategy" ("llm_summary", "recent_computer_screenshots", ...)
                           or, without one, "summary" | "summary_iterative" | "tool_halve"
                           | "image_prune" | "tool_results_only" | "exploration_only"
                           | "thrash_skip" | the subtype
trigger                    "pre_request" | "overflow_cascade" | "overflow_cascade_image_elision"
                           | "agent_summarize_context" | "manual" | "restore"
                           | "provider_error" | "thrash_detector"
scope                      the agent's scope (path C)
reason                     the agent's rationale (path C), the restore reason (path E),
                           or the error of a skipped compaction
resumed_from_previous      <bool>
summarizer_input_tokens    <int>
summarizer_output_tokens   <int>
summarizer_duration_ms     <int>
summarizer_cost_usd        <float | null>
summary_excerpt            <first 240 chars>
summary_text               <full text, or null>
ts                         <unix epoch float>
```

A root session's rows carry its current turn id, as its `llm_call_metric` rows and its compaction log do. A sub-agent's carry the step they ran in, as its `llm_call_metric` rows name it; a computer-use child's have none. The thrash detector's rows name the model, and the agent type and parent from the session's metadata (a sub-agent's session has them; a root session's and a computer-use child's have neither), but no turn id, which the runner does not know. Before 2026-11-06 no row had a turn id, and thrash rows had no model, agent type or parent.

Path C writes a shorter row from `_emit_summarize_event`, without `trigger_tokens`, `trigger_pressure_pct` and `omitted_images`. Before 2026-11-06 it had no window or pressure fields, and no `summary_tokens`, `resumed_from_previous` or `summarizer_duration_ms`: the agent's event carried neither the summary's size nor the summary call's duration.

### `pressure_signal`

Written from the bridge's `on_llm_call` when the band changes. Fields: `session_id`, `agent_type`, `parent_session_id`, `band`, `pressure_pct`, `used_tokens`, `effective_window`, `model`, `ts`. The bands are `clean` (under 15%), `pruning` (under 25%), `awareness` (under 40%), `soft` (under 55%), `strong` (under 70%) and `fallback`.

### `llm_call_metric`

One row per model call. Fields: `session_id`, `turn_id`, `agent_type`, `parent_session_id`, `model`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `cost_usd`, `duration_ms`, `call_kind`, `iterative_summarizer`, `ts`. `call_kind` is `main`, `summarizer`, `working_memory_extraction` or `cache_keepalive`. `iterative_summarizer` is set on summarizer rows only. A sub-agent's rows come from `_on_sub_llm_call` and carry the same two fields. Their `turn_id` is `<sub id>-t<n>`, where n counts the sub-agent's own calls, its steps. A summary or working-memory call is not a step: it carries the n of the step it ran in, as the parent's carry their turn, and it does not count toward the record's `iterations`. Before 2026-11-06 each one counted as a step with a turn id of its own.

### `summarize_context_call`

Only path C writes these. The dashboard's "Agent compaction decisions" panel reads them.

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
preserve_facts_missing     [<string>, ...]
pinned_ordinals            [<int>, ...]
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

## 9 — Dashboard, channels and caveats

### Dashboard (`src/renderer/components/MetricsDashboard.tsx`)

| Element | Source rows | Aggregation |
|---|---|---|
| Total spend tile | `llm_call_metric` | Sum of `cost_usd`. |
| Compactions tile | `compaction_event` outcome rows | Each compaction counts once, by its outcome: a `compaction_complete` row ("llm" in the subline) or a `context_pruning` or `media_pruning` row ("prune"). Start, skipped and failed rows are attempts and do not count. The subline also counts `thrash_skip` rows. Before 2026-11-06 an overflow recovery that only removed images wrote a `compaction_complete` row with trigger `overflow_cascade_image_elision`; the dashboard reads such a row as `media_pruning`, so it is a prune and not one of the runtime's summaries. |
| Tokens saved tile | `compaction_event` outcome rows | Sum of `tokens_before - tokens_after` over rows where it is positive. Halving rows count, since they carry both sizes. A start row has no `tokens_after`, so counting it counted its whole size as saved. |
| Cooperation | `compaction_event` complete rows | The agent's share of every summary. Agent-driven is `agent_summarize_context`; manual is `manual`; every other trigger is the runtime's (`pre_request`, `overflow_cascade`, `restore`, the old `context_pressure`). |
| Trigger source bars | `compaction_event` outcome rows | Grouped by `trigger`. |
| Savings trend | `compaction_event` outcome rows with `tokens_before > 0` | `(tokens_before - tokens_after) / tokens_before` over time. |
| Per-session compaction log | `compaction_event` rows for the session | Every row, sorted by `ts`. Only summary and halving rows show a size after and a saving. An image prune's event carries no sizes, so its row shows the images it removed (`omitted_images`, or a dash without one); before 2026-11-06 it showed "0 → 0" and a red 0%. A click shows `summary_text`. |
| Agent compaction decisions | `summarize_context_call` | Newest first, with the scope mix. |

### The three channels

They give the agent a chance to call `summarize_context` itself before the runtime compacts at 70%. None of them compacts anything.

- **Channel 1** is a tag on each tool result, `[ctx: NN% (used/usable) · advisory]`, from `_build_pressure_tag` in `bridge/freyja_bridge.py`. It starts at 25%. Past 70% the advisory says the runtime compacts the history before its next request.
- **Channel 2** is a `<system-reminder>` at the end of the last user message, from `_augment_messages_with_pressure_note` in `engine/runner.py`. It starts at 40% and names the 10-point band. It sits after every cache breakpoint, so it does not break the cache.
- **Channel 3** is a one-shot advisory, `[!CTX PRESSURE: window crossed X-Y% during this turn ...]`, put in front of the next tool result. `mark_channel3_crossing` sets it when the band rises into 55% or more during a turn; the bridge's tool-result wrapper takes it with `consume_channel3_advisory`. It says that past 70% the runtime compacts the history before its next request. Nothing counts or rejects tool calls.

### Caveats

- One turn can compact more than once. Path B can fire after an overflow, and path A can fire on the next iteration. Each writes its own rows.
- Compaction deletes entries from the live transcript. They survive in `raw_messages.jsonl`, in the summary in `compactions.jsonl`, in the renderer's own message list and in the snapshot pair.
- Only the newest compaction entry stays in the live transcript. A later compaction deletes the earlier entry with the rest of the old history. The earlier summaries stay in `compactions.jsonl`.
- When the summary extends a previous one from a digest, the previous summary's message is left out of the digest, so the summarizer does not read it twice.
- The thrash detector in `_attempt_compaction` skips a compaction when the last two each saved under 10%. It writes a `thrash_skip` row.
- Summary rows written before 2026-11-06 by paths A, B and D set a measured reading before against the tokenizer's count after. They overstate the saving by the gap between the two measures, a few points each. They carry no size to correct it with, so the dashboard reads them as recorded. A row with `trigger_tokens` is like for like. Path C rows from before then count the transcript only: the tokens they saved are right, the share is too high.
- A sub-agent's runner gets its own `summarize_context`, `session_memory` and `recall` tools, bound to the sub-agent's session. Otherwise a sub-agent would compact the parent's transcript. Its `summarize_context` events go to the sub-agent's pane, and they and the runner's write its compaction log and `compaction_event` rows (`append_subagent_compaction`), as a root session's do.
- `force_compact` does not go through the runner. It runs the compactor through `run_off_loop`, forwards the summary calls to the runner's `on_llm_call`, and calls `_mirror_compaction_event` itself.
- The sync `AgentRunner` is legacy. The bridge always uses `AsyncAgentRunner`. The sync runner takes `on_llm_call`, so its compactions report their summary calls, but nothing in the app runs it.

## 10 — The `session_memory` tool

`session_memory` lets the agent carry notes across compactions. It works with `summarize_context`: together they are how the agent takes part in its own memory management.

### 10.1 What it is

It is a markdown file per session at `~/.freyja/projects/<safe_sid>/memory.md`. The agent reaches it through a tool with four actions:

| Action | Behavior |
|---|---|
| `read` | Returns the contents, or an empty string if the file does not exist. |
| `write` | Replaces the file with `content`. |
| `append` | Adds `content` under a `## <timestamp>` header. Preferred for notes. |
| `clear` | Deletes the file. |

The code is in `bridge/tools/session_memory_tool.py`. The path comes from `bridge/project_paths.py:project_output_dir(session_id) / "memory.md"`.

The file is capped at 64 KB (`_MAX_BYTES`). A write or an append past the cap cuts the file from the head: the oldest notes go and the newest stay.

### 10.2 Expected workflow

The system prompt's `_CONTEXT_DISCIPLINE_BLOCK` tells the agent:

> Before compacting:
> - Stash anything you'll need to remember into `session_memory` — the file lives outside the transcript and survives every future compaction.

The tool's description says the same:

> Anything you write here SURVIVES compaction by construction — the file is never summarized. Use it to stash facts, plans, or notes that you need to keep retrievable even after the conversation history is condensed.

The intended workflow:

1. During a task, the agent appends notes: `session_memory(action="append", content="<finding / decision / file path>")`.
2. Before it calls `summarize_context`, it appends what it just did and what is still pending.
3. After a compaction (it sees the `[Previous conversation summary]` message), it should read `session_memory` to get back what the summary may have paraphrased away.
4. For literal strings that matter (exact file paths, error messages, identifiers), it can write them to `session_memory`, or pass them as `preserve_facts` to `summarize_context`, which appends any the summary lost.

### 10.3 What the code does

**Registration.** `bridge/tools/registry.py` registers one `SessionMemoryTool(session_id=_mem_session, on_mutation=...)`. `on_mutation` fires after a successful `write`, `append` or `clear`. The bridge uses it as the signal for a mid-session working-memory extraction.

**Sub-agents.** The child registry starts with the parent's tool instances. `_run_child` in `bridge/tools/sub_agent_tool.py` replaces two of them:

```python
if fork_context is None:
    if "summarize_context" in child_registry._tools:
        child_registry._tools["summarize_context"] = sub_summarize_tool
    if "session_memory" in child_registry._tools:
        child_registry._tools["session_memory"] = SessionMemoryTool(
            session_id=record.id,
        )
```

Without this, a sub-agent would write to the parent's `memory.md`. With it, each sub-agent has its own file. A fork keeps the stubs it was given, which refuse both calls.

**Concurrency.** `execute()` runs each call with `run_in_executor(None, ...)`, so two parallel calls race in the thread pool. `append` reads, then writes, so it is the most exposed. The cap is applied at write time too, so in a race one side's content can be lost. Agents rarely make parallel `session_memory` calls, but the race exists.

**Persistence.** Each successful write or append goes to disk at once. Reads go to disk too. The file survives a bridge restart, an app restart and every compaction.

**Restore.** On resume, the rebuilt tool points at the same file, since the session id does not change. Nothing injects it: the agent has to read it.

### 10.4 Telemetry

Each `session_memory` call writes a `tool_call_metric` row through the runner's `on_tool_metric` hook:

```jsonc
{
  "type": "tool_call_metric",
  "session_id": "<sid>",
  "turn_id": "<turn>",
  "agent_type": "<profile or null>",
  "tool_name": "session_memory",
  "duration_ms": <int>,
  "ok": <bool>,
  "result_bytes": <int>,    // size of the JSON response, not of the file
  "ts": <unix epoch>
}
```

The row does not carry the action, so read, write, append and clear count together.

The tool returns a JSON body (`{action, path, bytes, content, ...}`). The agent sees it as a normal tool result, and the user sees it in the conversation.

### 10.5 Surfaces

| Surface | Behavior |
|---|---|
| Conversation | Shows `session_memory` calls and results like any other tool. A `read` shows the whole file. |
| Activity panel | The same. No category of its own. |
| Metrics dashboard | `tool_call_metric` rows in the top-tools histogram. No action breakdown and no file size. |
| Export bundle | `memory.md` is not included. |
| `~/.freyja/sessions/<sid>.transcript.json` | Does not contain `memory.md`. |
| Session deletion (`delete_session`) | Removes the transcript file only. The `~/.freyja/projects/<sid>/` directory, with `memory.md`, `raw_messages.jsonl`, `compactions.jsonl` and the artifacts, stays on disk. |

### 10.6 Gaps

**G1 — Calls do not carry the action.** The dashboard cannot split memory use by action. A fix: put the action from the arguments on the `tool_call_metric` row, or have the tool write its own row.

**G2 — No reminder after a compaction or a restore.** The agent must call `read` to get its notes back. The `[Previous conversation summary]` message does not mention the file, and a restored session's prompt does not either. Options: add a line to the summary message when the file is not empty; put the file's size in the pressure note; or note the file in the prompt of a restored session.

**G3 — No view in the UI.** The user sees the notes only in the agent's tool calls. A panel next to the compaction log would help.

**G4 — Not in the export bundle.** The agent's notes are part of what happened in a session. A fix: add a `session_memory` field (`exists`, `bytes`, `content`) to the bundle, read from `~/.freyja/projects/<sid>/memory.md`.

**G5 — Parallel writes race.** A lock keyed on the file path would close it. The practical impact is low.

**G6 — Deletion keeps the project directory.** `delete_session` removes only the transcript file. This keeps the data, which matches the rule to never lose data, but nothing tells the user the files are still there.

**G7 — The file's size is not tracked over time.** A growth trend would show an agent that writes too much.

**G8 — One cap for the whole session.** Old notes from finished phases are cut first, and the agent cannot rotate the file. A `rotate` action could move it to `memory.<timestamp>.md` and start a new one.

**G9 — No cross-session import.** A user who branches a session for a related task has to copy the file by hand. This is a feature gap, not a correctness gap.

### 10.7 Related tools

| Tool | What it is for | When to use it instead of session_memory |
|---|---|---|
| `memory` | Cross-session user and project preferences | For anything that must outlive this session. |
| `record_user_preference` | Short form of the above | For a preference the user stated clearly. |
| `task_board` / `kanban` | Multi-agent task ledger | To track work items and assign them to sub-agents. |
| `write_file` to the project output directory | Long artifacts | When the content is a deliverable the user wants on disk. |
| `summarize_context(preserve_facts=...)` | Literal strings the summary must keep | When the summary should carry a fact verbatim. |
| `pin_entries` on `summarize_context` | Messages kept word for word across compactions | When a tool result is the source of truth. |

### 10.8 In one line

`session_memory` is the agent's notes file for one session: markdown on disk, outside the transcript, capped at 64 KB, with read, write, append and clear. It survives every compaction, each sub-agent has its own, and it is not in the UI or the export bundle.

## 11 — Implementation history

`COMPACTION-DECISION-DRAFT.md` has the early log. The changes that shaped the current flow:

- Phase 1 (2026-04-30): telemetry, Channel 1, cheap pruning at 25%.
- Metrics dashboard v2 (2026-05-07).
- Cooperative phase 2 (2026-05-12): `summarize_context`, the three channels, the iterative summary.
- Raw transcript log and the three-view export (2026-05-12).
- Channel 2 moved to the last user message as a `<system-reminder>` (2026-05-14).
- Compaction flow audit (2026-05-14): one set of events for every trigger, summarizer tagging, snapshots on every runtime path, immediate save, one token measure.
- Phase 2.5 (May 2026): the runtime threshold dropped from 0.80 to 0.70, and the strong band from 0.60 to 0.55.
- 2026-10-06 (9e0472b): restore compaction runs off the event loop.
- 2026-10-13 (20c9371, 0820478): the summary continues the session's cached prompt; old tool results are halved once, and only while the cache is cold; Anthropic sessions keep their cache warm between turns.
- 2026-10-27 (fdc1f00, 04d5bed): pre-request, overflow and agent compactions run off the event loop (`run_off_loop`); the summary deadline grows with the transcript.
- 2026-11-06 (ca37eee to 8c4e96d): a turn keeps the last call's measure; summary costs count from every path; the advisories stop saying tool calls may be rejected.
- 2026-11-06 (this round): a halved result is saved whole under `~/.freyja/truncated` and its marker names the file; halving rows carry the size before and after, and the 70% check after halving uses the smaller size; a manual compaction stops the keep-alive; restore compactions write telemetry and `compactions.jsonl`; a sub-agent's call reports move to the event loop; the accumulator takes a call's usage before the call is reported; the turn after a manual compaction keeps the size it left; the sync runner takes `on_llm_call`.
- 2026-11-06 (pruning round): a cheap `summarize_context` scope saves each result whole before it condenses it, and the preview names the file; a restored session keeps writing `raw_messages.jsonl`; a sub-agent writes the same log beside its sidecars, and its export reads each result whole from it; halving starts at 40% measured, keeps the newest 10 results whole and leaves results under 2,000 characters alone.
