# Goal and Task Coordination Plan

## Context

Freyja already has three coordination strategies:

- `bus`: profile-driven sub-agents coordinate through a shared message bus.
- `isolated`: parent-driven delegation where sub-agents work independently.
- `kanban`: board-driven multi-agent execution with explicit cards.

The next step is to split two concepts that are currently blurred:

- **Goal loop** should be its own strategy for autonomous continuation in the same session.
- **Solo mode** should become a visible task-led workflow instead of quiet isolated delegation.

## Hermes Goal Loop Takeaways

Hermes implements `/goal` as an active objective that survives across turns. After each assistant turn:

1. A small judge model receives the goal and latest assistant response.
2. The judge returns strict JSON: `{"done": boolean, "reason": string}`.
3. If not done, Hermes injects a continuation prompt into the same session.
4. The loop stops when the judge says done, a turn budget is exhausted, the user pauses/clears, or a real user message preempts.

Important properties to keep:

- The continuation happens in the **same session**, preserving tools, memory, files, and transcript.
- The judge is fail-open: judge errors should not prematurely mark work done.
- User input wins over automation.
- Progress should be visible as a loop timeline, not hidden in logs.

## Claude Code Task Takeaways

The Claude Code repo mostly exposes docs and plugin definitions, but the useful product pattern is clear:

- Maintain explicit task records instead of relying on prose plans.
- Use specialized agents against concrete tasks.
- Preserve partial results and status even when agents stop.
- Surface active tasks, assigned agents, progress, blockers, artifacts, and review state in the UI.

For Freyja, this means solo mode should not mean "nothing to see." It should mean:

- One parent orchestrator.
- A lightweight task ledger.
- Optional sub-agent allocation against tasks.
- A dashboard view focused on task state and agent ownership.

## Proposed Strategy Set

| Strategy | ID | Primary Use | Coordination Surface |
| --- | --- | --- | --- |
| Message bus | `bus` | Research fanout and cross-pollination | Findings bus |
| Tasks | `isolated` | Solo/parent-led work with visible task tracking | Task ledger |
| Board | `kanban` | Heavier multi-agent planning with dependencies | Kanban board |
| Goal loop | `goal` | Autonomous continuation toward one objective | Judge timeline |

`isolated` remains the wire id for compatibility, but the UX label becomes **Tasks**.

## Backend Changes

1. Add `goal` as a first-class coordination strategy.
2. Add `GoalState` and `GoalManager`:
   - active goal text
   - status: active, paused, done, cleared
   - turn budget and turns used
   - latest verdict/reason
   - structured event payloads for the dashboard
3. In `goal` sessions, automatically treat the first user message as the active goal.
4. Add a `/goal` bridge command for manual set/status/pause/resume/clear.
5. After each successful assistant turn in goal mode, run the judge and optionally queue a continuation prompt.
6. Add a lightweight task board for task-first solo mode:
   - task create/list/show/claim/update/complete/block/heartbeat
   - task ids like `task_001`
   - progress, assignee, summary, result, event history
7. Add a `tasks` tool only in task mode.
8. Let `sub_agent` accept an optional `task_id`; auto-create a task if task mode spawns a worker without one.

## UI Changes

### Header

Expose four strategy choices:

- Bus
- Tasks
- Board
- Goal

### Mission Dashboard

`bus` keeps its current message-bus visualization unchanged.

`kanban` keeps the existing board-oriented UI.

`goal` gets:

- Goal status, turn budget, latest judge verdict.
- Loop timeline of judge decisions and continuation prompts.
- Same-session lane showing each continuation pass.
- Sub-agent side rail if the goal loop delegates.

`isolated`/Tasks gets:

- Task health overview with active/done/blocked counts.
- Task board grouped by todo/active/blocked/done/cancelled.
- Task detail inspector.
- Agent allocation panel showing which sub-agent owns which task.

## Implementation Order

1. Commit this plan.
2. Implement goal strategy backend + `/goal` command.
3. Add goal dashboard views.
4. Commit goal mode.
5. Implement task board tool + sub-agent task allocation.
6. Add task dashboard views and update strategy labels.
7. Commit task mode.
8. Run Python compile and TypeScript/build checks.
9. Push.
