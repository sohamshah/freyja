# Warp File Edit UX Research

Date: 2026-04-30

This note starts with how Warp's actual agent works: the runtime paths, model selection, profile/permission system, and tool surface. It then narrows into how Warp displays agent file edits, what editing-related tools it exposes, and how to bring the same kind of dynamic file-write experience into Freyja without reducing agent capability.

## Warp Agent Intro

Warp has two related agent execution paths:

1. The first-party Agent Mode path, internally referred to by the built-in harness name `Oz`, sends structured requests to Warp's multi-agent backend and renders streamed response events inside the app.
2. The Agent SDK / ambient-agent path drives either the same built-in Oz runtime or an external CLI harness such as Claude Code, Gemini CLI, or Codex inside a managed terminal session.

Those paths share concepts like task ids, conversation ids, execution profiles, model preferences, snapshots, and session context, but they are not identical. The first-party path owns the full tool/action UI inside Warp. The CLI harness path delegates actual reasoning and tool use to an external agent process, then wraps that process with terminal control, external conversation records, transcript/snapshot upload, and resume support where available.

### First-Party Agent Mode Path

The normal in-app agent request starts from `RequestInput`. Warp records the active base model, coding model, CLI-agent model, and computer-use model from `LLMPreferences`, plus the conversation id, working directory, input messages, and any tool override.

Sources:

- `/Users/sohamshah/work/services/warp/app/src/ai/blocklist/controller.rs:168`
- `/Users/sohamshah/work/services/warp/app/src/ai/blocklist/controller.rs:240`

`RequestParams::new` then expands that UI-level input into the request shape the server needs. It attaches the session context, conversation token, tasks, model ids, memory/rules settings, Warp Drive context, context-window limit, MCP context, BYOK API keys, autonomy/isolation mode, web-search permission, computer-use permission, ask-user-question permission, research-agent permission, and orchestration permission.

Sources:

- `/Users/sohamshah/work/services/warp/app/src/ai/agent/api.rs:96`
- `/Users/sohamshah/work/services/warp/app/src/ai/agent/api.rs:156`
- `/Users/sohamshah/work/services/warp/app/src/ai/agent/api.rs:169`
- `/Users/sohamshah/work/services/warp/app/src/ai/agent/api.rs:237`
- `/Users/sohamshah/work/services/warp/app/src/ai/agent/api.rs:245`
- `/Users/sohamshah/work/services/warp/app/src/ai/agent/api.rs:258`
- `/Users/sohamshah/work/services/warp/app/src/ai/agent/api.rs:304`

`generate_multi_agent_output` builds a `warp_multi_agent_api::Request`, sends it to the server, and streams response events back until cancellation. The request advertises capability flags such as parallel tool calls, long-running commands, create-file support, todos UI, linked code blocks, image-file reading, reasoning messages, V4A file diffs, bundled skills, research agent support, and orchestration v2 support.

Sources:

- `/Users/sohamshah/work/services/warp/app/src/ai/agent/api/impl.rs:12`
- `/Users/sohamshah/work/services/warp/app/src/ai/agent/api/impl.rs:59`
- `/Users/sohamshah/work/services/warp/app/src/ai/agent/api/impl.rs:64`
- `/Users/sohamshah/work/services/warp/app/src/ai/agent/api/impl.rs:139`

The local `AIConversation` object stores far more than chat turns. It tracks task state, todos, code review state, usage metadata, server conversation token, task/run id, server metadata, transaction state, autoexecute override, hidden/reverted exchanges, suggestions, cost/token accounting, artifacts, and parent/child agent metadata.

Source: `/Users/sohamshah/work/services/warp/app/src/ai/agent/conversation.rs:121`

### Action Execution Model

Warp does not render tool calls as inert text. Server output becomes `AIAgentAction`s, and the local `BlocklistAIActionModel` / executor stack turns those actions into UI-owned operations. The executor has dedicated handlers for shell commands, file reads, artifact upload, code search, file-edit requests, grep, glob, MCP resources/tools, prompt suggestions, document read/create/edit, computer use, skill reads, conversation fetches, child-agent starts, child-agent messaging, and ask-user-question flows.

Sources:

- `/Users/sohamshah/work/services/warp/app/src/ai/blocklist/action_model/execute.rs:302`
- `/Users/sohamshah/work/services/warp/app/src/ai/blocklist/action_model/execute.rs:329`

The executor also categorizes actions into serial or parallel phases. Read-only local-context operations such as read files, search codebase, read skill, grep, and glob can run in a parallel read-only phase when the underlying executor allows it; other actions are serial barriers. That matters for Freyja because file-write visualization should be first-class without throttling or reducing agent capabilities.

Sources:

- `/Users/sohamshah/work/services/warp/app/src/ai/blocklist/action_model/execute.rs:102`
- `/Users/sohamshah/work/services/warp/app/src/ai/blocklist/action_model/execute.rs:388`

### Tool Surface

The first-party Warp agent advertises a broad built-in tool surface:

- Always available: `Grep`, `FileGlob`, `FileGlobV2`, `ReadMcpResource`, `CallMcpTool`, `InitProject`, `OpenCodeReview`, `RunShellCommand`, `SuggestNewConversation`, `Subagent`, `WriteToLongRunningShellCommand`, `ReadShellCommandOutput`, `ReadDocuments`, `CreateDocuments`, `EditDocuments`, and `SuggestPrompt`.
- Local sessions: `ReadFiles`, `ApplyFileDiffs`, `SearchCodebase`, and optionally `UploadFileArtifact`.
- Warpified remote sessions with a connected host: `ReadFiles` and `ApplyFileDiffs`.
- Feature/permission-gated tools: `FetchConversation`, `UseComputer`, `RequestComputerUse`, `InsertReviewComments`, `ReadSkill`, `StartAgent` / `StartAgentV2`, `SendMessageToAgent`, and `AskUserQuestion`.

Sources:

- `/Users/sohamshah/work/services/warp/app/src/ai/agent/api/impl.rs:153`
- `/Users/sohamshah/work/services/warp/app/src/ai/agent/api/impl.rs:177`
- `/Users/sohamshah/work/services/warp/app/src/ai/agent/api/impl.rs:202`
- `/Users/sohamshah/work/services/warp/app/src/ai/agent/api/impl.rs:215`
- `/Users/sohamshah/work/services/warp/app/src/ai/agent/api/impl.rs:224`

Warp also advertises a narrower tool surface for CLI subagents: long-running shell write/read, grep, glob, optional transfer of shell control to the user, and local/remote file reads plus code search where applicable.

Source: `/Users/sohamshah/work/services/warp/app/src/ai/agent/api/impl.rs:231`

### Models And Profiles

Warp's client does not hard-code one fixed model. It keeps model choices in `LLMPreferences`, grouped by feature: `agent_mode`, `coding`, `cli_agent`, and `computer_use`. The model metadata includes display name, base model name, provider, host routing config, vision support, reasoning level, context-window metadata, cost/usage metadata, and disable state. Supported provider labels in this layer are `OpenAI`, `Anthropic`, `Google`, `Xai`, and `Unknown`; routing hosts include direct API and AWS Bedrock.

Sources:

- `/Users/sohamshah/work/services/warp/app/src/ai/llms.rs:87`
- `/Users/sohamshah/work/services/warp/app/src/ai/llms.rs:110`
- `/Users/sohamshah/work/services/warp/app/src/ai/llms.rs:138`
- `/Users/sohamshah/work/services/warp/app/src/ai/llms.rs:381`

The fallback model ids are generic router ids rather than provider-specific names: base Agent Mode defaults to `auto`, coding defaults to `auto`, CLI agent defaults to `cli-agent-auto`, and computer use defaults to `computer-use-agent-auto`. The server supplies and refreshes the real choices; the active execution profile can override base, coding, CLI-agent, and computer-use model ids per terminal/profile.

Sources:

- `/Users/sohamshah/work/services/warp/app/src/ai/llms.rs:405`
- `/Users/sohamshah/work/services/warp/app/src/ai/llms.rs:432`
- `/Users/sohamshah/work/services/warp/app/src/ai/llms.rs:620`
- `/Users/sohamshah/work/services/warp/app/src/ai/llms.rs:660`
- `/Users/sohamshah/work/services/warp/app/src/ai/llms.rs:695`

Execution profiles are the permission and model-policy layer. A profile controls apply-code-diffs permission, read-files permission, command execution, write-to-PTY, MCP permissions, ask-user-question behavior, command allow/deny lists, directory allow lists, computer use, model overrides, context-window limit, plan sync, and web search. The default profile lets the agent decide file reads and code diffs, asks before command execution and PTY writes, disables computer use, and enables web search. The CLI profile is intentionally more permissive because it cannot stop for interactive approval in the same way.

Sources:

- `/Users/sohamshah/work/services/warp/app/src/ai/execution_profiles/mod.rs:221`
- `/Users/sohamshah/work/services/warp/app/src/ai/execution_profiles/mod.rs:260`
- `/Users/sohamshah/work/services/warp/app/src/ai/execution_profiles/mod.rs:341`

### External Harnesses

Warp's Agent SDK path uses `AgentDriver` to configure a headless terminal pane and execute an AI query. `AgentDriverOptions` include working directory, secrets, task id, parent run id, sharing, idle-on-complete, resume options, cloud providers, resolved environment, selected harness, and snapshot controls. A `Task` includes prompt, optional model, optional profile, MCP specs, and harness kind.

Sources:

- `/Users/sohamshah/work/services/warp/app/src/ai/agent_sdk/driver.rs:221`
- `/Users/sohamshah/work/services/warp/app/src/ai/agent_sdk/driver.rs:244`
- `/Users/sohamshah/work/services/warp/app/src/ai/agent_sdk/driver.rs:327`

The CLI-level harness enum includes:

- `Oz`: Warp's built-in MAA infrastructure.
- `Claude`: delegates to the `claude` CLI / Claude Code.
- `OpenCode`: delegates to the `opencode` CLI.
- `Gemini`: delegates to the `gemini` CLI.
- `Codex`: delegates to the `codex` CLI.

Sources:

- `/Users/sohamshah/work/services/warp/crates/warp_cli/src/agent.rs:121`
- `/Users/sohamshah/work/services/warp/crates/warp_cli/src/agent.rs:162`

The standalone Agent SDK harness dispatcher currently supports `Oz`, `Claude`, `Gemini`, and `Codex`; `OpenCode` exists in the shared enum but is marked unsupported in this driver. Each third-party harness validates that its CLI exists on `PATH`, can prepare CLI-specific config files, can build a runner, and may implement resume payload fetching.

Sources:

- `/Users/sohamshah/work/services/warp/app/src/ai/agent_sdk/driver/harness/mod.rs:58`
- `/Users/sohamshah/work/services/warp/app/src/ai/agent_sdk/driver/harness/mod.rs:134`
- `/Users/sohamshah/work/services/warp/app/src/ai/agent_sdk/driver/harness/mod.rs:166`

The third-party commands are intentionally autonomous:

- Claude Code runs `claude --session-id <uuid>` or `claude --resume <uuid>` with `--dangerously-skip-permissions`, optionally appending a system prompt file, and feeds the prompt through stdin.
- Gemini CLI runs `gemini --yolo -i "$(cat prompt)"`.
- Codex runs `codex --dangerously-bypass-approvals-and-sandbox "$(cat prompt)"`.

Sources:

- `/Users/sohamshah/work/services/warp/app/src/ai/agent_sdk/driver/harness/claude_code.rs:167`
- `/Users/sohamshah/work/services/warp/app/src/ai/agent_sdk/driver/harness/gemini.rs:89`
- `/Users/sohamshah/work/services/warp/app/src/ai/agent_sdk/driver/harness/codex.rs:88`

For external harnesses, Warp creates external conversation records and saves transcripts/snapshots rather than owning every individual tool result. Claude has the richest integration here: it supports resume payload fetching, uses a fixed external conversation format `claude_code_cli`, starts a parent bridge, and stores the running conversation id/block id after the CLI launches. Gemini and Codex create external conversation records with `gemini_cli` and `codex_cli` formats respectively, but their harness code notes that resume is not yet implemented.

Sources:

- `/Users/sohamshah/work/services/warp/app/src/ai/agent_sdk/driver/harness/claude_code.rs:80`
- `/Users/sohamshah/work/services/warp/app/src/ai/agent_sdk/driver/harness/claude_code.rs:155`
- `/Users/sohamshah/work/services/warp/app/src/ai/agent_sdk/driver/harness/claude_code.rs:389`
- `/Users/sohamshah/work/services/warp/app/src/ai/agent_sdk/driver/harness/gemini.rs:143`
- `/Users/sohamshah/work/services/warp/app/src/ai/agent_sdk/driver/harness/codex.rs:142`

### Implication For Freyja

The file-edit UX should be modeled after Warp's first-party action path, not the external CLI harness path. External harnesses are useful evidence that Warp supports multiple agents and model providers, but rich interactive file diffs come from structured local actions: the server advertises edit/diff capability, the app receives structured file-edit actions, local executors validate/apply them into candidate diffs, and the UI owns review, apply, reject, save, and summary state.

For Freyja, that means the right implementation is not to limit the agent or hide tool power. The right implementation is to add a structured file-change observation layer around the existing write/edit tools first, then introduce a first-class file-edit request tool once the renderer and artifact workspace can represent candidate diffs cleanly.

## File Edit Executive Summary

Warp treats agent edits as a first-class workflow, not as plain tool logs. The agent sends structured edit intent, Warp turns that intent into candidate diffs, the UI owns the review/apply/save lifecycle, and the final tool result returns a unified diff plus updated file contexts to the model.

Freyja currently has the raw pieces for file work, but not the product surface. `write_file` and `edit_file` run as normal tools, the bridge emits a generic `tool_result`, and the renderer infers an artifact from the tool name and path. That means the app can show that a file was touched, but not what changed, how it changed, whether multiple files changed together, or how to interact with those hunks.

The best path is incremental:

1. Add passive diff instrumentation around existing file-write tools.
2. Render those diffs as rich file-change cards in the conversation.
3. Add hunk/file interactions and artifact workspace integration.
4. Add a first-class `request_file_edits` tool only after the passive path is solid.

This gets most of the visual gain quickly without changing the agent's autonomy, write limits, screenshot limits, or ability to use shell/file tools.

## Warp: What It Does

### Agent Action Model

Warp has a dedicated `RequestFileEdits` action in its agent action enum:

- Source: `/Users/sohamshah/work/services/warp/crates/ai/src/agent/action/mod.rs:75`
- Shape: `RequestFileEdits { file_edits: Vec<FileEdit>, title: Option<String> }`

The `FileEdit` enum supports:

- `Edit(ParsedDiff)`
- `Create { file, content }`
- `Delete { file }`

Source: `/Users/sohamshah/work/services/warp/crates/ai/src/agent/action/mod.rs:803`

The result sent back to the model is also structured. On success it includes:

- unified diff text
- updated file contexts
- deleted files
- lines added
- lines removed

Source: `/Users/sohamshah/work/services/warp/crates/ai/src/agent/action_result/mod.rs:624`

### Edit Formats

Warp's diff validation layer supports two edit formats:

- `StrReplaceEdit`: file, search, replace
- `V4AEdit`: file, optional `move_to`, hunks

It converts those into a shared `DiffType`:

- `Create`
- `Update`
- `Delete`

Source: `/Users/sohamshah/work/services/warp/crates/ai/src/diff_validation/mod.rs:22`

Each visual hunk is represented as a `DiffDelta` with:

- `replacement_line_range`
- `insertion`

Source: `/Users/sohamshah/work/services/warp/crates/ai/src/diff_validation/mod.rs:166`

This is important: the UI does not try to infer intent from a prose tool result. It receives structured edit data that can be rendered, navigated, accepted, rejected, and summarized.

### Diff Application Pipeline

Warp's `RequestFileEditsExecutor` is the bridge between model intent and UI review:

- It owns an `ApplyDiffModel`.
- It stores `CodeDiffView` handles by action id.
- It preprocesses edit actions before execution.
- It applies file edits into candidate diffs.
- It feeds those candidate diffs into the diff view.
- It waits for accept/reject UI events before producing the action result.

Sources:

- `/Users/sohamshah/work/services/warp/app/src/ai/blocklist/action_model/execute/request_file_edits.rs:51`
- `/Users/sohamshah/work/services/warp/app/src/ai/blocklist/action_model/execute/request_file_edits.rs:126`
- `/Users/sohamshah/work/services/warp/app/src/ai/blocklist/action_model/execute/request_file_edits.rs:282`
- `/Users/sohamshah/work/services/warp/app/src/ai/blocklist/action_model/execute/request_file_edits.rs:340`

The critical flow is:

1. UI registers a `CodeDiffView` for the action.
2. `preprocess_action` applies proposed edits into candidate diffs.
3. `on_diffs_applied` maps those diffs to absolute paths and calls `set_candidate_diffs`.
4. `execute` subscribes to `SavedAcceptedDiffs` or `Rejected`.
5. On accept, Warp returns a structured `RequestFileEditsResult::Success`.

Source for the accept result handoff: `/Users/sohamshah/work/services/warp/app/src/ai/blocklist/action_model/execute/request_file_edits.rs:176`

### Diff Matching And Safety

Warp does not blindly write files from agent output. The diff application layer:

- Reads current file content from local disk or a remote backend.
- Applies create/edit/delete/rename semantics.
- Fails if a missing file is edited.
- Fails if an existing file is created.
- Fails if a file is both mutated and deleted.
- Fails on multiple creates or renames for the same target.
- Uses exact, indentation-aware, and fuzzy matching when applying patches.

Sources:

- `/Users/sohamshah/work/services/warp/app/src/ai/blocklist/action_model/execute/request_file_edits/diff_application.rs:57`
- `/Users/sohamshah/work/services/warp/app/src/ai/blocklist/action_model/execute/request_file_edits/diff_application.rs:160`

The useful product lesson is that failed diffs are first-class too. A failure becomes a structured error the model can recover from, rather than an opaque "tool failed".

### File Edit UI

The core UI is `CodeDiffView`.

It tracks:

- pending per-file diffs
- selected file tab
- review state
- whether the user edited the proposed content
- display mode
- local vs remote session type

Source: `/Users/sohamshah/work/services/warp/app/src/ai/blocklist/inline_action/code_diff_view.rs:478`

It has three display modes:

- `FullPane`
- `Embedded { max_height }`
- `InlineBanner { max_height, is_expanded, is_dismissed }`

Source: `/Users/sohamshah/work/services/warp/app/src/code/diff_viewer.rs:14`

For requested edits, Warp uses an embedded editor with a max height of 500px. For passive suggestions, it uses a compact inline banner around 94px that can expand to 400px.

Source: `/Users/sohamshah/work/services/warp/app/src/ai/blocklist/inline_action/code_diff_view.rs:135`

The view supports:

- file tabs
- per-file status labels for new/deleted/renamed files
- line stats badge
- accept
- auto-approve
- reject/refine
- edit mode
- minimize/done
- hunk navigation
- file switching
- passive "accept and continue with agent"
- passive "iterate with agent"
- review changes entry point
- revert after accept

Sources:

- keyboard bindings: `/Users/sohamshah/work/services/warp/app/src/ai/blocklist/inline_action/code_diff_view.rs:166`
- actions: `/Users/sohamshah/work/services/warp/app/src/ai/blocklist/inline_action/code_diff_view.rs:427`
- line stats: `/Users/sohamshah/work/services/warp/app/src/ai/blocklist/inline_action/code_diff_view.rs:1382`
- file tabs: `/Users/sohamshah/work/services/warp/app/src/ai/blocklist/inline_action/code_diff_view.rs:1844`
- render flow: `/Users/sohamshah/work/services/warp/app/src/ai/blocklist/inline_action/code_diff_view.rs:2623`

### Inline Diff Editor

Warp wraps a real code editor in `InlineDiffView`. That wrapper:

- applies diff deltas into the editor buffer
- toggles diff navigation
- scrolls to the focused hunk
- registers a backing file for save/revert
- emits events when diffs change, files save, saves fail, diffs are accepted, or users edit content

Sources:

- `/Users/sohamshah/work/services/warp/app/src/code/inline_diff.rs:25`
- `/Users/sohamshah/work/services/warp/app/src/code/inline_diff.rs:41`
- `/Users/sohamshah/work/services/warp/app/src/code/inline_diff.rs:186`

The editor itself exposes hunk features:

- add hunk as context
- revert hunk
- comment hunk
- collapsible diffs
- changed-line counts

Sources:

- `/Users/sohamshah/work/services/warp/app/src/code/editor/view.rs:449`
- `/Users/sohamshah/work/services/warp/app/src/code/editor/view.rs:465`
- `/Users/sohamshah/work/services/warp/app/src/code/editor/view.rs:489`
- `/Users/sohamshah/work/services/warp/app/src/code/editor/view.rs:541`
- `/Users/sohamshah/work/services/warp/app/src/code/editor/element.rs:1167`

## Warp: Tools Available To The Agent

Warp exposes a broad action/tool set, including:

- shell command execution
- writing to a long-running shell command
- reading shell command output
- reading files
- uploading artifacts
- searching codebase
- requesting file edits
- grep
- file glob and file glob v2
- MCP resource reads
- MCP tool calls
- document read/edit/create
- computer use and requested computer use
- reading skills
- fetching conversations
- starting agents
- sending messages to agents
- asking the user questions
- inserting code review comments

The relevant enum source is `/Users/sohamshah/work/services/warp/crates/ai/src/agent/action/mod.rs:32`.

For file edits specifically, the main agent-facing tool is not `write_file`. It is `RequestFileEdits`, which lets the model provide structured edits while the app controls diff application, review, save, and result feedback.

## Freyja: Current State

### Current Tool/Event Model

Freyja's bridge event model has generic tool events:

- `tool_use_start`
- `tool_input_delta`
- `tool_input_end`
- `tool_result`

Source: `/Users/sohamshah/work/services/freyja/src/shared/events.ts:212`

`ToolCallRecord` stores generic call metadata, arguments, result text, status, and optional screenshot frame. It does not have file-change records, before/after snapshots, hunk state, or edit review state.

Source: `/Users/sohamshah/work/services/freyja/src/shared/events.ts:62`

The bridge wrapper emits only finalized arguments and a truncated text preview after tool execution:

Source: `/Users/sohamshah/work/services/freyja/bridge/freyja_bridge.py:551`

### Current File Tools

Freyja has file tools:

- `read_file`
- `write_file`
- `edit_file`
- `edit_json`
- `list_directory`
- `glob`
- `grep`
- `bash`

Sources:

- registry: `/Users/sohamshah/work/services/freyja/bridge/tools/registry.py:86`
- `write_file`: `/Users/sohamshah/work/services/freyja/bridge/tools/file_tools.py:140`
- `edit_file`: `/Users/sohamshah/work/services/freyja/bridge/tools/file_tools.py:239`

`edit_file` already supports line-based replacement, anchor replacement, insert after line/pattern, and exact string replacement. That is a good foundation for dynamic visualization.

### Current Renderer Behavior

The renderer currently detects write-like tools by name:

Source: `/Users/sohamshah/work/services/freyja/src/renderer/state/store.ts:204`

On a successful `tool_result`, it reads the tool arguments, extracts a path, and creates or updates an `ArtifactRecord`.

Source: `/Users/sohamshah/work/services/freyja/src/renderer/state/store.ts:614`

The conversation still renders the call as a generic `ToolCallChip`, showing arguments and text result when expanded.

Source: `/Users/sohamshah/work/services/freyja/src/renderer/components/ToolCallChip.tsx:7`

This is why file writes currently feel static. There is no explicit "file change" domain object for the renderer to show.

## Gap Analysis

Warp has:

- structured file edit intent
- before-content and candidate after-content
- hunk-level model
- line stats
- tabbed multi-file review
- accept/reject lifecycle
- final model feedback with unified diff and updated contexts
- editor-backed save/revert behavior

Freyja has:

- direct mutating tools
- artifact inference
- generic tool call display
- no explicit file diff record
- no hunk model
- no review surface
- no structured edit result beyond the tool preview

The gap is not primarily CSS. It is data modeling. Freyja needs a file-change event stream and renderer state shape before it can display edits dynamically.

## Proposed Freyja Design

### Principle

Do not make the first implementation depend on a new agent tool. Instrument the tools Freyja already has, then add a structured `request_file_edits` tool as a second step.

That preserves the current agent experience and makes every existing `write_file`, `edit_file`, and `edit_json` call more visual immediately.

### New Shared Types

Add a first-class `FileChangeRecord` to `src/shared/events.ts`:

```ts
export interface FileChangeRecord {
  id: string
  toolCallId: string
  path: string
  filename: string
  operation: 'create' | 'modify' | 'delete' | 'rename' | 'unknown'
  status: 'pending' | 'applied' | 'failed'
  oldExists: boolean
  newExists: boolean
  oldHash?: string
  newHash?: string
  oldMtimeMs?: number
  newMtimeMs?: number
  linesAdded: number
  linesRemoved: number
  unifiedDiff?: string
  hunks: FileChangeHunk[]
  createdAt: number
}

export interface FileChangeHunk {
  oldStart: number
  oldLines: number
  newStart: number
  newLines: number
  heading?: string
  lines: Array<{
    kind: 'context' | 'add' | 'remove'
    oldLine?: number
    newLine?: number
    text: string
  }>
}
```

Add a bridge event:

```ts
| ({
    type: 'file_change'
    change: FileChangeRecord
  } & SessionId)
```

Add renderer state:

```ts
fileChanges: Record<string, FileChangeRecord>
fileChangeOrder: string[]
```

Extend `ToolCallRecord`:

```ts
fileChangeIds?: string[]
```

### Backend Instrumentation

Create a Python helper such as:

`/Users/sohamshah/work/services/freyja/bridge/tools/file_change_tracker.py`

Responsibilities:

- resolve tool paths relative to workspace
- snapshot file existence, content hash, mtime, and text content before execution
- snapshot the same data after execution
- produce unified diff with `difflib.unified_diff`
- parse unified diff into hunks for the renderer
- avoid binary files or huge full-content payloads
- emit compact structured `file_change` events

Use it inside `_new_tracing_registry` in `/Users/sohamshah/work/services/freyja/bridge/freyja_bridge.py`.

The wrapper already surrounds every tool execution and has access to:

- call id
- tool name
- arguments
- result
- duration
- session id

Source: `/Users/sohamshah/work/services/freyja/bridge/freyja_bridge.py:565`

For v1, track only explicit file mutation tools:

- `write_file`
- `edit_file`
- `edit_json`
- `write`
- `edit`

For v2, add shell-driven detection:

- If the workspace is a git repo, sample `git diff --name-only` before/after and compute changed files.
- If it is not a git repo, optionally watch known touched paths from command text or use a bounded mtime scan.
- Keep this out of v1 because shell commands can modify arbitrary files and false positives would make the first implementation noisy.

### Renderer Components

Add:

`/Users/sohamshah/work/services/freyja/src/renderer/components/FileChangeCard.tsx`

The card should render:

- file path
- operation badge
- status
- line stats
- per-file tab strip when a tool changed multiple files
- compact hunk summaries
- expandable inline diff
- "open artifact" / "open file" affordance
- copy path
- optional raw tool result fallback

For now, it should replace the body of `ToolCallChip` for write-like tools that have file changes. If there is no `file_change` event, fall back to the current generic chip.

Important performance rules:

- Render hunks collapsed by default for large diffs.
- Render only the selected file for multi-file changes.
- Cap initial diff lines, with "show more".
- Keep full old/new file contents out of React state.
- Do not parse diff text in React when the bridge can emit structured hunks.

### Artifact Workspace Integration

Keep creating `ArtifactRecord`s for changed files, but enrich the path from file changes rather than inferring only from tool args.

The artifact workspace should eventually show:

- last operation
- line stats
- click-through to diff card
- current file preview

This makes artifacts and file changes complementary:

- artifact = durable file reference
- file change = operation-specific before/after visual history

### Interactive Features

Phase 1 should be view-only because current tools already apply changes immediately.

Phase 2 can add:

- collapse/expand all hunks
- jump to next/previous hunk
- open file in workspace preview
- copy unified diff
- "restore previous file" for whole-file revert when old content exists

Phase 3 can add hunk-level revert, but that needs stronger patch safety because the file may have changed after the original write.

### First-Class `request_file_edits` Tool

After passive visualization is solid, add a Warp-style tool:

```json
{
  "name": "request_file_edits",
  "parameters": {
    "type": "object",
    "properties": {
      "title": { "type": "string" },
      "edits": {
        "type": "array",
        "items": {
          "oneOf": [
            {
              "type": "object",
              "properties": {
                "type": { "const": "str_replace" },
                "file": { "type": "string" },
                "search": { "type": "string" },
                "replace": { "type": "string" }
              },
              "required": ["type", "file", "search", "replace"]
            },
            {
              "type": "object",
              "properties": {
                "type": { "const": "create" },
                "file": { "type": "string" },
                "content": { "type": "string" }
              },
              "required": ["type", "file", "content"]
            },
            {
              "type": "object",
              "properties": {
                "type": { "const": "delete" },
                "file": { "type": "string" }
              },
              "required": ["type", "file"]
            }
          ]
        }
      }
    },
    "required": ["edits"]
  }
}
```

Execution behavior:

1. Tool parses and validates edits.
2. Bridge computes candidate before/after diffs without writing.
3. Renderer shows a pending review card.
4. User accepts/rejects, or auto-approval policy accepts.
5. Bridge writes files on accept.
6. Tool returns unified diff, updated file contexts, deleted files, line stats.

This is the closest Warp-equivalent behavior, but it is not the right first step because it requires a new async request/response bridge between tool execution and renderer UI.

## Implementation Plan

### Phase 1: Passive Diff Cards

Scope:

- no new agent behavior
- no new permission model
- no write limits changed
- no accept/reject gating
- existing writes still apply immediately

Files:

- `/Users/sohamshah/work/services/freyja/src/shared/events.ts`
- `/Users/sohamshah/work/services/freyja/bridge/freyja_bridge.py`
- `/Users/sohamshah/work/services/freyja/bridge/tools/file_change_tracker.py`
- `/Users/sohamshah/work/services/freyja/src/renderer/state/store.ts`
- `/Users/sohamshah/work/services/freyja/src/renderer/components/FileChangeCard.tsx`
- `/Users/sohamshah/work/services/freyja/src/renderer/components/ToolCallChip.tsx`
- `/Users/sohamshah/work/services/freyja/src/renderer/components/ParallelToolGroup.tsx`

Acceptance criteria:

- `write_file` creating a file shows a create card with `+N`.
- `write_file` overwriting a file shows a modify card with adds/removes.
- `edit_file` line replacement shows a hunk-level diff.
- `edit_json` shows a hunk-level diff.
- The old generic tool result is still available when expanded.
- Existing artifact list still updates.
- Large diffs do not render thousands of lines by default.
- Build passes.

### Phase 2: Better Review UX For Already-Applied Changes

Add:

- selected file tab when a tool touches multiple files
- expand/collapse all
- next/previous hunk
- copy unified diff
- open file/artifact preview
- restore whole file from pre-change snapshot when safe

Acceptance criteria:

- Multiple changes from one tool are grouped.
- Keyboard hunk navigation works inside the card.
- Revert is disabled if the file has changed since the captured post-write hash.

### Phase 3: Structured `request_file_edits`

Add:

- new tool schema
- bridge-side patch parser and fuzzy matcher
- pending edit review event
- renderer accept/reject event
- async tool wait until user/autoapproval resolves
- final structured result to the model

Acceptance criteria:

- Agent can propose edits without immediately writing files.
- User can accept/reject in the conversation.
- Accepted edits write to disk and return updated contexts.
- Rejected edits return a cancellation result.
- Failed diff application gives a model-actionable error.

### Phase 4: Live Edit Streaming

Add visual progress while content arrives:

- when `tool_input_delta` starts for a write-like tool, show "receiving edit"
- when finalized args arrive, show target path and pending operation
- when execution completes, transition to applied diff
- for `request_file_edits`, show candidate hunks before write

This gives the dynamic feel without putting huge partial file contents into React state.

## Recommended Starting Point

Start with Phase 1.

It is the highest leverage because it upgrades the visual experience for tools Freyja already uses every day. It also creates the data model needed for Warp-style interactive edits, without forcing the agent into a new workflow yet.

The first implementation should avoid Monaco or a heavy editor dependency. Freyja currently has a small dependency set, and `package.json` does not include an editor/diff renderer dependency. A custom line diff card is enough for the first pass and keeps render cost predictable.

Source: `/Users/sohamshah/work/services/freyja/package.json:29`

Once the passive cards are stable, adding `request_file_edits` becomes a clean extension instead of a one-shot large rewrite.
