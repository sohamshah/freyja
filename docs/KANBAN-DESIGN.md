# Kanban Coordination — Current State and Build Plan

> Status: Living design note. Last updated 2026-05-10.
> Owner: coordination-strategy stream.
> Related: `docs/GOAL-TASK-COORDINATION-PLAN.md`.

## Implementation status (2026-05-10)

The bridge half of every strategic move has shipped. The dashboard
half is mostly still on paper: only the column-vocabulary rename
landed with the status-machine work. Concretely:

**Shipped on the bridge:**

- **B — mission root card.** First user message materializes
  `card_001` (status `running`, `metadata.role = "mission_root"`).
  Subsequent parent-created cards auto-adopt the root as a parent;
  the root is gating-transparent so children start in `ready`.
- **D — two-phase creation + specifier.** New `specifier` agent
  profile with a kanban-only tool surface. Spec fields
  (`definition_of_done`, `references`, `verify_with`,
  `token_budget`) live under `metadata` and surface at the top of
  card payloads as `spec` so workers don't have to dig.
- **F — multi-end-state + circuit breaker.** Status vocabulary is
  now `triage / ready / running / done_unverified / done / blocked /
  crashed / timed_out / failed / cancelled`. `consecutive_failures`
  trips the breaker at 3 — the next failure-class transition is
  rewritten to `failed` and the dispatcher locks the card out.
- **E — worker tool surface slim-down.** Child `KanbanTool` built
  with `owned_task_id` advertises a narrower enum (no `create`,
  `claim`, `link`, `unblock`) and refuses mutations against any
  card id other than the worker's own.
- **A + C — auto-dispatch + verifier seal.** Background asyncio
  loop on `_BridgeSession`, ticking every 30s plus after every
  parent turn. Three dispatch lanes (verifier > worker >
  specifier) capped by `max_parallel = 3`. The `verify` agent
  profile now lists `kanban` in its tool surface and signs off via
  `done_unverified → done` or rejects via `done_unverified →
  running`.
- **G — persistence.** Append-only JSONL journal at
  `~/.freyja/sessions/{id}.kanban.jsonl`. Every board mutation
  writes a line; session restore replays the log before any tool
  call lands.
- **Stale detector + reclaim sweep.** The dispatcher tick flags
  silent running cards via `kanban_stale` after 180s and reclaims
  them via `crashed` at 600s. Worker heartbeats refresh `updated_at`
  so a slow-but-alive worker stays out of those windows.

**Control surfaces shipped:** `/autopilot on|off` slash command;
`kanban_autopilot` IPC. The bridge silently no-ops when the session
isn't in kanban mode.

**Pending — dashboard work.** Most of what the "Visualizing all
this" section below describes is still pending. The renderer reads
the new vocab into the existing columns but doesn't differentiate
the new lanes, doesn't show the mission anchor as a cover card, and
has no autopilot strip or dispatch ticker. The detail panel doesn't
render the `spec` fields or `consecutive_failures` retry counter.
See `## Dashboard build plan (next)` near the bottom for the
sequenced shipping plan.

**Pending — composition.** Kanban-with-goal-loop composition and
cross-session board sharing are still open questions (see Open
Questions). Not blocking dashboard work.

## Context

Freyja exposes four sub-agent coordination strategies, picked per session
(`bus`, `isolated`, `kanban`, `goal`). This document covers `kanban` only:
what it does today, the latent issues a deep read of the code surfaced,
and the design moves we should make next. Smaller punch-list fixes are
filed as tasks at the bottom.

## What kanban does today

### Architecture sketch

```
                ┌────────────────────────────┐
                │  _BridgeSession            │
                │  (one per active session)  │
                │  coordination_strategy =   │
                │      "kanban"              │
                │  kanban_board: Session     │
                │      KanbanBoard           │  ← in-memory, lock-guarded
                └─────────────┬──────────────┘
                              │ injected at registry build time
                              ▼
        ┌──────────────────────────────────────────┐
        │ build_desktop_registry(kanban_board=…)   │
        │                                          │
        │  parent tool list includes:              │
        │   - kanban (HOT) bound to parent actor   │
        │   - sub_agent (HOT)                      │
        └────────────┬─────────────────────────────┘
                     │ sub_agent(... kanban_task_id="card_017")
                     ▼
        ┌──────────────────────────────────────────┐
        │ SubAgentTool._run_child                  │
        │   builds child tool registry             │
        │   strips "kanban" from inheritance       │
        │   re-registers KanbanTool with the       │
        │     child's actor_id                     │
        │   prepends coordination_guidance         │
        │   _mark_kanban_running(record)           │
        │   → status="running", comment="started"  │
        │   ... child runs ...                     │
        │   _mark_kanban_terminal(record, …)       │
        │   → status=done/blocked/cancelled        │
        │     summary=final text                   │
        │     artifacts=record.created_files       │
        └──────────────────────────────────────────┘
```

### Tool surface

`KanbanTool` is HOT-tier (schema always loaded — kanban is *the*
coordination surface in this mode, so we don't make the agent call
`tool_search` for it). One tool, one `action` enum:

| Action      | What it does                                              |
| ----------- | --------------------------------------------------------- |
| `list`      | Snapshot of all cards (no history payload)                |
| `create`    | New card with optional parents/priority/assignee          |
| `show`      | Full card including comments + events tail                |
| `claim`     | Sets status=running, fills assignee (no race protection)  |
| `update`    | Bulk-edit status / assignee / body / summary / artifacts  |
| `comment`   | Append a progress note                                    |
| `complete`  | status=done, optional `created_cards` check               |
| `block`     | status=blocked, comment required                          |
| `heartbeat` | Synthetic "still alive" comment + metadata stamp          |
| `link`      | Add a parents/children edge with cycle protection         |

Cards have id (`card_001`-style monotonic), title, body, assignee,
status, priority (0–5), parents/children, started/completed timestamps,
summary, result, artifacts (file paths), metadata, and append-only
`comments` + `events`. Status auto-progresses `todo → ready` when all
parents reach `done`.

### Sub-agent integration

`sub_agent(label, task, kanban_task_id="card_017")` is the chosen
ergonomics. The tool:

1. Stamps `record.kanban_task_id = "card_017"`.
2. Emits `subagent_spawn` + `session_spawned` (the dashboard turns these
   into a child session row).
3. Strips `kanban` from the inherited tool set; registers a fresh
   `KanbanTool` with the child's `actor_id` so attribution is correct.
4. Calls `_mark_kanban_running(record)` → `update(status="running",
   assignee=child.label, comment="Sub-agent started")`.
5. Runs the child. On terminal state calls `_mark_kanban_terminal` →
   `update(status=done|blocked|cancelled, summary=<final text>,
   artifacts=list(record.created_files))`.
6. The coordination prompt suffix tells the child: "Your assignment is
   `card_017`. Call `show` first. Heartbeat/comment during long work.
   `complete` with verified artifacts when done. `block` with exact
   blocker."

### Event flow → renderer

Every kanban action emits a `system_event` whose `subtype` is
`kanban_<action>` and whose `details.task` (or `details.tasks` for
`list`) is the full `task.to_dict()` payload. The renderer's
`collectKanbanCards()` is an event-sourced fold: walk system events
oldest→newest, upsert by id. The bridge owns truth; the renderer is a
projection. This is a quietly important property — the board view
survives if we ever lose the in-memory board, as long as we still have
the event log.

## What works well

- **HOT tier for the coordination surface.** No tool-search round-trip
  before any board action.
- **Symmetric parent/child use.** Same `KanbanTool` class, different
  `actor_id` — events attribute correctly.
- **Topological gating with auto-promote.** Children flip `todo → ready`
  the moment all parents reach `done`; cycles are blocked at link time.
- **Verified artifact linkage on completion.** `_mark_kanban_terminal`
  pulls artifact paths from `artifact_store.paths_for_creator(record.id)`,
  not from the agent's say-so. The card's `artifacts` field is grounded
  in filesystem reality.
- **Event-sourced renderer.** The dashboard rebuilds card state by
  folding `kanban_*` system events — i.e. the persistence story for the
  board is the same as for system events generally.

## Latent issues found on close reading

Five small things worth knowing before we design bigger moves.

### 1. `claim` is dead code; there is zero race protection

`KanbanTool.action="claim"` exists, accepts an `assignee`, and calls
`update(status="running", assignee=…, comment="Claimed card")`. But:

- It does **not** check current status or current assignee. Two callers
  both succeed.
- The spawn pipeline (`_mark_kanban_running`) bypasses `claim` and calls
  `update(status="running", …)` directly.

Net: with multiple background sub-agents auto-dispatched to `ready`
cards, two agents will eventually pick the same card and both succeed.
The "second" one silently overwrites the first's assignee/comment.

### 2. Cancelled parents trap children in `todo` forever

`_promote_unblocked_children` is only triggered when a card transitions
to `done`. If a parent goes to `cancelled`, its `todo` children stay
`todo` indefinitely — the system never decides what to do. Either we
need an explicit policy (cascade-cancel, auto-promote, or surface as a
blocker for the parent agent to resolve) or this is a latent stall.

### 3. `heartbeat` doesn't actually do anything useful

The action appends a comment ("Heartbeat") and stamps
`metadata.heartbeatAt`. Nothing watches that field. There's no stale
detector that fires when a `running` card hasn't had an event in N
minutes. The agent has no reason to call `heartbeat` and nothing
notices when it doesn't. It's vestigial.

### 4. `priority` is vestigial too

Stored, range-checked (0–5), sorted-by inside `list`. Never affects
scheduling, never highlighted in the dashboard, never read by the spawn
pipeline. Either wire it into auto-dispatch (the obvious place) or drop
it from the schema.

### 5. State transitions aren't validated

`update(status=…)` accepts any of the six statuses regardless of where
the card is now. `done → running` and `cancelled → ready` are both
silently allowed. Today this is fine because only sub-agents drive
state — but the moment a verifier loop or auto-dispatcher is in the
mix, accidental backflow becomes a real risk.

## Strategic moves we want to build

The headline shift: kanban mode becomes **queued, board-driven
autonomous work**, not a paper trail the parent agent has to keep
manually pushing forward. Six pieces add up to that picture.

### A. Auto-dispatch — the board runs the work

> Status: shipped 2026-05-10 (commit 98b8c36).

A background asyncio loop on `_BridgeSession`, started when the
session's `coordination_strategy == "kanban"`, periodically scans the
board for `ready` cards with an assignee and spawns sub-agents for
them. The shape mirrors `_maybe_continue_goal` / `_judge_goal` in
`bridge/freyja_bridge.py` — the goal-loop pattern is the right
template (turn budget, queued-message preemption, chat-visible system
events).

```
every N seconds (and after each parent turn):
  if auto_dispatch_enabled and no queued user messages:
    for card in board.list() where status == "ready"
                                and assignee is set
                                and not already running:
        if running_subagents < max_parallel:
            spawn sub_agent(kanban_task_id=card.id,
                            agent_type=card.assignee)
            # spawn pipeline flips card to "running" before the
            # worker ever sees it
```

The parent's job collapses to *planning* and *reviewing*. State
transitions become a single-owner story: cards go to `running`
because the dispatcher decided to spawn for them, full stop.

Worker tool surface follows the same single-owner rule: workers don't
need a `claim` tool — they're already assigned when the dispatcher
spawns them. We slim the child `KanbanTool` accordingly (see Move E
below). The parent and dispatcher keep the full surface; the parent
can still manually claim a card it wants to work itself.

Decisions baked in for v1:

- **Default off, opt-in per session** via a dashboard toggle. Auto-
  runs burn tokens; nobody should be surprised.
- **`max_parallel = 3`** at the session level, on top of the existing
  `MAX_ACTIVE_SUBAGENTS = 30` global ceiling.
- **`blocked` end state pauses dispatch on that card**. Parent gets a
  chat-visible event and decides. No auto-retry until the parent
  unblocks.
- **Dispatch tick of 30–60 seconds.** Trade-off between latency and
  pressure on the runner; goal-loop's after-turn cadence is the right
  default plus an idle sweep on a timer.

### B. Mission root card — the user's intent has an anchor

> Status: shipped 2026-05-10 (commit 2e6875c).

When a session enters kanban mode, auto-create `card_001` from the
user's first message: title = first ~80 chars, body = full message,
assignee = parent, status = `running`. Every subsequent card created
during that session links to `card_001` as a parent unless explicitly
detached.

Why this matters:

- **Dashboard finally shows the mission top-down** rather than a flat
  list of cards.
- **Closing `card_001`** becomes the session's natural completion
  event — useful if/when goal-loop and kanban compose.
- **The parent can be `block`-ed too**, which is the foundation for
  user-input gating ("blocked: pick A or B before we go further").
- **Cross-restart anchor.** Pairs with Move G (persistence) — the
  root card is what makes "resume this mission tomorrow" coherent.

### C. Verifier — completion is a promotion, not a self-declaration

> Status: shipped 2026-05-10 (commit 98b8c36, paired with A).
> Opt-in routing added 2026-05-11: cards carry a
> `requires_verification` flag set by the parent (or specifier).
> `complete` routes to `done_unverified` only when the flag is true;
> otherwise it seals directly to `done`. Quick lookups, image gen,
> ambiguous tasks, or anything cheap to redo skip the verifier by
> default — the bar for flipping the flag is "is a second pair of
> eyes worth the extra spawn cost here?"

Today the worker calling `complete` is treated as ground truth. We
know that's wrong often enough that a verification gate pays for
itself.

The shape: introduce `done_unverified` as a status the worker
transitions to (via `complete`). The dispatcher auto-spawns a
`verify` profile agent against `done_unverified` cards. The verifier
either:

- **Signs off** → `done`, children auto-promote per the usual
  topological rules; or
- **Rejects** → card goes back to `running` with the same assignee,
  and the verifier's structured feedback is appended to the card
  body so the worker's next turn sees the critique.

Why a status flag (`done_unverified` → `done`) instead of a separate
`review` state: it keeps the state machine smaller and lets the
verifier dispatch through the same auto-dispatch loop as ordinary
workers, without a special-case "review queue."

The verifier's prompt is grounded in the card's `definition_of_done`
(see Move D). Without that grounding, verifiers degenerate to "lgtm"
within a few cards.

### D. Two-phase creation — `triage` → `ready` via a specifier

> Status: shipped 2026-05-10 (commit 57761d6).

Card bodies today are freeform strings. Workers improvise and lose
tokens reverse-engineering what the parent meant. Two changes:

1. **A new status `triage`** ahead of `ready`. A card created with
   just a title lands in `triage`. Cards in `triage` are not
   dispatched.

2. **A `specifier` agent profile** that takes a `triage` card and
   fills in structured fields, then promotes the card to `ready`.
   Triggered either by the parent's explicit request or auto-
   dispatched on `triage` cards (like any other profile).

The structured fields live on the card directly:

| Field                | Shape                          | Purpose                                                       |
| -------------------- | ------------------------------ | ------------------------------------------------------------- |
| `definition_of_done` | `string[]` of conditions       | The verifier walks this list                                  |
| `references`         | `{files, findings, cards}`     | Pre-loaded into the worker's first turn                       |
| `verify_with`        | optional shell command         | The verifier runs this and pastes output into the card        |
| `token_budget`       | int hint                       | Worker self-paces; circuit-breaker (Move F) treats this as a budget |

Together with `show` inlining parent context (Task #14), the worker
opens a card and has everything it needs. Fewer detours, fewer wasted
spawns.

### E. Worker tool surface slim-down + ownership gate

> Status: shipped 2026-05-10 (commit 989a143).

When the dispatcher spawns a worker, the child `KanbanTool`
registered for it has a *narrower* surface than the parent's. Two
specific changes:

1. **No `claim`** — the worker is already assigned. `claim` exists
   on the parent's tool surface (for cards the parent wants to work
   itself); workers don't see it. This kills an entire class of
   races without needing CAS semantics on every action.

2. **Ownership gate on terminal actions.** A worker calling
   `complete` / `block` / `heartbeat` / `update` against any card id
   that isn't its assigned `kanban_task_id` is rejected with a
   structured error. Models regularly hallucinate ids they saw
   mentioned in context; the gate catches that before the
   hallucinated id mutates the board.

`create` and `link` are also gated by default — workers shouldn't be
restructuring the board mid-task. We may relax this later for
explicit decomposition profiles, but the default is closed.

### F. Multi-end-state status table + circuit breaker

> Status: shipped 2026-05-10 (commit 989a143).

Today the end-state vocabulary is `done` and `blocked` (and a
catch-all `cancelled`). Under autonomy that's not enough — a stuck
worker, an exception, and a "needs human" pause all collapse to
`blocked` and we lose the ability to react differently. Split into:

| End state    | Meaning                                                       | Dispatcher behaviour                                  |
| ------------ | ------------------------------------------------------------- | ----------------------------------------------------- |
| `done`       | Verified complete                                             | Promote children, no further action                   |
| `blocked`    | Waiting on user input or external decision                    | Surface to parent; no auto-retry                      |
| `crashed`    | Worker exited unclean (exception, runner failure)             | Increment `consecutive_failures`; eligible for reclaim |
| `timed_out`  | Exceeded `token_budget` or wallclock budget                   | Increment `consecutive_failures`; eligible for reclaim |
| `failed`     | Circuit breaker tripped after repeated retries                | Stop spawning. Surface to parent.                     |
| `cancelled`  | Explicitly stopped (user or parent)                           | No promotion, no retry                                |

The circuit breaker is `consecutive_failures` on the card. Crash,
timeout, or spawn failure increments it; a successful run resets it.
Past a threshold (default 3) the card goes to `failed`, the
dispatcher stops spawning for it, and the parent gets a chat-visible
event. This is the bound that prevents flapping workers from burning
tokens forever — without it, auto-dispatch is dangerous.

### G. Persistence — boards survive bridge restarts

> Status: shipped 2026-05-10 (commit f48129a).

The board today is in-memory on `_BridgeSession` and dies with the
process. Long missions deserve to outlive a bridge restart.

The renderer already does event-sourced state reconstruction —
`collectKanbanCards` folds `kanban_*` system events into card state.
Mirror that on the bridge side: an append-only JSONL log at
`~/.freyja/sessions/<id>/kanban.jsonl`, written on every board
action. On session restore, replay the log to rebuild the in-memory
board before any tool runs.

Two consequences:

- "Resume this mission tomorrow" is coherent: the root card is the
  durable anchor (Move B); the log replays the rest.
- The log is the audit trail. Every state transition is recorded
  with actor, timestamp, and full delta.

### Sequencing — what depends on what

Some moves only make sense once others are in. A clean order:

1. **B (root card)** — half a day, no dependencies. Ships first.
2. **D (two-phase creation + structured body)** — independent of the
   others, makes A and C dramatically better. Ships before A/C.
3. **F (multi-end-state + circuit breaker)** — a prereq for A. The
   circuit breaker has to exist *before* auto-dispatch starts
   spawning, or the first flapping card eats a budget.
4. **E (worker surface slim-down + ownership gate)** — another A
   prereq. Stops the worker from hallucinating its way around the
   board under autonomy.
5. **A + C as one feature** — "queued kanban mode." A without C
   means faulty `complete` calls cascade into children that inherit
   the breakage; C without A is an unused status. Build together.
6. **G (persistence)** — independent in implementation, dramatically
   more valuable once missions are long enough that restarts hurt.
   Ship alongside or just after A.

## Smaller fixes worth doing (tracked as tasks)

These are the punch-list items from the close read. Each is filed as
a separate task; none of them block the strategic moves above, and a
few are prereqs that fall out of them.

| # | Fix                                                                         | Slot                       | Status   |
| - | --------------------------------------------------------------------------- | -------------------------- | -------- |
| 5 | Race-safe `claim` — dispatcher-owned, single-owner state transition         | Move A prereq              | ✓ shipped (effectively via E + A) |
| 6 | Cancelled-parent promotion policy — close the "todo stuck forever" stall    | Independent                | ✓ shipped (8b74501) |
| 7 | Stale running-card detector → `kanban_stale` event                          | Pairs with Move F          | ✓ shipped (b12594d) |
| 8 | `digest` action returning `{stuck, in_review, newly_unblocked, …}`          | Independent                | ✓ shipped (8b74501) |
| 9 | Cap `events` and `comments` to a rolling window + counters                  | Independent                | ✓ shipped (8b74501) |
| 10 | Persist board state via event-sourced JSONL                                 | Move G in practice         | ✓ shipped (f48129a) |
| 11 | Wire `priority` into list-order, then auto-dispatch order                   | Independent / Move A       | ✓ shipped (8b74501) |
| 12 | Heartbeats as liveness signal for the dispatcher                            | Pairs with #17             | ✓ shipped (b12594d) |
| 13 | Validate status transitions — block illegal moves, support new end states  | Move F prereq              | ✓ shipped (563b4d4) |
| 14 | `show` returns parent summaries + artifacts inline                          | Independent, helps Move C  | ✓ shipped (8b74501) |
| 15 | Worker `KanbanTool` slim-down + terminal-action ownership gate              | Move E in practice         | ✓ shipped (989a143) |
| 16 | `consecutive_failures` counter + circuit-breaker `failed` state             | Move F in practice         | ✓ shipped (989a143) |
| 17 | Stale-card reclaim path — demote stuck cards, increment failure counter    | Move F in practice         | ✓ shipped (b12594d) |

## What we're deliberately not building right now

These are real design alternatives we considered and pushed off.
Recording them so we don't have the same conversation twice.

- **Subprocess workers.** Sub-agents will stay in-process. Subprocess
  isolation buys durability against parent crash, but the price is
  process-spawn overhead, IPC for tool calls, and a much harder UX
  for streaming the sub-agent's output back into the renderer's
  session view. Our session-per-process boundary already gives us
  the right unit of isolation.

- **SQLite as the board substrate.** Event-sourced JSONL gives us
  the same correctness properties for our single-process workloads
  and pairs with the renderer's existing fold pattern. A SQL board
  is the right call once boards span processes; until then it's
  premature.

- **Multiple named boards per session.** One session, one board.
  "Cross-session boards" is a real future question — see Open
  Questions — but we won't build it until we know what session-
  scoped looks like under load.

- **External dispatcher daemon.** The dispatcher runs as an asyncio
  task on `_BridgeSession`, exactly like the goal-loop continuation
  task. We don't need a process the user has to start separately.

- **Cross-channel notifications.** The renderer is the only channel,
  and `kanban_*` system events already flow there. If we ever add
  bot integrations, we can revisit.

- **Auto-cascading cancellation.** When `card_017` is cancelled, we
  *don't* automatically cancel its children. We emit a
  `kanban_orphan` event for each affected child and let the parent
  decide. The default of "preserve work, ask before destroying" is
  almost always right.

## Visualizing all this — the mission dashboard

The board view today is decent but designed for a manual orchestration
model where the parent agent does the dispatching. The strategic moves
above shift the centre of gravity to "the board runs the work, the user
supervises." The dashboard has to change shape to match — both to
surface new state (autopilot, verifier, end-state palette) and to make
existing state usable under autonomy.

### What the current view does well

- Two tabs, sensibly split: **Overview** (`KanbanHealthView`) as a
  metric-ribbon + compact board, **Swarm** (`KanbanAgentsView`) as a
  full board + task detail panel + agent lanes + event feed.
- Per-card detail panel with prev/next nav, dependency chips, activity
  list, agent status with a tool-activity strip.
- Paper aesthetic on `ready` cards distinguishes "plan" cards from
  in-flight work.
- Event-sourced state: `collectKanbanCards` folds `kanban_*` system
  events into card state, so the dashboard naturally tracks whatever
  the bridge emits without bespoke wiring per action.

### What's wrong or missing

1. **The mission has no anchor.** The board reads as a flat list of
   cards. There's no "this is what the user asked for, everything else
   descends from it." Until Move B lands the board isn't actually
   showing a mission, it's showing tickets.

2. **All non-green end states collapse.** Today crashes show up as
   `blocked` with an exception in the comment. Under Move F we'll have
   `crashed`, `timed_out`, `failed`, each with a different operator
   response. They cannot look the same.

3. **No autopilot state slot.** Auto-dispatch (Move A) is a background
   loop. The user needs to know it's running, what its concurrency
   budget is, what it just decided, and what it's about to do. The
   current UI has nowhere to put any of this.

4. **No board-level activity stream.** The event feed exists but it's
   per-card-aggregate. Under autonomy the user mostly wants
   "dispatcher just spawned card_017", "verifier rejected card_023",
   "card_014 reclaimed (heartbeat stale)" — board-level events, not
   per-card comments.

5. **Quiet lanes are second-class.** Empty columns get demoted to a
   pill bar at the top. That trick stops working when we go from 6
   statuses to 9 — there'll be more pills than columns most of the
   time. Need to keep the state-machine map visible without dedicating
   a column to every empty lane.

6. **Card selection is asymmetric.** Cards on the swarm tab open the
   detail panel; cards on the overview tab don't. Either both should,
   or the overview's compact board should clearly be read-only.

7. **No watch list.** Cards needing human attention (blocked, near-
   circuit-breaker, stale running, awaiting user input) are scattered
   across columns. No queued triage view of "what does the operator
   need to look at, in priority order?"

8. **Detail panel duplicates content.** Latest signal repeats what's
   in activity. Agent status shows tools that are also in the
   activity strip. Three different sections about the same recent
   events.

9. **`compact` prop on `KanbanBoard` is doing too much.** Overview's
   mini-board and Swarm's full board share a component with two
   different shapes baked in via a boolean. They should split — the
   compact summary is a different beast from the full board.

10. **No search or filter.** Past ten cards, navigating by clicking
    becomes painful. No way to filter by status, profile, or text.

### What the new layout needs

Three audiences are using the dashboard simultaneously and they need
different views:

- **Observe mode** — board running autonomously, user wants to know
  "is everything OK?" Wants high-signal summary, easy to spot
  anomalies.
- **Supervise mode** — board has blockers/failures, user is steering.
  Wants drill-into-card depth and one-click actions.
- **Plan mode** — start of a mission, building out the board, triage
  cards being expanded. Wants a working surface, not a status display.

The Overview tab serves Observe. The Swarm tab serves Supervise. Plan
mode is currently nowhere; the new triage lane on the Swarm tab can
absorb it for v1 (a separate Plan tab is a v2 question).

#### Required additions, mapped to strategic moves

All rows below are pending unless marked ✓. Marked items shipped as
part of the status-vocabulary rename in Wave 2 (commit 563b4d4) and
need no further work.

| Visualization element              | Belongs in   | Lands with move      | Status   | Notes                                                                                                          |
| ---------------------------------- | ------------ | --------------------- | -------- | -------------------------------------------------------------------------------------------------------------- |
| Mission root card "cover"          | Overview + Swarm | B                  | ☐ V1     | Always-visible card pinned above the board. User's prompt + parent's running summary. Distinct paper-folder treatment so it doesn't read as a worker card. |
| Autopilot strip                    | Overview + Swarm | A                  | ☐ V1     | Always-on header band: `AUTOPILOT ON · 2/3 slots · next tick in 24s`. Toggle to flip on/off. Reads "AUTOPILOT OFF — manual mode" when disabled. |
| Dispatch ticker                    | Overview     | A                     | ☐ V1     | Live feed of dispatcher decisions, verifier verdicts, reclaim events. Color-coded by event class. Primary surface on Overview, available as a togglable side rail on Swarm. |
| Watch list                         | Overview     | A + F                 | ☐ V3     | Cards needing human attention: blocked, near-circuit-breaker, stale running, awaiting user input. Sorted by urgency. One-click jump to card detail. |
| `triage` lane + spec-status badge  | Swarm        | D                     | ◐ V2     | Column exists (Wave 2). Still need distinct paper-card treatment + "needs spec" overlay + specifier-running spinner. |
| `done_unverified` lane             | Swarm        | C                     | ◐ V2     | Column exists (Wave 2). Need amber-tinted cards + "review pending" or "verifier: [name] (running)" label. |
| Verifier verdict badge             | Per-card     | C                     | ☐ V1     | `✓ verified by [verifier]` on `done` cards. `✗ rejected — see feedback` on cards bounced back to `running`. |
| `crashed` / `timed_out` lanes      | Swarm        | F                     | ◐ V2     | Columns exist (Wave 2) with text colors. Still need card materials (recoverable amber vs budget-exceeded peach) and retry counter prominently shown. |
| `failed` lane                      | Swarm        | F                     | ◐ V2     | Column exists (Wave 2). Still need red ring, "circuit broken" label, "force-reset" affordance. |
| Retry counter pill on card         | Per-card     | F                     | ☐ V2     | `2/3` pill on cards with `consecutive_failures > 0`. Goes red on the last attempt. |
| Heartbeat indicator                | Per-running-card | A + #12          | ☐ V2     | Pulsing dot + `last heartbeat 30s ago`. Pulse rate maps to recency. |
| Stale halo                         | Per-running-card | #7               | ☐ V2     | Amber outer ring when heartbeat is aging past `stale_after_seconds`. |
| Reclaim event in stream            | Dispatch ticker + per-card events | #17  | ☐ V1     | `card_014 reclaimed (heartbeat stale 12m, demoted to ready)`. Ships with the ticker. |
| Token budget burn bar              | Per-running-card | D                  | ☐ V2     | Progress against `token_budget` on the card. Goes amber > 80%, red at 100% (triggers `timed_out`). |
| `definition_of_done` checklist     | Detail panel | D                     | ☐ V3     | Render as a checkable list in detail. The verifier marks items checked as it goes (Move C). |
| `references` pill chips            | Per-card + detail | D                | ☐ V3     | Tiny chips on the card body showing file/finding/card count: `📎 3 files · 2 cards`. Detail expands to the full list. |
| `verify_with` code block           | Detail panel | D                     | ☐ V3     | Render as a small code block in the detail with the verifier's last-run output. |
| Mission age + persistence stamp    | Overview header | G                  | ☐ V1     | `Started 2h ago · persisted across 1 restart`. Indicates the board outlived a bridge restart. |
| Verifier-rejection comment styling | Detail panel + activity | C            | ☐ V3     | A `running` card whose latest event is a verifier rejection should style that comment distinctly so the worker (and operator reading along) doesn't miss it. |

#### Existing problems to fix in the same pass

- **Split `KanbanBoard`** into two components: `KanbanMiniBoard`
  (overview, read-only, fixed compact layout) and `KanbanFullBoard`
  (swarm, selectable cards, scrollable columns). The `compact` prop
  goes away.
- **Symmetric selection** on the overview: clicking a card in the
  mini-board opens it in a peek panel, or jumps to the Swarm tab with
  that card selected. Pick one.
- **Drop "quiet lanes" pill bar.** Replace with: lanes with zero cards
  collapse to a 32px-wide stub showing only the label and count. The
  state machine stays visible without dedicating a 380px column to
  emptiness.
- **Deduplicate detail panel.** Activity feed is the source of truth;
  remove "latest signal" (it's just `activity[0]`) and trim the agent
  status to identity + attach button, since the tool strip already
  carries the live activity.
- **Keyboard nav.** Arrow keys cycle cards in the detail panel.
  `j`/`k` are accelerators; `/` opens filter.
- **Filter + search.** A small filter chip row above the board:
  `status:running`, `profile:researcher`, `assigned`, `blocked >5m`,
  free-text. State lives in URL params so it survives navigation.

#### Lane layout under the new state machine

With Move D and Move F lands, the active status vocabulary becomes:

```
triage  →  ready  →  running  →  done_unverified  →  done
                       ↓             ↓                ↑
                       ↓             └─ verifier rejects → back to running
                       ↓
                       ├→ blocked   (needs user)
                       ├→ crashed   (worker died, retry-eligible)
                       ├→ timed_out (budget exceeded, retry-eligible)
                       ├→ failed    (circuit-broken, dispatcher locked out)
                       └→ cancelled (explicitly stopped)
```

Nine columns is a lot. Two acceptable presentations:

- **Wide-board mode** (≥1600px viewport): all nine columns visible,
  empty ones collapsed to 32px stubs. Operators on a big monitor get
  the full state map.
- **Narrow mode** (<1600px): collapse paired statuses into single
  columns: `triage+ready` as "Plan", `running+done_unverified` as
  "In progress", `blocked+crashed+timed_out` as "Stuck",
  `done` as "Shipped", `failed+cancelled` as "Closed". Sub-status
  shows as a small badge on each card.

The narrow-mode collapse is the default the Swarm tab opens with;
operators can switch to wide mode for the full state-machine view.

### Sequencing the visualization work

| Move shipping | Dashboard work that ships with it                                                                |
| ------------- | ------------------------------------------------------------------------------------------------ |
| B (root card) | Mission cover card. Autopilot toggle wired (renders "OFF — manual" until A ships).               |
| D (two-phase) | `triage` lane treatment. Structured detail-panel sections (`definition_of_done`, `references`, `verify_with`, `token_budget`). Reference chips on cards. |
| F (end states) | New lane styles for `crashed` / `timed_out` / `failed`. Retry counter pill on cards. Watch list (since `failed` near-misses are the main watch-list driver). |
| E (worker slim-down) | Nothing user-facing; pure plumbing.                                                        |
| A + C (queued kanban) | Autopilot strip live. Dispatch ticker. Verifier verdict badges. `done_unverified` lane. Heartbeat indicator + stale halo (paired with #7 + #17). |
| G (persistence) | Mission age line; "persisted across N restarts" stamp.                                          |

The fix-while-we're-in-there punch list (split board components,
symmetric selection, drop quiet-lanes pill bar, dedupe detail panel,
keyboard nav, filter/search) ships during whichever move is touching
that file. None of it needs to wait.

## Dashboard build plan (next)

Now that the bridge is fully on the new state machine, the dashboard
work splits into three coherent waves. Each is sized to ship as one
or two commits without leaving the UI in a half-finished state.

### Wave V1 — Mission visibility

Goal: the user opens a kanban session and understands what's
happening at a glance. Mostly additive — new components above the
existing board, no structural refactor yet.

Components:

1. **`MissionCoverCard`** — pinned above the kanban board on both
   Overview and Swarm tabs. Renders the mission root card with a
   distinct "paper folder" treatment (heavier shadow, slightly
   warmer paper than ready cards). Shows: title, user's original
   prompt (truncated with expand), elapsed mission time, running
   summary if the parent has one, child-card rollup
   (`12 cards · 3 running · 7 done`).

2. **`AutopilotStrip`** — header band above the dashboard.
   Two states:

   ```
   ┌─────────────────────────────────────────────────────────────┐
   │ ● AUTOPILOT ON   2/3 slots used   next tick in 24s   [pause]│
   └─────────────────────────────────────────────────────────────┘
   ```
   ```
   ┌─────────────────────────────────────────────────────────────┐
   │ ○ AUTOPILOT OFF   manual mode                       [enable]│
   └─────────────────────────────────────────────────────────────┘
   ```

   Click on the right-edge button toggles auto-dispatch via the
   existing `kanban_autopilot` IPC. Slot count comes from the
   running-sub-agent registry; tick countdown is derived locally
   from the dispatcher's known cadence + last-tick timestamp.

3. **`DispatchTicker`** — board-level event feed, distinct from
   per-card activity. Renders `kanban_dispatched` /
   `kanban_reclaimed` / `kanban_stale` / `kanban_autopilot_*` /
   `kanban_orphan` events as a single stream:

   ```
   ┌─ Dispatcher pulse ──────────────────────────────────────────┐
   │ 19:42  ↗ dispatched code on card_017 (specifier lane)       │
   │ 19:41  ✓ verifier signed off card_014 ("tests pass")        │
   │ 19:38  ⟲ reclaimed card_011 — heartbeat stale 12m           │
   │ 19:35  ⓘ stale: card_009 (3m without activity)              │
   │ 19:34  ▶ autopilot on                                        │
   └─────────────────────────────────────────────────────────────┘
   ```

   Lives on Overview as the primary content under the
   AutopilotStrip; available on Swarm as a togglable side rail.
   Color: monochrome with one-character glyph prefix per event
   class (no rainbow).

4. **Mission age + persistence stamp** — small subtitle in the
   Overview header next to the existing context-meter:
   `Started 2h 14m ago · persisted across 1 restart`. The restart
   count comes from a new `restartCount` counter on the kanban
   journal (increment on each `replay_events` call past size 0).

5. **Verifier verdict badge** — single per-card affordance:
   `✓ verified by gpt-5.5` on `done` cards (when the most recent
   transition was a verifier seal), `✗ rejected — see feedback` on
   running cards whose latest event is a verifier rejection.

Touchpoints: new components in
`src/renderer/components/kanban/` (a new subdir),
`MissionDashboard.tsx` for layout integration, `events.ts` for any
new event-shape additions, `store.ts` for the autopilot toggle
action + state slice + persistence-restart counter wiring on the
bridge side.

### Wave V2 — Lane vocabulary

Goal: the new statuses get their proper visual treatment. Lane
materials, retry counters, heartbeat indicators, token-budget
burn bars. Most of this is per-card chrome; the column structure
from Wave 2 stays as-is.

Components:

1. **Lane materials** — per-status visual differentiation:

   | Status            | Treatment                                                                         |
   | ----------------- | --------------------------------------------------------------------------------- |
   | `triage`          | Paper card + "needs spec" overlay. Specifier-running shows a tiny spinner.        |
   | `ready`           | Existing cream paper (unchanged).                                                 |
   | `running`         | Existing dark gradient (unchanged).                                               |
   | `done_unverified` | Amber tint, "review pending" or verifier-active label.                            |
   | `done`            | Existing green tint + verifier badge (V1).                                        |
   | `blocked`         | Existing dim glass (unchanged).                                                   |
   | `crashed`         | Recoverable amber: warm-amber background, retry counter prominent.                |
   | `timed_out`       | Budget-exceeded peach: cooler peach background, "exceeded budget" label, retry counter. |
   | `failed`          | Circuit-broken red: red ring + "circuit broken" label + force-reset affordance.   |
   | `cancelled`       | Existing dimmed (unchanged).                                                      |

2. **Retry counter pill** — `2/3` pill on any card with
   `consecutive_failures > 0`. Sits on the card's top-right.
   Goes red on the last attempt (= one more failure trips
   the breaker).

3. **Heartbeat indicator** — pulsing dot + `last heartbeat 30s ago`
   on `running` cards. Pulse rate maps to recency (faster when
   fresher); silences after `KANBAN_STALE_SECONDS` and the dot
   goes amber. After `KANBAN_RECLAIM_SECONDS` the card is no
   longer running (it's been reclaimed to `crashed`) so the dot
   doesn't appear on that lane.

4. **Stale halo** — amber outer ring on running cards once their
   age past `updated_at` exceeds `KANBAN_STALE_SECONDS / 2`. Same
   threshold the dispatcher uses for emitting `kanban_stale`. The
   ring intensifies as the card approaches `KANBAN_RECLAIM_SECONDS`.

5. **Token budget burn bar** — thin progress bar at the bottom of
   a running card showing `tokens_used / spec.token_budget` when
   the card has a budget. Goes amber > 80%, red at 100%. The
   underlying counter comes from the running sub-agent's usage
   (which the renderer already tracks per agent).

Touchpoints: card-component subdir
(`src/renderer/components/kanban/cards/`), the existing
`kanbanCardMaterialClass` / `kanbanProgressFillClass` helpers
get split per-status, `MissionDashboard.tsx` only changes where
it imports from.

### Wave V3 — Operator workflow

Goal: the Supervise-mode user can drive a board efficiently. This
is the bigger refactor — splits the board component, restructures
the detail panel, adds the watch list and keyboard nav.

Components:

1. **Split `KanbanBoard` → `KanbanMiniBoard` + `KanbanFullBoard`.**
   The `compact` prop goes away. The mini-board (Overview) is
   read-only and fixed-layout; the full board (Swarm) is
   selectable, scrollable, supports wide/narrow column modes.

2. **Quiet-lane stubs.** Lanes with zero cards collapse to a
   32px-wide vertical stub showing only the label and `(0)`
   count. Clicking expands them back. The state machine stays
   visible without dedicating a 380px column to emptiness.

3. **Watch list** — board-level "needs attention" queue on the
   Overview. Sources:

   - `blocked` cards (highest priority — needs user)
   - `running` cards where `consecutive_failures >= 2` (one more
     failure trips the breaker)
   - `running` cards past `KANBAN_STALE_SECONDS` without activity
   - `done_unverified` cards waiting > 30s for a verifier slot
     (autopilot is saturated)

   Sorted by urgency. One-click jumps to the card's detail panel
   on the Swarm tab.

4. **Detail-panel restructure.** Drop "latest signal" (duplicates
   activity[0]), trim agent-status to identity + attach button
   (tool strip already carries live activity). Add new structured
   sections for cards in kanban mode:

   - **Definition of done** (Move D spec.definition_of_done) —
     rendered as a checklist. The verifier marks items checked as
     it walks them.
   - **References** — files / findings / sibling cards from
     `spec.references`, each clickable.
   - **Verify with** — `spec.verify_with` code block + last-run
     output from the verifier.
   - **Verifier rejection** — when a card is in `running` with a
     prior `done_unverified` and the latest event is a verifier
     rejection, the rejection feedback gets a distinct callout
     above the regular activity feed so the worker doesn't miss
     it on the next `show`.

5. **Reference chips on cards.** Tiny chips on the card body
   showing file/finding/card count: `📎 3 files · 2 cards`. Detail
   expands to the full list.

6. **Keyboard nav.** Arrow keys cycle cards in the detail panel;
   `j`/`k` accelerators; `/` opens filter; `Esc` closes detail.

7. **Filter + search.** Filter chip row above the board:
   `status:running`, `profile:researcher`, `assigned`,
   `blocked >5m`, free-text. State in URL params so it survives
   navigation.

Touchpoints: the entire `KanbanBoard` component splits;
`MissionDashboard.tsx` imports change. The detail-panel
restructure is mostly local to the existing detail component but
adds three new structured sections from the new `spec` field.

### Order to ship

V1 first — it's purely additive, biggest visible impact for the
smallest diff. V2 second — touches per-card rendering but stays
within the existing layout. V3 last — it's where the
structural refactor lands and needs more careful review.

Within V1, the natural sub-order is:

1. Persistence-stamp + autopilot strip (smallest, depends only on
   data already flowing — stamp needs a tiny bridge counter).
2. Mission cover card (largest visual change, needs the new
   subdir setup).
3. Dispatch ticker (largest data shape change — adds event
   collation that we may also reuse for the watch list in V3).
4. Verifier verdict badge (smallest — one expression in card
   chrome).

## Open questions

- **Kanban + goal-loop composition.** A goal as the active objective
  with auto-dispatched cards underneath is the obvious composition,
  but two judge loops in play (the goal judge and the verifier)
  interact non-trivially. Probably worth a small spike before we
  commit.

- **Verifier dispatch policy.** Same `verify` profile globally, or
  per-card override via `metadata.verify_with_profile`? Default to
  global, allow override.

- **Specifier dispatch policy.** Same question — global `specifier`
  profile, with a card-level override for domain-specific cards
  (e.g. a frontend card wants a frontend-spec specifier).

- **Cross-session sharing.** "Ship the SSO feature" might live
  across a week of separate chat sessions. Is the right unit of
  board the session, or something larger keyed off the workspace +
  a user-chosen slug? For now: session-scoped, with persistence
  designed so the data could be lifted to a wider scope later.

- **Worker `create` / `link` permissions.** We default to closed —
  workers can't restructure the board mid-task. Is there a
  decomposition profile (e.g. `planner`) that should have these
  unlocked? Decide as use cases land.
