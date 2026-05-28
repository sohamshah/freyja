// Shared event types between main, preload, renderer, and the Python bridge.
// Keep this file dependency-free.

export type BridgeMode = 'live' | 'demo' | 'error'

export type CoordinationStrategy = 'bus' | 'isolated' | 'kanban' | 'goal'

export type SkillConfidence = 'unvalidated' | 'experimental' | 'verified' | 'deprecated'

export type SubagentState = 'pending' | 'running' | 'done' | 'failed' | 'cancelled'

export interface Skill {
  id: string
  name: string
  skillType: 'build' | 'guard' | 'reference' | 'workflow' | 'tool'
  description: string
  triggers: string[]
  tags: string[]
  confidence: SkillConfidence
  retrievalCount: number
  successSignals: number
  failureSignals: number
  loadCount?: number
  scope?: 'project' | 'user' | 'compat' | 'plugin'
  status?: 'available' | 'suggested' | 'loaded' | 'pruned'
  path?: string
}

export interface MemoryRevision {
  ts: number
  session_id?: string
  actor?: string
  action: string
  note?: string
  prev_text?: string
  prev_kind?: string
  prev_scope?: string
}

export interface MemoryRecord {
  id: string
  scope: 'user' | 'project' | 'session' | 'subagent'
  kind: string
  text: string
  summary: string
  tags: string[]
  source?: string
  path?: string
  confidence?: string
  createdAt?: number
  updatedAt?: number
  /** Session id of whoever first recorded the memory. */
  createdBySession?: string
  /** Actor label of the original recorder (e.g. "parent", "user",
   *  "record_user_preference"). */
  createdByActor?: string
  /** Per-revision audit trail. Append-only — every edit, archive,
   *  restore, or merge_into/merge_from event is captured here. */
  revisions?: MemoryRevision[]
  /** Memory ids this entry replaces (for merged canonicals). */
  supersedes?: string[]
  /** If this memory has itself been replaced, the new id. */
  supersededBy?: string
  /** Soft-delete flag. Archived items are hidden from agent context
   *  and from the default sidebar listing. */
  archived?: boolean
}

export interface SubagentRecord {
  id: string
  label: string
  mode: 'foreground' | 'background'
  state: SubagentState
  task: string
  agentType?: string
  startedAt: number
  elapsedMs: number
  tokensIn: number
  tokensOut: number
  toolsCalled: number
  parentId?: string
  result?: string
  artifactPath?: string
  createdFiles?: string[]
  coordinationStrategy?: CoordinationStrategy
  kanbanTaskId?: string
  taskId?: string
}

export interface BusMessageRecord {
  index: number
  topic: 'findings' | 'errors' | 'progress' | 'read'
  senderId: string
  senderLabel: string
  content: string
  timestamp: number
  /** Populated only on read events (topic === 'read') — the exact indices
   *  returned by that read_findings call. Lets the bus flow view's
   *  timeline draw an arc from the read row to each source finding chip
   *  instead of inferring from since_index. */
  messageIndices?: number[]
}

export interface ArtifactRecord {
  id: string
  path: string
  filename: string
  /** 'parent' or a subagent id */
  creator: string
  creatorLabel: string
  createdAt: number
  fileType: string
  /** 'write' | 'edit' | 'create' | 'update' | 'delete' | 'subagent_artifact' */
  operation: string
  changeSetId?: string
  toolCallId?: string
  additions?: number
  deletions?: number
  binary?: boolean
  diffTruncated?: boolean
}

export type FileChangeOperation = 'create' | 'update' | 'delete' | 'rename'

export interface FileChangeRecord {
  path: string
  filename: string
  fileType: string
  operation: FileChangeOperation
  oldPath?: string
  additions: number
  deletions: number
  beforeHash?: string | null
  afterHash?: string | null
  beforeSize?: number
  afterSize?: number
  beforeLineCount?: number | null
  afterLineCount?: number | null
  binary?: boolean
  tooLarge?: boolean
  diff?: string | null
  diffTruncated?: boolean
}

export interface FileChangeSet {
  id: string
  toolCallId: string
  toolName: string
  source: 'tool' | 'bash'
  cwd?: string
  createdAt: number
  summary: string
  files: FileChangeRecord[]
  totals: {
    files: number
    additions: number
    deletions: number
  }
  truncated?: boolean
}

export interface ToolCallRecord {
  id: string
  name: string
  arguments?: Record<string, unknown>
  partialJson?: string
  status: 'running' | 'success' | 'error'
  result?: string
  isError?: boolean
  durationMs?: number
  startedAt: number
  /** Group ID — tool calls from the same model response share this.
   *  Used to detect parallel execution and render as lanes. */
  groupId?: string
  /** Screenshot frame captured during this tool's execution. Populated
   *  when a `screenshot_frame` event arrives while this tool is the
   *  currently-running one on its session. Lets the conversation
   *  render a thumbnail inline at the point of the call.
   *
   *  Modern renderer state stores a lightweight `frameId` and keeps the
   *  heavy bytes in a renderer media cache. `pngBase64` remains optional
   *  for persisted/legacy sessions. */
  frame?: {
    frameId?: string
    pngBase64?: string
    mimeType: string
    width: number
    height: number
    takenAt: number
    reason?: string
    byteSize?: number
  }
  /** Images returned by non-screenshot tools, such as image generation.
   *  Stored through the same renderer media cache as screenshot frames,
   *  but rendered as creative output instead of captured screen state. */
  resultImages?: Array<{
    frameId?: string
    pngBase64?: string
    mimeType: string
    width: number
    height: number
    takenAt: number
    reason?: string
    byteSize?: number
    label?: string
  }>
  fileChangeSet?: FileChangeSet
}

export interface MessagePart {
  type: 'text' | 'thinking' | 'tool_call' | 'tool_result' | 'subagent' | 'system'
  text?: string
  toolCallId?: string
  subagentId?: string
  systemSubtype?: string
  /** Stable id of the originating system event when `type === 'system'`.
   *  Lets the inline renderer look up the full event payload (e.g. the
   *  judge's verdict details) without duplicating data into the part. */
  eventId?: string
}

export interface MessageAttachmentRef {
  id: string
  /** `video` is currently Gemini-only; the bridge drops it with a
   *  transcript annotation if the active session isn't on a google
   *  family model. */
  type: 'image' | 'video'
  /** For images: a data: URL that <img> can render directly. For
   *  videos: a blob: URL that <video> can play, or empty if the
   *  renderer chose to show a glyph card instead of a real preview. */
  previewUrl: string
  name?: string
  mimeType?: string
  sizeBytes?: number
}

export interface Message {
  id: string
  role: 'user' | 'assistant' | 'system'
  parts: MessagePart[]
  createdAt: number
  inputTokens?: number
  outputTokens?: number
  cacheReadTokens?: number
  cacheWriteTokens?: number
  attachments?: MessageAttachmentRef[]
  /** True when the user pinned this message so the compactor never folds
   *  it into a summary (F1). Toggled via the message context menu and
   *  broadcast back via the entry_pin_changed event. */
  pinned?: boolean
}

export interface SessionSnapshot {
  id: string
  title: string
  workspace: string
  model: string
  reasoningLevel?: string
  coordinationStrategy?: CoordinationStrategy
  /** Top-level preset id this session was started under (e.g.
   *  "claude-code", "codex"). Persisted in the session sidecar so the
   *  preset's persona block + tool surface re-apply when the operator
   *  opens this session after a restart. Empty when the session was
   *  started with a raw model and no preset bundle. */
  presetId?: string
  createdAt: number
  updatedAt: number
  messageCount: number
  totalInputTokens: number
  totalOutputTokens: number
  cacheReadTokens: number
  /** Cumulative USD spend for this session in isolation (not including
   *  any subagents). The renderer aggregates parent + descendants for
   *  display via `aggregateSessionCost`. */
  totalCost?: number
  /** Present on sessions spawned as sub-agents. */
  parentSessionId?: string
  /** Ids of sessions spawned from this one. */
  childSessionIds?: string[]
  /** Task prompt given to a sub-agent when spawned. */
  task?: string
  /** Agent type specialization (explore, code, verify, etc.) */
  agentType?: string
  /** Kanban card assigned to this sub-agent session in board mode. */
  kanbanTaskId?: string
  /** Task ledger item assigned to this sub-agent session in task mode. */
  taskId?: string
  /** Whether a sub-agent session has completed. */
  completed?: boolean
  completedAt?: number
  success?: boolean
  /** How this session came to exist:
   *   - 'operator'  : root session the operator opened
   *   - 'agent'     : sub-agent spawned by another agent
   *   - 'rewoken-agent'    : archived sub-agent re-woken via an agent talk()
   *   - 'rewoken-operator' : archived sub-agent re-woken via an operator talk()
   *  Set from the session_spawned event payload. */
  wokenBy?: 'operator' | 'agent' | 'rewoken-agent' | 'rewoken-operator'
}

export interface CommandAttachment {
  /** Both flow as base64. `video` is Gemini-only — the bridge silently
   *  drops it (with a transcript note) when the active session model
   *  isn't on the google family, so the renderer is expected to gate
   *  before sending. */
  type: 'image' | 'video'
  mimeType: string
  dataBase64: string
  /** Optional display metadata. Filename is used by the bridge for
   *  VideoBlock.filename so reruns can show the original name; size
   *  is for the renderer's pill UI only. */
  filename?: string
  sizeBytes?: number
}

// --- Commands sent from renderer to main (and by main to the bridge) ---

export type BridgeCommand =
  | { type: 'hello'; sessionId: string; workspace: string; model: string }
  | {
      type: 'send_message'
      sessionId?: string
      content: string
      model?: string
      reasoningLevel?: string
      coordinationStrategy?: CoordinationStrategy
      attachments?: CommandAttachment[]
    }
  | { type: 'cancel'; sessionId?: string }
  | { type: 'force_cancel'; sessionId?: string }
  | { type: 'diagnose' }
  | { type: 'compact'; sessionId?: string; model?: string; reasoningLevel?: string; coordinationStrategy?: CoordinationStrategy }
  | {
      type: 'goal_control'
      sessionId?: string
      action: 'set' | 'status' | 'pause' | 'resume' | 'clear' | 'stop' | 'done'
      goal?: string
      reason?: string
      model?: string
      reasoningLevel?: string
      coordinationStrategy?: CoordinationStrategy
    }
  | { type: 'set_model'; sessionId?: string; model: string; reasoningLevel?: string; coordinationStrategy?: CoordinationStrategy }
  | {
      type: 'set_coordination_strategy'
      sessionId: string
      coordinationStrategy: CoordinationStrategy
      model?: string
      reasoningLevel?: string
    }
  | { type: 'list_skills'; sessionId?: string }
  | { type: 'list_subagents'; sessionId?: string }
  | { type: 'list_tools'; sessionId?: string }
  | {
      type: 'new_session'
      sessionId?: string
      model?: string
      reasoningLevel?: string
      coordinationStrategy?: CoordinationStrategy
      /** Top-level preset id (e.g. "claude-code", "codex"). When set,
       *  the bridge looks up the preset's model / thinking /
       *  coordination / tool surface / system-prompt block and applies
       *  them at session init. Explicit model / reasoningLevel /
       *  coordinationStrategy in the same command still win. */
      presetId?: string
    }
  | {
      type: 'switch_session'
      sessionId: string
      model?: string
      reasoningLevel?: string
      coordinationStrategy?: CoordinationStrategy
      /** Resume the preset binding from a persisted session. The
       *  bridge re-applies the preset's persona block + tool surface
       *  on the resumed session id. */
      presetId?: string
    }
  | { type: 'usage'; sessionId?: string }
  | { type: 'list_files'; sessionId?: string; query: string; limit?: number }
  | {
      type: 'permission_response'
      sessionId?: string
      requestId: string
      approved: boolean
      response?: string
    }
  | {
      type: 'set_permission_policy'
      sessionId?: string
      autoApprove: PermissionTier
    }
  | { type: 'set_computer_enabled'; enabled: boolean }
  | { type: 'computer.emergency_stop'; reason?: string }
  | {
      // Edit a user message in place. Truncates the engine transcript to
      // BEFORE the message at `messageOrdinal` (0-indexed across user +
      // assistant message-bearing entries), then re-issues the turn with
      // the new content + attachments.
      type: 'edit_user_message'
      sessionId: string
      messageOrdinal: number
      content: string
      attachments?: CommandAttachment[]
    }
  | {
      // Re-run from a user message — drops every message at/after the
      // ordinal in the engine transcript and re-issues the user message
      // verbatim to start a fresh assistant response.
      type: 'rerun_user_message'
      sessionId: string
      messageOrdinal: number
    }
  | {
      // Delete the message at `messageOrdinal` and every message after
      // it. Truncate-and-stop: no follow-up turn is dispatched.
      type: 'delete_messages_from'
      sessionId: string
      messageOrdinal: number
    }
  | {
      // Deep-clone the current session at a message boundary into a new
      // session. The new session contains messages 0..messageOrdinal-1
      // (i.e. everything BEFORE the right-clicked message). Subagent
      // transcripts in `childSessionIds` are copied with new IDs;
      // workspace files on disk are NOT copied.
      type: 'branch_session'
      sessionId: string
      messageOrdinal: number
      newName?: string
      /** All descendant session ids the renderer wants cloned alongside
       *  the parent. The bridge mirrors these as fresh transcript files
       *  on disk and returns the id remap via `session_branched`. */
      childSessionIds?: string[]
    }
  | {
      // Delete a session: drop it from the bridge's in-memory map,
      // cancel any pending task, and unlink its persisted transcript
      // from ~/.freyja/sessions. The renderer is responsible for the
      // cascade (passing every descendant id in `cascadeSessionIds`)
      // so the bridge can drop the whole subtree atomically.
      type: 'delete_session'
      sessionId: string
      cascadeSessionIds?: string[]
    }
  | {
      // Memory curation from the renderer. The bridge updates the
      // structured JSONL, appends a revision capturing actor/session,
      // and broadcasts a memory_updated event so the sidebar refreshes.
      type: 'memory_update'
      sessionId: string
      id: string
      text?: string
      kind?: string
      scope?: string
      tags?: string[]
      note?: string
    }
  | { type: 'memory_delete'; sessionId: string; id: string; note?: string }
  | {
      // Pin / unpin a transcript entry so the compactor never folds it
      // into a summary (F1 — see docs/COMPACTION-DECISION-DRAFT.md).
      // ``messageOrdinal`` matches the addressing scheme used by
      // edit_user_message / rerun_user_message / delete_messages_from
      // — 0-indexed across user+assistant message-bearing entries.
      type: 'toggle_entry_pin'
      sessionId: string
      messageOrdinal: number
      pinned: boolean
    }
  | { type: 'memory_restore'; sessionId: string; id: string; note?: string }
  | {
      type: 'memory_merge'
      sessionId: string
      ids: string[]
      text: string
      kind?: string
      scope?: string
      tags?: string[]
      note?: string
    }
  | {
      /** Operator-initiated kanban card creation. Flows through the
       *  same `SessionKanbanBoard.create()` path agents use; the only
       *  difference is `actor="operator"` on the bridge side, which
       *  lands in the renderer's card view as `createdBy: "operator"`.
       *  Renderer drives this from the dashboard's board column's
       *  "+ Add task" form. */
      type: 'kanban_operator_create'
      sessionId: string
      title: string
      body?: string
      /** Optional agent profile (e.g. "explore", "code"). When omitted
       *  the card lands in `triage` and a specifier agent picks it up
       *  to figure out the right worker. */
      assignee?: string
      /** 0 = highest priority, 5 = lowest. Defaults to 2 (medium). */
      priority?: number
      /** Card ids this card depends on. Stored as parents on the
       *  board; status auto-flips to `triage` until they're done. */
      parents?: string[]
      /** Card ids that this card blocks. The bridge reparents them
       *  onto the newly created card via the board's `link` method. */
      children?: string[]
    }
  | { type: 'shutdown' }

// --- Events produced by the bridge and forwarded to the renderer ---

export interface ToolCatalogEntry {
  name: string
  summary: string
  description: string
  tier: string
}

/**
 * Every session-scoped event carries an optional `sessionId`. Global events
 * (ready, log, error, skill updates, tool catalog) leave it unset.
 */
type SessionId = { sessionId?: string }

export type BridgeEvent =
  | ({ type: 'ready'; mode: BridgeMode; capabilities: Record<string, unknown> } & SessionId)
  | { type: 'error'; message: string; recoverable?: boolean }
  | { type: 'log'; level: 'info' | 'warn' | 'error' | 'debug'; message: string }
  | ({ type: 'turn_start'; turnId: string } & SessionId)
  | ({ type: 'text_delta'; text: string } & SessionId)
  | ({ type: 'thinking_delta'; thinking: string } & SessionId)
  | ({ type: 'tool_use_start'; id: string; name: string } & SessionId)
  | ({ type: 'tool_input_delta'; id: string; partialJson: string } & SessionId)
  | ({ type: 'tool_input_end'; id: string; arguments: Record<string, unknown> } & SessionId)
  | ({
      type: 'tool_result'
      id: string
      preview: string
      isError: boolean
      durationMs: number
      images?: Array<{
        id?: string
        dataBase64: string
        mimeType: string
        width: number
        height: number
        label?: string
      }>
    } & SessionId)
  | ({ type: 'file_change_set'; changeSet: FileChangeSet } & SessionId)
  | { type: 'tool_catalog_entry'; tool: ToolCatalogEntry }
  | ({
      type: 'system_event'
      subtype: string
      message: string
      details?: Record<string, unknown>
    } & SessionId)
  | ({ type: 'subagent_spawn'; record: SubagentRecord } & SessionId)
  | ({ type: 'subagent_update'; id: string; patch: Partial<SubagentRecord> } & SessionId)
  | ({ type: 'subagent_done'; id: string; result: string; elapsedMs: number } & SessionId)
  | ({ type: 'bus_message'; message: BusMessageRecord } & SessionId)
  | ({
      type: 'inbox_event'
      action: 'enqueued' | 'delivered' | 'dropped'
      message: {
        id: string
        fromSession: string
        fromLabel: string
        fromRole: 'operator' | 'agent'
        content: string
        force: boolean
        replyTo: string | null
        timestamp: number
        deliveredAt: number | null
        /** Optional origin tag. `'spawn'` marks a synthetic event emitted
         *  at sub-agent spawn time so the comm graph can show parent →
         *  child intent (the task itself is delivered as the runner's
         *  initial user message, not via inbox push). Regular talk() and
         *  operator_talk traffic omits this field. */
        kind?: 'spawn'
      }
    } & SessionId)
  | ({
      type: 'subagent_event'
      id: string
      payload: {
        type: string
        text?: string
        thinking?: string
        tool_name?: string
        tool_id?: string
      }
    } & SessionId)
  | ({
      /** Generative UI widget — emitted by the `show_widget` tool when
       *  an agent wants to render an interactive HTML/SVG fragment
       *  inline in the conversation. The renderer keys widgets by
       *  `toolCallId` so they appear at the exact spot in the message
       *  stream where the tool was invoked. */
      type: 'widget_render'
      toolCallId: string
      widget: {
        id: string
        title: string
        kind: 'html' | 'svg'
        code: string
        loadingMessages: string[]
        createdAt: number
      }
    } & SessionId)
  | ({ type: 'memory_retrieved'; memory: MemoryRecord; reason?: string } & SessionId)
  | ({ type: 'memory_updated'; memory: MemoryRecord; reason?: string } & SessionId)
  | ({ type: 'skill_retrieved'; skill: Skill; reason?: string } & SessionId)
  | ({ type: 'skill_loaded'; skill: Skill; reason?: string } & SessionId)
  | ({ type: 'skill_pruned'; skill: Skill; reason?: string } & SessionId)
  | ({ type: 'skill_updated'; skill: Skill } & SessionId)
  | ({
      type: 'usage'
      contextTokens?: number
      inputTokens: number
      outputTokens: number
      cacheReadTokens: number
      cacheWriteTokens: number
      cost: number
    } & SessionId)
  | ({
      type: 'usage_snapshot'
      contextTokens?: number
      inputTokens: number
      outputTokens: number
      cacheReadTokens: number
      cacheWriteTokens: number
      cost: number
    } & SessionId)
  | ({ type: 'message_stop'; stopReason: string } & SessionId)
  | ({ type: 'turn_complete'; turnId: string; success: boolean } & SessionId)
  | ({
      type: 'file_matches'
      query: string
      matches: Array<{ path: string; name: string }>
    } & SessionId)
  | ({
      type: 'permission_request'
      requestId: string
      level: 'info' | 'low' | 'medium' | 'high' | 'dangerous'
      prompt: string
      reason?: string
      details?: string
    } & SessionId)
  | ({
      /** Renderer-side title update. The bridge fires this after the
       *  first user → assistant exchange completes so it can give a
       *  default-titled session a concise Haiku-generated label
       *  ("Spec review", "Pricing audit", etc.). `source: 'auto'`
       *  signals automatic origin so future operator/manual renames
       *  can be distinguished if needed; nothing currently branches
       *  on it. */
      type: 'session_renamed'
      title: string
      source?: 'auto' | 'operator'
    } & SessionId)
  | ({
      /** Append a single message to a session's transcript out-of-band
       *  (i.e. not produced by the runner's turn loop). Used by the
       *  deep judge synthesis pass to inject the "render verdict now"
       *  user prompt into the judge subagent's session so the operator
       *  can see investigation → synthesis as one continuous chat.
       *
       *  The reducer treats this as a hard append: the message lands
       *  at the end of the existing transcript, indexed by its own id
       *  (no merging with the streaming-message id). Use turn_start +
       *  text_delta for streaming assistant content; this event is for
       *  fully-formed messages whose text is known up front. */
      type: 'message_appended'
      role: 'user' | 'assistant' | 'system'
      content: string
      messageId?: string
      createdAt?: number
    } & SessionId)
  | ({
      type: 'session_spawned'
      parentSessionId: string
      title: string
      model: string
      reasoningLevel?: string
      task: string
      /** Full system prompt the child runner is configured with. Stored
       *  on the child slice so the conversation pane can render it as a
       *  collapsible header — useful for inspecting judge / calibrator
       *  child sessions where the system prompt is the whole contract. */
      systemPrompt?: string
      mode?: string
      agentType?: string
      coordinationStrategy?: CoordinationStrategy
      kanbanTaskId?: string
      taskId?: string
      workspace?: string
      createdAt: number
      /** Re-wake metadata — set by SubAgentTool.resume_archived. */
      wokenBy?: 'operator' | 'agent'
      resumed?: boolean
    } & SessionId)
  | ({
      type: 'session_completed'
      success: boolean
      elapsedMs: number
      contextTokens?: number
      inputTokens?: number
      outputTokens?: number
      toolsCalled?: number
      artifactPath?: string
      createdFiles?: string[]
    } & SessionId)
  | {
      // Bridge confirms a branch operation. `idRemap` maps every old
      // session id (parent + each cloned subagent) to its new id; the
      // renderer applies the same remap to its in-memory snapshots.
      type: 'session_branched'
      originalSessionId: string
      newSessionId: string
      newName: string
      messageOrdinal: number
      idRemap: Record<string, string>
      childMappings: Array<{ oldId: string; newId: string }>
    }
  // --- Computer-use events ---
  | ({
      type: 'computer_session_start'
      parentSessionId: string
      goal: string
      targetApp?: string
      maxSteps: number
    } & SessionId)
  | ({
      type: 'computer_session_end'
      outcome: 'done' | 'failed' | 'cancelled' | 'stuck'
      summary: string
    } & SessionId)
  | ({
      type: 'screenshot_frame'
      pngBase64: string // historical name -- may actually be JPEG data
      mimeType?: string // "image/png" or "image/jpeg"; defaults to png
      width: number
      height: number
      takenAt: number
      reason?: string
    } & SessionId)
  | ({
      type: 'action_planned'
      action: string
      description?: string
      x?: number
      y?: number
      w?: number
      h?: number
      key?: string
      modifiers?: string[]
      double?: boolean
      length?: number
    } & SessionId)
  | ({
      type: 'action_executed'
      action: string
      success: boolean
      durationMs: number
      error?: string
    } & SessionId)
  | { type: 'emergency_stop'; reason?: string; stopped?: number }

// --- Channel names ---

export const IPC = {
  bridgeEvent: 'harness:bridge-event',
  sendCommand: 'harness:send-command',
  getMode: 'harness:get-mode',
  requestDemoBurst: 'harness:demo-burst',
  restartBridge: 'harness:restart-bridge',
  openExternal: 'shell:open-external',
  appInfo: 'app:info',
  sessionList: 'session:list',
  sessionLoad: 'session:load',
  sessionSave: 'session:save',
  sessionIndexSave: 'session:index-save',
  sessionDelete: 'session:delete',
  sessionExport: 'session:export',
  settingsGet: 'settings:get',
  settingsUpdate: 'settings:update',
  artifactRead: 'artifact:read',
  artifactWrite: 'artifact:write',
  compactionMetrics: 'compaction:metrics',
  // Gateway / Slack onboarding
  gatewayStatus: 'gateway:status',
  gatewayInstall: 'gateway:install',
  gatewayUninstall: 'gateway:uninstall',
  gatewayStart: 'gateway:start',
  gatewayStop: 'gateway:stop',
  slackManifest: 'gateway:slack:manifest',
  slackCopyManifest: 'gateway:slack:copy-manifest',
  slackVerifyTokens: 'gateway:slack:verify-tokens',
  slackSaveTokens: 'gateway:slack:save-tokens',
  slackSetAllowlist: 'gateway:slack:set-allowlist',
  slackGetConfig: 'gateway:slack:get-config',
  /** Returns which LLM provider keys are present vs missing in the
   *  desktop's process.env. Used by the wizard to warn the operator
   *  if launching from Finder (or a shell without the relevant API
   *  keys exported) means the daemon won't be able to reach any LLM. */
  llmKeysProbe: 'gateway:llm-keys:probe',
} as const

// ── Gateway IPC result types ────────────────────────────────────

export interface GatewayStatus {
  pid: number | null
  freyjaBin: string | null  // resolved path to the `freyja` binary
  plistInstalled: boolean
  plistPath: string
  logPath: string
  errPath: string
  slackConfigured: boolean   // both tokens present in ~/.freyja/.env
  // Cheap snapshot of workspaces this install is configured for.
  // `teamId` is always present (read from gateway.yaml allowlist);
  // the rest are best-effort and may be missing if the gateway
  // daemon hasn't published live workspace info yet. The UI should
  // fall back to teamId when display fields are missing.
  workspaces: Array<{
    teamId: string
    teamName?: string
    botUserId?: string
    botName?: string
    allowlist?: string[]     // empty = allow any in this workspace
  }>
  error?: string
}

export interface SlackVerifyResult {
  ok: boolean
  botName?: string
  botUserId?: string
  teamId?: string
  teamName?: string
  error?: string
}

export interface SlackManifestResult {
  ok: boolean
  manifestJson?: string
  manifestPath?: string
  error?: string
}

export interface SimpleResult {
  ok: boolean
  error?: string
  message?: string
}

export interface LlmKeysProbeResult {
  ok: boolean
  /** Keys that are present in the desktop's process.env AND already
   *  saved to ~/.freyja/.env where the daemon will read them. */
  present: string[]
  /** Keys missing from process.env entirely. The daemon won't have
   *  these unless the operator adds them to ~/.freyja/.env by hand. */
  missing: string[]
  /** Whether at least one frontier-tier key (Anthropic / OpenAI) is
   *  available somewhere — useful for the wizard to say "you'll need
   *  this to use Slack" with a stronger warning vs. soft heads-up. */
  hasFrontierKey: boolean
  error?: string
}

export interface CompactionMetricsResult {
  ok: boolean
  rows: CompactionTelemetryRow[]
  error?: string
}

export type CompactionTelemetryRow =
  | {
      type: 'pressure_signal'
      ts: number
      session_id: string
      agent_type?: string | null
      parent_session_id?: string | null
      band: 'clean' | 'pruning' | 'awareness' | 'soft' | 'strong' | 'fallback'
      pressure_pct: number
      used_tokens: number
      effective_window: number
      model?: string
    }
  | {
      type: 'compaction_event'
      ts: number
      session_id: string
      agent_type?: string | null
      parent_session_id?: string | null
      subtype: string
      model?: string
      tokens_before: number
      tokens_after: number
      mechanism: string
      trigger?: string
      scope?: string | null
      reason?: string | null
      resumed_from_previous?: boolean
      /** First ~240 chars of the produced summary — for at-a-glance
       *  lists where the full text is too long. */
      summary_excerpt?: string | null
      /** Full produced summary text. Powers the clickable
       *  compaction-log rows in the dashboard so users can inspect
       *  exactly what was summarized. */
      summary_text?: string | null
    }
  | {
      type: 'llm_call_metric'
      ts: number
      session_id: string
      turn_id?: string
      agent_type?: string | null
      parent_session_id?: string | null
      model: string
      input_tokens: number
      output_tokens: number
      cache_read_tokens: number
      cache_write_tokens: number
      cost_usd: number | null
      duration_ms: number
      /** "main" for regular agent-loop calls; "summarizer" for
       *  compaction LLM calls forwarded through the runner's
       *  on_llm_call hook. Lets the dashboard break out compaction
       *  overhead distinct from main-loop spend. */
      call_kind?: 'main' | 'summarizer'
      /** Only set when call_kind === 'summarizer': true iff this
       *  summarizer call used the SUMMARY_UPDATE_PROMPT (iterative
       *  path) instead of the fresh SUMMARY_PROMPT. */
      iterative_summarizer?: boolean | null
    }
  | {
      type: 'tool_call_metric'
      ts: number
      session_id: string
      turn_id?: string
      agent_type?: string | null
      parent_session_id?: string | null
      tool_call_id?: string
      tool_name: string
      duration_ms: number
      ok: boolean
      result_bytes: number
    }
  | {
      type: 'profile_invocation'
      ts: number
      session_id: string
      parent_session_id: string
      agent_type: string
      model: string
      max_iterations: number
      task_preview: string
    }
  | {
      type: 'profile_completion'
      ts: number
      session_id: string
      parent_session_id: string
      agent_type: string
      iterations_used: number
      final_outcome: 'success' | 'error' | 'cancelled'
      duration_ms: number
    }
  | {
      type: 'summarize_context_call'
      ts: number
      session_id: string
      turn_id?: string
      agent_type?: string | null
      parent_session_id?: string | null
      scope: string
      level_requested?: string
      level_used?: string
      preserve_facts_count?: number
      preserve_facts_missing?: string[]
      pinned_ordinals?: number[]
      reason?: string
      pressure_pct_at_call?: number | null
      tokens_before: number
      tokens_after: number
      resumed_from_previous?: boolean
      entries_removed?: number
      success: boolean
      error?: string | null
      elapsed_ms?: number
      model?: string
    }

/** Response from the artifact:read IPC. */
export interface ArtifactReadResult {
  ok: boolean
  /** Plain text content (decoded utf-8). Null when binary. */
  content: string | null
  /** Base64-encoded bytes for binary files (images, etc). Null otherwise. */
  binary: string | null
  /** MIME type inferred from extension. */
  mimeType: string
  /** Size in bytes. */
  size: number
  /** Error message if ok=false. */
  error?: string
}

export type PermissionTier = 'none' | 'low' | 'medium' | 'high' | 'yolo'

export interface ComputerControlSettings {
  /** Master toggle -- off by default. Flipping this on the first time
   *  triggers the permission wizard (Screen Recording + Accessibility). */
  enabled: boolean
  /** First-run wizard completion state. `never` = not yet shown.
   *  `done` = user confirmed it's set up. Rewizard = ask again. */
  wizardState: 'never' | 'done' | 'rewizard'
  /** Per-app allowlist -- if non-empty, ONLY these bundle ids may be
   *  driven. Empty = allow any app not on the blocklist. */
  allowlist: string[]
  /** Per-app blocklist -- these bundle ids are ALWAYS refused. */
  blocklist: string[]
  /** Default cap on `computer_use.max_steps` when the caller doesn't
   *  specify one. */
  maxStepsDefault: number
  /** Whether to show live screenshots in the parent feed (thumbnail)
   *  vs. only in the activity panel. */
  showScreenshotsInline: boolean
}

export interface DesktopSettings {
  version: 1
  permissions: {
    autoApprove: PermissionTier
  }
  computer: ComputerControlSettings
}

/** Default blocklist for computer control -- shipped on day one.
 *  Password managers + banks + keychain. */
export const DEFAULT_COMPUTER_BLOCKLIST: string[] = [
  'com.agilebits.onepassword7',
  'com.agilebits.onepassword8',
  'com.apple.keychainaccess',
  'com.bitwarden.desktop',
  'com.lastpass.LastPass',
  'com.dashlane.mac',
]

export type AppInfo = {
  version: string
  electronVersion: string
  platform: NodeJS.Platform
  workspace: string
  harnessRoot: string
}
