# Bridge production roadmap

**Status.** Plan — not yet started. Written 2026-05-28 after a long
debugging session for the Slack gateway surfaced architectural debt
that the per-feature fix cadence couldn't resolve. This document
captures the path to "I would deploy this for someone other than
me." Each epic is independently scoped; sequencing is captured at
the bottom.

**Cross-refs.**
- [`SLACK-GATEWAY.md`](./SLACK-GATEWAY.md) — current Slack gateway
  shape. This roadmap is what supersedes it.
- [`ALWAYS-ON-PLATFORM.md`](./ALWAYS-ON-PLATFORM.md) — broader
  always-on architecture sketch. Several of the ideas here (event
  sourcing, supervisor-style turn lifecycle) trace back to it.

---

## 0. Problem statement

The current architecture has two `_BridgeState` instances: one in
the long-running daemon (managed by launchd, hosts the Slack
adapter), one spawned per-launch by Electron (managed by the main
process, hosts the desktop UI). Both write to the SAME transcript
files in `~/.freyja/sessions/`.

That single decision — two writers, no coordination — is the root
cause of most of the bugs we shipped and patched today. A
non-exhaustive list:

- **Filename sanitizer drift.** Daemon and renderer each had their
  own sanitizer; the daemon's strip-style and the renderer's
  underscore-replace style produced different paths for the same
  session id, so the renderer couldn't find the daemon's writes.
  Patched with a backward-compat resolver + one-shot migration.
- **Stale cached slice on click.** The renderer cached
  `sessionArchive[id]` from the last view; when the daemon kept
  writing to the same session via Slack, the desktop showed
  arbitrarily old content on next click. Patched with a per-click
  cache bypass.
- **Stale message count in sidebar.** `hydrateFromDisk` only added
  new sessions; existing rows never refreshed their metadata as the
  daemon advanced them. Patched with a gateway-aware merge.
- **Concurrent-inbound race.** Two Slack messages arriving close
  together both registered consumers and both overwrote
  `session.gateway_source` at MESSAGE arrival time. The
  in-flight turn for message 1 then routed its `send_attachment`
  + approval-prompt calls to message 2's thread, and message 2
  got no response at all because its consumer wrongly finalized
  on message 1's `turn_complete`. Patched with a per-turn
  `on_turn_start` hook.
- **Multi-workspace echo loop.** The bot-message filter compared
  against `self._bot_user_id` (singular), missing secondary
  workspaces' bot ids. Patched with `self._team_bot_user_ids.values()`.
- **Send_attachment registration race.** `_BridgeSession.initialize()`
  ran during `try_restore_transcript()`, which itself ran during
  `ensure_session()`, which itself ran BEFORE `route_message`
  set `gateway_source`. So the registration block always saw
  `gw_source is None` and silently no-oped. Patched by threading
  `gateway_source` through `ensure_session()`.
- **Sub-agent inheritance.** Sub-agents inherited `send_attachment`
  AS A TOOL only when their `agent_type.tool_include` whitelist
  didn't strip it. Patched with a force-include set.

Every one of those is a symptom of the same root cause: state is
mutated at the wrong moment by the wrong process. Patches are
holding for now, but the next feature touching this surface area
will hit the same class of bug.

**The architectural fix is to collapse to one writer.** Everything
in this roadmap either supports that consolidation, or builds on
it. Doing it in pieces — keeping the two-bridge shape while bolting
on more sync — would produce the same outcome we already have.

---

## 1. Goals and non-goals

### Production-grade means

1. **Correctness under concurrency.** Two messages arriving in
   the same DM within 100ms produce two cleanly-separated
   conversations in the right threads, with the right contexts,
   without cross-contamination. Today this requires a per-turn
   hook on a state machine that's only partially decoupled.

2. **Correctness under crash.** A `launchctl stop` or panic mid-
   turn doesn't lose user work, doesn't double-respond on
   restart, doesn't leave the user staring at a half-message.

3. **Observability sufficient to debug a stranger's install.**
   Structured logs, health endpoints, audit trails, metrics.
   "Send a Slack message and hope" is not how we should be
   diagnosing failures.

4. **Bidirectional surface support.** The desktop and Slack are
   two views into the same conversation. Type in either; the
   other reflects state live. Continue a Slack conversation in
   the desktop without ceremony.

5. **Resource bounds.** Caches don't grow forever; transcripts
   don't bloat to 60MB; OOM-bait isn't accepted from external
   input.

6. **Secrets handling that survives audit.** API tokens not in
   plaintext, not in env vars visible to `ps`, not in plist
   files that have ever been world-readable.

### What we're explicitly NOT building

- **Slack workflow steps, app home, slash command UIs beyond
  what we already ship.** This roadmap is about the runtime,
  not the surface.
- **Multi-operator collaboration.** Single operator per install.
- **Cloud-hosted variant.** Local-only daemon. If we ever want
  a hosted version, half this work is reusable but the deployment
  story is different.
- **Vendor parity with hosted assistants** (Slack AI, etc.). We
  do specific things they don't and don't try to do what they do.
- **Provider abstraction layer.** Keep the existing engine.runner
  shape; LLM provider swaps are a separate concern.

---

## 2. Foundational decisions (made, with rationale)

These shape every epic. Locking them down here so they're not
re-litigated mid-implementation.

### D1. One bridge daemon. Two clients attach.

The daemon process owns all session state, all transcript writes,
all tool execution, all adapter connections. The Slack adapter
runs INSIDE it (already does). The desktop UI is a CLIENT of it,
connecting via local IPC.

This is the inversion of today's architecture. Today the desktop
spawns its own Python bridge subprocess; the daemon is a separate
process; they share filesystem state via convention. Tomorrow
there's one Python process and the desktop talks to it.

Rationale: every concurrent-write bug we have is "two writers."
Coordinating two writers via filesystem conventions is a known
losing pattern. Collapsing to one writer is well-understood and
solves the class of bug, not the specific instance.

### D2. Sessions are state machines persisted via event sourcing.

A session today is "a JSON file with a transcript array." A
session tomorrow is "an append-only NDJSON event log of state
transitions, with a periodically-rebuilt materialized view."

Transitions: `created`, `user_turn_received`, `assistant_turn_started`,
`tool_called`, `tool_result_received`, `assistant_turn_streamed`,
`assistant_turn_completed`, `turn_cancelled`, `turn_failed`, etc.

Rationale: lets us crash mid-turn and resume. Lets us subscribe
multiple clients (desktop, Slack adapter) to the same session
without race. Lets us audit precisely what the daemon did and when.

### D3. IPC protocol is versioned, framed JSON over Unix domain socket.

Not HTTP, not gRPC. Length-prefixed JSON frames over `~/.freyja/.bridge.sock`.

Rationale: simplest thing that supports streaming both directions,
has zero deployment dependencies, gives us bidirectional async
naturally. HTTP would require an embedded server in the daemon and
SSE/WebSocket complexity. gRPC requires schema generation tooling
in two languages.

### D4. Atomic single-file transcripts are dead.

Today every turn rewrites the entire transcript file. With a
60-turn session that's 60 full N-message file rewrites. Plus a
crash mid-write corrupts the file.

Future: append-only NDJSON for the event log. Periodic compaction
into a snapshot + recent-events tail.

### D5. The desktop is a renderer, not a runtime.

The desktop's job is to subscribe to events for an active session
and render them. It does not execute tools. It does not call LLMs.
It does not own session state. Everything happens in the daemon.

This is the biggest change for the desktop codebase. Most of the
current `bridge.ts` machinery — process spawn, PYTHONPATH wiring,
environment scrubbing — goes away.

### D6. Production targets local-only single-operator first.

Multi-tenant, cloud-hosted, multi-operator are explicitly out of
scope for this roadmap. We design the architecture so they're
possible later (single bridge daemon scales horizontally; the
event log is shippable) but we don't implement them.

---

## 3. The epics

Each epic below is independently shippable AFTER its declared
dependencies. Tasks within an epic are roughly ordered.

### EPIC 1 — Single bridge process. Two surfaces attach to it.

**Problem.** Two writers (daemon + desktop's spawned Python
bridge) to the same transcript files. Every concurrent-write bug
in §0 traces here.

**Approach.** Make the daemon the canonical bridge. The desktop's
main process connects to it via a Unix domain socket and forwards
events to the renderer over the existing preload IPC.

**Tasks.**
1. **Define the IPC protocol.** Versioned schema document under
   `docs/BRIDGE-IPC-PROTOCOL.md`. Methods (RPC-style):
   `subscribe(session_id)`, `unsubscribe(session_id)`,
   `list_sessions()`, `send_user_turn(session_id, content,
   attachments, source_metadata)`, `cancel_turn(session_id,
   turn_id)`, `health()`, `metrics()`. Server-pushed events:
   `text_delta`, `tool_use_start`, `tool_input_end`,
   `tool_result`, `turn_started`, `turn_completed`, `system_event`,
   `session_state_changed`.
2. **Implement the daemon's IPC server.** Bind to
   `~/.freyja/.bridge.sock` (0600 mode). Length-prefixed JSON
   frames. Per-client subscription tracking. Backpressure aware
   (slow client doesn't block fast clients).
3. **Build a thin client in `src/main/`.** Replaces today's
   `bridge.ts` Python spawn machinery. Connects on app start,
   reconnects on disconnect with exponential backoff. Forwards
   events to the renderer via existing IPC channels (no renderer
   changes needed at this layer).
4. **Remove the desktop's Python bridge subprocess machinery.**
   `pythonSpawnEnv`, `resolvePythonCli`, PYTHONHOME logic in
   `bridge.ts` — all gone. The daemon is the only Python process.
5. **Migrate desktop session creation through IPC.** What the
   desktop calls `new_session` today becomes
   `daemon.send_user_turn(<new_session_id>, ...)` after
   `daemon.create_session(...)`.
6. **Single-binary install path.** The daemon binary is the
   Python bundle in the .app's Resources. The Electron main
   process launches the daemon if not running. launchd manages
   it across logins.
7. **Backward-compat read of existing sessions.** Migration from
   old format → event log on first read.

**Dependencies.** None.

**Estimated effort.** 8–12 engineering days.

**Open questions.**
- Single daemon shared across multiple workspaces, or one daemon
  per workspace? (Recommendation: single daemon, isolation via
  session id namespacing.)
- Daemon restart behavior when the desktop is connected — drop
  the connection cleanly, let the desktop reconnect?
- Permission story for the Unix socket on multi-user macs.

---

### EPIC 2 — Turn lifecycle that survives daemon restart.

**Problem.** Today a turn is a transient `asyncio.Task`. Daemon
crash mid-turn → user sees half a message → state is lost. Every
`npm run rebuild` cycles the daemon, so this isn't an edge case;
it's the dominant interaction.

**Approach.** Turns become persistent state machines. The daemon
writes intent before acting, transitions on durable storage, and
recovers in-flight turns on restart.

**Tasks.**
1. **Turn ledger.** Append-only NDJSON file per session at
   `~/.freyja/sessions/<id>.turns.json`. Each record:
   `{turn_id, source, content, attachments, state, started_at,
   completed_at?, error?}`. fsync'd on write.
2. **Turn id derivation.** Use Slack's `event_ts` as the turn id
   for Slack-originated turns; UUIDs for desktop-originated. This
   doubles as idempotency: a duplicate inbound (Socket Mode
   redelivery) finds the turn already in the ledger and no-ops.
3. **State machine implementation.** `queued → running →
   tool_executing → streaming → completed | failed | cancelled`.
   Transition on durable disk write before any user-visible
   side effect.
4. **Crash recovery on startup.** Scan turn ledgers for
   non-terminal turns. For each, decide:
   - If the LLM call hadn't started: re-enqueue from `queued`.
   - If mid-call: try resume via Anthropic's idempotency key
     (their `metadata` field; provider-specific support varies).
     Fall back to posting "your previous turn was interrupted —
     retry?" with Block Kit buttons.
   - If post-LLM but pre-final: assume the LLM bill was paid;
     reconstruct the in-progress message, post it as final.
5. **Graceful shutdown.** SIGTERM stops accepting new turns,
   waits up to 30s for active turns to drain, then exits. SIGKILL
   relies on recovery (#4).
6. **Cancellation as a ledger record.** `/stop` writes
   `{turn_id, action: cancel}` to the ledger. Running task
   checks for cancellation at each await point.
7. **Turn-failed surface.** When a turn ends in `failed` state,
   post the structured error back to the originating chat with
   a "what went wrong" message that doesn't leak stack traces
   but is actionable.

**Dependencies.** Easier after EPIC 1 (single writer means no
coordination of ledger writes). Could be done before but at the
cost of additional cross-process care.

**Estimated effort.** 5–7 engineering days.

**Open questions.**
- Anthropic idempotency support semantics — verify that resuming
  a half-streamed response gives us the same completion vs. a
  fresh one. May need provider-specific resume strategies.
- Should the ledger be one file per session, or one global file?
  Tradeoff: per-session avoids cross-session contention but
  produces more files.

---

### EPIC 3 — Slack ⇄ Desktop handoff.

**Problem.** The user-stated goal: continue a Slack conversation
in the desktop, and vice versa.

**Approach.** Once EPIC 1 lands this is mostly UX work — the
daemon already owns the session, both surfaces subscribe. The
remaining design is the operator's affordances.

**Tasks.**
1. **"Open in Freyja" deeplink.** Slack slash command `/freyja
   open` posts a `freyja://session/<id>` URL. Click in Slack →
   opens Freyja → focuses on that session live.
2. **Source indicator in the desktop sidebar.** Each session
   row shows which surface dispatched the most recent turn
   (slack icon vs desktop icon). Helps the operator see where
   conversations live.
3. **Outbound from desktop on a Slack session.** When the
   operator types in the desktop input on a gateway session, the
   user-turn goes to the daemon with `source_metadata` carrying
   the operator's identity. `send_attachment` calls from that
   turn post to wherever the operator last interacted in Slack
   (so attachments still appear in the active Slack thread).
4. **Identity reconciliation.** The framed_text and
   gateway_source for a desktop-dispatched turn on a Slack
   session needs to be designed deliberately. Recommended:
   `chat_type=desktop_on_slack`, `chat_id=<slack_dm_channel>`,
   `thread_id=<last_active_thread>`. The system prompt block
   tells the agent it's responding via desktop but the chat
   surface is still Slack, so format accordingly.
5. **Inbound attachment surfacing in desktop.** A file uploaded
   in Slack appears in the desktop session's input bar as a
   visible attachment chip — operator sees "1 file attached from
   Slack" with a thumbnail.
6. **"Active in" indicator.** A small status pill in the title
   bar when an active turn is running on the current session,
   sourced from either surface.
7. **Cross-surface audit log.** Per session, the event log
   captures which surface dispatched each turn. Surfaced in
   session detail view as a column.

**Dependencies.** EPIC 1 (single bridge). EPIC 2 (lifecycle)
makes this nicer but isn't strictly required.

**Estimated effort.** 5–6 engineering days.

**Open questions.**
- Custom URL scheme registration on macOS — how does that work
  with the signed .app + sandbox?
- What's the right model when both surfaces have an unsent draft
  for the same session? Lock to one editor, or sync drafts?

---

### EPIC 4 — Transcript correctness.

**Problem.** Current transcripts are full-file rewrites per turn,
no schema versioning, no per-session size cap, no crash-safe writes
(despite tmp+rename being used in some paths). Plus the existing
59MB desktop transcript on disk is the predictable end-state for
any long-lived session.

**Approach.** Append-only event log with periodic compaction.

**Tasks.**
1. **Schema versioning.** Add `schema_version` to every record;
   single module owns migrations.
2. **Append-only NDJSON event log.** Each transition appends one
   line; the materialized view (current transcript array) is
   reconstructed on read or via a periodic compactor that
   rewrites a snapshot file.
3. **Compaction pass.** When the event log exceeds 10MB or 1000
   events, fold prior events into a snapshot file. Keep N
   snapshots; rotate the oldest into an `~/.freyja/sessions/
   archive/` directory.
4. **Per-session size cap.** Configurable in `gateway.yaml`,
   default 50MB. When approached, force a context-window
   compaction + archive prior transcript turns. The agent
   continues working with the compacted context.
5. **Disk-quota tracking.** Daemon tracks total
   `~/.freyja/sessions/` size; warns + force-archives when over
   user-configured threshold (default 5GB).
6. **Crash-safe writes via fsync.** Every append fsyncs the
   event log before returning. Trade durability for throughput
   — but most workloads are slow enough this doesn't matter.

**Dependencies.** Easier after EPIC 1. EPIC 2's ledger should
share infrastructure with this epic's event log (same NDJSON +
fsync primitive).

**Estimated effort.** 4–6 engineering days.

**Open questions.**
- Compaction-during-turn semantics — if compaction runs while a
  turn is appending, do we hold a lock or queue compaction?
- Schema migrations on read vs background migration daemon — both
  have UX implications during version transitions.

---

### EPIC 5 — Observability and rebuild safety.

**Problem.** "Send a Slack message and hope" is how we debug
today. Failures are silent (daemon down? socket dropped? LLM
rate-limited?). Rebuild safety is "it built so it must work."

**Approach.** Structured logs, health endpoint, rebuild
verification, metrics with retention.

**Tasks.**
1. **Structured logging.** Every log line is a JSON object:
   `{ts, level, logger, msg, session_id?, turn_id?, source?,
   event_type?, ...}`. Existing `~/.freyja/logs/gateway.log` becomes
   machine-parseable.
2. **`freyja gateway tail` CLI.** Filters on session/turn/source/
   level/event_type. Mirrors `journalctl --follow` ergonomics.
3. **Health endpoint via IPC.** `daemon.health()` returns:
   ```
   {
     connected_to_slack: bool,
     slack_team_id: str | null,
     active_turns: int,
     queued_turns: int,
     last_inbound_at: timestamp,
     last_outbound_at: timestamp,
     error_count_last_5min: int,
     uptime_sec: int,
     bridge_version: str,
   }
   ```
4. **Title bar status pill reads health.** Today's `slack live /
   slack stopped` pill becomes 5 states from health: `healthy /
   degraded / disconnected / failing / starting`. Click for
   details.
5. **Rebuild verification.** After `launchctl start` in
   `rebuild.sh`, connect to the daemon socket, call `health()`,
   poll for connected_to_slack=true within 10s. If absent, dump
   `gateway.err` tail + roll back to `/Applications/Freyja.old.app`
   (which we now keep on every rebuild).
6. **Slack delivery audit.** Every outbound API call records
   `{turn_id, slack_channel, slack_ts, method, latency_ms,
   http_status, retry_count}`. Persisted to disk; CLI:
   `freyja gateway audit --session=<id>`.
7. **Retry budget.** Outbound Slack API retries with exponential
   backoff on 5xx and 429 (honoring `Retry-After`). Cap total
   retries per call at 3. Failed-after-retry records on turn
   ledger.
8. **Metrics.** 24h rolling window: turn count, p50/p95 latency,
   error rate, tool-call count by name, average tokens per turn.
   CLI: `freyja gateway metrics`. Foundation for later Prometheus
   export.

**Dependencies.** EPIC 1 (health endpoint via IPC).

**Estimated effort.** 4–5 engineering days.

**Open questions.**
- Log retention policy — rotate by size? by age? Both?
- Metrics persistence across daemon restarts — in-memory only
  vs. periodic snapshot?

---

### EPIC 6 — Tool-execution safety and resource limits.

**Problem.** No turn budget (token / tool-call / wall-clock).
Inbound files can be arbitrarily large. Slack cache directory is
unbounded. Tokens leak via subprocess env. Outbound rate-limit
budget is per-tool, not global.

**Approach.** Enforce hard limits at the boundary. Configurable
where reasonable; safe defaults always.

**Tasks.**
1. **Per-turn resource budget.** Configurable in `gateway.yaml`:
   `max_tokens`, `max_tool_calls`, `max_wall_clock_sec`. Enforced
   in the turn loop. When exceeded, abort and post a clear "this
   turn hit its budget" notification.
2. **Inbound file size cap.** 100MB default in
   `gateway.yaml`. Streaming download with chunked read; reject
   anything bigger with a clear Slack message reply.
3. **Cache directory size cap.** `~/.freyja/projects/slack-cache/`
   capped at 5GB default. LRU eviction.
4. **Inbound MIME-type allowlist.** Reject executable / archive
   types (`.exe`, `.dll`, `.dmg`, etc.) unless explicitly
   allowlisted in `gateway.yaml`. Reduces attack surface.
5. **Subprocess token handoff via stdin.** Replace today's
   `_SLACK_BOT` / `_SLACK_APP` env vars with a stdin handoff —
   Python reads tokens from stdin instead of `os.environ`.
   Touches 4 IPC handlers in `gatewayBridge.ts`. Eliminates
   `ps -ef` token visibility.
6. **Global outbound rate limiter.** Token-bucket gating chat.update
   / chat.postMessage / files_upload_v2 / assistant_threads_setStatus.
   Per-channel + per-workspace buckets. Priority queue: text deltas
   > progress edits > status updates.
7. **Sub-agent event mirroring.** Parent's stream consumer
   subscribes to descendant session ids too. Fixes the
   "generate_image inside a sub-agent doesn't reach Slack" gap.

**Dependencies.** EPIC 1 (clean subscription model for #7).

**Estimated effort.** 5–6 engineering days.

**Open questions.**
- Per-turn budgets at session vs. workspace vs. operator scope?
- Inbound MIME allowlist defaults — strict or permissive?

---

### EPIC 7 — Approval flow as a first-class state machine.

**Problem.** Approval requests are in-memory `asyncio.Future`s.
Daemon crash → pending approvals lost. No persistent scopes
(per-call only). No audit log. Patterns are hardcoded.

**Approach.** Persist approval state on the turn ledger. Add
scope hierarchy. Configurable patterns.

**Tasks.**
1. **Persist approval records to the turn ledger.** State:
   `{approval_id, turn_id, tool_name, command, reason, requested_at,
   resolved_at?, scope?, decision?, decided_by?}`. Daemon restart
   re-posts pending approval prompts (the old response_url is dead
   but a new chat.postMessage with Block Kit works).
2. **Approval scope hierarchy.** Per-call (default), per-session,
   per-tool-per-session, per-tool-permanent. UI: 4 buttons on
   the Block Kit prompt.
3. **Persistent scopes.** Per-session scope stored in the turn
   ledger; per-tool-permanent stored in
   `~/.freyja/approval_grants.yaml`. Mirrors Hermes's scope model.
4. **Configurable patterns.** Move destructive bash patterns into
   `~/.freyja/gateway.yaml` so workspaces can customize. Add
   glob support for protected paths (operator's "never approve
   writes outside ~/work" rule).
5. **Cancel + heartbeat.** Block Kit prompt shows "waiting for
   approval — 9:42 remaining" updated each minute. React-to-deny
   with `❌` works as an alternative to clicking the button.
6. **Audit log query.** `freyja gateway approvals --session=<id>`
   shows every decision made, when, by which operator.

**Dependencies.** EPIC 2 (turn ledger infrastructure).

**Estimated effort.** 4–5 engineering days.

**Open questions.**
- Block Kit update frequency for the heartbeat — every minute is
  6 updates per 10-min prompt, might hit rate limits.
- Whether to surface per-tool-permanent grants in the desktop
  UI (settings panel?) or stay CLI-only.

---

### EPIC 8 — Multi-workspace and identity.

**Problem.** Multi-workspace install is partly broken (the echo
loop fix today is one piece). Allowlist UX is "configure via YAML
and hope." Token rotation isn't supported.

**Approach.** First-class multi-workspace. Per-workspace overrides
for everything. Token rotation aware.

**Tasks.**
1. **Per-workspace allowlist UX.** When a user from an unknown
   workspace DMs the bot, post an ephemeral message in the
   operator's primary DM ("a new workspace just messaged me —
   allowlist?") with Block Kit approve/deny buttons. Decision
   persisted to `gateway.yaml`.
2. **Per-workspace config overrides.** Everything in
   `gateway.yaml` can be overridden per-workspace:
   `enable_tool_filter`, `reply_in_thread`, approval patterns,
   default model, allowlist scope.
3. **Heartbeat `auth.test`.** Every 10 minutes, validate each
   workspace's token. On 4xx, mark workspace as unavailable +
   post failure notification to operator's primary DM.
4. **Token rotation.** Support `SLACK_BOT_TOKEN_NEXT`. On startup,
   try the new token first; fall back to old until operator
   confirms by deleting the old token from `~/.freyja/.env`.
5. **Per-workspace transcript scoping** (already works via session
   id keys — just verify multi-workspace flow end-to-end).

**Dependencies.** None hard, but easier after EPIC 5
(observability — auth.test failures need to surface).

**Estimated effort.** 4–5 engineering days.

**Open questions.**
- Cross-workspace operator identity — same operator with different
  user_ids in different workspaces. Treat as one operator or many?
- First-time bot install in a new workspace — automated or always
  require operator manual approval?

---

### EPIC 9 — Testing as part of the rebuild flow.

**Problem.** Most bugs hit in the last few days were "obvious in
hindsight, undetectable without integration tests." The fix
cadence has been: ship → discover → patch.

**Approach.** Integration test harness that runs the daemon
end-to-end with mocked Slack. Run on every rebuild.

**Tasks.**
1. **Bridge IPC test harness.** Spin up the daemon in a test
   mode pointing at a temporary `FREYJA_HOME`. Connect a test
   client. Inject inbound Slack events via the slack-bolt test
   harness. Assert outbound calls + resulting transcript state.
2. **Smoke test suite.** Implement the 5 tests called out in
   the audit: threaded reply, send_attachment in thread, inbound
   image, /reset, destructive approval. Each runs in <5s.
3. **Race condition tests specifically:**
   - Two messages back-to-back → each in its own thread with
     its own response.
   - Daemon kill mid-turn → restart → in-flight turn recovered
     or marked failed with notification.
   - Slack 429 response → backoff + retry succeeds.
   - Transcript file corruption (truncated mid-write) → daemon
     restart recovers from last snapshot + replays event log.
4. **Run on every `npm run rebuild`.** Failure aborts the
   rebuild with the failing test's output. Currently nothing
   stops a broken build from being installed.
5. **Performance regression tests.** Measure key metrics (turn
   start latency, time-to-first-token) on a fixed test fixture.
   Fail the rebuild if regressions exceed thresholds.

**Dependencies.** EPIC 1 (IPC protocol the harness exercises).

**Estimated effort.** 5–7 engineering days for the initial suite;
ongoing maintenance.

**Open questions.**
- How real do the LLM calls need to be in the harness? Recorded
  fixtures vs. real Anthropic calls behind a flag.
- Performance test fixtures — what's the canonical workload?

---

### EPIC 10 — Secrets and data handling.

**Problem.** API keys in plaintext .env. Tokens visible via
process introspection. Transcripts contain PII / secrets but
aren't encrypted.

**Approach.** OS keychain integration. Encryption at rest. Log
redaction.

**Tasks.**
1. **OS keychain on macOS.** Slack tokens, LLM provider keys,
   etc. retrieved via macOS Keychain Services on daemon startup
   instead of `~/.freyja/.env`. Migration: first launch reads
   from .env, writes to keychain, removes from .env. Subsequent
   launches read keychain only.
2. **Transcript encryption at rest.** Optional flag in
   `gateway.yaml`. AES-GCM with key derived from a keychain-stored
   master secret. Slow but necessary for operators handling
   sensitive material.
3. **Log redaction.** Regex-based scrubber on every log line:
   API key patterns, OAuth tokens, file paths under `~/.ssh/`,
   `~/.aws/`, `~/.kube/`. Pluggable patterns per workspace.
4. **Memory store encryption.** `~/.freyja/knowledge/memory.jsonl`
   contains durable user preferences and project context;
   sometimes sensitive. Encrypt with same key as transcripts.
5. **Token redaction in error messages.** Today's `verify failed:
   <stderr>` IPC response could leak. Add a redactor layer.

**Dependencies.** EPIC 1 (single Python process simplifies
keychain integration).

**Estimated effort.** 4–5 engineering days.

**Open questions.**
- Linux / Windows parity — separate epic when those ship.
- Master-secret recovery if the keychain is wiped. Recovery
  phrase printed during setup?

---

## 4. Sequencing

```
EPIC 1 (single bridge)  ─┬─→  EPIC 2 (turn lifecycle)  ─┬─→  EPIC 7 (approval state)
                         │                                │
                         ├─→  EPIC 3 (handoff UX)         │
                         │                                │
                         ├─→  EPIC 4 (transcript)  ──────→┘
                         │
                         ├─→  EPIC 5 (observability)
                         │
                         ├─→  EPIC 6 (resource limits)
                         │
                         ├─→  EPIC 8 (multi-workspace)
                         │
                         ├─→  EPIC 9 (testing)
                         │
                         └─→  EPIC 10 (secrets)
```

**Recommended order.**

1. **EPIC 1** first. Everything assumes it. Don't sequence around it.
2. **EPIC 5** before EPIC 2. Debugging the turn lifecycle work
   without structured logs and health endpoints will be misery.
3. **EPIC 9** in parallel with EPIC 2 onward. Write the test
   for each item before fixing it.
4. **EPIC 2 + EPIC 4** together. The turn ledger (EPIC 2) and
   the event log (EPIC 4) share infrastructure; building both
   at once avoids rework.
5. **EPIC 3** after EPIC 2 — the handoff feels much better when
   crashes don't lose work mid-conversation.
6. **EPIC 6, 7, 8, 10** independent after their dependencies.
   Sequence based on which production pressure hits first.

**Honest total estimate.** EPICs 1 through 5: 4–6 weeks of
focused work. EPICs 6 through 10: 4–5 more weeks, sequencing
flexibly. Total: 8–11 weeks to "I would deploy this for someone
other than me." Less if EPIC 10 is cut. More if real polish.

---

## 5. Decision log

Decisions LOCKED IN by writing this document (per §2):

- **D1**: One bridge daemon, two clients.
- **D2**: Event-sourced sessions with materialized views.
- **D3**: Unix socket + framed JSON for IPC.
- **D4**: Append-only NDJSON instead of full-file rewrites.
- **D5**: Desktop is a renderer, not a runtime.
- **D6**: Single-operator local-only first.

Decisions DEFERRED until their epic starts (each epic has
"Open questions"). These are the design conversations that
need to happen at implementation start, not at planning time.

---

## 6. What this doesn't fix

A few classes of issue this roadmap explicitly leaves alone:

- **Provider abstraction / fallback.** If Anthropic is down,
  this roadmap doesn't help you fall back to OpenAI mid-turn.
  Separate concern.
- **Conversation summarization quality.** Compaction is touched
  in EPIC 4 but the LLM-based summarization itself is a
  separate research thread.
- **Tool ergonomics for new platforms.** Adding Discord /
  Telegram / WhatsApp adapters reuses the SlackAdapter shape
  but isn't in this roadmap.
- **Desktop UI polish beyond the handoff affordances.** The
  sidebar, conversation view, settings panel — out of scope.

---

## 7. Document maintenance

This is a living plan. As epics ship, move them out of "future
work" and into a "completed" section at the bottom. As decisions
in the open-questions sections get made, fold them into the
decision log.

Anyone touching the gateway should read this before making
architectural changes — to avoid sequencing themselves into
a corner that the larger plan resolves cleanly.
