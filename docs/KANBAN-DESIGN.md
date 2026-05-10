# Kanban Coordination — Current State and Build Plan

> Status: Living design note. Last updated 2026-05-10.
> Owner: coordination-strategy stream.
> Related: `docs/GOAL-TASK-COORDINATION-PLAN.md`.

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

| # | Fix                                                                         | Slot       |
| - | --------------------------------------------------------------------------- | ---------- |
| 5 | Race-safe `claim` — dispatcher-owned, single-owner state transition         | Move A prereq |
| 6 | Cancelled-parent promotion policy — close the "todo stuck forever" stall    | Independent |
| 7 | Stale running-card detector → `kanban_stale` event                          | Pairs with Move F |
| 8 | `digest` action returning `{stuck, in_review, newly_unblocked, …}`          | Independent |
| 9 | Cap `events` and `comments` to a rolling window + counters                  | Independent |
| 10 | Persist board state via event-sourced JSONL                                 | Move G in practice |
| 11 | Wire `priority` into list-order, then auto-dispatch order                   | Independent / Move A |
| 12 | Heartbeats as liveness signal for the dispatcher                            | Pairs with #17 |
| 13 | Validate status transitions — block illegal moves, support new end states  | Move F prereq |
| 14 | `show` returns parent summaries + artifacts inline                          | Independent, helps Move C |
| 15 | Worker `KanbanTool` slim-down + terminal-action ownership gate              | Move E in practice |
| 16 | `consecutive_failures` counter + circuit-breaker `failed` state             | Move F in practice |
| 17 | Stale-card reclaim path — demote stuck cards, increment failure counter    | Move F in practice |

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

| Visualization element              | Belongs in   | Lands with move      | Notes                                                                                                          |
| ---------------------------------- | ------------ | --------------------- | -------------------------------------------------------------------------------------------------------------- |
| Mission root card "cover"          | Overview + Swarm | B                  | Always-visible card pinned above the board. User's prompt + parent's running summary. Distinct paper-folder treatment so it doesn't read as a worker card. |
| Autopilot strip                    | Overview + Swarm | A                  | Always-on header band: `AUTOPILOT ON · 2/3 slots · next tick in 24s`. Toggle to flip on/off. Reads "AUTOPILOT OFF — manual mode" when disabled. |
| Dispatch ticker                    | Overview     | A                     | Live feed of dispatcher decisions, verifier verdicts, reclaim events. Color-coded by event class. Primary surface on Overview, available as a togglable side rail on Swarm. |
| Watch list                         | Overview     | A + F                 | Cards needing human attention: blocked, near-circuit-breaker, stale running, awaiting user input. Sorted by urgency. One-click jump to card detail. |
| `triage` lane + spec-status badge  | Swarm        | D                     | Distinct visual treatment — the paper card but with a "needs spec" overlay. Specifier in-progress shows a small spinner on the card. |
| `done_unverified` lane             | Swarm        | C                     | Amber-tinted, between `running` and `done`. Cards here show "review pending" or "verifier: [name] (running)". |
| Verifier verdict badge             | Per-card     | C                     | `✓ verified by [verifier]` on `done` cards. `✗ rejected — see feedback` on cards bounced back to `running`. |
| `crashed` / `timed_out` lanes      | Swarm        | F                     | Separate from `blocked`. Distinct colors (recoverable amber vs budget-exceeded peach). Show retry counter prominently. |
| `failed` lane                      | Swarm        | F                     | Visually distinct from `cancelled` — red ring, "circuit broken" label, prominent "force-reset" affordance. |
| Retry counter pill on card         | Per-card     | F                     | `2/3` pill on cards with `consecutive_failures > 0`. Goes red on the last attempt. |
| Heartbeat indicator                | Per-running-card | A + #12          | Pulsing dot + `last heartbeat 30s ago`. Pulse rate maps to recency. |
| Stale halo                         | Per-running-card | #7               | Amber outer ring when heartbeat is aging past `stale_after_seconds`. |
| Reclaim event in stream            | Dispatch ticker + per-card events | #17  | `card_014 reclaimed (heartbeat stale 12m, demoted to ready)`. |
| Token budget burn bar              | Per-running-card | D                  | Progress against `token_budget` on the card. Goes amber > 80%, red at 100% (triggers `timed_out`). |
| `definition_of_done` checklist     | Detail panel | D                     | Render as a checkable list in detail. The verifier marks items checked as it goes (Move C). |
| `references` pill chips            | Per-card + detail | D                | Tiny chips on the card body showing file/finding/card count: `📎 3 files · 2 cards`. Detail expands to the full list. |
| `verify_with` code block           | Detail panel | D                     | Render as a small code block in the detail with the verifier's last-run output. |
| Mission age + persistence stamp    | Overview header | G                  | `Started 2h ago · persisted across 1 restart`. Indicates the board outlived a bridge restart. |

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
