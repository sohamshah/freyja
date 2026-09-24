# Mid-turn input and background sub-agents

Freyja never makes the operator wait for the agent to finish before they can speak, and the agent never sits blocked on a sub-agent. This document describes how that works and where the code lives.

## What the operator sees

**Enter while the agent is working** queues the message as a follow-up. It shows as a pending bubble above the composer marked "next step". The agent reads it at its next step boundary: after the current LLM call returns and after the current batch of tool calls finishes. The message then appears in the transcript at the point where it landed, marked "slid in mid-turn", and the rest of the agent's reply continues below it. It does not start a separate turn.

**Ctrl+Enter (or Cmd+Enter) while the agent is working** cuts in. The agent stops what it is doing right away. If it was writing a reply, the reply stops mid-sentence and the text already shown is kept. If it was running a bash command, the command moves to the background and keeps running (see below); other running tools are stopped, and tools that had not started are skipped. The message is then injected and the agent carries on with it in view. A queued bubble can be upgraded the same way with its "now" button.

**Tab (or Option+Enter) while the agent is working** queues the message for after the turn. It does not touch the running work. When the turn ends, it runs as its own turn, one queued message per turn. Use it for "when you're done, also do X".

**Taking messages back.** Esc takes every waiting message back into the composer without stopping the turn. Up in an empty composer takes back the newest one. The ✕ on a bubble takes back that one. The text (and any images) returns only after the bridge confirms the agent had not read it yet. If the agent got there first, the bubble becomes a normal message in the transcript instead, so nothing is ever both answered and sitting in the composer.

**Long commands don't block you.** When you send a message while a bash command has been running for 15 seconds or more, or the moment you cut in, the command moves to the background. It keeps running and its output goes to a log file. The agent gets the output so far and reads your message now. When the command exits, a memo reports its exit code and last output, and an idle agent is woken to look at it.

When the agent is idle, Enter, Tab, and Ctrl+Enter all just send.

**Sub-agents always run in the background.** A `sub_agent` or `computer_use` call returns as soon as the child is launched. The child keeps working in the swarm view while the parent carries on, answers questions, or ends its turn. When a child finishes, fails, or is stopped, the parent receives a memo. The memo shows in the transcript as a "sub-agent finished" card. If the parent is mid-turn, the memo lands at its next step. If the parent is idle, the memo wakes it with a turn of its own.

**Stopping is split in two.** "stop turn" (⌘Esc) stops the reply that is running and hands any queued follow-ups back to the composer. Background sub-agents and backgrounded commands keep going. "stop N agents" stops this session's sub-agents and backgrounded commands and leaves the conversation alone; the parent gets one combined notice about them, not one memo each, and is not woken. The emergency stop still stops everything. A stop names the turn you meant: if a different turn has started by the time it arrives (a memo wake turn, say), the bridge refuses it and says so, rather than stopping work you never saw. A stopped command gets SIGTERM for its whole process group, then SIGKILL a second later.

After a stop, the partial reply stays in the transcript, and the next turn begins with a short note telling the model that the operator stopped it on purpose, what was cut, and what is still running in the background.

## How it works

### One queue: the session inbox

Everything that can arrive while the agent works goes through the session's `SessionInbox` (`bridge/inbox.py`). Each message has a `kind`:

| kind | from | example |
|---|---|---|
| `followup` | the operator, typed while a turn ran | "also check the cookie expiry" |
| `queued` | the operator, with Tab: run after the turn | "when you're done, write the changelog" |
| `memo` | a background sub-agent or backgrounded command that finished | "[sub-agent memo · log digger · done · 2m14s] …" |
| `talk` | another agent, via the `talk` tool | "[message from agent · planner …]" |

When the inbox is full, it drops the oldest agent item (memo or talk) and never the operator's own input. Agent-written text (a sub-agent's report, a peer's message, command output) has runtime control tags like `<system-reminder>` defanged, and a memo's report sits inside its own `<subagent-report>` block with a line saying no human input occurred. That way a child can't pose as the operator.

The runner drains the inbox at every step boundary through its `on_pre_iteration` hook (`_BridgeSession._drain_inbox_into_session`). Follow-ups injected mid-turn get a short `<system-reminder>` header so the model reads them as the operator speaking now and not as tool output. If the operator cut in, the header says what was cut short.

### The runner's three hooks (`engine/runner.py`)

1. **`on_pre_iteration`** injects queued input before each provider call.
2. **`has_pending_input`** is checked when the model ends its turn. If something arrived while the final reply was streaming, the loop runs one more step instead of ending with the message unread.
3. **`request_interrupt()`** cuts the in-flight provider call or tool batch short. The runner races both against an interrupt event. Streamed text is kept only when there is input to inject next, because a transcript that ends on an assistant message is a prefill, which newer Anthropic models reject. Every tool call still gets exactly one result: `[interrupted]` for tools that were running and `[not run]` for tools that never started. An operator stop (task cancellation) is never swallowed by the race.

### Routing a send (`send_message` in `bridge/freyja_bridge.py`)

- If the session is native and a turn task is running (`accepts_followups()`), the message becomes a `followup` in the inbox (`push_operator_followup`). With `force` it also calls `interrupt_for_inbox()`.
- If the turn's runner has already finished and the task is in its tail (usage, goal judge, kanban tick), the follow-up waits in the inbox. `_drain_after_turn` then promotes it into the next turn's user message (`followups_promoted`). Late follow-ups are merged into one turn first; then each `queued` message runs as its own turn.
- `talk(wait_for_reply=True)` stops waiting as soon as operator input arrives.
- Only the operator can force-interrupt a root session, and only the operator or a child's own parent can force-interrupt that child. An agent's `force` to anyone else is plain delivery.

### Commands that step aside (`bridge/tools/background_shell.py`)

- The tracing wrapper makes the session's `ToolContext` current for every tool call (a ContextVar). It holds the session's `ToolYield` signal, the background log directory, and the start and exit callbacks. That lets the one shared `BashTool` serve every session.
- `BashTool` pumps output as it arrives (`StreamingProcess`), polling the yield signal. A queued follow-up asks softly, and the command yields once it has run `SOFT_YIELD_AFTER_S` (15s). A cut-in asks hard, and it yields at once. The signal is cleared at each step boundary.
- The runner gives tools marked `yields_on_interrupt` up to `TOOL_YIELD_GRACE_S` (2s) to return on their own after a cut-in before it cancels them. Everything else is cancelled at once.
- The session keeps `background_commands`, lists running ones in the background-work reminder, waits for them in quiescence and the goal gate, and stops them with the `subagents` and `all` stop scopes.
- Harness runtimes (Claude Code, Codex) own their own turn loop, so they keep the old queue-after-turn behavior.

### Background sub-agents and memos

- `SubAgentTool.execute` always launches the child as a background task and returns at once. `mode` arguments from old transcripts are ignored.
- When a child the parent's model spawned (`record.notify_parent`) reaches a terminal state, `SubAgentSpec.on_child_terminal` calls `_BridgeSession._on_child_terminal`. That builds the memo (`build_subagent_memo`: outcome, report, output file, files produced, siblings still running) and pushes it into the parent's inbox.
- An idle parent is woken after a short debounce (`MEMO_WAKE_DEBOUNCE_S`) so children that finish together are handled in one wake turn. The wake turn's synthetic prompt (`MEMO_WAKE_PROMPT`) never seeds a goal, a mission card, or skill cadence.
- No memo is sent when the parent killed the child itself. When the operator stopped it, the memo is queued without waking the parent.
- Bridge-internal spawns (goal judge, calibrator, skill drafter, kanban workers) do not memo the parent. The judges still block, because they need the answer inline; that is infrastructure, not the model delegating work.
- `subagents` has no blocking wait. It offers `list`, `status`, `result`, and `kill`. A per-request system reminder lists the children still running so the agent keeps track of them while it talks to the operator.
- `computer_use` children hold a process-wide screen lease while they run. The parent's own mutating computer tools (and a general sub-agent's, which are the same objects) refuse to act until the lease is released. Observing tools still work.
- Running children survive `reset()` (model switch, runtime swap), which always restores the transcript afterward. Deleting a session stops its children.

### Callers that treat a session's work as one job

Scheduled fires and voice missions used to treat "the turn task finished" as "the job is done". With background children that is no longer true, so both now call `wait_until_quiescent(sess)`. It waits until no turn is running, none of the session's background work (children or commands) is running, and the inbox is empty. A scheduler timeout or cancel stops the turn and the children together. A job with no timeout of its own waits at most `BACKGROUND_WORK_WAIT_CAP_S` (30 minutes) on background work alone, then delivers with a note that some work was still running.

### Slack

A follow-up from the same sender in the same thread as the running turn slides into that turn, and its answer streams into the same post (`_can_slide_into_running_turn` in `bridge/gateway/run.py`). Anything else still queues, because it needs its own reply anchor. That covers top-level DM messages, other users in a channel thread, and the duplicate files copy Slack sends for a mention. Memo wake turns stream to the thread through the session's `talk_wake_hook`.

## Renderer

- `sendMessage(content, { force })` holds a mid-turn message in `pendingFollowups[sessionId]` instead of the transcript.
- `inbox_injected` places follow-ups where they landed. The streaming reply is closed at that point and the rest of the turn continues in a new message below them, because the timeline sorts by `createdAt`.
- `followups_promoted` turns pending bubbles into ordinary messages just ahead of the next turn.
- `followup_withdrawn` removes a bubble and, when the operator stopped the turn, puts its text back in the composer.
- Memos render as `MemoChip` in the conversation stream. Follow-up inbox events are not shown as chips, because they are already user messages.

## Tests

- `tests/test_mid_turn_injection.py`: runner behavior (extension, stream and tool interrupts, the yield grace, stop propagation and stop notes).
- `tests/test_background_subagents.py`: memos, the `subagents` tool, drain and wake behavior, the queued kind, confirmed withdraw, eviction, forged-tag defanging, cancel scopes and turn-matched stops, quiescence and its cap, the Slack slide-in rule, the screen lease, and the scheduler abort hook.
- `tests/test_background_commands.py`: real subprocesses moving to the background, their memos, and process-group stops.
- `test-followup-placement.mjs` (`npx tsx`): renderer placement of follow-ups and memos.
