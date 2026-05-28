# Always-On Agent Platform — Design

> Status: Draft · Author: Soham + Claude (assistant) · Date: 2026-05-13
>
> Successor concept for Freyja's coordination layer. Captures the vision,
> scope, candidate approaches, and considerations behind turning Freyja
> from an interactive agent harness into an ambient agent fabric that
> runs continuously, fires on schedules and external events, and reaches
> the operator wherever they are.

## TL;DR

Freyja today is an interactive agent harness — the operator launches the app, kicks off a session, drives it, closes the app. When the app closes, every running agent dies. The path forward is to elevate Freyja into an **ambient agent fabric**: a daemon that runs continuously under operator-authored workflows; fires on schedules and external triggers; persists state durably; enforces capability-based policy on unattended action; and reaches the operator through a multi-channel notification fabric. The interactive desktop app becomes one window (the primary one) into an organization of agents that work 24/7. This document captures the problem framing, the candidate approaches considered, the recommended architecture, the failure/recovery/security model, concrete schemas and protocols, the operator UX shifts, the phased delivery plan, and the open decisions to lock in before code.

## 1. Vision & motivation

### 1.1 The shift

Today's mental model: *"I open Freyja, kick off a session, drive it interactively, close Freyja."* The app is a launchpad for sessions; the sessions are conversational artifacts; everything stops when I quit.

Production mental model: *"Freyja is an organization of agents I employ. They run 24/7 under policies I've set. I open the app to review what they've done, direct attention, and adjust workflows. Most interactions happen via notifications and an operator inbox, not by sitting at the desktop app."*

This is a paradigm shift, not a feature addition. It changes what Freyja is.

### 1.2 Why this matters

Three reasons it's the right next move:

1. **The work doesn't stop when the laptop closes.** A research watcher that wakes hourly to scan for new papers; a digest that fires every morning at 9; a triage workflow that handles GitHub webhook failures — none of these are valuable if they only run while the operator is actively present.
2. **The reach extends to places the operator can't or won't drive interactively.** "Monitor my Slack and alert me on mentions" is a low-value feature when it only works if I'm staring at the screen. It becomes high-value when it runs continuously and pings my phone when something demands attention.
3. **Composition of always-on agents creates emergent value.** One agent's output becomes another's input. A morning digest pulls from yesterday's findings of a continuous watcher. A trigger response chains into a verifier. The whole becomes more than the sum of the parts only when the parts are persistently alive.

### 1.3 The product implication

The desktop app stops being the "where Freyja lives" — it becomes the primary viewport into an agent fabric that *also* exposes itself through notifications, phone push, Slack DMs, and (eventually) a mobile companion. Freyja stops being software you launch and becomes software that's *with you*.

### 1.4 What "production grade" means here

Freyja is not enterprise SaaS. It's a local-first desktop tool for power users — engineers, researchers, operators. "Production grade" in this context means:

- **Reliable**: doesn't lose work, doesn't drop schedule fires, doesn't silently fail.
- **Observable**: the operator can debug what happened at 3am two weeks ago.
- **Recoverable**: process crash, OS reboot, network blip — recovers correctly without manual intervention.
- **Bounded**: resource usage, cost, side effects all have hard ceilings.
- **Auditable**: every action is traceable to a cause; every cause is traceable to a trigger or schedule.
- **Secure**: unattended agents can't escalate privileges, exfiltrate data, take destructive actions outside their granted scope.
- **Composable**: schedules, triggers, sessions, watchers compose without weird emergent behavior.
- **Beautiful**: the interactive surfaces stay at the level of polish today's app has.

Not in scope: multi-tenant SaaS, hosted infrastructure, 100k concurrent users. This is one user's (or small team's) ambient agent organization.

## 2. Current state (grounded in code)

Before designing forward, the design has to be honest about where Freyja is today. Concrete observations from the codebase:

### 2.1 Bridge architecture (today)

- Single Python process spawned by Electron's main process. See `src/main/bridge.ts:65` (HarnessBridge class).
- IPC: stdin/stdout newline-delimited JSON. Bridge's `_command_loop` is at `bridge/freyja_bridge.py:5078`.
- Multi-session is *already* supported: `_BridgeState.sessions: dict[str, _BridgeSession]` at `freyja_bridge.py:4996`. Each session has its own `pending_task: asyncio.Task` and `queued_messages` list.
- Sessions are not isolated processes — they all run on one asyncio event loop in one Python process.

### 2.2 Lifecycle coupling

- Bridge dies when Electron quits. `src/main/main.ts:676` `app.on('before-quit')` calls `bridge.stop()`.
- On macOS `window-all-closed` doesn't quit Electron (dock icon persists), so closing the window keeps the bridge alive — but the bridge is still tethered to Electron's lifecycle. Force-quit or relaunch and everything dies.
- No headless mode exists; there's no `freyja-bridge` standalone entry point.

### 2.3 Persistence (today)

Two parallel persistence layers, both file-based:

- **Renderer-owned**: `~/.freyja/sessions/{id}.json` (UI slice) + `_index.json` (fast list). Code: `src/main/persistence.ts`. Updated via IPC from the renderer to the main process.
- **Bridge-owned**: `~/.freyja/sessions/{id}.transcript.json` (LLM transcript) + `{id}.goal.json` (goal-mode sidecar) + `~/.freyja/projects/{safeId}/raw_messages.jsonl` + `compactions.jsonl` (append-only). Code: `bridge/transcript_persistence.py`.

Restoration on app start happens via `_BridgeSession.try_restore_transcript()` at `freyja_bridge.py:2004`. The renderer reads its slice from disk independently.

### 2.4 Existing background patterns

The codebase already has the seeds of always-on behavior:

- **Kanban autopilot**: per-session asyncio task that wakes every `KANBAN_DISPATCH_INTERVAL` seconds (`freyja_bridge.py:3082` `_start_kanban_dispatcher`). This is literally the prototype for what a scheduled fire loop would look like.
- **Goal-mode continuation**: each agent turn auto-triggers the next as long as the verdict is not done (`freyja_bridge.py:4188` `_maybe_continue_goal`).
- **Subagent background mode**: `sub_agent_tool.py:422` `_run_background` — fire-and-forget child sessions.
- **Calibrator parallel-fire**: `asyncio.create_task(self._run_judge_calibrator(...))` at `freyja_bridge.py:2967` runs the calibrator without blocking goal-set.
- **Message queuing under busy**: `_schedule_or_queue_turn` at `freyja_bridge.py:5393` — if the session has a pending task, the new message queues; drains via `_run_turn_queue` (line 5422).

These patterns mean the bridge is *already* asyncio-friendly and multi-session-capable. The architectural work is layering above the engine, not rebuilding it.

### 2.5 Permission model (today)

- Five tiers: `none / low / medium / high / yolo`. Implemented at `freyja_bridge.py:1121` `_parse_auto_approve`.
- Set via `FREYJA_PERMISSION_AUTO` env var or per-session command.
- Coarse: tier applies to the entire session uniformly. No per-tool or per-path policy.

### 2.6 What's missing

- No scheduling primitives. Grep for `cron`/`Schedule`/`CronCreate` returns zero results in the codebase.
- No native notifications. No `Notification` import in `src/main/main.ts`.
- No state-snapshot-on-reattach. Renderer can rejoin a live bridge and start receiving events, but it can't ask the bridge "what's your current state for session X?"
- No durable state store. Everything is JSON-per-file.
- No workflow abstraction. Session is the unit of work.
- No capability policy. Permissions are global per-session tiers.
- No audit log.
- No trigger plugin model. No mechanism for external events to fire sessions.

The good news is in §2.4 — Freyja is closer to the target than a fresh codebase would be. The work is real but additive.

## 3. Scope

### 3.1 In scope

- Always-on daemon mode (bridge survives Electron lifecycle)
- Schedule-based workflow execution (cron-style)
- External trigger sources (webhook, file watcher, calendar, Slack, GitHub, etc.) via a plugin protocol
- Persistent watcher sessions (long-running, wake-and-sleep cycles)
- Multi-channel notification fabric (macOS, Slack, phone push, email digest)
- Operator inbox (batched attention queue with reply round-trips)
- Capability-based policy with audit log
- Workflow / Run / Session three-layer model
- SQLite-backed durable state
- Orchestrator / Engine process split
- Workflow library / sharing (export/import YAML bundles)
- Cost projection and per-workflow budgets

### 3.2 Out of scope (explicit non-goals)

- **Multi-tenant SaaS hosting.** Freyja is single-user, local-first. No cloud control plane.
- **Cloud sync of workflows.** Sharing happens via export/import, not via a hosted registry.
- **Distributed execution across machines.** Symphony has SSH workers; we don't need them. Possible future, not now.
- **Custom DSL for workflow logic.** Workflows are YAML with a Python extension hatch. No new language.
- **Real-time multi-author collaboration on workflows.** Single-author; sharing via export.
- **Replacing the existing engine.** All today's engine code (`AsyncAgentRunner`, tool registry, sub-agent system, message bus, goal loop, calibrator, judge profiles) is reused as-is.
- **LLM provider routing changes.** Today's multi-provider + fallback works; not touching it.

### 3.3 What gets harder (explicit costs)

- Process boundary debugging. When something fails between orchestrator and engine, the failure surface is larger than today's single-process bridge.
- macOS-specific assumptions deepen (launchd, Keychain, macOS notifications). The architecture is portable but the implementation isn't initially. Linux/Windows users get a degraded experience until porting.
- State migration. Existing users have JSON-per-file persistence on disk; the new SQLite store needs a one-time migration on first launch of the new daemon.

## 4. Conceptual model

Three nouns, in this order. Getting them right is the single most important design decision.

### 4.1 Workflow

A **definition**. The "what runs when, under what policy."

Persistent, versioned, possibly shareable. Lives in `~/.freyja/workflows/<id>.yaml` (operator scope) or `<repo>/.freyja/workflows/<id>.yaml` (project scope, version-controlled with code).

A workflow declares:
- Identity (id, name, version, description)
- Triggers (one or more: schedule, webhook, fs event, etc.)
- Mode (`fresh` / `continue` / `persistent`)
- Agent configuration (model, profile, coordination strategy, prompt template, judge rules)
- Capability set (what the agent's allowed to do)
- Budget caps (per-run, per-day)
- Notification policy (which channels for which events at which urgency)
- Outputs (digest files, external API pushes, etc.)
- Retry policy on failure

### 4.2 Run

One **execution** of a workflow.

A Run has:
- An idempotency key (so duplicate triggers don't double-fire)
- A state machine: `pending → claimed → running → (completed | failed | timed_out | budget_exceeded | cancelled | paused)`
- A parent_run_id (for runs spawned by other runs, e.g. workflow B chained off workflow A's completion)
- Actual cost, duration, the trigger event that fired it
- Audit log entries per tool call

### 4.3 Session

The **conversational unit** inside a Run. The thing today's bridge already understands.

A Run has 1+ sessions:
- Most runs have 1 session (the agent does its work, terminates)
- Fan-out workflows can have N sessions (spawn 5 explore-fast sub-agents in parallel)
- Persistent watchers have one long-lived session that the run keeps reattaching to

Sessions stay as they are today. Transcripts persist as today. Sub-agents inherit run + capability context.

### 4.4 Why these three nouns

- **Workflows are the "always on" abstraction.** They outlive any single execution. They're what you pause, share, edit, version. They're the unit of operator intent.
- **Runs separate "an attempt to execute" from "the conversational state of that attempt."** This is required because:
  - Retries create multiple runs of one workflow
  - Dry-runs are runs without side effects
  - Fan-out workflows have multiple runs (one per fanned branch)
  - Querying "all fires of this schedule" requires a Run row per fire
- **Sessions stay as the unit of conversation** because that's what the engine knows about. The engine doesn't need to know about Workflows or Runs; it just executes one session at a time.

### 4.5 Backward compatibility

Today's interactive sessions become Runs of an implicit `interactive` workflow:
- `trigger: manual`
- `capabilities: interactive` (today's high tier mapped to a capability preset)
- `mode: continue`

This is free backward compat — no user-visible breakage, all existing sessions become first-class citizens in the new model.

## 5. Architecture

### 5.1 Process model

Two distinct processes (could be one for simple deployments, but the boundary is real):

```
┌─ Orchestrator (always-on daemon) ─────────────────────────────────┐
│                                                                    │
│   trigger sources ─►  scheduler / webhook / fs / slack / calendar │
│                              │                                     │
│                              ▼                                     │
│   workflow registry ─►  match event → workflow                    │
│                              │                                     │
│                              ▼                                     │
│   run queue       ─►  pending runs (SQLite)                       │
│                              │                                     │
│                              ▼                                     │
│   capability      ─►  policy engine                               │
│   engine                     │                                     │
│                              ▼                                     │
│   dispatcher      ─►  claim a free engine worker                  │
│                              │                                     │
│                              ▼                                     │
│   notification    ─►  notification fabric                         │
│   fabric                                                           │
│                                                                    │
│   SQLite (~/.freyja/freyja.db)                                    │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘
                              ▲ ▼ (UDS, JSON line protocol)
        ┌─────────────────────┼─────────────────────┐
        ▼                     ▼                     ▼
  ┌──────────┐          ┌──────────┐          ┌──────────┐
  │ Engine 1 │          │ Engine 2 │          │ Engine 3 │   (pool, N configurable)
  │ executes │          │ executes │          │ executes │
  │ runs     │          │ runs     │          │ runs     │
  └──────────┘          └──────────┘          └──────────┘
        │                     │                     │
        ▼                     ▼                     ▼
  ┌────────────────────────────────────────────────────────┐
  │ Transcript files + knowledge.jsonl + artifacts          │
  │ ~/.freyja/sessions/<id>/                                │
  └────────────────────────────────────────────────────────┘

  ┌────────────────────┐                       ┌─────────────────┐
  │ Renderer (Electron)│ ◄──── UDS, JSON ─────►│ Notification    │
  │ transient, attaches│                       │ channels        │
  │ to orchestrator    │                       │ (macos / slack  │
  │ read + steer       │                       │  / push / email)│
  └────────────────────┘                       └─────────────────┘
```

### 5.2 Orchestrator responsibilities

- Owns durable state (SQLite at `~/.freyja/freyja.db`)
- Owns workflow registry (loads `~/.freyja/workflows/*.yaml` + per-project `<repo>/.freyja/workflows/*.yaml`)
- Runs the trigger sources (one persistent loop per source)
- Maintains the run queue and dispatches to engines
- Enforces capability policy (every tool call passes through)
- Routes notifications
- Launched by launchd at user login

Critically: the orchestrator does *not* execute agent turns. It's all IO and scheduling; lightweight compute. This is what makes the architecture survive crashes — the orchestrator can restart engines without losing its own state.

### 5.3 Engine worker responsibilities

- Stateless executor
- Connects to orchestrator over UDS on startup, identifies itself with capacity
- Claims runs from the orchestrator's queue
- Loads run state from SQLite + transcript files, executes turns, writes back
- Reports state changes + events back to orchestrator
- Sends periodic heartbeats so orchestrator can detect stalls

For low-concurrency desktops (1-3 active sessions), N=1 is fine — barely heavier than today's bridge. For higher concurrency (10 schedules + 5 watchers + interactive use), N=3-5 scales linearly without rearchitecting.

### 5.4 Renderer

- Electron app connects to orchestrator over UDS
- Multiple renderer instances can connect concurrently (enables future mobile/web companion)
- Renderer never talks to engine workers directly — asks the orchestrator "show me run X", orchestrator pipes through transcript events
- Closing the renderer doesn't kill anything

### 5.5 Storage layout

**SQLite at `~/.freyja/freyja.db`** (WAL mode):

- `workflows` — definitions
- `schedules` — cron rows per workflow
- `trigger_subscriptions` — non-schedule triggers per workflow
- `runs` — every execution attempt, with state machine
- `sessions` — per-run session metadata
- `audit_log` — every policy check + every tool call
- `events_outbox` — notifications waiting to be sent
- `idempotency_keys` — at-most-once trigger handling
- `schema_version` — for migrations

**File system at `~/.freyja/sessions/<id>/`**:

- `transcript.jsonl` (append-only, source of truth for replay)
- `state.json` (cached snapshot, rebuildable from transcript)
- `goal.json` (goal-mode sidecar; today's pattern)
- `knowledge.jsonl` (append-only structured facts; for persistent watcher sessions)
- `artifacts/` (files the session produced)

**File system at `~/.freyja/workflows/<id>.yaml`**: operator-scope workflow definitions.

**File system at `~/.freyja/audit.jsonl`**: append-only mirror of the SQLite audit log, for grep-friendly debugging.

### 5.6 IPC

- **Orchestrator ↔ engine workers**: UDS at `~/.freyja/engine.sock`, line-delimited JSON with explicit message types (`engine_hello`, `claim_run`, `run_event`, `run_state`, `heartbeat`, `cancel_run`).
- **Orchestrator ↔ renderer**: UDS at `~/.freyja/control.sock`, evolved from today's JSON protocol with request/response correlation IDs.
- **Trigger sources → orchestrator**: in-process (Python plugin protocol; sources run inside the orchestrator process).
- **Notification channels**: HTTP outbound for Slack/push/email; native bindings for macOS notifications.

## 6. Candidate approaches considered

Per major decision, record alternatives and the recommendation's rationale.

### 6.1 Lifecycle decoupling

| Option | Description | Verdict |
|---|---|---|
| A | Keep bridge as Electron child, save state on quit, resume on next launch | Rejected — doesn't deliver always-on at all |
| B | Bridge becomes a daemon; Electron is a client (proposed) | Recommended |
| C | Bridge runs as a system service, installed via launchctl, app is just a UI | Recommended as a refinement of B — the daemon installs itself on first run |

**Choice**: B with C-style installer. Lifecycle independent of Electron, but the user doesn't manage launchd manually — the app installs the plist on first launch.

### 6.2 IPC mechanism

| Option | Description | Verdict |
|---|---|---|
| A | Keep stdin/stdout (today) | Doesn't survive daemon model; renderer can't reconnect mid-stream |
| B | Unix domain socket (UDS) (proposed) | Recommended |
| C | localhost TCP | Adds port-conflict surface; loses peer-credential checks |
| D | gRPC | Adds protobuf + codegen; protocol surface is small enough that JSON-line is enough |

**Choice**: B. UDS is fast, supports peer credentials (so we can validate the connecting process), and the protocol can stay line-delimited JSON.

### 6.3 Storage backend

| Option | Description | Verdict |
|---|---|---|
| A | Keep JSON-per-file (today) | Loses queryability, races under load |
| B | SQLite (WAL) (proposed) | Recommended |
| C | LMDB / RocksDB | KV is fine for blobs but loses query — audit log + cross-workflow analytics push toward SQL |
| D | Hand-rolled doc store (TinyDB, custom append-log) | Reinvents SQLite badly |

**Choice**: B. Universally available, ACID, supports rich queries, WAL allows concurrent reads during writes, inspectable with `sqlite3` CLI, trivial to back up.

### 6.4 Workflow definition format

| Option | Description | Verdict |
|---|---|---|
| A | YAML with Liquid templating (Symphony style) | Recommended primary |
| B | Python code with a DSL | Less shareable, requires Python knowledge to read, harder for LLM to author |
| C | JSON | Less human-readable, no comments, multi-line strings are awful |
| D | GUI-only, no source format | Trap — source of truth becomes opaque, can't version-control |
| E | YAML primary + Python extension hatch (proposed) | Recommended |

**Choice**: E. YAML is portable, shareable, version-controllable, easy for both humans and LLMs to author. Complex logic that doesn't fit YAML goes in `extension.py` referenced by the workflow.

### 6.5 Scheduling primitive

| Option | Description | Verdict |
|---|---|---|
| A | Cron string (with timezone) | Recommended user surface |
| B | Custom expression language | Reinvents cron without payoff |
| C | Code expression (Python) | Powerful but unsafe; cron is enough for 95% of use |
| D | Job queue with delay/repeat semantics | Recommended under the hood |

**Choice**: A as the primary user-facing form, with D as the underlying execution model. Cron strings are the UX; every fire goes through an idempotent job queue with retry policies underneath.

### 6.6 Trigger source coupling

| Option | Description | Verdict |
|---|---|---|
| A | Hardcode the 4-5 sources we need | Accumulates one-off source handling; trap |
| B | Plugin protocol from day one (proposed) | Recommended |
| C | User-pluggable trigger sources from external Python files | Interesting but premature |

**Choice**: B. The protocol surface is tiny (start/stop/validate); committing from day one keeps the orchestrator clean.

### 6.7 Permission model

| Option | Description | Verdict |
|---|---|---|
| A | Keep current tiers globally | Too coarse for unattended action |
| B | Capability sets per workflow (proposed) | Recommended |
| C | Per-tool individual policy | Too granular; every tool needs its own policy doc |

**Choice**: B with tier presets as syntactic sugar. Capability sets are explicit, declarable, easy to audit. Tiers become presets that map to capability bundles.

### 6.8 Watcher memory model

| Option | Description | Verdict |
|---|---|---|
| A | Append to transcript forever | Unmanageable after a week (token blowup) |
| B | Reset transcript each wake, no memory | Watcher learns nothing |
| C | Transcript per wake + structured knowledge base (proposed) | Recommended |
| D | Per-wake transcript + LLM-generated summary stored as message | Captures gist but loses structure; can't query "what was p95 latency last Tuesday" |

**Choice**: C. Transcript per wake (ephemeral, compacted after) + knowledge base (structured, durable, semantic-searchable).

### 6.9 Engine process model

| Option | Description | Verdict |
|---|---|---|
| A | Single bridge process running all sessions (today) | No crash isolation |
| B | Process pool of engine workers (proposed) | Recommended |
| C | Process-per-session | Too much overhead (Python startup ~500ms, 50-100MB RSS per process) |

**Choice**: B. Pool of N gives crash isolation without process-per-session overhead. N=1 is basically today; N>1 gets parallelism + isolation.

### 6.10 Notification fabric

| Option | Description | Verdict |
|---|---|---|
| A | Single channel (macOS only) | Single point of failure; doesn't reach operator away from desk |
| B | Multi-channel with routing (proposed) | Recommended |
| C | External service (Pushover, Apprise, ntfy.sh) | Could be one of the channels; routing logic stays internal |

**Choice**: B internal. Ship with macos + slack + push channels. External services can be one of the channels via channel adapters.

### 6.11 Push notification provider for phone

| Option | Description | Verdict |
|---|---|---|
| A | APNs directly | Requires paid Apple Developer account + proxy service |
| B | Pushover | Operator pays $5 once; simple HTTP API |
| C | ntfy.sh | Free, self-hostable, simple HTTP API |
| D | Twilio SMS | Per-message cost; works for any phone |

**Choice**: Ship with B (Pushover) and C (ntfy.sh) as the easy paths. APNs direct is a v2.

### 6.12 Workflow location: operator-scope vs project-scope

| Option | Description | Verdict |
|---|---|---|
| A | `~/.freyja/workflows/` only | Misses repo-anchored workflows |
| B | `<repo>/.freyja/workflows/` only | Misses workflows that aren't tied to a repo |
| C | Both (proposed) | Operator scope for "my morning digest"; project scope for "this repo's CI triage" |

**Choice**: C. Workflow loader scans both locations on startup. Conflicts resolved by ID with project-scope winning (closer to the work).

## 7. Failure / recovery / security considerations

Production-grade means you've thought about everything that can go wrong.

### 7.1 Daemon crashes

- Orchestrator crash → launchd restarts it (KeepAlive=true in plist).
- On startup: scan SQLite for runs in `claimed` or `running` state with dead engine PIDs → reset to `pending`, requeue.
- Engine worker crash → orchestrator detects via socket close, requeues claimed runs.
- Both crash simultaneously → same recovery on next orchestrator start.

### 7.2 Mid-tool-execution crash

The hardest case. Tool side effects already happened externally; the engine never saw the result.

- Before invoking any tool, the engine writes a `tool_intent` row to SQLite with `intent_id`, `tool_name`, `arguments`, `started_at`.
- After tool returns, the engine writes a `tool_result` row referencing the same `intent_id`.
- On crash recovery, the engine scans for intents without results and decides per-tool:
  - `idempotent: true` (e.g. `read_file`, `grep`): retry. Same call, same result, no harm.
  - `idempotent: false` (e.g. `write_file` with append, `send_slack_message`, `git push`): pause the run, fire an `attention` notification asking the operator to confirm what happened externally, let the operator resolve.

Tool annotations live in the tool definition. Today's `permissions.py` already classifies by `PermissionLevel`; add an `Idempotency` enum (`SAFE | UNCERTAIN | UNSAFE`).

### 7.3 Schedule fires during daemon downtime

- Each scheduled fire has idempotency key `<workflow_id>:<scheduled_time_iso>`.
- On startup, the scheduler scans for missed fires within `catchup_window` (default 1h, configurable per schedule).
- For each missed slot, policy:
  - `catchup: skip` (default) — log "missed slot at T, skipping" and move on.
  - `catchup: fire_once` — fire one run for the most-recent missed slot only.
  - `catchup: fire_all_missed` — fire one run per missed slot (rare; for "I genuinely want every slot").

### 7.4 Trigger delivery duplicates

- Each trigger event has a source-provided event_id (GitHub delivery ID, calendar event ID, Slack message_ts, etc.).
- Orchestrator looks up `(source, source_event_id)` in `idempotency_keys` before dispatching.
- Duplicates are logged and dropped (HTTP 200 returned to webhook caller so upstream stops retrying).

### 7.5 Budget overruns

- Soft-pause: agent finishes current LLM call, then run transitions to `budget_exceeded`.
- Notification fires (urgency: attention).
- Operator can grant more budget and resume, or let it stay paused indefinitely.
- Daily budget across all runs of a workflow tracked separately; hitting the daily cap pauses *all* future fires until next day.

### 7.6 Capability denials

- Tool invocation blocked at the engine boundary (in the tracing wrapper that already exists).
- Audit log entry with `allowed: false`, `reason: "no fs.write capability for /etc/passwd"`.
- Agent receives a structured error: `{"error": "capability_denied", "capability": "fs.write", "target": "/etc/passwd"}`.
- Agent can reason about the denial and try another approach. This is a key design point — the agent shouldn't just hard-fail; capability denials are recoverable signals.

### 7.7 Trust model for unattended action

The single most important security design decision.

- **Default deny on all capabilities for new workflows.** Operator must explicitly grant each capability when creating/editing a workflow.
- **Capability presets exist** (`unattended-safe`, `unattended-trusted`, `interactive`) but the operator picks one explicitly. No magic auto-promotion.
- **No unattended workflow gets `computer` (mouse/keyboard) by default.** Must be explicitly enabled per workflow, with a banner warning.
- **Workflows declare requested capabilities at the top of their YAML.** When editing a workflow, the renderer shows a diff against the current grants and requires confirmation for any new grant.
- **Capability grants are revocable**. Removing a capability from a workflow immediately stops in-flight runs from using it.

### 7.8 Sandboxing for unattended computer-use

If `computer` capability is granted to an unattended workflow, the workflow runs against a sandboxed display:

- **Option for v1**: hidden Electron window with its own webview, used for browser-only tasks. Works without OS-level sandboxing.
- **Option for v2**: separate macOS user account login, switched into via `screencapture -U` or similar. Most isolation, most operational pain.

For v1, the recommended posture is: **don't grant `computer` to unattended workflows at all.** Raise the question explicitly in workflow validation. The operator has to type "I understand this is risky" or similar to enable it.

### 7.9 Webhook authentication

- HTTP server bound to `127.0.0.1` only — not exposed externally.
- Token-based auth: every workflow's webhook trigger gets a random token; incoming POST must include it as `X-Freyja-Token` header.
- Token rotation: operator can rotate per workflow via the renderer.
- For external services that need to reach the webhook (GitHub, Linear), the operator runs a tunnel (ngrok, cloudflared) themselves; Freyja doesn't manage external exposure.

### 7.10 Secret management

- Workflows often need API keys (GitHub, Slack, Linear, etc.).
- Today: env vars in `.env`. Fragile, no per-workflow scoping.
- Production: per-workflow secret references that resolve from macOS Keychain.
  ```yaml
  agent:
    secrets:
      GITHUB_TOKEN: keychain:freyja/github_token
  ```
- Secrets are injected into the engine subprocess's env only for runs of that workflow.
- Secrets are never logged, never written to transcripts, never sent to LLM (unless the workflow explicitly opts in via a `expose_secret_to_llm:` declaration).

### 7.11 Privacy

- All state stays local. No telemetry, no analytics, no remote logging.
- Notifications via Slack/push use operator-configured channels; operator's data, operator's choice.
- Audit log accessible only to the operator (file mode 0600 on the SQLite file).

### 7.12 Resource limits

- Per-workflow concurrency cap (default 1 — schedules don't queue if previous run still running; configurable up).
- Per-workflow daily fire cap (default unlimited; recommended in YAML).
- Per-engine memory cap (soft warning, hard kill at e.g. 2GB RSS).
- Total active runs cap (default 20; queued runs beyond cap stay pending).

### 7.13 Time-zone correctness

- All times stored in SQLite as Unix epoch (UTC).
- Cron expressions interpreted in the workflow's declared timezone.
- DST transitions: a fire scheduled for "2:30am" on a DST-spring-forward day fires once at the new time; no missed fire alarm.
- Operator travels across time zones: workflow timezones stay fixed unless changed.

## 8. Concrete designs

Specific enough that someone can start building.

### 8.1 Workflow YAML schema

```yaml
id: morning-pr-digest
name: Morning PR digest
version: 1
description: |
  Summarizes new activity on open GitHub PRs since yesterday's run.
  Groups by repo. Flags anything that needs my response.

triggers:
  - type: schedule
    cron: "0 9 * * 1-5"
    timezone: America/Los_Angeles
    catchup: skip
  - type: workflow_completed
    workflow_id: nightly-repo-scan
    when: success

mode: fresh                            # 'fresh' | 'continue' | 'persistent'

agent:
  model: claude-opus-4-7
  reasoning: high
  coordination_strategy: goal
  judge_rules:
    judge_profile: standard
    rigor_score: 2
  goal: |
    Summarize new activity on my open GitHub PRs since the last run.
    Group by repo. Flag anything that needs my response.
  prelude_template: |
    Previous run: {{previous_run.completed_at | default:"never"}}
    Previous summary findings:
    {{previous_run.knowledge_summary | default:"(first run)"}}
  secrets:
    GITHUB_TOKEN: keychain:freyja/github_token

capabilities:
  preset: unattended-safe
  overrides:
    http.get: ["https://api.github.com/*"]
    fs.write: ["~/Documents/freyja/digests/"]
    llm.spend:
      per_run_usd: 2.00
      per_day_usd: 10.00

outputs:
  - type: file
    path: ~/Documents/freyja/digests/pr-digest-{{run.started_at | date:'YYYY-MM-DD'}}.md
    content: assistant.final_message
  - type: notify
    channel: macos
    urgency: info
    title: "PR digest ready"
    body: "{{assistant.final_message | first_paragraph | truncate:200}}"

notification_policy:
  on_complete: info
  on_failed: attention
  on_budget_exceeded: alarm
  on_capability_denied: attention

retry_policy:
  max_attempts: 2
  backoff: exponential
  base_seconds: 60

concurrency:
  max_concurrent_runs: 1
  on_overlap: skip                     # 'skip' | 'queue' | 'replace'

enabled: true
```

### 8.2 Run state machine

```
                  ┌─────────┐
                  │ pending │
                  └────┬────┘
                       │ engine.claim()
                       ▼
                  ┌─────────┐
                  │ claimed │
                  └────┬────┘
                       │ engine.begin()
                       ▼
                  ┌─────────┐
            ┌─────┤ running ├─────┐
            │     └────┬────┘     │
            │          │          │
            │     pause│          │tool.write blocked
            │          ▼          │
            │     ┌─────────┐     │
            │     │ paused  │     │
            │     └────┬────┘     │
            │          │ resume   │
            │          └─────┐    │
            │                ▼    ▼
   ┌────────┴────────┬──────────┴──────────┐
   ▼                 ▼                      ▼
┌────────┐    ┌───────────┐         ┌───────────────────┐
│ done   │    │ failed    │         │ timed_out         │
└────────┘    └───────────┘         └───────────────────┘
                          ┌───────────────────┐
                          │ budget_exceeded   │
                          └───────────────────┘
                                  ┌───────────────┐
                                  │ cancelled     │
                                  └───────────────┘
```

Terminal states: `done`, `failed`, `timed_out`, `budget_exceeded`, `cancelled`. All other states are transient.

### 8.3 SQLite schema

```sql
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE schema_version (
  version INTEGER PRIMARY KEY,
  applied_at INTEGER NOT NULL
);

CREATE TABLE workflows (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  definition_yaml TEXT NOT NULL,        -- canonical source
  source_path TEXT NOT NULL,            -- where the file lives on disk
  scope TEXT NOT NULL,                  -- 'operator' | 'project'
  enabled INTEGER NOT NULL DEFAULT 1,
  created_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL
);

CREATE TABLE schedules (
  workflow_id TEXT NOT NULL,
  cron TEXT NOT NULL,
  timezone TEXT NOT NULL DEFAULT 'UTC',
  catchup TEXT NOT NULL DEFAULT 'skip',
  last_fire_at INTEGER,
  next_fire_at INTEGER NOT NULL,
  PRIMARY KEY (workflow_id, cron),
  FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
);

CREATE INDEX idx_schedules_next_fire ON schedules(next_fire_at);

CREATE TABLE trigger_subscriptions (
  id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL,
  source_type TEXT NOT NULL,            -- 'webhook' | 'fs' | 'calendar' | 'slack' | 'github' | ...
  source_config TEXT NOT NULL,          -- JSON
  filter_expression TEXT,               -- optional CEL-like expression
  enabled INTEGER NOT NULL DEFAULT 1,
  FOREIGN KEY (workflow_id) REFERENCES workflows(id) ON DELETE CASCADE
);

CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  workflow_id TEXT NOT NULL,
  workflow_version INTEGER NOT NULL,
  trigger_source TEXT NOT NULL,
  trigger_event_id TEXT,
  idempotency_key TEXT UNIQUE,
  parent_run_id TEXT REFERENCES runs(id),
  state TEXT NOT NULL,
  state_changed_at INTEGER NOT NULL,
  engine_pid INTEGER,
  engine_socket_id TEXT,
  started_at INTEGER,
  ended_at INTEGER,
  budget_cents INTEGER,
  spent_cents INTEGER NOT NULL DEFAULT 0,
  retries INTEGER NOT NULL DEFAULT 0,
  pause_reason TEXT,
  pause_payload TEXT,                   -- JSON; e.g. {"awaiting": "operator_reply", "question": "..."}
  FOREIGN KEY (workflow_id) REFERENCES workflows(id)
);

CREATE INDEX idx_runs_state ON runs(state);
CREATE INDEX idx_runs_workflow_id ON runs(workflow_id);
CREATE INDEX idx_runs_state_changed_at ON runs(state_changed_at);

CREATE TABLE sessions (
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  agent_type TEXT NOT NULL,
  model TEXT NOT NULL,
  status TEXT NOT NULL,
  message_count INTEGER NOT NULL DEFAULT 0,
  last_event_at INTEGER,
  FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts INTEGER NOT NULL,
  run_id TEXT,
  session_id TEXT,
  actor TEXT NOT NULL,                  -- 'engine' | 'operator' | 'workflow' | 'trigger:<source>'
  action TEXT NOT NULL,                 -- 'tool_call:fs.write' | 'capability_check' | ...
  target TEXT,
  capability_checked TEXT,
  allowed INTEGER NOT NULL,
  reason TEXT,
  duration_ms INTEGER,
  cost_cents INTEGER
);

CREATE INDEX idx_audit_run ON audit_log(run_id);
CREATE INDEX idx_audit_ts ON audit_log(ts);
CREATE INDEX idx_audit_action ON audit_log(action);

CREATE TABLE tool_intents (
  intent_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  tool_name TEXT NOT NULL,
  arguments_json TEXT NOT NULL,
  idempotency TEXT NOT NULL,            -- 'safe' | 'uncertain' | 'unsafe'
  started_at INTEGER NOT NULL,
  result_id INTEGER,                    -- FK to tool_results
  FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE TABLE tool_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  intent_id TEXT NOT NULL UNIQUE,
  completed_at INTEGER NOT NULL,
  is_error INTEGER NOT NULL,
  result_text TEXT,
  FOREIGN KEY (intent_id) REFERENCES tool_intents(intent_id) ON DELETE CASCADE
);

CREATE TABLE events_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at INTEGER NOT NULL,
  run_id TEXT,
  channel TEXT NOT NULL,
  urgency TEXT NOT NULL,
  payload TEXT NOT NULL,                -- JSON
  scheduled_for INTEGER,                -- for batching/DND
  sent_at INTEGER,
  ack_at INTEGER,
  reply TEXT
);

CREATE INDEX idx_outbox_scheduled ON events_outbox(scheduled_for) WHERE sent_at IS NULL;

CREATE TABLE idempotency_keys (
  key TEXT PRIMARY KEY,
  run_id TEXT NOT NULL,
  resolved_at INTEGER NOT NULL,
  FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE TABLE notifications_inbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  outbox_id INTEGER NOT NULL,           -- which delivery generated this
  run_id TEXT,
  workflow_id TEXT,
  urgency TEXT NOT NULL,
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  arrived_at INTEGER NOT NULL,
  read_at INTEGER,
  archived_at INTEGER,
  snoozed_until INTEGER,
  primary_action_url TEXT,
  FOREIGN KEY (outbox_id) REFERENCES events_outbox(id)
);
```

### 8.4 Orchestrator ↔ engine protocol

JSON-line over UDS. Each message has a `type` discriminator.

Engine connects and identifies:
```json
{"type": "engine_hello", "pid": 12345, "capacity": 1, "version": "1.0", "model_keys_available": ["anthropic", "openai", "fireworks"]}
```

Orchestrator dispatches:
```json
{"type": "claim_run", "run_id": "run_abc", "deadline_ms": 30000}
```

Engine acks claim (or rejects if at capacity):
```json
{"type": "claim_ack", "run_id": "run_abc"}
{"type": "claim_reject", "run_id": "run_abc", "reason": "at_capacity"}
```

Engine streams events (same shape as today's bridge events, scoped to run_id):
```json
{"type": "run_event", "run_id": "run_abc", "event": {"type": "text_delta", "sessionId": "...", "text": "..."}}
```

Engine signals state changes:
```json
{"type": "run_state", "run_id": "run_abc", "state": "completed", "spent_cents": 47}
{"type": "run_state", "run_id": "run_abc", "state": "paused", "pause_reason": "operator_reply_required", "pause_payload": {"question": "should I escalate?"}}
```

Heartbeats (every 30s; orchestrator considers run stalled after 3 missed):
```json
{"type": "heartbeat", "run_ids": ["run_abc", "run_def"]}
```

Orchestrator can request cancel:
```json
{"type": "cancel_run", "run_id": "run_abc", "reason": "operator_cancel"}
```

### 8.5 Orchestrator ↔ renderer protocol

JSON-line over UDS with explicit request/response correlation.

Renderer connects, requests state snapshot:
```json
{"type": "hello", "correlation_id": "c1"}
```

Orchestrator responds with snapshot of all active workflows and recent runs:
```json
{"type": "hello_ack", "correlation_id": "c1", "workflows": [...], "active_runs": [...], "inbox_unread": 7}
```

Renderer subscribes to live events for specific runs (or all):
```json
{"type": "subscribe", "correlation_id": "c2", "scope": {"run_ids": ["run_abc"]}}
{"type": "subscribe", "correlation_id": "c3", "scope": {"workflow_ids": ["morning-pr-digest"]}}
{"type": "subscribe", "correlation_id": "c4", "scope": "all"}
```

Orchestrator pushes events:
```json
{"type": "event", "subscription_id": "c2", "event": {...}}
```

Renderer commands (workflow CRUD, run cancel, capability grant, etc.):
```json
{"type": "command", "correlation_id": "c5", "command": "workflow.update", "params": {...}}
```

### 8.6 Trigger source plugin protocol

```python
from typing import Protocol, Callable, Awaitable
from dataclasses import dataclass

@dataclass
class TriggerEvent:
    source: str               # 'schedule' | 'webhook' | 'fs' | ...
    source_event_id: str      # for idempotency
    arrived_at: float         # unix epoch
    payload: dict             # source-specific
    workflow_id: str          # which workflow this is for (resolved by source)

@dataclass
class TriggerSubscription:
    id: str
    workflow_id: str
    config: dict              # source-specific
    filter_expression: str | None

@dataclass
class ValidationResult:
    valid: bool
    errors: list[str]

class TriggerSource(Protocol):
    name: str  # 'schedule' | 'webhook' | 'fs' | ...

    async def start(
        self,
        subscriptions: list[TriggerSubscription],
        on_event: Callable[[TriggerEvent], Awaitable[None]],
    ) -> None: ...

    async def stop(self) -> None: ...

    def validate_config(self, config: dict) -> ValidationResult: ...

    async def on_subscription_added(self, subscription: TriggerSubscription) -> None: ...

    async def on_subscription_removed(self, subscription_id: str) -> None: ...
```

Built-in sources for v1: `schedule`, `webhook`, `fs`, `workflow_completed`. v2: `slack`, `github`, `calendar`, `email_imap`.

### 8.7 Capability check API

In the engine, before any tool invocation:

```python
result = run.capabilities.check(
    capability="fs.write",
    target="/Users/sohamshah/Documents/freyja/digests/today.md",
)
if not result.allowed:
    audit_log.write(
        run_id=run.id,
        action=f"tool_call:{tool_name}",
        capability_checked="fs.write",
        allowed=False,
        reason=result.reason,
    )
    return ToolResult(
        call_id=call_id,
        content=json.dumps({
            "error": "capability_denied",
            "capability": "fs.write",
            "target": target,
            "reason": result.reason,
        }),
        is_error=True,
    )
```

Capability set methods:
- `check(capability, target=None) → CheckResult { allowed, reason }`
- `spend_budget(cents) → bool`  # returns false if exceeded
- `effective_set() → dict`  # for UI display

Capabilities supported (initial set):
- `fs.read`, `fs.write`, `fs.execute` — path glob list per capability
- `http.get`, `http.post`, `http.put`, `http.delete` — URL pattern list per capability
- `shell` — bool (with allowlist of commands as a refinement)
- `computer` — bool (mouse, keyboard, screen)
- `subagent.spawn` — list of allowed agent types
- `llm.spend` — `{per_run_usd, per_day_usd}` numerics
- `notify` — list of allowed channels
- `secret.read` — list of allowed keychain references

### 8.8 Notification fabric API

```python
from typing import Literal, Protocol
from dataclasses import dataclass

@dataclass
class Notification:
    id: str
    run_id: str | None
    workflow_id: str | None
    urgency: Literal['info', 'attention', 'alarm']
    title: str
    body: str
    primary_action_url: str | None
    reply_policy: Literal['none', 'optional', 'required']
    snooze_until: float | None = None

@dataclass
class DeliveryResult:
    delivered: bool
    channel_message_id: str | None
    error: str | None

class NotificationFabric:
    async def dispatch(self, notification: Notification) -> None: ...
    async def acknowledge(self, notification_id: str) -> None: ...
    async def reply(self, notification_id: str, reply: str) -> None: ...
    async def snooze(self, notification_id: str, until: float) -> None: ...

class NotificationChannel(Protocol):
    name: str
    async def deliver(self, notification: Notification) -> DeliveryResult: ...
    async def supports_reply(self) -> bool: ...
```

DND / batching policy:
```yaml
dnd:
  - urgency: info
    quiet_hours: "22:00-08:00"
    action: batch_until_morning
  - urgency: attention
    quiet_hours: "00:00-06:00"
    action: defer_to_06
  - urgency: alarm
    action: deliver_always
batching:
  morning_digest_time: "08:00"
  channel_preference: [macos, email]
```

Reply round-trip: a `reply_required` notification creates a pause on the run (state → `paused`, pause_reason: `awaiting_reply`). When the operator replies (via macOS notification action, Slack DM, or push), the orchestrator wakes the run and injects the reply into the next user turn.

## 9. UX surface

The paradigm shift requires UX shifts.

### 9.1 New: Operator inbox

Top-level view (not buried inside a session). Sorted, batchable attention queue.

Layout:
- Hero: "47 items since you last checked"
- List items grouped by workflow: `[morning-pr-digest] PR digest ready (8:32am)` with one-line preview, urgency dot, age
- Sort: urgency × age
- Actions per item: open run, reply (if reply_required), snooze, archive
- Filters: by urgency, by workflow, unread only

### 9.2 New: Workflows view

Top-level view replacing today's "session list" as the primary navigation.

Sections:
- Active workflows (those with recent runs or scheduled within 24h)
- All workflows (full list with enable/disable toggles)
- Per workflow: name, description, next-fire time, last-run summary, capability badge, budget gauge

Click into workflow → editor (YAML with schema validation, capability picker, history of past runs).

### 9.3 Evolved: Run viewer (today's session pane)

Header shows: workflow name (linked), trigger that fired this run, capability set summary, budget gauge.

Body: live transcript as today, but with inline audit log markers showing each tool call's capability decision (`✓ fs.write` or `✗ http.post: not in allowlist`).

For paused runs: prominent "Waiting on operator reply: <question>" with reply composer.

For workflows with sub-agents: the swarm view (today's pattern) shows children with capability inheritance visible.

### 9.4 New: Audit log viewer

SQL-backed table viewer.

Filters: workflow, run, time range, capability, allowed/denied.

Useful queries pre-baked:
- "What did the morning-pr-digest workflow do in the last 30 days?"
- "Show all capability denials this week"
- "Which tool calls exceeded $0.10?"

### 9.5 New: Cost dashboard

Per-workflow spend (daily, weekly, monthly).
Projections vs caps.
Outliers ("this fire cost 3x average; click to see why").

### 9.6 Evolved: Live activity strip

Always-visible across the app (collapsed by default). Expands to today's MissionDashboard.

Shows: currently-running runs with progress, recently-completed runs with outcome, next-fire countdown for upcoming schedules.

### 9.7 Workflow editor

YAML editor with:
- Schema validation (errors inline)
- Autocomplete (capabilities, trigger types, model names)
- Capability picker UI (visual checkboxes that generate the YAML)
- "Test fire" button: dry-run against a sample trigger payload OR real fire with confirmation
- History tab: past runs with cost, duration, outcome
- Diff vs. file on disk (workflows can be edited externally; renderer detects and offers reload)

### 9.8 Trigger configuration UI

Per-source UIs (since each source has different config):
- Webhook: show the localhost URL + token, copy buttons; recent received events log
- File watcher: paths watched with checkbox tree; recent triggered files
- Calendar: subscribed calendars list with auth flow
- Slack: workspace + channel auth + recent message previews

### 9.9 Notification settings

Channel configuration:
- Per-channel auth (Slack workspace + token, Pushover key, etc.)
- Per-channel enable/disable
- Quiet hours per channel
- Test notification button

Routing rules:
- Per-urgency channel preferences
- Per-workflow overrides

## 10. Sequencing / phased plan

Six phases by dependency. Each delivers value independently.

### Phase A — Workflow/Run/Session model + SQLite migration

**Goal**: Introduce the three-noun model and durable SQL state. Preserve all existing functionality.

**Deliverables**:
- SQLite schema + migrations infrastructure
- Workflow loader (`workflows/*.yaml` → DB; reload on file change)
- Run state machine (Python class + DB-backed transitions)
- Backward compat: existing interactive sessions become runs of implicit `interactive` workflow
- Renderer reads workflow + run + session metadata from DB via orchestrator
- One-time migration script: import existing `~/.freyja/sessions/*.json` into SQLite as sessions of an `interactive` workflow

**Risk**: schema design. Getting it right is cheap to extend, expensive to rework. Worth a careful design review pre-implementation.

**Effort**: 2-3 weeks of focused work.

### Phase B — Orchestrator + engine worker split

**Goal**: Decouple lifecycle. Daemon runs always; engines are pooled.

**Deliverables**:
- Orchestrator process: launchd-launched, owns DB, runs trigger sources
- Engine worker process: pool of 1+ (configurable), claims runs
- UDS-based IPC: orchestrator ↔ engine, orchestrator ↔ renderer
- Renderer attach/detach with state snapshot exchange
- Crash recovery for orchestrator + engine restarts
- Single-process dev mode (orchestrator + engine in one process) for debugging

**Risk**: getting run-claim semantics right (only one engine claims a run; orphaned claims requeue safely).

**Effort**: 2-3 weeks.

### Phase C — Capability-based policy + audit log

**Goal**: Trust model for unattended action.

**Deliverables**:
- Capability set declared per workflow (in YAML)
- Capability check at every tool boundary
- Audit log entry for every check
- Audit log viewer (SQL-backed query UI)
- Preset bundles (interactive / unattended-safe / unattended-trusted)
- Operator review screen for capability grants (diff against current grants on workflow edit)

**Effort**: 1-2 weeks. Mostly instrumentation.

### Phase D — Trigger source plugin protocol + initial sources

**Goal**: External signals can fire workflows.

**Deliverables**:
- TriggerSource Protocol + dispatch loop in orchestrator
- Built-in sources: schedule (cron), webhook (localhost HTTP), file_watcher, workflow_completed
- Trigger configuration UI per source
- Token-based auth for webhooks
- Idempotency key handling

**Effort**: 2-3 weeks.

### Phase E — Notification fabric + operator inbox

**Goal**: Reachability + attention management.

**Deliverables**:
- NotificationFabric with channels: macos, slack, push (via Pushover and/or ntfy.sh)
- DND + batching policy engine
- Notification reply round-trip (`reply_required` notifications pause runs until reply)
- Operator inbox UI (new top-level view)
- Per-workflow notification policy

**Effort**: 2-3 weeks. DND/batching logic needs care.

### Phase F — Persistent watchers + knowledge base

**Goal**: Long-running sessions that learn over time.

**Deliverables**:
- `mode: persistent` workflow mode
- Knowledge base (`knowledge.jsonl`) per persistent session
- `kb.put`, `kb.get`, `kb.search` tools (with embeddings via local model or remote API)
- Periodic consolidation pass (sub-agent that promotes transcript findings to KB)
- Agent-set-wake tool (`set_next_wake(at | on_event)`)
- UI status: "asleep · next wake in 47m"

**Effort**: 2-3 weeks. Most of the work is prompt-shape design for the wake cycle.

### Phase G — Polish + composition + extensions

**Goal**: Make the platform a place to live.

**Deliverables**:
- Workflow library / sharing (export/import YAML bundles)
- Workflow dependency edges (`depends_on:`)
- Cost projection + spend reports
- Reconciliation loop (Symphony pattern: external state changes can cancel runs)
- Additional trigger sources: Slack, GitHub, calendar, email IMAP
- Mobile companion (PWA against the orchestrator on home network)
- Workflow visualization (DAG view)

**Effort**: Open-ended; ~4 weeks for the listed pieces.

**Total realistic timeline**: 14-18 weeks of focused work to phase F (ambient agent fabric working end-to-end). Phase G is ongoing.

## 11. Open questions / decisions to lock in

Before writing code, lock these in:

1. **YAML vs code for workflows.** Proposed: YAML primary + Python extension hatch. Decision needed.
2. **One DB or per-workflow DBs.** Proposed: one DB with `workflow_id` partitioning on all tables. Decision needed.
3. **Default N engines.** Proposed: N=1 default, configurable up via setting. Decision needed.
4. **Default permission preset for new workflows.** Proposed: `unattended-safe`. Decision needed.
5. **Push notification provider.** Options: APNs (paid + proxy), Pushover ($5 one-time, simple), ntfy.sh (free, self-hostable). Proposed: ship with Pushover + ntfy.sh; APNs later. Decision needed.
6. **macOS computer-use in unattended mode.** Proposed: not allowed in v1; requires explicit opt-in with warning banner in v2. Decision needed.
7. **Default catch-up policy for schedules.** Proposed: `skip`. Decision needed.
8. **Workflow definition source location.** Proposed: both `~/.freyja/workflows/` and `<repo>/.freyja/workflows/`. Decision needed.
9. **Mobile companion: PWA vs native.** Proposed: PWA for v1 simplicity, native later. Decision needed.
10. **Telemetry / analytics.** Proposed: none (local-first, no remote logging). Decision needed.
11. **Secret storage.** Proposed: macOS Keychain via `keychain:freyja/<key>` references in YAML. Linux fallback: encrypted file with system-keyring unlock. Decision needed.
12. **Knowledge base embeddings provider.** For watcher KB semantic search: local (e.g. sentence-transformers via subprocess) vs remote (OpenAI ada via existing key). Proposed: remote initially for simplicity, local later. Decision needed.

## 12. Risks

Things to watch for during implementation:

- **Scope creep**: 14-18 weeks is a substantial chunk. Interactive Freyja still needs to ship improvements in parallel. Each phase MUST ship value independently — no "Phase A is foundation, Phase F is value." Phase A delivers SQLite-backed query/audit immediately; Phase B delivers daemon survival; etc.
- **State migration complexity**: existing users have JSON-per-file persistence. The migration script needs careful testing (dry-run mode, rollback path).
- **Backward compat for in-flight sessions during deploy**: when the new orchestrator launches for the first time on a machine with running sessions, the old bridge must finish what it's doing or its state must be migrated mid-flight. Coordinated cutover required.
- **The trust model is socially complex**: requiring explicit capability grants creates friction. Mitigation: smart defaults (`unattended-safe` covers 80% of cases), capability suggestions derived from the workflow's stated tools, "I learned this workflow needs X — grant?" prompts.
- **Notification noise**: with 10+ workflows running, the operator will drown unless the batching/DND logic is sharp. This is product work as much as engineering work. May need user-research iteration after launch.
- **Persistent watchers compounding costs**: a watcher that wakes every hour and runs the deep profile costs ~$1/day. 10 such watchers = $300/month. Need clear cost visibility from day one + cost projection warnings before enabling expensive watchers.
- **Process boundary debugging**: when something fails between orchestrator and engine, debugging the IPC is harder than today's single process. Need good structured logs + a "single-process dev mode" that runs orchestrator + engine in one Python process.
- **macOS-specific dependencies**: launchd, Keychain, macOS notifications. The architecture is portable but the implementation isn't initially. Linux/Windows users get a degraded experience until porting. Document the gap explicitly.
- **Watcher consolidation correctness**: the periodic sub-agent that promotes transcript findings to KB can hallucinate or compress badly. Need a review surface for KB entries (operator can curate) and validation that the KB doesn't accumulate contradictions silently.
- **Webhook security**: localhost-only binding plus token auth is good but not foolproof. Malicious processes on the same machine could read the token from disk. Mitigation: 0600 mode on token files, regenerate per-workflow, document the trust assumption.

## 13. What success looks like

Concrete acceptance criteria for the production-grade version. If we can demo these end-to-end, we've succeeded:

- I can close my laptop at 5pm with Freyja running 4 schedules + 2 watchers. The schedules fire on time. The watchers wake on their cadences. When I open my laptop at 8am, I have:
  - 4 digest markdown files in `~/Documents/freyja/digests/`
  - An operator inbox with 6 items, 2 marked attention
  - A spend summary showing $0.84 used overnight, well under budget
- I can ask a workflow "how much have you cost me this month?" via a SQL query on the audit log and get a real answer.
- I can edit a workflow YAML, hit save, and the schedule picks up the change without restarting anything.
- I can `git pull` and discover a new workflow in `<repo>/.freyja/workflows/` — Freyja loads it automatically.
- An agent run that hits a budget cap pauses, fires a notification with the run ID, and resumes when I grant more budget.
- I can ask Freyja to dry-run a workflow against a captured webhook payload, see what it would do, and grant capabilities accordingly.
- A workflow that fails 3 times in a row pauses itself and alerts me — doesn't keep burning money in a retry loop.
- Killing the orchestrator process (laptop crash, force quit, kill -9) and restarting it recovers cleanly — all in-flight runs either resume or transition to a known state.
- A persistent watcher that's been running for 90 days has a knowledge base of 200 entries I can browse and curate. The watcher's per-wake prompt stays bounded; the KB grows.
- A notification on my phone at 3am asks "should I escalate this regression?" I tap "yes" and the answer routes back into the watcher's next think loop.
- I can share my morning-pr-digest workflow with a colleague by exporting a single YAML file; they import it, grant capabilities, and it runs in their Freyja with their secrets.

This is a tall order. But it's the right ceiling.

## 14. Open architectural questions for future thought

Capturing here so they don't get lost:

- **Workflow versioning and rollback**: if I edit a workflow and the next fire goes badly, can I roll back to the prior version? Probably yes — every workflow change creates a new row in `workflows` with incremented version, runs reference `workflow_version`.
- **Workflow as a sub-program**: can workflow A invoke workflow B as a sub-routine (not just chained via dependency)? Worth thinking about — would enable composition like "the morning digest workflow calls the PR-summarize workflow per repo, then aggregates."
- **Cross-machine sync**: even local-first, an operator with a desktop + laptop might want some workflows on both. Sync protocol? CRDT for workflow definitions?
- **Workflow forking / branching**: like git branches but for workflow definitions. Edit a copy, test it, merge back when happy.
- **Live observability for power users**: a tail-like CLI that streams audit_log entries in real time. `freyja tail --workflow morning-pr-digest --capability fs.write`.
- **Workflow marketplaces (eventually)**: a registry of community-contributed workflows. Trust model: download → review YAML + capability list → install.

## 15. Glossary

- **Workflow**: a definition. What runs when, under what policy.
- **Run**: one execution of a workflow.
- **Session**: the conversational unit inside a run.
- **Trigger source**: external system (cron, webhook, fs, etc.) that emits events.
- **Trigger event**: a single fire from a source, with an idempotency key.
- **Capability set**: declarative permissions a workflow has been granted.
- **Orchestrator**: the always-on daemon. Owns state, schedules, triggers, policy.
- **Engine worker**: a pooled Python process that executes runs.
- **Persistent watcher**: a workflow with `mode: persistent` — sleeps between wakes.
- **Knowledge base**: structured, append-only fact store per persistent session.
- **Operator inbox**: the batched attention queue surfacing notifications.
- **DND policy**: rules for batching/deferring notifications by urgency × time.

## 16. Document changelog

- 2026-05-13: Initial draft.
