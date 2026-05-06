// Shared event types between main, preload, renderer, and the Python bridge.
// Keep this file dependency-free.

export type BridgeMode = 'live' | 'demo' | 'error'

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
}

export interface BusMessageRecord {
  index: number
  topic: 'findings' | 'errors' | 'progress' | 'read'
  senderId: string
  senderLabel: string
  content: string
  timestamp: number
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
  fileChangeSet?: FileChangeSet
}

export interface MessagePart {
  type: 'text' | 'thinking' | 'tool_call' | 'tool_result' | 'subagent' | 'system'
  text?: string
  toolCallId?: string
  subagentId?: string
  systemSubtype?: string
}

export interface MessageAttachmentRef {
  id: string
  type: 'image'
  previewUrl: string
  name?: string
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
}

export interface SessionSnapshot {
  id: string
  title: string
  workspace: string
  model: string
  reasoningLevel?: string
  createdAt: number
  updatedAt: number
  messageCount: number
  totalInputTokens: number
  totalOutputTokens: number
  cacheReadTokens: number
  /** Present on sessions spawned as sub-agents. */
  parentSessionId?: string
  /** Ids of sessions spawned from this one. */
  childSessionIds?: string[]
  /** Task prompt given to a sub-agent when spawned. */
  task?: string
  /** Agent type specialization (explore, code, verify, etc.) */
  agentType?: string
  /** Whether a sub-agent session has completed. */
  completed?: boolean
  completedAt?: number
  success?: boolean
}

export interface CommandAttachment {
  type: 'image'
  mimeType: string
  dataBase64: string
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
      attachments?: CommandAttachment[]
    }
  | { type: 'cancel'; sessionId?: string }
  | { type: 'force_cancel'; sessionId?: string }
  | { type: 'diagnose' }
  | { type: 'compact'; sessionId?: string; model?: string; reasoningLevel?: string }
  | { type: 'set_model'; sessionId?: string; model: string; reasoningLevel?: string }
  | { type: 'list_skills'; sessionId?: string }
  | { type: 'list_subagents'; sessionId?: string }
  | { type: 'list_tools'; sessionId?: string }
  | { type: 'new_session'; sessionId?: string; model?: string; reasoningLevel?: string }
  | { type: 'switch_session'; sessionId: string; model?: string; reasoningLevel?: string }
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
  | ({ type: 'memory_retrieved'; memory: MemoryRecord; reason?: string } & SessionId)
  | ({ type: 'memory_updated'; memory: MemoryRecord; reason?: string } & SessionId)
  | ({ type: 'skill_retrieved'; skill: Skill; reason?: string } & SessionId)
  | ({ type: 'skill_loaded'; skill: Skill; reason?: string } & SessionId)
  | ({ type: 'skill_pruned'; skill: Skill; reason?: string } & SessionId)
  | ({ type: 'skill_updated'; skill: Skill } & SessionId)
  | ({
      type: 'usage'
      inputTokens: number
      outputTokens: number
      cacheReadTokens: number
      cacheWriteTokens: number
      cost: number
    } & SessionId)
  | ({
      type: 'usage_snapshot'
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
      type: 'session_spawned'
      parentSessionId: string
      title: string
      model: string
      reasoningLevel?: string
      task: string
      mode?: string
      agentType?: string
      workspace?: string
      createdAt: number
    } & SessionId)
  | ({
      type: 'session_completed'
      success: boolean
      elapsedMs: number
      inputTokens?: number
      outputTokens?: number
      toolsCalled?: number
      artifactPath?: string
    } & SessionId)
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
} as const

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
