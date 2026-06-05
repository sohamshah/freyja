# Grounded Memory — Design

**Status:** draft / proposal · **Authors:** Soham + Claude · **Date:** 2026-06-04
**Related:** `docs/COMPACTION-DECISION-DRAFT.md`, `docs/COMPACTION-FLOW.md`, `docs/SKILLS-MEMORY-DESIGN.md`

---

## TL;DR

An agent in `session-mpxlhh4o` built real work (created `widget_tools.py`, 680 lines; edited ema-next), then later told the user *"I have no recollection of making changes to agent-harness this session. Everything in our conversation so far has been read-only exploration."* It recovered only by manually querying the artifact manifest + git + an on-disk map-doc.

The forensic root cause is **not** LLM-summary decay (no LLM summary existed at the moment of forgetting; context was at ~26–39% of a 1M window). It is that **the runtime durably recorded every action the whole time and never surfaced that record back to the agent.** The forgetting is a **surfacing/grounding failure, not data loss.**

This doc proposes **Grounded Memory**: treat the context window as a *viewport over durable, trust-tiered backing stores*, not as the system of record. The **spine** (ship first) is an **Effect Ledger** (runtime-authored ground truth) that is **auto-injected first-person into context** and backed by **corrective guardrails**. The **rich layer** (your structured-store idea) and **frontier** (graph/recall/sleep-time) build on that spine.

---

## 1. Problem scope

### 1.1 The incident (ground truth from the session JSON)

| Time | What happened |
|---|---|
| 04:58 | User: implement generative-UI widgets in `agent-harness` (tools + events) and `ema-next` (rendering). |
| 05:5x | Agent explores agent-harness: `list_directory`, ~150 `read_file`/`grep`. |
| 06:07–09:16 | **48 write-actions**: `write_file widget_tools.py` (680 lines), `edit_file __init__.py / runner_factory.py`, edits across ema-next, plus a map-doc written to the project dir. Each file tool auto-appended a row to `manifest.jsonl`. |
| 05:53–08:08 | **~64 `context_pruning` events** (`prune_old_tool_results`) halving old tool results. Context sat at **26–39% of a 1M window** the whole time. |
| 06:03 | Agent self-calls `summarize_context(scope=tool_results_only)` → in-place rewrite, `entries_removed=0`, no LLM, no compaction entry. |
| 06:34 | Agent self-calls `summarize_context(scope=exploration_only, preserve_facts="…agent-harness backend is DONE+verified on branch feat/widget-tools (new widget_tools.py…)")`. **`preserve_facts` silently no-op'd** (see §1.2). |
| (pivot) | User pivots to "deep-dive Freyja generative-UI rendering"; a `grep` returns a 144K-char minified-bundle dump. Agent compacts again (`tool_results_only`, frees 46K). |
| 08:17 | User: *"start a subagent and create a PR for the changes you made to agent-harness today. If you don't remember, take stock of session artifacts; if you still can't remember, tell me and don't do anything."* |
| 08:17 | **Agent: "I have no recollection… everything in our conversation so far has been read-only exploration."** It then reconstructs the truth from `artifacts` (manifest) + `git status`/`diff` + the on-disk map-doc — but only because the user explicitly told it to check artifacts. |
| 08:27 | The same forgetting **recurs** for the ema-next work; recovered via the map-doc + git. |

### 1.2 Forensic root cause (corrects the initial hypothesis)

The initial hypothesis blamed the LLM summarizer (iterative-summary decay, `[Previous conversation summary]` framing, 400K middle-truncation). **The forensics falsify that for this incident** — the first and only LLM summary ran ~15 h later, on a resumed turn, and was *faithful*. The actual causes:

1. **The durable write-ledger exists but is pull-only.** `bridge/artifact_store.py` (`SessionArtifactStore`) auto-records every `write_file`/`edit_file`/`edit_json` to `<project_dir>/manifest.jsonl` (path, operation, lines, sha256, creator). The HOT-tier `artifacts` tool exposes it — but **nothing ever injects it into context** (not the system prompt, not after pruning, not on resume). The data that refutes "read-only" was on disk, unshown.

2. **The agent's own defense is silently broken in the cheap scopes.** `preserve_facts` → `_repair_preserve_facts` (`summarize_context_tool.py:469`) only attaches its appendix to an `is_compaction` entry. The cheap scopes (`tool_results_only` / `exploration_only`) **never create one**, so the 06:34 anchor evaporated with no error. The promise at `freyja_bridge.py:1820` ("the runtime guarantees they appear") is **false for exactly the scopes a cost-conscious agent reaches for first.**

3. **No reconcile-before-asserting discipline.** `_CONTEXT_DISCIPLINE_BLOCK` (`freyja_bridge.py:1769`) only says *"stash **before** compacting."* Nothing tells the agent to check the manifest/git **before** asserting it did or didn't do something.

4. **Dilution at a topic pivot.** Write confirmations are short (`"Created file: …widget_tools.py\nWrote N characters (680 lines)"`, under the 500-char truncation floor) so they were **not byte-destroyed** — but they were demoted to terse one-liners and drowned among ~150 reads + a 144K grep dump. After the pivot, the agent's working self-model read "this session = exploration."

**Secondary / structural:** the manifest covers only the 3 structured file tools — **bash/git/shell writes are invisible** (`MUTATING_FILE_TOOL_NAMES`); `session_memory` is opt-in and was empty; `raw_messages.jsonl` preserves everything but has no agent read-back tool.

**Latent (real, did not cause this incident):** single running summary + iterative decay; reads/writes conflated in one neutral list with no first-person "Actions I took" section; `role="system" [Previous conversation summary]` framing; force-preserve footer regex matches only `/artifacts/*.md`.

### 1.3 Why it's a class, not a one-off

The system collapses **three distinct kinds of memory** into one lossy LLM summary + one empty opt-in scratchpad:

1. **Episodic ground truth** — what I *did* (files, commands, searches, artifacts). Should be machine-captured, never LLM-trusted.
2. **Semantic working memory** — what it *means* (task, decisions, state). LLM-authored, lossy, but can be *structured*.
3. **Verbatim archive** — *everything*, for recall/audit.

Conflating them means the agent's belief about its own actions rides on the most lossy channel. Separating them by **author and trust** is the fix.

---

## 2. Principles (the reframe)

> **The context window is a viewport, not the document.** Today the live transcript *is* the document and compaction is lossy edits to the only copy. Make the transcript a **render** over durable backing stores; compaction becomes **re-rendering at lower resolution with a lossless backing store**. Post-compaction, context is **reconstructed from authoritative stores**, not **degraded from prior context**.

> **Separate memory by who can be trusted to author it, and surface the trust tier.**

| Author | Trust | Fidelity | Store |
|---|---|---|---|
| **Runtime** (deterministic) | ground truth | exact | **Effect Ledger** — what I did |
| **Agent / summarizer** (interpretive) | lossy | semantic | **Structured Working Memory** — what it means |
| **Time** (verbatim) | complete | raw | **Searchable Archive** — everything, recallable |

The agent's live context becomes a **budget-tiered render** over these three, reassembled whenever pressure forces eviction, with trust tiers preserved. "I might have forgotten" is replaced by "the ledger says I did X; trust it over my in-context memory."

---

## 3. The idea space (both brainstorms)

### 3.1 Soham's brainstorm

1. **Prompt improvement** around `summarize_context` — instruct the agent to describe what to retain. *(No guarantees — it's the optimization layer.)*
2. **Store all changes / artifacts / research / web-searches** in a session; either make them easy to search (tell the agent post-compaction which tool to recollect with) **or** inject structured context into the system prompt post-compaction.
3. **Expand `session_memory` into a structured persistent store** — a tool for the agent to organize summaries of work by **project / change / diff / artifact** — so post-compaction injection has a *ready, accurate, agent-written* source. *In addition to* the conversation summary (which still replaces the message list).
4. **Preserve the full session and make it searchable** (already preserved via `raw_messages.jsonl`); **extend list-sessions** to return much more, for current and other sessions, perhaps via a new tool.

### 3.2 Claude's brainstorm (adversarially scored against *this* incident)

| Approach | Impact / 10 |
|---|---|
| **Standing write-ledger `<system-reminder>`** via the existing reminder seam | **8.5** |
| **Event-anchored re-injection** after prune/compaction | 8 |
| **Pinned first-person "Actions I took" entry** | 8 |
| **Structured write-stub** replaces truncation (`✎ WROTE path (N lines, op)`) | 7.5 |
| **Carry authorship through the cheap path + fix the silent `preserve_facts` no-op** | 7.5 |
| **Auto-injected reconciliation note** (manifest + `git status`) | 7 |
| **Reconcile-before-asserting** system-prompt rule | 6 |
| **Forgetting-detector telemetry** | 6.5 |
| First-person "Actions I performed" summary section / reframe `[Previous conversation summary]` | 1–1.5 *(latent-only)* |

### 3.3 Convergence

| Soham | Claude | → Subsystem |
|---|---|---|
| #1 prompt | reconcile rule + guardrails | **F. Cooperative + corrective layer** |
| #2 store + inject/search | standing + event-anchored ledger injection | **A. Effect Ledger** + **E. Re-grounding injection** |
| #3 structured store | first-person "Actions" memory, provenance | **B. Structured Working Memory** *(the centerpiece)* |
| #4 preserve + searchable + cross-session | recall tool over archive | **C. Searchable Archive + recall** |

The two halves are complementary: **Claude's ledger keeps Soham's structured store honest and cheap to maintain; Soham's structured store turns Claude's flat ledger into a real working memory.**

---

## 4. Target architecture (full vision)

```
        ┌──────────────────────── THE BRIDGE = memory orchestrator ───────────────────────┐
 tool   │                                                                                  │
 results│  CAPTURE                  PROJECT (on compaction)            RENDER (each turn)    │
 (2478) │  ┌────────────┐           ┌──────────────────┐              ┌──────────────────┐  │
 ──────►│  │EFFECT      │──ground───►│ summarizer emits │──seeds──────►│ re-grounding     │  │
        │  │LEDGER      │   truth    │  TWO outputs:    │              │ header (1st-     │  │
        │  │(general    │           │  • prose summary │──► transcript │ person, trust-   │──┼─► last user
        │  │ manifest)  │           │  • structured    │     head      │ tiered, recall + │  │   message
        │  └────────────┘           │    mem upserts   │              │ unknowns)        │  │  (cache-safe
        │  ┌────────────┐           └────────┬─────────┘              └──────────────────┘  │   tail-append)
        │  │OBSERVATION │                    ▼                                              │
        │  │pointers    │           ┌──────────────────┐    ┌──────────────────┐            │
        │  └─────┬──────┘           │ STRUCTURED        │◄──►│ provenance links │            │
        │        ▼                  │ WORKING MEMORY    │    │ (memory graph)   │            │
        │  ┌────────────┐           │ workstream/       │    └──────────────────┘            │
        │  │ARCHIVE     │◄──recall──│ artifact/decision/│   engine/ stays ignorant of bridge │
        │  │raw_messages│           │ finding/open      │   stores; it compacts + accepts     │
        │  │compactions │           └──────────────────┘   injected reminders                 │
        │  └────────────┘                                                                    │
        └──────────────────────────────────────────────────────────────────────────────────┘
```

**Subsystems:** A. Effect Ledger · B. Structured Working Memory · C. Searchable Archive + recall · D. Compaction-as-Projection · E. Re-grounding injection · F. Cooperative + corrective layer. (A/E/F = spine; B/C/D = rich layer; graph/tiers/sleep-time = frontier.)

---

## 5. The spine (build first)

The spine alone closes the incident class and needs no engine/bridge surgery:

- **A. Effect Ledger** — generalize the manifest; classify **effects** (always kept/injected) vs **observations** (compact pointer, recall-able); capture bash/git writes via a `git status` delta.
- **E. Re-grounding injection** — a **standing first-person write-ledger** `<system-reminder>` riding the existing reminder seam, plus a **one-shot post-prune/compaction** re-grounding header.
- **F. Corrective guardrails** — fix the silent `preserve_facts` no-op; add a **reconcile-before-asserting** rule; add a **self-model monitor** (forgetting detector).

---

## 6. Implementation design

### 6.1 Data model — Effect Ledger row (v2)

Generalize `SessionArtifactStore` rows with a `kind` discriminator. Keep `manifest.jsonl` for back-compat; effects/observations write to the same store with new kinds (or a sibling `ledger.jsonl` if you want a clean file).

```jsonc
{
  "id": "eff_…",
  "sessionId": "session-…",
  "creatorId": "session-…",          // distinguishes subagents
  "creatorLabel": "Main agent",
  "kind": "file_write",              // see classification table
  "class": "effect",                 // effect | observation
  "operation": "create",             // create|edit|delete|commit|search|fetch|spawn|...
  "summary": "created widget_tools.py (680 lines)",   // injectable one-liner
  "path": "/…/widget_tools.py",      // for file/shell effects
  "repo": "/…/agent-harness",        // resolved git root (effects)
  "toolCallId": "toolu_…",
  "archiveRef": { "turn": 42, "offset": 10243 },      // observations → archive span
  "createdAt": 1780472122146,
  "exists": true, "bytes": 26124, "lines": 680, "sha256": "19f2…"
}
```

### 6.2 Capture — at the tool-result hook (`freyja_bridge.py:2478`)

Today the hook records only `MUTATING_FILE_TOOL_NAMES` + `generate_image`. Generalize it with a **classification map**:

| Class | Tools | Ledger row | Injected? |
|---|---|---|---|
| **Effect** | `write_file`, `edit_file`, `edit_json`, `generate_image`, `subagents`/spawn, `schedule`, **mutating `bash`** | full, verbatim, permanent | **yes** |
| **Observation** | `read_file`, `grep`, `glob`, `web_search`, `web_fetch`, `view_image` | compact pointer (target + size + `archiveRef`) | no (recall-able) |
| **Neither** | everything else | — | — |

**Closing the bash/git blind spot — reconciliation *as capture*:**

```python
# after any bash whose cwd is inside a git repo (pseudo, bridge-side):
root = git_root(cwd)
if root:
    after = git_status_porcelain(root)               # cheap, cached per root
    delta = diff(baseline[root], after)              # added/modified/deleted paths
    for path, op in delta:
        artifact_store.record_file(path, kind="shell_effect", operation=op,
                                   source="bash_git_delta", creator_id=session_id)
    baseline[root] = after
```

This captures `git commit`, `>` redirects, `mv`, build outputs — **precisely, with no command parsing** — and makes the same `git status` primitive the agent used to *recover* also the one that *captures*. Effects from bash become first-class ledger rows.

### 6.3 Storage & layering

- Ledger lives in `project_output_dir(session_id)` (`bridge/project_paths.py:17`) — same dir as `manifest.jsonl` and `memory.md`. Append-only, lock-guarded (mirror `SessionArtifactStore._append`).
- **Layering rule:** `engine/` imports nothing from `bridge/`. **The bridge owns all memory stores and assembles every injected block.** The engine only (a) compacts the transcript and (b) accepts injected reminders via the existing callback. Do **not** reach into `engine/session.py`/`compaction.py` to read the manifest.

### 6.4 Render / inject

**(a) Standing write-ledger reminder** — mirror `_build_stale_task_reminders` (`freyja_bridge.py:8100`) as a sibling producer; point the wiring at `freyja_bridge.py:3393` to a wrapper returning **both** blocks (the runner's `_gather_extra_reminders` at `runner.py:1540` already joins a list). It flows through `_augment_messages_with_pressure_note` (`runner.py:1555`) → tail-appended to the **last user message**, so the system/tools/summary cache breakpoints stay intact (~150–300 tokens when it fires, 0 when unchanged).

```python
def _build_write_ledger_reminder(self) -> list[str]:
    rows = self.artifact_store.latest_by_path(creator_id=self.session_id)   # dedup newest-per-path
    effects = [r for r in rows if r.get("class") == "effect"][:12]          # cap; effects only
    if not effects:
        return []
    digest = hash(tuple(r["id"] for r in effects))
    if digest == self._last_ledger_digest and self._turns_since_ledger < K: # debounce on content-hash
        return []                                                           # …with a floor: re-emit every K turns
    self._last_ledger_digest = digest; self._turns_since_ledger = 0
    lines = ["<system-reminder>",
             "Files YOU have created/edited this session (durable runtime ledger — "
             "GROUND TRUTH, survives compaction; trust this over your in-context memory):"]
    for r in effects:
        lines.append(f"  • {r['summary']}   [{r['operation']}, {r.get('repo','')}]")
    if self._uncaptured_bash_count:
        lines.append(f"({self._uncaptured_bash_count} bash commands not captured — run `git status` if you need shell effects.)")
    lines.append("Don't mention this reminder to the user.")
    lines.append("</system-reminder>")
    return ["\n".join(lines)]
```

**(b) One-shot post-prune/compaction re-grounding header** — mirror `consume_channel3_advisory` (`runner.py:1655`). After `prune_old_tool_results` / `_attempt_compaction` (the `_handle_context_pressure` chokepoint, `runner.py:812`), set a pending flag drained on the next `_gather_extra_reminders`:

```
<system-reminder>
Context was just compacted — older tool-result detail may have been condensed.
This does NOT mean nothing happened. Per the durable ledger, this session you have:
  • created widget_tools.py (680 lines, agent_harness/cli/tools/)
  • edited __init__.py, runner_factory.py (agent_harness/)
  • edited AssistantMessage.tsx (ema-next)
These files exist on disk now even if the conversation no longer shows you creating them.
Full detail: recall(query=…).
</system-reminder>
```

**(c) Replace the `[Previous conversation summary]` injection** (`session.py:302-306`) — when the LLM summary path *does* run, render the inject as **first-person + ground-truth-tiered**, prepending the ledger digest above the prose summary (latent-risk hardening; low urgency but cheap).

### 6.5 Guardrails (the guarantee layer)

1. **Fix the silent `preserve_facts` no-op** (`summarize_context_tool.py:469`). For cheap scopes that create no compaction entry, either (a) write the facts into the Effect Ledger as a `pinned_fact` row that the injector always surfaces, or (b) return a hard error `preserve_facts unsupported in scope=<x>; use session_memory or a summarizing scope`. **And correct the false doc claim** at `freyja_bridge.py:1820`.

2. **Reconcile-before-asserting** — add to `_CONTEXT_DISCIPLINE_BLOCK` (`freyja_bridge.py:1769`):
   > *Before claiming you did or did not do something this session ("no changes were made", "I only explored"), consult the write-ledger reminder above and, if a repo is involved, `git status`. The ledger is ground truth; your in-context memory may have been compacted.*

3. **Self-model monitor (forgetting detector)** — after each assistant turn, if the text asserts a negative self-claim (`/\b(no changes|read-only|didn't (create|edit|write)|nothing to commit)\b/`) **while** `artifact_store.list(creator_id=session_id)` is non-empty, (a) inject an immediate correction on the next turn and (b) emit a `forgetting_detected` telemetry row. Doubles as a live guard *and* the training signal.

### 6.6 Telemetry

Extend `compaction_telemetry`: add `ledger_effects_count`, `ledger_injected_bytes`, and the `forgetting_detected` row (links a negative self-claim to ledger/git divergence). Today telemetry captures only tokens/timing, so the forgetting failure is undetectable — this makes it measurable and builds the corpus for the team's trained-policy bet.

---

## 7. Before / After traces

The same session, step by step — **what information the agent has at each step**, and the operations/prompts involved. *(Today)* = current behavior. *(Grounded)* = after the spine.

### 7.1 Stage-by-stage

| # | Operation | **Today** — what the agent has | **Grounded** — what additionally happens |
|---|---|---|---|
| 1 | explore agent-harness (`list_dir`, `read_file`×N, `grep`) | full read results in context | each read → **observation pointer** row (target + `archiveRef`); not injected |
| 2 | `write_file widget_tools.py` (680 lines) | tool result `"Created file: …widget_tools.py\nWrote 24kB (680 lines)"`; **manifest row** written (pull-only) | **effect row** `kind=file_write, summary="created widget_tools.py (680 lines)"`; standing reminder now lists it on the **next** user message |
| 3 | `edit_file __init__.py`, `runner_factory.py` | tool results; manifest rows | effect rows; reminder digest updates → re-emits |
| 4a | ~64 `context_pruning` halve old **read** results | older read results → `"[Content truncated…]"` | unchanged (reads *should* be condensed) — but a **one-shot re-grounding header** is queued: "context compacted; per the ledger you created widget_tools.py…" |
| 4b | `summarize_context(tool_results_only)` | tool results → one-liners; assistant reasoning kept | unchanged |
| 4c | `summarize_context(exploration_only, preserve_facts="agent-harness backend DONE… widget_tools.py…")` | **`preserve_facts` SILENTLY no-ops** — the anchor is lost, no error | **guardrail #1**: facts persisted as a `pinned_fact` ledger row (or hard error). The "DONE" anchor survives. |
| 5 | edit ema-next files; write map-doc to project dir | tool results; manifest rows | effect rows; ema-next edits now in the standing reminder |
| 6 | user pivots → `grep` Freyja repo → **144K-char dump** | huge observation floods recent context; older write confirmations demoted/diluted | observation pointer only for the dump's bulk; **effect ledger unaffected** (write history stays pure and small) |
| 7 | `summarize_context(tool_results_only)` frees 46K | grep noise condensed; **no LLM summary exists** | one-shot re-grounding header injected after the compaction |
| 8 | **User: "create a PR for the changes you made to agent-harness today…"** | — | — |
| 9 | agent answers | see §7.3 **Today** | see §7.3 **Grounded** |

### 7.2 The decisive moment (step 9) — context, side by side

**Today — what's in the agent's context at 08:17:**
- Recent turns: Freyja generative-UI exploration (post-pivot).
- Older turns: write confirmations present but condensed to one-liners, diluted among ~150 reads + the grep dump.
- A pressure tag on the last tool result: `[ctx: 31% · advisory]`.
- **No `[Previous conversation summary]`** (no LLM compaction ran).
- No ledger, no reconciliation cue, no surviving `preserve_facts` anchor.

**Agent's reasoning → output (verbatim from the session):**
> *"I have no recollection of making changes to agent-harness this session. Everything in our conversation so far has been read-only exploration."*

It then **only** recovered because the user's message explicitly said *"take stock of session artifacts"* → it called `artifacts{list}` + `git status`/`diff` + re-read the map-doc.

---

**Grounded — what's in the agent's context at 08:17:**

The **last user message** carries the user's prompt plus the standing reminder (tail-appended, cache-safe):

```
[user] start a subagent and create a PR for the changes you made to agent-harness today…

<system-reminder>
Files YOU have created/edited this session (durable runtime ledger — GROUND TRUTH,
survives compaction; trust this over your in-context memory):
  • created widget_tools.py (680 lines)            [create, agent_harness/cli/tools]
  • edited __init__.py (+3)                         [edit, agent_harness/cli/tools]
  • edited runner_factory.py (+14)                  [edit, agent_harness/server]
  • created feat/widget-tools branch; committed     [shell_effect, agent_harness]   ← git-delta capture
  • edited AssistantMessage.tsx                      [edit, ema-next]
Pinned facts: "agent-harness backend is DONE+verified on branch feat/widget-tools"   ← rescued preserve_facts
Don't mention this reminder to the user.
</system-reminder>
```

Plus the standing discipline rule (system prompt): *"Before claiming you did or did not do something, consult the write-ledger; it is ground truth."*

**Agent's reasoning → output:**
> *"Yes — this session I created `agent_harness/cli/tools/widget_tools.py` (680 lines: the `show_widget`/`widget_spec` tools) and edited `__init__.py` and `runner_factory.py`, on branch `feat/widget-tools`. Spawning a subagent now to open the PR for exactly those files; I'll exclude the unrelated `knowledge/ema/index.jsonl` change and untracked research docs."*

No manual archaeology. The forgetting is structurally impossible: the agent cannot say "read-only" while `created widget_tools.py (680 lines)` is GROUND-TRUTH-labeled in its current turn. If the self-model monitor *had* seen a negative claim, it would fire a correction the same turn.

### 7.3 What changed, mechanically

| Failure mechanism (today) | Spine fix |
|---|---|
| Write record diluted/demoted, never surfaced | Standing first-person ledger reminder (always fresh, effects-only, cache-safe) |
| `preserve_facts` silent no-op on cheap scopes | Guardrail #1 — persist to ledger as `pinned_fact` or hard error |
| Agent asserts from emptied context | Reconcile-before-asserting rule + self-model monitor |
| bash/git writes invisible | `git status` delta capture → `shell_effect` rows |
| Compaction reads as "things vanished" | One-shot re-grounding header: "evicted ≠ lost; here's what you did; `recall` for detail" |

---

## 8. Rich layer (forward) — Soham's structured store, fully built

Replace flat `memory.md` with a typed, entity-organized store (sibling JSON/SQLite in the project dir):

- **Entities:** `Workstream` (user request + phase + status) → `Artifact` (file note linked to its ledger row + git diff) → `Decision` → `Finding` → `OpenThread`.
- **Authorship is mostly machine, lightly agent:** the runtime seeds `Artifact` entries from the ledger (no LLM); the summarizer fills semantic notes; the agent adds only intent/decisions it alone knows. *This avoids the empty-`session_memory` failure — the store populates itself; the agent garnishes.*
- **D. Compaction-as-Projection:** one summarizer call, **two outputs** — the prose continuity summary (shorter; detail now lives in the store) **and** structured upserts, **seeded with the effect-ledger delta** so the model cannot omit "created widget_tools.py." Upserts validated against the ledger. Hook at `compactor.compact` (`freyja_bridge.py:3719`) / `force_compact` (`:4094`), reusing the `on_summarizer_call` thread.
- Injection then formats *exactly the active workstream + its artifacts + open threads*, not a blob.

## 9. Frontier (forward) — bells & whistles

1. **Memory-as-graph, bootstrapped from provenance** (not LLM entity extraction): every entity links to the ledger rows that evidence it + archive spans + related entities. Team's B2/Mem0g bet, grounded in machine-captured provenance.
2. **Trust tiers surfaced** (GROUND TRUTH vs YOUR NOTES vs SUMMARY), with instruction to trust in that order.
3. **Diff-aware artifact memory** — intent linked to the real git diff ("by diff").
4. **C. Searchable Archive + `recall`** over `raw_messages.jsonl` + `compactions.jsonl`, intra- and **cross-session** (extend list-sessions → "resume a workstream").
5. **Compaction receipts** — "evicted turns 5–86; N effects + M entries survived, all recall-able. Nothing lost, only moved."
6. **Sleep-time curation** (team's E') — background subagent dedups/promotes/tightens the store.
7. **Training corpus** — (transcript-span → structured-delta) pairs + `forgetting_detected` labels feed the team's Q3 trained-specialist bet.
8. **Budget-tiered injection** — always inject effects + active workstream; the rest on-demand.

## 10. Sequencing

- **Milestone 1 — Spine (the guarantee):** §6.1–6.5. Effect/observation ledger + git-delta capture; standing + one-shot injection; `preserve_facts` fix + reconcile rule + self-model monitor. *Closes the incident class. No engine/bridge surgery.*
- **Milestone 2 — Rich (Soham #3):** structured working memory + compaction-as-projection + `recall`. *= team's Letta-style B1 tier, bootstrapped from the ledger.*
- **Milestone 3 — Frontier:** graph/provenance, diff-aware memory, sleep-time curation, cross-session resume, training-corpus generation. *= where B2/Mem0g/E'/Dataset-1 want to go, now on a ground-truth foundation.*

## 11. Risks & open questions

- **Authorship tax (top risk):** if maintaining the structured store is per-turn homework, the agent skips it → empty `session_memory` again. *Mitigation: runtime/summarizer populate ~80%; the agent adds only intent.*
- **Ledger signal:** ledger everything and you recreate dilution *inside* the ledger. *The effect/observation split + caps + tiered injection are the discipline.*
- **Trust calibration:** "trust the ledger over your memory" is powerful but the ledger can be stale (file later deleted) or partial (bash gap). *Tiers + explicit "uncaptured" line bound it.*
- **Cost & cache:** fold the structured update into the *existing* summarizer call (one call, two outputs); seed deterministically from the ledger; inject only via cache-safe tail-append.
- **Open:** debounce floor `K` for the standing reminder? Per-subagent ledgers roll up to the parent how? `recall` semantic index — eager vs lazy, intra- vs cross-session cost?

## 12.5 Implementation status (shipped on `feat/grounded-memory`)

The **spine + recall + summary hardening** is implemented and unit-tested
(37 tests; full suite shows no new regressions). What landed:

**New:**
- `bridge/session_ledger.py` — `SessionLedger` (effect/observation split,
  creator attribution, disk hydration on resume, digest, first-person reminder
  renderer, negative-claim detector). Pure helpers are unit-tested.
- `bridge/tools/recall_tool.py` — `recall` over `raw_messages.jsonl`
  (search/timeline, intra- and cross-session).
- Tests: `tests/test_session_ledger.py`, `test_recall_tool.py`,
  `test_compaction_ground_truth.py`, `test_ledger_reminder_producer.py`.

**Wired:**
- Capture at the tool-result hook (`freyja_bridge.py` `traced_execute`), with
  `creator_id` = the acting session.
- Standing first-person write-ledger `<system-reminder>` + one-shot
  "just compacted" framing, via `_build_extra_system_reminders` on the existing
  `get_extra_system_reminders` seam (cache-friendly tail-append). Debounced on
  content-hash with a turn floor; filtered to the session's own `creator_id`.
- `preserve_facts` silent-no-op fixed: cheap scopes persist facts to the ledger
  (`on_pinned_facts`); the false guarantee in `_CONTEXT_DISCIPLINE_BLOCK` was
  corrected; a reconcile-before-asserting rule + `recall` pointer were added.
- Self-model monitor (forgetting detector) → one-shot correction +
  `forgetting_detected` telemetry.
- Summarizer seeded with the ledger ground truth (`compact(ground_truth=…)`);
  first-person "Actions I performed" section + never-drop rule in the summary
  templates; inject reframed to first person (marker prefix preserved).

**Decisions / scope:**
- Observations: only *research* (`web_search`/`web_fetch`/`fetch_url`) is
  persisted; local reads/greps/non-mutating bash are left to `recall` to keep
  the ledger high-signal.
- Bash effects are captured by **command-pattern** classification (high-recall);
  the `git status --porcelain` before/after delta (exact file attribution) is a
  noted future upgrade.

**Known follow-ups (not yet done):**
- Sub-agents share the parent's ledger object; their writes are attributed by
  `creator_id` and filtered OUT of the parent's reminder, but a sub-agent does
  not yet receive its own write-ledger reminder (its runner isn't wired with
  `get_extra_system_reminders`). Fine for the "parent forgot its own work" case.
- The full **Structured Working Memory** (Milestone 2: entity store +
  compaction-as-projection) and the **frontier** items are not built.
- Renderer surfaces (showing the ledger / a recall panel in the UI) are backend-
  only so far.

## 12.6 Second pass — B–D follow-ups + Milestone 2a (shipped)

Closing the gaps from §12.5's "known follow-ups":

- **B1** forced/`/compact` compaction now seeded with the ledger ground truth
  (runner `get_compaction_ground_truth` callback; both forced paths).
- **B2** sub-agents now get their own write-ledger reminder + summarizer seed,
  filtered to the child's `creator_id` (shared ledger object, per-child
  debounce state in `_run_child`).
- **B3** ledger backfills from `manifest.jsonl` on `ensure()` so resumed
  pre-ledger sessions aren't blank.
- **C4** mutating bash captured by `git status --porcelain` before/after delta
  (exact files), command-pattern as fallback. *(C5 — write-aware pruning —
  intentionally not done; the 1.5/10 finding.)*
- **C6** pinned facts creator-filtered, consistent with effects.
- **D** observability: `forgetting_detected` + `compaction_receipt` emitted as
  `system_event`s (render in the existing activity feed / inline chips); a
  main-process `readActionLedger` reader + `session:actionLedger` IPC + preload
  method; an `ActionLedgerSection` renderer panel in the ActivityPanel.
  *(Renderer validated by `tsc --noEmit`; not yet live-smoke-tested.)*
- **Milestone 2a**: `bridge/working_memory.py` (entity store + `render()`),
  `bridge/tools/working_memory_tool.py` (`read`/`upsert`/`resolve`), registry +
  bridge wiring, ledger-seeded reads. Tests in `tests/test_working_memory.py`.

## 12.65 Third pass — Milestone 2b + hardening (shipped)

**Milestone 2b — compaction-as-projection (done):**
- The summarizer prompts (both fresh + iterative) now ask for an optional
  `<working_memory>[...]</working_memory>` JSON block after the summary.
  `engine/compaction.py` parses it (`_parse_working_memory_block`, defensive)
  and hands the upserts to an `on_working_memory_upserts` callback threaded
  through `compact()`.
- `bridge/working_memory.py:apply_working_memory_upserts` resolves/creates
  workstreams by title, links children, and dedups by primary value so
  re-emitting the same entry across rounds updates rather than duplicates.
- Wired into **all** LLM compaction paths: runtime/overflow (`runner`
  `apply_working_memory_upserts`), `/compact` + force_compact, **and the
  agent-driven cooperative `summarize_context`** (the primary path — via a new
  `SummarizeContextTool` callback).
- `render()` joins post-compaction re-grounding: a one-shot
  `<working_memory>` injection fires on the turn after a runtime-forced
  compaction (gated on `compaction_count`, which bumps only on
  runtime/forced/`force_compact` paths — exactly when the agent didn't
  initiate the compaction itself). Tests in `tests/test_working_memory.py` +
  `tests/test_compaction_ground_truth.py`.

**Hardening (this pass):**
- **Critical bug fixed:** the new `<working_memory>` prompt examples contained
  literal `{}` which `str.format()` mis-read as fields — would have broken
  *all* LLM compaction. Escaped (`{{`/`}}`); tests assert both prompts format.
- **Thread-safety:** `SessionLedger._rows` and `WorkingMemory._entities` were
  mutated outside their locks while read methods iterated them — a real
  `RuntimeError: list changed size during iteration` under the
  worker-thread compaction (force_compact's `to_thread`) racing the event
  loop's `render()`. Reads now snapshot under the lock; mutations are locked;
  `WorkingMemory` uses an RLock for the nested `upsert`→`_save`. Proven with a
  7-thread stress test.
- **Git over-attribution:** under parallel bash, two mutating commands in the
  same repo would cross-attribute. Added a per-repo in-flight guard
  (`begin/end_git_capture`); concurrent commands fall back to command-only
  recording. Claim is released on the tool-error path too (no leak).
- **render bug:** `render_working_memory` accepted `ledger_effects` but never
  rendered them — the `working_memory(read)` surface silently omitted files.
  Fixed.
- C4 git snapshot now gated on `classify_bash_command == "effect"` (no
  subprocess for read-only bash); `_ledger_ground_truth` pinned-facts now
  creator-filtered for consistency.

**Still not done:** sub-agent compaction doesn't project into the parent's
working memory (sub-agents get the ledger reminder + ground-truth seed but not
the WM projection — keeps the parent's structured memory clean); the renderer
WM panel / recall UI (2c observability); provenance graph + sleep-time
curation (2c). Live Electron smoke test of the panels still pending.

## 12.7 Milestone 2 — Structured Working Memory (scope)

The ledger (Milestone 1) is *machine-authored ground truth of what happened*.
Milestone 2 adds the *semantic, agent-authored* layer: what the work **means**,
organized by entity, durable and queryable — your idea #3.

**Entity model** (`bridge/working_memory.py`, one JSON doc per session at
`<project_dir>/working_memory.json`):

| Entity | Fields | Authored by |
|---|---|---|
| `workstream` | title, request, status (active/paused/done), phase | agent (the top-level "what we're doing") |
| `decision` | title, rationale, workstreamId | agent |
| `finding` | text, source, workstreamId | agent |
| `open_thread` | text, status (open/resolved), workstreamId | agent |
| `artifact_note` | path, note, workstreamId | agent note layered over a **ledger** artifact |

**Authorship split (avoids the empty-`session_memory` failure):** the runtime
seeds `artifact` facts straight from the ledger (no LLM); the agent adds only
the high-level intent/decisions/findings it alone knows; the summarizer (Phase 2b)
maintains the rest as a compaction side-effect.

**Surfaces:**
- Agent tool `working_memory` (actions: `read` / `upsert` / `resolve`) — the
  authoring surface.
- `render()` — a compact "active workstream + its artifacts + open threads"
  block for post-compaction injection (joins agent entities with ledger
  artifacts), distinct from the flat ledger reminder.

**Phases:**
- **2a (foundation, this pass):** the store + the agent tool + ledger-seeded
  artifacts + `render()` + registry/bridge wiring + tests.
- **2b (next):** compaction-as-projection — the summarizer emits prose **and**
  structured upserts in one call, seeded by the ledger and validated against it;
  `render()` output joins the post-compaction injection.
- **2c (frontier):** provenance graph (entities ↔ ledger rows ↔ archive spans),
  diff-aware artifact memory, sleep-time curation.

## 12.7b Fourth pass — 2c chunks 1 + 2 (shipped: observability + diff-aware)

The memory surfaces now have a UI, in a deliberately-crafted design language.

**Design decision (recorded):** a 7-agent judge panel scored three metaphors —
**Instrument/chronograph (31)**, **Constellation/fieldmap (28)**, **THE PRESS /
editorial broadsheet (36)**. THE PRESS won as the only metaphor that natively
fits all four surfaces (column = work-graph, morgue = recall, erratum/receipt =
the two cards, galley = diff), with THE INSTRUMENT's numeric discipline grafted
on (tabular timestamps, vernier mini-diff, "exactly one thing moves" motion).
The operator chose **The Ledger-Cards** centerpiece variant — each workstream a
discrete `.glass-raised` card, the single live one carrying the accent ring +
`.council-tile.is-running` pulse ("ink still wet"), done folded to a sage line.

**Shipped (chunk 1 — observability):**
- `src/renderer/components/memory/primitives.tsx` — six shared primitives
  (StatusPip, LeafMark, MarginMark+diff-peek, DatelineTS, SectionSlug,
  GalleyArtifact) so all four surfaces read as one language.
- `WorkingMemorySection.tsx` — the Ledger-Cards centerpiece (live pulse, paused
  dim, done fold, serif decks, loading ghost). Mounted above the action ledger.
- `RecallPanel.tsx` — "The Morgue" drawer: search + time-spine + role-marked
  clipping rows + match highlight + day boundaries. Store-backed drawer
  (`recallDrawer`/`openRecallDrawer`) so it opens from the panel and the cards.
- `MemorySystemCards.tsx` — `InlineForgetting` (erratum: struck belief over
  ledger truth) + `InlineCompactionReceipt` (sage "nothing lost" stamp). Wired
  into the `Conversation.tsx` system switch + `store.ts` inlineSystemSubtypes.
- IPC plumbing: `readWorkingMemory` + `readRecall` (`persistence.ts`),
  `session:workingMemory` / `session:recall` channels, main handlers, preload
  methods — mirroring `readActionLedger`.
- CSS: `:root` palette vars (`--accent/--ok/--warn/--danger`), `.memory-pip--live`,
  `.memory-card--live` (council-tile pulse, reduced-motion gated),
  `.memory-dash-rule`.

**Shipped (chunk 2 — diff-aware artifact memory):** the `2478` hook captures
`change_set["totals"]` + the first file's diff into an `extra` dict threaded
onto the ledger effect row (`record_from_tool(extra=…)` → `record_effect`'s
`row.update(extra)`); `working_memory.render()` appends a compact `(+N −M)` to
artifact_note rows; the panel surfaces it as the MarginMark + "▸ pull proof"
diff peek. Diff never enters the standing reminder (only `summary` is used).

**Bug caught in the craft pass:** `raw_messages.jsonl` writes `ts` in epoch
**seconds** (`time.time()`) but `relativeTime()` expects **ms** (like the ledger
/ WM, which use `time.time()*1000`) — every recall row would have read
"20000d ago". Fixed by normalizing seconds→ms in `readRecall`. Also the build's
brace-free prompt examples, the SystemEventLookupContext export, and the
store-backed recall drawer (reachable from two trees) were all handled.

**Verification:** tsc exit 0; new Python files ruff-clean (net-new ≤ 0); full
suite 240 passed / 21 pre-existing failures (unchanged). **Not yet done:** the
live Electron smoke test (needs a running app — the surfaces are validated by
tsc + code review only), and 2c chunks 3–7.

## 13 — Milestone 2c: scope + candidate strategies

The spine (Milestone 1) plus 2a/2b already **fix the forgetting bug**. 2c is
*enrichment and surfacing* — making the durable memory queryable, higher-
fidelity, self-maintaining, and visible. None of it is load-bearing for the
original failure; it is the difference between "works" and "polished + powerful."
Grounded facts that shape the strategies: ledger effect rows have **no stable
`id`** today (provenance prereq); change-set diffs already exist
(`bridge/file_changes.py` → `artifact_store.record_change_set`, with
additions/deletions/diff); a durable asyncio **scheduler** service exists
(`bridge/scheduler/`) as a host for background work; the renderer's inline
system-event switch (`Conversation.tsx:~1184`, `InlineRefusal`/`InlineGoalVerdict`
pattern) is the template for rich cards, and `forgetting_detected` /
`compaction_receipt` currently fall through to the generic chip.

### Chunk 1 — Observability / UX surfaces  *(highest value, lowest risk — build first)*
The backend emits everything; the renderer barely shows it.
- **Working-memory panel** — a `WorkingMemorySection.tsx` (sibling to the shipped
  `ActionLedgerSection`) rendering workstreams → decisions/findings/open-threads.
  *Strategy:* new main-process reader `readWorkingMemory(id)` cloning
  `readActionLedger` (reads `working_memory.json`) + IPC channel + preload method;
  a renderer component fetched on `activityTick`.
- **Recall UI** — search box → temporal results over the archive.
  *Strategy:* either a thin reader IPC over `raw_messages.jsonl` (mirror
  `loadSessionExportBundle`) or reuse the agent `recall` tool's output shape; a
  results component with turn/role/snippet rows.
- **Rich inline cards** for `forgetting_detected` (a self-model *correction*
  moment — show the divergence) and `compaction_receipt` (a *nothing-lost*
  reassurance — a ledger-stamped receipt). *Strategy:* add subtype branches to
  the `Conversation.tsx` system switch following `InlineRefusal`.
- **Live Electron smoke test** of these panels — the one validation gap across
  everything built so far; belongs here.
- *Design intent (this is a "new design vision" pass):* the information types are
  distinctive and deserve bespoke treatment — working memory is a **living work-
  graph with status** (wants a spine/branch motif, status as color+glyph, typed
  children as distinct marks), recall is **temporal retrieval** (wants a timeline/
  scrubber), the cards are **moments** in the stream (want memorable, restrained
  motion). Build a small shared primitive layer (status dot, memory node, mini-
  diff, timeline spine) so the surfaces cohere.

### Chunk 2 — Diff-aware artifact memory  *(high value, data already exists)*
Link the agent's semantic `artifact_note` to the **actual git diff**.
*Strategy:* the `2478` tool-result hook already builds a `change_set`
(additions/deletions/diff) and calls `record_change_set`; carry a compact diff
stat (+/- counts, maybe a truncated unified diff ref) onto the matching ledger
effect row and/or the `artifact_note`, so "what did I change in X and why"
returns **intent + the real diff**. Surface it in the working-memory panel as a
mini-diff. Touches `bridge/freyja_bridge.py` (hook join), `bridge/working_memory.py`
(artifact_note diff ref), and the panel.

### Chunk 3 — Provenance graph / linking  *(the queryable-graph payoff — ambitious)*
Link the three siloed stores so "show me everything about workstream X" walks
entity → ledger rows → archive spans. *Strategy:* (a) give ledger rows a stable
`id`; (b) add `evidenceRefs` (ledger ids / `toolCallId`s) to working-memory
entities; (c) teach `recall` to resolve a `toolCallId` → its archive turn. This
is the team's **B2 / Mem0g knowledge-graph bet, bootstrapped from machine
provenance** instead of LLM entity extraction (cheaper, more accurate).

### Chunk 4 — Sleep-time curation  *(team's "E'" — matters for long/autonomous sessions)*
A background pass that keeps structured memory clean: dedup entities, **promote
pending→done by checking git/ledger ground truth**, tighten verbose notes, prune
resolved threads, reconcile WM against the ledger. *Strategy:* host it in the
existing `bridge/scheduler/` service as a low-cost routine or a dedicated
subagent role; runs off the main loop.

### Chunk 5 — Sub-agent WM projection  *(a policy decision deferred from 2b)*
Sub-agent compactions get the ledger reminder + ground-truth seed but **don't
project into working memory** (keeps the parent's memory clean). *Strategy:*
decide — project into the parent's WM with sub-agent attribution, or a child-
scoped WM — then wire the child runner's compaction callback. Small once decided.

### Chunk 6 — Cross-session workstream resume  *(ties to idea #4)*
`recall` already searches cross-session. *Strategy:* add "resume workstream X from
session Y" by importing its WM entities into the current session — "continue prior
work" instead of "read old logs."

### Chunk 7 — Training-corpus harness  *(strategic, Q3+, lowest urgency)*
2b's projection already produces (transcript-span → structured-delta) pairs and
`forgetting_detected` labels. *Strategy:* an export/measurement harness for the
team's trained-compaction-specialist bet. Depends on *collecting* data first, so
it's last.

### Recommended sequencing
1 → 2 → 3, then 4 / 6 / 5 as appetite allows, 7 whenever the training work is
prioritized. **Chunks 1 + 2 are the clear next build** (visible + high-fidelity,
both grounded in data that already exists); **3 is the ambitious payoff**; **4–7
are genuine frontier** — build when a real long-session / training use-case
demands them, not speculatively.

## 12. Appendix — seam index

| Concern | Location |
|---|---|
| Tool-result capture hook | `bridge/freyja_bridge.py:2478` (`MUTATING_FILE_TOOL_NAMES` gate) |
| Artifact store | `bridge/artifact_store.py` (`record_file`, `latest_by_path`, `manifest.jsonl`) |
| Store instantiation on bridge | `bridge/freyja_bridge.py:2634` / `:2821` / `.ensure()` `:2900` |
| Reminder producer wiring | `bridge/freyja_bridge.py:3393` → `_build_stale_task_reminders` `:8100` |
| Reminder consume + tail-append | `engine/runner.py:1540` (`_gather_extra_reminders`) / `:1555` (`_augment_messages_with_pressure_note`) |
| One-shot advisory pattern | `engine/runner.py:1619`–`1665` (`mark_channel3_crossing` / `consume_channel3_advisory`) |
| Prune chokepoint | `engine/runner.py:812` (`_handle_context_pressure`) → `engine/session.py:350` (`prune_old_tool_results`) |
| Cheap summarize scopes | `bridge/tools/summarize_context_tool.py:539` / `:590` |
| `preserve_facts` silent no-op | `bridge/tools/summarize_context_tool.py:469` (`_repair_preserve_facts`); false doc `freyja_bridge.py:1820` |
| LLM summary + injection | `engine/compaction.py` (`SummaryCompaction`) / `engine/session.py:302` (`get_messages` inject) |
| Compaction call sites | `bridge/freyja_bridge.py:3719` (tool dispatch) / `:4094` (`force_compact`) |
| Discipline prompt block | `bridge/freyja_bridge.py:1769` (`_CONTEXT_DISCIPLINE_BLOCK`) |
| Archive (verbatim) | `bridge/freyja_bridge.py:8299` (`_append_raw_message_log` → `raw_messages.jsonl`) |
| Project dir | `bridge/project_paths.py:17` (`project_output_dir`) |
