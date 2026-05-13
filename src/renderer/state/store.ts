import { create } from 'zustand'
import type {
  BridgeEvent,
  BridgeMode,
  CoordinationStrategy,
  ArtifactRecord,
  DesktopSettings,
  FileChangeSet,
  MemoryRecord,
  Message,
  MessagePart,
  PermissionTier,
  SessionSnapshot,
  Skill,
  SubagentRecord,
  ToolCallRecord,
  ToolCatalogEntry,
} from '@shared/events'
import {
  getPersistableFrame,
  normalizeFrame,
  registerFrame,
  releaseFrame,
  retainFrame,
  type FrameRef,
} from '../lib/frameMedia'
import { extractConversationSummary } from '../lib/conversationSummary'

/** Per-computer-session live state: latest screenshot frame, planned
 *  action (for the highlight ring), and action history. */
export interface ComputerSessionState {
  sessionId: string
  parentSessionId?: string
  goal: string
  targetApp?: string
  status: 'idle' | 'running' | 'done' | 'failed' | 'cancelled'
  latestFrame?: FrameRef
  /** Total number of screenshot_frame events received for this session.
   *  Surfaced in the UI as a diagnostic badge. */
  frameCount: number
  plannedAction?: {
    action: string
    description?: string
    x?: number
    y?: number
    w?: number
    h?: number
    plannedAt: number
  }
  history: Array<{
    action: string
    description?: string
    success: boolean
    durationMs: number
    at: number
  }>
  summary?: string
}

export interface SystemEventRecord {
  id: string
  subtype: string
  message: string
  at: number
  details?: Record<string, unknown>
}

/**
 * Per-session state snapshot — everything that gets swapped when the user
 * switches between sessions. Components still read flat fields off the
 * top-level store, and switchSession archives/restores these slices.
 */
export interface SessionSlice {
  messages: Message[]
  currentStreamingMessageId: string | null
  currentTurnId: string | null
  thinking: string
  isStreaming: boolean
  toolCalls: Record<string, ToolCallRecord>
  toolCallOrder: string[]
  fileChanges: FileChangeSet[]
  subagents: Record<string, SubagentRecord>
  subagentOrder: string[]
  usage: {
    currentContextTokens: number
    totalInputTokens: number
    totalOutputTokens: number
    totalCacheReadTokens: number
    totalCacheWriteTokens: number
    totalCost: number
    lastTurnInputTokens: number
    lastTurnOutputTokens: number
    contextWindow: number
  }
  systemEvents: SystemEventRecord[]
  /** Durable per-card kanban snapshot. Keyed by `card_NNN` id; value
   *  is the raw `details.task` dict from the most recent `kanban_*`
   *  event for that card. Unlike `systemEvents` (which is a rolling
   *  100-entry buffer), this map is NEVER trimmed — so completed
   *  cards from earlier in a long session don't disappear when the
   *  parent moves on to a new batch of work. The bridge keeps the
   *  authoritative state in memory + on the journal; this is the
   *  renderer's copy so card-state reconstruction never depends on
   *  ephemeral events being present. */
  kanbanCards: Record<string, Record<string, unknown>>
  busMessages: Array<import('@shared/events').BusMessageRecord>
  /** Inbox events — every inter-agent or operator→agent message that
   *  touched this session (sent OR received). Used for inline chip
   *  rendering in the parent transcript and "you have N unread"
   *  indicators in the sidebar. Capped at 100 most recent. */
  inboxEvents: Array<{
    id: string
    action: 'enqueued' | 'delivered' | 'dropped'
    fromSession: string
    fromLabel: string
    fromRole: 'operator' | 'agent'
    content: string
    force: boolean
    replyTo: string | null
    timestamp: number
    sessionId: string  // the session that received this event
  }>
  artifacts: Array<import('@shared/events').ArtifactRecord>
  model: string
  reasoningLevel: string
  coordinationStrategy: CoordinationStrategy
  /** System prompt sent to the model. Captured from the bridge's
   *  `system_prompt_set` event for session export / training data. */
  systemPrompt?: string
}

export interface SessionPane {
  id: string
  sessionId: string
  createdAt: number
}

export interface ModelChoice {
  id: string
  family: string
  label: string
  tier: string
  contextWindow: number
  description: string
  thinking?: boolean
  reasoningMode?: 'none' | 'adaptive' | 'budget' | 'effort' | 'binary' | 'required'
  reasoningLevels?: string[]
  reasoningDefault?: string
  reasoningHistory?: string[]
  envVar?: string
  available?: boolean
}

export interface HarnessState extends SessionSlice {
  mode: BridgeMode
  modeDetail: string
  ready: boolean
  activeSessionId: string
  sessions: SessionSnapshot[]
  /** Archived per-session slices, keyed by session id. */
  sessionArchive: Record<string, SessionSlice>
  /** Center workspace panes. Only the active engine session is writable;
   *  split panes are live read-only views of archived session slices. */
  sessionPanes: SessionPane[]
  activePaneId: string
  skills: Record<string, Skill>
  memories: Record<string, MemoryRecord>
  logs: Array<{ level: string; message: string; at: number }>
  toolCatalog: Record<string, ToolCatalogEntry>
  availableModels: ModelChoice[]
  // File picker matches for the current @query
  fileMatches: Array<{ path: string; name: string }>
  fileQuery: string
  // Pending permission requests from the bridge, most-recent first.
  permissionQueue: Array<{
    requestId: string
    sessionId?: string
    level: 'info' | 'low' | 'medium' | 'high' | 'dangerous'
    prompt: string
    reason?: string
    details?: string
  }>
  // Attachments queued for the next send
  pendingAttachments: Array<{ id: string; type: 'image'; mimeType: string; dataBase64: string; previewUrl: string }>
  // Derived / UI
  inputDraft: string
  commandPaletteOpen: boolean
  missionDashboardOpen: boolean
  // 'swarm' / 'findings' / 'telemetry' are kept for legacy callers; the
  // live tab set is overview / activity / profiles.
  missionDashboardTab:
    | 'overview'
    | 'activity'
    | 'profiles'
    | 'swarm'
    | 'findings'
    | 'telemetry'
  /** Cross-session compaction metrics dashboard (header button toggle). */
  metricsDashboardOpen: boolean
  activeSubagentId: string | null
  focusedPanel: 'sidebar' | 'conversation' | 'activity'
  debugOpen: boolean
  modelPickerOpen: boolean
  settingsOpen: boolean
  /** User preference — hide the left Sidebar panel for a focused view. */
  sidebarCollapsed: boolean
  /** User preference — hide the right Activity panel for a focused view. */
  activityPanelCollapsed: boolean
  focusMode: boolean
  preFocusPanelState: {
    sidebarCollapsed: boolean
    activityPanelCollapsed: boolean
  } | null
  /** Tool-call focus target used by changes/artifacts navigation. */
  focusedToolCallId: string | null
  focusedToolCallSerial: number
  /** User preference — pixel width of the Activity panel when open.
   *  Persisted to localStorage so the preference survives reloads. */
  activityPanelWidth: number
  /** User preference — pixel width of the Sidebar workspace panel when
   *  open. Same drag-handle pattern as the activity panel, mirrored to
   *  the right edge. Persisted to localStorage. */
  sidebarWidth: number
  settings: DesktopSettings
  toast: { id: string; message: string; tone: 'info' | 'ok' | 'warn' | 'danger'; at: number } | null
  /** Live state for each active computer-use session. Frames are
   *  latest-only (no history) to keep memory bounded. */
  computerSessions: Record<string, ComputerSessionState>
  /** Floating panic window is visible whenever any session is
   *  running. Derived from computerSessions but cached here for
   *  cheap selector access. */
  computerActive: boolean
  /** Wizard state for the permission setup flow. */
  computerWizardOpen: boolean
}

export interface HarnessActions {
  handleEvent(ev: BridgeEvent): void
  setInputDraft(v: string): void
  sendMessage(content: string): Promise<void>
  /** Operator-typed message into any agent session's inbox. Routes
   *  through the bridge's TalkRouter so root sessions, live sub-agents,
   *  and archived (re-wakeable) sub-agents all work uniformly.
   *  - sessionId can be any visible session id.
   *  - force=true interrupts the recipient mid-operation. */
  operatorTalk(sessionId: string, content: string, force?: boolean): Promise<void>
  cancelTurn(): Promise<void>
  setModel(model: string, reasoningLevel?: string): Promise<void>
  setReasoningLevel(reasoningLevel: string): Promise<void>
  setCoordinationStrategy(strategy: CoordinationStrategy): Promise<void>
  openSubagent(id: string | null): void
  toggleCommandPalette(open?: boolean): void
  toggleMissionDashboard(
    open?: boolean,
    tab?: HarnessState['missionDashboardTab'],
  ): void
  toggleMetricsDashboard(open?: boolean): void
  toggleModelPicker(open?: boolean): void
  setFocusedPanel(p: HarnessState['focusedPanel']): void
  requestDemoBurst(): Promise<void>
  newSession(model?: string, reasoningLevel?: string): Promise<void>
  switchSession(sessionId: string): Promise<void>
  openSessionPane(sessionId: string, mode?: 'replace' | 'split'): Promise<void>
  closeSessionPane(paneId: string): Promise<void>
  setActiveSessionPane(paneId: string): void
  /** Memory CRUD bridged via IPC. The bridge writes through to disk
   *  and broadcasts a memory_updated event the store picks up; these
   *  helpers are thin wrappers around sendCommand. */
  updateMemory(id: string, patch: { text?: string; kind?: string; scope?: string; tags?: string[]; note?: string }): Promise<void>
  deleteMemory(id: string, note?: string): Promise<void>
  restoreMemory(id: string, note?: string): Promise<void>
  mergeMemories(ids: string[], text: string, opts?: { kind?: string; scope?: string; tags?: string[]; note?: string }): Promise<void>
  listTools(): Promise<void>
  toggleDebug(open?: boolean): void
  toggleSidebar(collapsed?: boolean): void
  toggleActivityPanel(collapsed?: boolean): void
  setActivityPanelWidth(width: number): void
  setSidebarWidth(width: number): void
  focusToolCall(id: string): void
  toggleFocusMode(focus?: boolean): void
  showToast(message: string, tone?: 'info' | 'ok' | 'warn' | 'danger'): void
  clearToast(): void
  runSlashCommand(name: string, args?: string): boolean
  /** Goal brief CRUD bridged via IPC. The brief is the operator-authored
   *  instruction set the judge consumes on every turn (rigor, voice,
   *  must/should/may criteria, never-do list, when-to-stop). */
  updateJudgeRules(brief: {
    voice: string
    rigorScore: number
    judgeProfile: 'quick' | 'standard' | 'deep'
    criteria: Array<{ id: string; text: string; priority: 'must' | 'should' | 'may' }>
    neverDo: string[]
    whenToStop: string
    judgeTools?: string[]
    judgeMaxIterations?: number
  }): void
  /** Operator-initiated judge calibration. Always overwrites existing
   *  rules with the calibrator's proposal. Auto-fires once on /goal set
   *  too (in that case, only applies if rules are still default). */
  recalibrateJudge(): void
  /** Adopt the calibrator's pending proposal (when auto-apply was
   *  blocked because the operator had pre-authored rules). */
  acceptCalibratorProposal(): void
  /** Dismiss the pending proposal without adopting it. */
  dismissCalibratorProposal(): void
  attachImage(file: File | Blob): Promise<void>
  removeAttachment(id: string): void
  /** Edit a user message in place — local state truncates to before the
   *  message and reinserts a new user message; bridge truncates the
   *  engine transcript and runs a fresh turn. */
  editUserMessage(messageId: string, content: string): Promise<void>
  /** Re-run a user message verbatim — local state drops everything from
   *  the message onward; bridge truncates and re-issues the same content. */
  rerunUserMessage(messageId: string): Promise<void>
  /** Delete a message and everything after it. No follow-up turn. */
  deleteMessagesFrom(messageId: string): Promise<void>
  /** Pin (or unpin) a message so the compactor preserves it verbatim
   *  through every future summary. */
  toggleEntryPin(messageId: string, pinned: boolean): Promise<void>
  /** Branch the current session at a message boundary. New session
   *  contains messages 0..N-1; subagent transcripts are deep-cloned. */
  branchSessionFrom(messageId: string, newName?: string): Promise<void>
  requestFileMatches(query: string): Promise<void>
  answerPermission(requestId: string, approved: boolean): Promise<void>
  hydrateFromDisk(): Promise<void>
  persistSession(sessionId: string): Promise<void>
  persistSessionIndex(): Promise<void>
  persistActiveSession(): Promise<void>
  persistAllSessions(): Promise<void>
  loadPersistedSessionIntoArchive(sessionId: string): Promise<boolean>
  toggleSettings(open?: boolean): void
  hydrateSettings(): Promise<void>
  setPermissionTier(tier: PermissionTier): Promise<void>
  escalateSessionPolicy(tier: PermissionTier): Promise<void>
  switchToParent(): Promise<void>
  // ─── Session management ──────────────────────────────────────
  renameSession(sessionId: string, title: string): void
  deleteSession(sessionId: string): Promise<void>
  downloadSession(sessionId: string): Promise<void>
  // ─── Computer control ────────────────────────────────────────
  setComputerEnabled(enabled: boolean): Promise<void>
  openComputerWizard(open?: boolean): void
  emergencyStopComputer(reason?: string): Promise<void>
}

// Tool names that create or modify files — used to extract artifacts
const FILE_WRITE_TOOLS = new Set([
  'write_file', 'write', 'edit_file', 'edit', 'edit_json',
])

let messageCounter = 0
function nextId(prefix = 'm'): string {
  messageCounter += 1
  return `${prefix}_${Date.now().toString(36)}_${messageCounter}`
}

/** Find a message's 0-indexed ordinal among the message-bearing entries
 *  (user + assistant) in the renderer's view. The bridge counts engine
 *  transcript entries the same way, so this index round-trips to the
 *  right entry on the bridge side. Returns -1 if not found. */
function messageOrdinalById(messages: Message[], messageId: string): number {
  let ordinal = -1
  for (const m of messages) {
    if (m.role !== 'user' && m.role !== 'assistant') continue
    ordinal += 1
    if (m.id === messageId) return ordinal
  }
  return -1
}

/** Walk the parent-child session graph and return every descendant id of
 *  `parentId`, in BFS order. Used to enumerate which subagent transcripts
 *  the bridge should clone alongside the parent during a branch. */
function collectDescendantSessionIds(
  sessions: SessionSnapshot[],
  parentId: string,
): string[] {
  const out: string[] = []
  const queue: string[] = [parentId]
  const seen = new Set<string>([parentId])
  while (queue.length > 0) {
    const cur = queue.shift()!
    for (const s of sessions) {
      if (s.parentSessionId === cur && !seen.has(s.id)) {
        seen.add(s.id)
        out.push(s.id)
        queue.push(s.id)
      }
    }
  }
  return out
}

/** Best-known cumulative cost for a session: live `slice.usage.totalCost`
 *  when the session is loaded, otherwise the snapshot's persisted
 *  `totalCost` so unloaded subagents still contribute their tracked
 *  spend. Returns 0 when neither source has it. */
function costForSessionId(state: HarnessState, sessionId: string): number {
  const snapshot = state.sessions.find((s) => s.id === sessionId)
  if (state.activeSessionId === sessionId) {
    return state.usage?.totalCost ?? snapshot?.totalCost ?? 0
  }
  const archive = state.sessionArchive[sessionId]
  if (archive?.usage?.totalCost != null) return archive.usage.totalCost
  return snapshot?.totalCost ?? 0
}

/** Sum a session's own cost with the cost of every descendant subagent.
 *  Used by the activity panel so the "session spend" for a parent
 *  reflects total work (parent + spawned agents + nested subagents). */
export function aggregateSessionCost(
  state: HarnessState,
  sessionId: string,
): number {
  let total = costForSessionId(state, sessionId)
  for (const descendantId of collectDescendantSessionIds(
    state.sessions,
    sessionId,
  )) {
    total += costForSessionId(state, descendantId)
  }
  return total
}

/** Re-encode an image down to fit under the LLM provider's image cap.
 *  Tries progressively smaller (max-dim, JPEG-quality) settings until
 *  the raw byte size lands under `targetBytes`. Returns null on failure
 *  so the caller can fall back to the original blob.
 *
 *  Anthropic's API caps each image's base64 STRING at 5 MiB; raw bytes
 *  inflate ~33% as base64, so a 3.5 MiB raw target gives ~10% headroom
 *  on the 5 MiB ceiling. Always re-encodes as JPEG (PNGs are what blow
 *  past the cap when screenshots carry alpha + lossless compression). */
async function downscaleImageForLLM(
  file: File | Blob,
  targetBytes = 3_500_000,
): Promise<{ bytes: Uint8Array; mimeType: string } | null> {
  let url: string | null = null
  try {
    url = URL.createObjectURL(file)
    const img = new Image()
    await new Promise<void>((resolve, reject) => {
      img.onload = () => resolve()
      img.onerror = () => reject(new Error('image decode failed'))
      img.src = url!
    })
    const attempts: Array<[number, number]> = [
      [2400, 0.88],
      [1800, 0.85],
      [1400, 0.82],
      [1100, 0.78],
      [900, 0.75],
      [720, 0.72],
    ]
    let lastResult: { bytes: Uint8Array; mimeType: string } | null = null
    for (const [maxDim, quality] of attempts) {
      const scale = Math.min(1, maxDim / Math.max(img.width || 1, img.height || 1))
      const w = Math.max(1, Math.round((img.width || 1) * scale))
      const h = Math.max(1, Math.round((img.height || 1) * scale))
      const canvas = document.createElement('canvas')
      canvas.width = w
      canvas.height = h
      const ctx = canvas.getContext('2d')
      if (!ctx) continue
      ctx.drawImage(img, 0, 0, w, h)
      const blob = await new Promise<Blob | null>((resolve) =>
        canvas.toBlob(resolve, 'image/jpeg', quality),
      )
      if (!blob) continue
      const buf = new Uint8Array(await blob.arrayBuffer())
      lastResult = { bytes: buf, mimeType: 'image/jpeg' }
      if (buf.length <= targetBytes) return lastResult
    }
    // Last resort — return the smallest attempt even if still over.
    return lastResult
  } catch {
    return null
  } finally {
    if (url) URL.revokeObjectURL(url)
  }
}

const MODEL_CONTEXT_WINDOWS: Record<string, number> = {
  // Claude 4.7
  'claude-opus-4-7': 1_000_000,
  // Claude 4.6
  'claude-opus-4-6': 1_000_000,
  'claude-sonnet-4-6': 1_000_000,
  // Claude 4.5 / 4
  'claude-haiku-4-5': 200_000,
  'claude-opus-4-5': 200_000,
  'claude-sonnet-4-5': 1_000_000,
  'claude-sonnet-4': 200_000,
  'claude-opus-4': 200_000,
  // OpenAI
  'gpt-5.5': 1_050_000,
  'gpt-5.4': 1_050_000,
  'gpt-5.4-pro': 1_050_000,
  'gpt-5.4-mini': 400_000,
  'gpt-5.4-nano': 400_000,
  'gpt-5.3-codex': 400_000,
  'gpt-4-turbo': 128_000,
  // Keep these in sync with engine/providers.py MODEL_META.
  // Previously anything not listed here silently defaulted to 200k,
  // so zai-glm-4.7 sessions showed `ctx X/200k` while the actual
  // Cerebras context window is 131k — compaction triggers felt
  // "wrong" because the UI denominator didn't match the provider.
  'zai-glm-4.7': 131_072,
  'deepseek-v4-pro': 1_048_576,
  'glm-5.1': 202_752,
  'kimi-k2.6': 262_144,
  'minimax-m2.7': 196_608,
  'qwen3.6-plus': 1_000_000,
  'glm5': 202_752,
  'kimi-k2.5': 262_144,
  'minimax-m2.5': 196_608,
}

function contextWindowFor(model: string): number {
  return MODEL_CONTEXT_WINDOWS[model] ?? 200_000
}

const MODEL_REASONING_FALLBACKS: Record<string, { levels: string[]; defaultLevel: string }> = {
  'claude-opus-4-7': { levels: ['auto'], defaultLevel: 'auto' },
  'claude-opus-4-6': { levels: ['none', 'low', 'medium', 'high', 'max'], defaultLevel: 'max' },
  'claude-sonnet-4-6': { levels: ['none', 'low', 'medium', 'high'], defaultLevel: 'high' },
  'claude-haiku-4-5': { levels: ['none', 'low', 'medium', 'high'], defaultLevel: 'high' },
  'claude-opus-4-5': { levels: ['none', 'low', 'medium', 'high'], defaultLevel: 'high' },
  'claude-sonnet-4-5': { levels: ['none', 'low', 'medium', 'high'], defaultLevel: 'high' },
  'gpt-5.5': { levels: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'], defaultLevel: 'high' },
  'gpt-5.4': { levels: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'], defaultLevel: 'high' },
  'gpt-5.4-mini': { levels: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'], defaultLevel: 'medium' },
  'gpt-5.4-nano': { levels: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'], defaultLevel: 'low' },
  'gpt-5.3-codex': { levels: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh'], defaultLevel: 'medium' },
  'deepseek-v4-pro': { levels: ['none', 'low', 'medium', 'high', 'max'], defaultLevel: 'high' },
  'glm-5.1': { levels: ['none', 'low', 'medium', 'high'], defaultLevel: 'high' },
  'kimi-k2.6': { levels: ['none', 'low', 'medium', 'high'], defaultLevel: 'high' },
  'minimax-m2.7': { levels: ['low', 'medium', 'high'], defaultLevel: 'medium' },
  'qwen3.6-plus': { levels: ['none', 'low', 'medium', 'high'], defaultLevel: 'medium' },
  'minimax-m2.5': { levels: ['low', 'medium', 'high'], defaultLevel: 'medium' },
}

function modelChoiceFor(model: string, models: ModelChoice[] = []): ModelChoice | undefined {
  return models.find((m) => m.id === model)
}

function reasoningLevelsFor(model: string, models: ModelChoice[] = []): string[] {
  const choice = modelChoiceFor(model, models)
  if (choice?.reasoningLevels?.length) return choice.reasoningLevels
  return MODEL_REASONING_FALLBACKS[model]?.levels ?? []
}

function defaultReasoningFor(model: string, models: ModelChoice[] = []): string {
  const choice = modelChoiceFor(model, models)
  const fallback = MODEL_REASONING_FALLBACKS[model]
  const levels = reasoningLevelsFor(model, models)
  if (levels.length === 0 || choice?.reasoningMode === 'none') return 'none'
  const candidate = choice?.reasoningDefault || fallback?.defaultLevel || levels[0] || 'none'
  return levels.includes(candidate) ? candidate : levels[0]
}

function normalizeReasoningFor(
  model: string,
  reasoningLevel: string | undefined,
  models: ModelChoice[] = [],
): string {
  const levels = reasoningLevelsFor(model, models)
  if (levels.length === 0) return 'none'
  const normalized = (reasoningLevel || defaultReasoningFor(model, models)).toLowerCase()
  if (normalized === 'off' && levels.includes('none')) return 'none'
  if (levels.includes(normalized)) return normalized
  return defaultReasoningFor(model, models)
}

function normalizeCoordinationStrategy(value?: string | null): CoordinationStrategy {
  if (value === 'isolated' || value === 'kanban' || value === 'bus' || value === 'goal') return value
  if (value === 'solo' || value === 'delegate') return 'isolated'
  if (value === 'board') return 'kanban'
  if (value === 'goals' || value === 'goal-loop' || value === 'ralph') return 'goal'
  return 'bus'
}

function emptySlice(
  model: string = 'claude-sonnet-4-6',
  reasoningLevel?: string,
  models: ModelChoice[] = [],
  coordinationStrategy: CoordinationStrategy = 'bus',
): SessionSlice {
  const normalizedReasoning = normalizeReasoningFor(model, reasoningLevel, models)
  return {
    messages: [],
    currentStreamingMessageId: null,
    currentTurnId: null,
    thinking: '',
    isStreaming: false,
    toolCalls: {},
    toolCallOrder: [],
    fileChanges: [],
    subagents: {},
    subagentOrder: [],
    usage: {
      currentContextTokens: 0,
      totalInputTokens: 0,
      totalOutputTokens: 0,
      totalCacheReadTokens: 0,
      totalCacheWriteTokens: 0,
      totalCost: 0,
      lastTurnInputTokens: 0,
      lastTurnOutputTokens: 0,
      contextWindow: contextWindowFor(model),
    },
    systemEvents: [],
    kanbanCards: {},
    busMessages: [],
    inboxEvents: [],
    artifacts: [],
    model,
    reasoningLevel: normalizedReasoning,
    coordinationStrategy: normalizeCoordinationStrategy(coordinationStrategy),
  }
}

function emptyState(): HarnessState {
  const bootId = 'session-boot'
  return {
    ...emptySlice(),
    mode: 'error',
    modeDetail: 'initializing',
    ready: false,
    activeSessionId: bootId,
    sessions: [
      {
        id: bootId,
        title: 'Current session',
        workspace: '',
        model: 'claude-sonnet-4-6',
        reasoningLevel: defaultReasoningFor('claude-sonnet-4-6'),
        coordinationStrategy: 'bus',
        createdAt: Date.now(),
        updatedAt: Date.now(),
        messageCount: 0,
        totalInputTokens: 0,
        totalOutputTokens: 0,
        cacheReadTokens: 0,
      },
    ],
    sessionArchive: {},
    sessionPanes: [{ id: 'pane-main', sessionId: bootId, createdAt: Date.now() }],
    activePaneId: 'pane-main',
    skills: {},
    memories: {},
    logs: [],
    toolCatalog: {},
    availableModels: [],
    fileMatches: [],
    fileQuery: '',
    permissionQueue: [],
    pendingAttachments: [],
    inputDraft: '',
    commandPaletteOpen: false,
    missionDashboardOpen: false,
    missionDashboardTab: 'overview',
    metricsDashboardOpen: false,
    modelPickerOpen: false,
    activeSubagentId: null,
    focusedPanel: 'conversation',
    debugOpen: false,
    settingsOpen: false,
    sidebarCollapsed:
      (typeof localStorage !== 'undefined' &&
        localStorage.getItem('ah.sidebarCollapsed') === '1') || false,
    activityPanelCollapsed:
      (typeof localStorage !== 'undefined' &&
        localStorage.getItem('ah.activityPanelCollapsed') === '1') || false,
    focusMode: false,
    preFocusPanelState: null,
    focusedToolCallId: null,
    focusedToolCallSerial: 0,
    activityPanelWidth: (() => {
      if (typeof localStorage === 'undefined') return 320
      const raw = localStorage.getItem('ah.activityPanelWidth')
      const parsed = raw ? parseInt(raw, 10) : NaN
      // Clamp to a sane range so a stale/corrupt value can't render
      // the panel unusable.
      if (!Number.isFinite(parsed)) return 320
      return Math.max(260, Math.min(900, parsed))
    })(),
    sidebarWidth: (() => {
      if (typeof localStorage === 'undefined') return 256
      const raw = localStorage.getItem('ah.sidebarWidth')
      const parsed = raw ? parseInt(raw, 10) : NaN
      if (!Number.isFinite(parsed)) return 256
      return Math.max(220, Math.min(640, parsed))
    })(),
    settings: {
      version: 1,
      permissions: { autoApprove: 'low' },
      computer: {
        enabled: false,
        wizardState: 'never',
        allowlist: [],
        blocklist: [
          'com.agilebits.onepassword7',
          'com.agilebits.onepassword8',
          'com.apple.keychainaccess',
          'com.bitwarden.desktop',
          'com.lastpass.LastPass',
          'com.dashlane.mac',
        ],
        maxStepsDefault: 60,
        showScreenshotsInline: true,
      },
    },
    toast: null,
    computerSessions: {},
    computerActive: false,
    computerWizardOpen: false,
  }
}

/** Pull the slice fields out of a HarnessState. */
function sliceFromState(s: HarnessState): SessionSlice {
  return {
    messages: s.messages,
    currentStreamingMessageId: s.currentStreamingMessageId,
    currentTurnId: s.currentTurnId,
    thinking: s.thinking,
    isStreaming: s.isStreaming,
    toolCalls: s.toolCalls,
    toolCallOrder: s.toolCallOrder,
    fileChanges: s.fileChanges,
    subagents: s.subagents,
    subagentOrder: s.subagentOrder,
    usage: s.usage,
    systemEvents: s.systemEvents,
    kanbanCards: s.kanbanCards,
    busMessages: s.busMessages,
    inboxEvents: s.inboxEvents,
    artifacts: s.artifacts,
    model: s.model,
    reasoningLevel: s.reasoningLevel,
    coordinationStrategy: s.coordinationStrategy,
    systemPrompt: s.systemPrompt,
  }
}

type PersistedSessionMetaPayload = {
  version: 1
  id: string
  title: string
  model: string
  reasoningLevel?: string
  coordinationStrategy?: CoordinationStrategy
  workspace: string
  createdAt: number
  updatedAt: number
  messageCount: number
  totalInputTokens: number
  totalOutputTokens: number
  cacheReadTokens: number
  parentSessionId?: string
  childSessionIds?: string[]
  task?: string
  agentType?: string
  completed?: boolean
  completedAt?: number
  success?: boolean
}

type PersistedSessionPayload = PersistedSessionMetaPayload & {
  slice: SessionSlice
}

function sliceForSession(state: HarnessState, sessionId: string): SessionSlice | undefined {
  return sessionId === state.activeSessionId
    ? sliceFromState(state)
    : state.sessionArchive[sessionId]
}

function hasPersistableSessionState(
  session: SessionSnapshot,
  slice?: SessionSlice,
): boolean {
  const messageCount = slice?.messages.length ?? session.messageCount ?? 0
  return (
    messageCount > 0 ||
    !!slice?.isStreaming ||
    !!session.parentSessionId ||
    !!session.childSessionIds?.length ||
    !!session.task ||
    !!session.completed
  )
}

function persistedMetaFromSession(
  session: SessionSnapshot,
  slice?: SessionSlice,
): PersistedSessionMetaPayload {
  const usage = slice?.usage
  return {
    version: 1,
    id: session.id,
    title: session.title || 'Session',
    model: slice?.model || session.model,
    reasoningLevel: slice?.reasoningLevel || session.reasoningLevel,
    coordinationStrategy: normalizeCoordinationStrategy(
      slice?.coordinationStrategy || session.coordinationStrategy,
    ),
    workspace: session.workspace || '~/',
    createdAt: session.createdAt,
    updatedAt: session.updatedAt || Date.now(),
    messageCount: slice?.messages.length ?? session.messageCount ?? 0,
    totalInputTokens: usage?.totalInputTokens ?? session.totalInputTokens ?? 0,
    totalOutputTokens: usage?.totalOutputTokens ?? session.totalOutputTokens ?? 0,
    cacheReadTokens: usage?.totalCacheReadTokens ?? session.cacheReadTokens ?? 0,
    parentSessionId: session.parentSessionId,
    childSessionIds: session.childSessionIds,
    task: session.task,
    agentType: session.agentType,
    completed: session.completed,
    completedAt: session.completedAt,
    success: session.success,
  }
}

function persistedPayloadFromSession(
  session: SessionSnapshot,
  slice: SessionSlice,
): PersistedSessionPayload {
  return {
    ...persistedMetaFromSession(session, slice),
    slice: materializeFramesForPersistence(slice),
  }
}

function materializeFramesForPersistence(slice: SessionSlice): SessionSlice {
  let changed = false
  const toolCalls: Record<string, ToolCallRecord> = {}
  for (const [id, call] of Object.entries(slice.toolCalls)) {
    let nextCall = call
    if (call.frame) {
      const frame = getPersistableFrame(call.frame as FrameRef)
      if (frame && frame !== call.frame) {
        changed = true
        nextCall = { ...nextCall, frame }
      }
    }
    if (call.resultImages?.length) {
      const resultImages = call.resultImages
        .map((image) => {
          const frame = getPersistableFrame(image as FrameRef)
          return frame ? { ...frame, label: image.label } : image
        })
      if (resultImages.some((image, idx) => image !== call.resultImages?.[idx])) {
        changed = true
        nextCall = { ...nextCall, resultImages }
      }
    }
    toolCalls[id] = nextCall
  }
  return changed ? { ...slice, toolCalls } : slice
}

function normalizePersistedFrames(slice: SessionSlice): SessionSlice {
  const model = slice.model || 'claude-sonnet-4-6'
  const contextWindow = slice.usage?.contextWindow ?? contextWindowFor(model)
  const totalInputTokens = slice.usage?.totalInputTokens ?? 0
  const persistedContext = (slice.usage as any)?.currentContextTokens
  const currentContextTokens =
    typeof persistedContext === 'number'
      ? persistedContext
      : totalInputTokens > contextWindow
        ? 0
        : totalInputTokens
  slice = {
    ...slice,
    model,
    reasoningLevel: normalizeReasoningFor(model, slice.reasoningLevel),
    coordinationStrategy: normalizeCoordinationStrategy(slice.coordinationStrategy),
    fileChanges: slice.fileChanges ?? [],
    // Defensive default for sessions persisted before the durable
    // kanbanCards snapshot was introduced — load them as empty so
    // downstream consumers can `... ?? {}` cleanly.
    kanbanCards: slice.kanbanCards ?? {},
    usage: {
      ...slice.usage,
      currentContextTokens,
      totalInputTokens,
      totalOutputTokens: slice.usage?.totalOutputTokens ?? 0,
      totalCacheReadTokens: slice.usage?.totalCacheReadTokens ?? 0,
      totalCacheWriteTokens: slice.usage?.totalCacheWriteTokens ?? 0,
      totalCost: slice.usage?.totalCost ?? 0,
      lastTurnInputTokens: slice.usage?.lastTurnInputTokens ?? totalInputTokens,
      lastTurnOutputTokens: slice.usage?.lastTurnOutputTokens ?? 0,
      contextWindow,
    },
  }
  let changed = false
  const toolCalls: Record<string, ToolCallRecord> = {}
  for (const [id, call] of Object.entries(slice.toolCalls)) {
    const nextCall = normalizeToolCallFrames(call)
    if (nextCall !== call) changed = true
    toolCalls[id] = nextCall
  }
  return changed ? { ...slice, toolCalls } : slice
}

function normalizeToolCallFrames(call: ToolCallRecord): ToolCallRecord {
  let changed = false
  let nextCall = call
  const frame = normalizeFrame(call.frame as FrameRef | undefined)
  if (frame) retainFrame(frame)
  if (frame && frame !== call.frame) {
    changed = true
    nextCall = { ...nextCall, frame }
  }
  if (call.resultImages?.length) {
    const resultImages = call.resultImages.map((image) => {
      const frameRef = normalizeFrame(image as FrameRef | undefined)
      if (frameRef) retainFrame(frameRef)
      return frameRef ? { ...frameRef, label: image.label } : image
    })
    if (resultImages.some((image, idx) => image !== call.resultImages?.[idx])) {
      changed = true
      nextCall = { ...nextCall, resultImages }
    }
  }
  return changed ? nextCall : call
}

function releaseSliceFrames(slice?: SessionSlice): void {
  if (!slice) return
  for (const call of Object.values(slice.toolCalls)) {
    releaseFrame(call.frame as FrameRef | undefined)
    for (const image of call.resultImages ?? []) {
      releaseFrame(image as FrameRef | undefined)
    }
  }
}

function upsertArtifactsFromChangeSet(
  artifacts: ArtifactRecord[],
  changeSet: FileChangeSet,
  creator = 'parent',
  creatorLabel = 'Main agent',
): ArtifactRecord[] {
  let next = artifacts
  for (const file of changeSet.files) {
    const artifact: ArtifactRecord = {
      id: `${changeSet.id}:${file.path}`,
      path: file.path,
      filename: file.filename,
      creator,
      creatorLabel,
      createdAt: changeSet.createdAt || Date.now(),
      fileType: file.fileType,
      operation: file.operation,
      changeSetId: changeSet.id,
      toolCallId: changeSet.toolCallId,
      additions: file.additions,
      deletions: file.deletions,
      binary: file.binary,
      diffTruncated: file.diffTruncated,
    }
    const idx = next.findIndex((a) => a.path === file.path && a.creator === creator)
    if (idx === -1) {
      next = [...next, artifact]
    } else {
      next = next.map((a, i) =>
        i === idx
          ? {
              ...a,
              ...artifact,
              // Preserve the original id if this file was already visible
              // from an older heuristic artifact entry.
              id: a.id || artifact.id,
            }
          : a,
      )
    }
  }
  return next
}

/**
 * Pure function: fold a bridge event into a SessionSlice and return a new
 * slice. Used both for live updates (against the top-level slice) and for
 * cold updates to archived sessions.
 */
function applyEventToSlice(slice: SessionSlice, ev: BridgeEvent): SessionSlice {
  const next: SessionSlice = { ...slice }

  switch (ev.type) {
    case 'turn_start': {
      const msgId = nextId('msg')
      const newMessage: Message = {
        id: msgId,
        role: 'assistant',
        parts: [],
        createdAt: Date.now(),
      }
      next.messages = [...slice.messages, newMessage]
      next.currentStreamingMessageId = msgId
      next.currentTurnId = ev.turnId
      next.isStreaming = true
      next.thinking = ''
      return next
    }

    case 'text_delta': {
      if (!slice.currentStreamingMessageId) return next
      next.messages = slice.messages.map((m) => {
        if (m.id !== slice.currentStreamingMessageId) return m
        const parts = [...m.parts]
        const last = parts[parts.length - 1]
        if (last?.type === 'text') {
          parts[parts.length - 1] = { ...last, text: (last.text ?? '') + ev.text }
        } else {
          parts.push({ type: 'text', text: ev.text })
        }
        return { ...m, parts }
      })
      return next
    }

    case 'thinking_delta': {
      // Also keep the flat string for backwards compat (search, export)
      next.thinking = slice.thinking + ev.thinking
      // Append to the current streaming message as a thinking part
      // so it renders inline, not as a detached block at the bottom.
      if (!slice.currentStreamingMessageId) return next
      next.messages = slice.messages.map((m) => {
        if (m.id !== slice.currentStreamingMessageId) return m
        const parts = [...m.parts]
        const last = parts[parts.length - 1]
        if (last?.type === 'thinking') {
          parts[parts.length - 1] = { ...last, text: (last.text ?? '') + ev.thinking }
        } else {
          parts.push({ type: 'thinking', text: ev.thinking })
        }
        return { ...m, parts }
      })
      return next
    }

    case 'tool_use_start': {
      // Determine groupId: if the last part in the current message is
      // also a tool_call (no text/thinking between them), they're parallel
      // and share the same group. Otherwise start a new group.
      let groupId = `tg_${Date.now().toString(36)}_${ev.id.slice(-4)}`
      if (slice.currentStreamingMessageId) {
        const curMsg = slice.messages.find((m) => m.id === slice.currentStreamingMessageId)
        if (curMsg) {
          const lastPart = curMsg.parts[curMsg.parts.length - 1]
          if (lastPart?.type === 'tool_call' && lastPart.toolCallId) {
            const prevTc = slice.toolCalls[lastPart.toolCallId]
            if (prevTc?.groupId) {
              groupId = prevTc.groupId
            }
          }
        }
      }

      const record: ToolCallRecord = {
        id: ev.id,
        name: ev.name,
        status: 'running',
        startedAt: Date.now(),
        partialJson: '',
        groupId,
      }
      next.toolCalls = { ...slice.toolCalls, [ev.id]: record }
      next.toolCallOrder = [...slice.toolCallOrder, ev.id]
      if (slice.currentStreamingMessageId) {
        next.messages = slice.messages.map((m) =>
          m.id === slice.currentStreamingMessageId
            ? { ...m, parts: [...m.parts, { type: 'tool_call', toolCallId: ev.id } satisfies MessagePart] }
            : m,
        )
      }
      return next
    }

    case 'tool_input_delta': {
      const existing = slice.toolCalls[ev.id]
      if (!existing) return next
      next.toolCalls = {
        ...slice.toolCalls,
        [ev.id]: {
          ...existing,
          partialJson: (existing.partialJson ?? '') + ev.partialJson,
        },
      }
      return next
    }

    case 'tool_input_end': {
      const existing = slice.toolCalls[ev.id]
      if (!existing) return next
      next.toolCalls = {
        ...slice.toolCalls,
        [ev.id]: { ...existing, arguments: ev.arguments, partialJson: undefined },
      }
      return next
    }

    case 'file_change_set': {
      const changeSet = ev.changeSet
      next.fileChanges = [
        ...slice.fileChanges.filter((c) => c.id !== changeSet.id),
        changeSet,
      ]
      const existing = slice.toolCalls[changeSet.toolCallId]
      if (existing) {
        next.toolCalls = {
          ...slice.toolCalls,
          [changeSet.toolCallId]: {
            ...existing,
            fileChangeSet: changeSet,
          },
        }
      }
      next.artifacts = upsertArtifactsFromChangeSet(slice.artifacts, changeSet)
      return next
    }

    case 'tool_result': {
      const existing = slice.toolCalls[ev.id]
      if (!existing) return next
      const resultImages = ev.images?.map((image, index) => {
        const frame = registerFrame({
          pngBase64: image.dataBase64,
          mimeType: image.mimeType || 'image/png',
          width: image.width || 0,
          height: image.height || 0,
          takenAt: Date.now(),
          reason: image.label || `result image ${index + 1}`,
        }, `tool_${ev.id}`)
        retainFrame(frame)
        return { ...frame, label: image.label }
      })
      if (resultImages) {
        for (const image of existing.resultImages ?? []) {
          releaseFrame(image as FrameRef | undefined)
        }
      }
      next.toolCalls = {
        ...slice.toolCalls,
        [ev.id]: {
          ...existing,
          status: ev.isError ? 'error' : 'success',
          result: ev.preview,
          isError: ev.isError,
          durationMs: ev.durationMs,
          ...(resultImages ? { resultImages } : {}),
        },
      }
      // Extract artifact from file-writing tool calls
      if (!ev.isError && FILE_WRITE_TOOLS.has(existing.name)) {
        const args = (existing.arguments ?? {}) as Record<string, unknown>
        const filePath = String(args.path ?? args.file_path ?? args.file ?? '')
        if (filePath) {
          const filename = filePath.split('/').pop() ?? filePath
          const ext = filename.includes('.') ? filename.split('.').pop()?.toLowerCase() ?? '' : ''
          const artifact: import('@shared/events').ArtifactRecord = {
            id: ev.id,
            path: filePath,
            filename,
            creator: 'parent',
            creatorLabel: 'Main agent',
            createdAt: Date.now(),
            fileType: ext,
            operation: existing.name.includes('edit') ? 'edit' : 'write',
          }
          // Don't duplicate if the same path already exists
          const exists = slice.artifacts.some((a) => a.path === filePath)
          if (!exists) {
            next.artifacts = [...slice.artifacts, artifact]
          } else {
            // Update the existing entry's timestamp
            next.artifacts = slice.artifacts.map((a) =>
              a.path === filePath
                ? {
                    ...a,
                    createdAt: Date.now(),
                    operation: a.changeSetId ? a.operation : artifact.operation,
                  }
                : a,
            )
          }
        }
      }
      return next
    }

    case 'bus_message': {
      const rawTimestamp = ev.message.timestamp || Date.now()
      const timestamp = rawTimestamp < 1_000_000_000_000 ? rawTimestamp * 1000 : rawTimestamp
      next.busMessages = [
        ...slice.busMessages.slice(-100),
        { ...ev.message, timestamp },
      ]
      return next
    }

    case 'inbox_event': {
      if (!ev.sessionId) return next
      const m = ev.message
      next.inboxEvents = [
        ...slice.inboxEvents.slice(-99),
        {
          id: m.id,
          action: ev.action,
          fromSession: m.fromSession,
          fromLabel: m.fromLabel,
          fromRole: m.fromRole,
          content: m.content,
          force: !!m.force,
          replyTo: m.replyTo ?? null,
          timestamp: m.timestamp,
          sessionId: ev.sessionId,
        },
      ]
      return next
    }

    case 'system_event': {
      // Generate the system-event id once and reuse it when threading
      // a system part into the message stream — that lets the inline
      // verdict card look up its rich payload by the same id.
      const sysEventId = nextId('sys')
      next.systemEvents = [
        ...slice.systemEvents.slice(-100),
        {
          id: sysEventId,
          subtype: ev.subtype,
          message: ev.message,
          at: Date.now(),
          details: ev.details,
        },
      ]
      // Durable kanban card snapshot. `kanban_*` events carry the
      // full `task` (or `tasks[]`) payload; upsert into the per-
      // card snapshot map so completed cards aren't lost when the
      // 100-entry systemEvents buffer rolls over. Without this,
      // a parent that builds a second batch of cards in the same
      // session pushes the first batch's create events out of the
      // ring buffer, and `collectKanbanCards` rebuilds with the
      // older cards missing.
      if (ev.subtype.startsWith('kanban_')) {
        const details = (ev.details ?? {}) as Record<string, unknown>
        const taskPayload = details.task
        const tasksList = details.tasks
        const existing = slice.kanbanCards ?? {}
        let mergedKanban: Record<string, Record<string, unknown>> | null = null
        if (taskPayload && typeof taskPayload === 'object') {
          const task = taskPayload as Record<string, unknown>
          const id = typeof task.id === 'string' ? task.id : null
          if (id) {
            mergedKanban = { ...existing, [id]: task }
          }
        }
        if (Array.isArray(tasksList) && tasksList.length > 0) {
          const merged = mergedKanban ?? { ...existing }
          for (const entry of tasksList) {
            if (!entry || typeof entry !== 'object') continue
            const task = entry as Record<string, unknown>
            const id = typeof task.id === 'string' ? task.id : null
            if (id) merged[id] = task
          }
          mergedKanban = merged
        }
        if (mergedKanban) {
          next.kanbanCards = mergedKanban
        }
      }
      // Capture the system prompt for session export / training data
      if (ev.subtype === 'system_prompt_set' && ev.details?.systemPrompt) {
        next.systemPrompt = ev.details.systemPrompt as string
      }
      const chatVisible = ev.details?.chatVisible === true
      const inlineSystemSubtypes = [
        'compaction_start',
        'compaction_complete',
        'compaction_skipped',
        'tool_truncation',
        'context_pruning',
        'media_pruning',
      ]
      if (
        slice.currentStreamingMessageId &&
        (chatVisible || inlineSystemSubtypes.includes(ev.subtype))
      ) {
        next.messages = slice.messages.map((m) =>
          m.id === slice.currentStreamingMessageId
            ? {
                ...m,
                parts: [
                  ...m.parts,
                  {
                    type: 'system',
                    text: ev.message,
                    systemSubtype: ev.subtype,
                    eventId: sysEventId,
                  },
                ],
              }
            : m,
        )
      } else if (chatVisible) {
        next.messages = [
          ...slice.messages,
          {
            id: nextId('msg'),
            role: 'assistant',
            parts: [
              {
                type: 'system',
                text: ev.message,
                systemSubtype: ev.subtype,
                eventId: sysEventId,
              },
            ],
            createdAt: Date.now(),
          },
        ]
      }
      return next
    }

    case 'subagent_spawn': {
      next.subagents = { ...slice.subagents, [ev.record.id]: ev.record }
      next.subagentOrder = [...slice.subagentOrder, ev.record.id]
      if (slice.currentStreamingMessageId) {
        next.messages = slice.messages.map((m) =>
          m.id === slice.currentStreamingMessageId
            ? { ...m, parts: [...m.parts, { type: 'subagent', subagentId: ev.record.id }] }
            : m,
        )
      }
      return next
    }

    case 'subagent_update': {
      const existing = slice.subagents[ev.id]
      if (!existing) return next
      next.subagents = { ...slice.subagents, [ev.id]: { ...existing, ...ev.patch } }
      return next
    }

    case 'subagent_done': {
      const existing = slice.subagents[ev.id]
      if (!existing) return next
      next.subagents = {
        ...slice.subagents,
        [ev.id]: { ...existing, state: 'done', result: ev.result, elapsedMs: ev.elapsedMs },
      }
      return next
    }

    case 'usage': {
      const contextTokens = ev.contextTokens ?? ev.inputTokens
      next.usage = {
        ...slice.usage,
        currentContextTokens: contextTokens,
        totalInputTokens: ev.inputTokens,
        totalOutputTokens: ev.outputTokens,
        totalCacheReadTokens: ev.cacheReadTokens,
        totalCacheWriteTokens: ev.cacheWriteTokens,
        totalCost: ev.cost,
        lastTurnInputTokens: contextTokens,
        lastTurnOutputTokens: ev.outputTokens,
      }
      return next
    }

    case 'usage_snapshot': {
      const contextTokens = ev.contextTokens ?? ev.inputTokens
      next.usage = {
        ...slice.usage,
        currentContextTokens: contextTokens,
        totalInputTokens: ev.inputTokens,
        totalOutputTokens: ev.outputTokens,
        totalCacheReadTokens: ev.cacheReadTokens,
        totalCacheWriteTokens: ev.cacheWriteTokens,
        totalCost: ev.cost || slice.usage.totalCost,
      }
      return next
    }

    case 'turn_complete':
      // Only clear streaming state if this is the CURRENT turn. A stale
      // turn_complete from a cancelled turn must not clobber a newer
      // turn that's already streaming — that race causes the UI to go
      // blank while the agent keeps working in the background.
      if (!slice.currentTurnId || ev.turnId === slice.currentTurnId) {
        next.currentStreamingMessageId = null
        next.currentTurnId = null
        next.isStreaming = false
      }
      return next

    default:
      return next
  }
}

export const useHarness = create<HarnessState & HarnessActions>((set, get) => ({
  ...emptyState(),

  handleEvent(ev) {
    set((prev) => {
      // Non-session global events live in their own section.
      if (ev.type === 'ready') {
        const models = (ev.capabilities?.models as ModelChoice[] | undefined) ?? []
        const capModel = (ev.capabilities?.model as string | undefined) ?? prev.model
        const nextReasoning = normalizeReasoningFor(
          capModel,
          prev.reasoningLevel,
          models.length > 0 ? models : prev.availableModels,
        )
        const firstSessionId = ev.sessionId || prev.activeSessionId
        const nextWorkspace = (ev.capabilities?.workspace as string | undefined)
          || prev.sessions.find((s) => s.id === prev.activeSessionId)?.workspace
          || prev.sessions[0]?.workspace
          || ''
        const nextStrategy = normalizeCoordinationStrategy(
          (ev.capabilities?.coordinationStrategy as string | undefined)
          || prev.coordinationStrategy,
        )
        return {
          ...prev,
          ready: true,
          mode: ev.mode,
          modeDetail:
            ev.mode === 'live' ? 'live bridge' : ev.mode === 'demo' ? 'demo mode' : 'error',
          activeSessionId: firstSessionId,
          sessions: prev.sessions.map((s) =>
            s.id === prev.activeSessionId
              ? {
                  ...s,
                  id: firstSessionId,
                  workspace: nextWorkspace,
                  model: capModel,
                  reasoningLevel: nextReasoning,
                  coordinationStrategy: nextStrategy,
                }
              : s,
          ),
          sessionPanes: (prev.sessionPanes.length > 0
            ? prev.sessionPanes
            : [{ id: 'pane-main', sessionId: prev.activeSessionId, createdAt: Date.now() }]
          ).map((pane) =>
            pane.sessionId === prev.activeSessionId
              ? { ...pane, sessionId: firstSessionId }
              : pane,
          ),
          availableModels: models.length > 0 ? models : prev.availableModels,
          model: capModel,
          reasoningLevel: nextReasoning,
          coordinationStrategy: nextStrategy,
          usage: { ...prev.usage, contextWindow: contextWindowFor(capModel) },
        }
      }
      if (ev.type === 'log') {
        return {
          ...prev,
          logs: [...prev.logs.slice(-200), { level: ev.level, message: ev.message, at: Date.now() }],
        }
      }
      if (ev.type === 'error') {
        let messages = prev.messages
        if (prev.currentStreamingMessageId) {
          messages = prev.messages.map((m) =>
            m.id === prev.currentStreamingMessageId
              ? {
                  ...m,
                  parts: [
                    ...m.parts,
                    {
                      type: 'system',
                      text: ev.message,
                      systemSubtype: 'error',
                    } satisfies MessagePart,
                  ],
                }
              : m,
          )
        }
        return {
          ...prev,
          logs: [...prev.logs.slice(-200), { level: 'error', message: ev.message, at: Date.now() }],
          messages,
          currentStreamingMessageId: null,
          currentTurnId: null,
          isStreaming: false,
          toast: {
            id: nextId('toast'),
            message: ev.message,
            tone: 'warn',
            at: Date.now(),
          },
        }
      }
      if (ev.type === 'memory_retrieved' || ev.type === 'memory_updated') {
        return {
          ...prev,
          memories: { ...prev.memories, [ev.memory.id]: ev.memory },
        }
      }
      // Bridge-driven pin toggle (in case future tool surfaces toggle
      // pins without a renderer round-trip). Idempotent with the
      // optimistic update in toggleEntryPin().
      if ((ev as any).type === 'entry_pin_changed') {
        const ordinal = (ev as any).messageOrdinal as number
        const pinned = Boolean((ev as any).pinned)
        if (typeof ordinal !== 'number' || ordinal < 0) return prev
        const userAssistant = prev.messages
        if (ordinal >= userAssistant.length) return prev
        const targetId = userAssistant[ordinal]?.id
        if (!targetId) return prev
        return {
          ...prev,
          messages: prev.messages.map((m) =>
            m.id === targetId ? { ...m, pinned } : m,
          ),
        }
      }
      if (ev.type === 'skill_retrieved' || ev.type === 'skill_loaded' || ev.type === 'skill_pruned') {
        const previous = prev.skills[ev.skill.id]
        const nextStatus =
          ev.type === 'skill_loaded'
            ? 'loaded'
            : ev.type === 'skill_pruned'
              ? 'pruned'
              : previous?.status === 'loaded' || previous?.status === 'pruned'
                ? previous.status
                : 'suggested'
        return {
          ...prev,
          skills: {
            ...prev.skills,
            [ev.skill.id]: { ...previous, ...ev.skill, status: nextStatus },
          },
        }
      }
      if (ev.type === 'skill_updated') {
        const previous = prev.skills[ev.skill.id]
        return {
          ...prev,
          skills: {
            ...prev.skills,
            [ev.skill.id]: {
              ...previous,
              ...ev.skill,
              status: previous?.status ?? ev.skill.status ?? 'available',
            },
          },
        }
      }
      if (ev.type === 'tool_catalog_entry') {
        return {
          ...prev,
          toolCatalog: { ...prev.toolCatalog, [ev.tool.name]: ev.tool },
        }
      }
      if (ev.type === 'file_matches') {
        return {
          ...prev,
          fileMatches: ev.matches,
          fileQuery: ev.query,
        }
      }
      if (ev.type === 'session_spawned') {
        // A sub-agent just came up as its own session. Create a session
        // snapshot, attach it to the parent's childSessionIds, and seed
        // the archive with an empty slice so child events have somewhere
        // to land.
        const newSessionId = ev.sessionId!
        const existing = prev.sessions.find((s) => s.id === newSessionId)
        if (existing) {
          // Resume path: the session already exists from its first spawn.
          // We DON'T create a duplicate; instead refresh status flags so
          // the sidebar's running indicator + the new wokenBy marker
          // pick up the re-engagement.
          if (ev.resumed) {
            const newWokenBy: SessionSnapshot['wokenBy'] =
              ev.wokenBy === 'operator' ? 'rewoken-operator' : 'rewoken-agent'
            return {
              ...prev,
              sessions: prev.sessions.map((s) =>
                s.id === newSessionId
                  ? {
                      ...s,
                      completed: false,
                      success: undefined,
                      completedAt: undefined,
                      wokenBy: newWokenBy,
                      updatedAt: Date.now(),
                    }
                  : s,
              ),
            }
          }
          return prev
        }
        const childModel = ev.model || prev.model
        const childReasoning = normalizeReasoningFor(
          childModel,
          ev.reasoningLevel,
          prev.availableModels,
        )
        const snapshot: SessionSnapshot = {
          id: newSessionId,
          title: ev.title || 'Sub-agent',
          workspace: ev.workspace || prev.sessions[0]?.workspace || '~/',
          model: childModel,
          reasoningLevel: childReasoning,
          createdAt: ev.createdAt || Date.now(),
          updatedAt: Date.now(),
          messageCount: 0,
          totalInputTokens: 0,
          totalOutputTokens: 0,
          cacheReadTokens: 0,
          parentSessionId: ev.parentSessionId,
          task: ev.task,
          agentType: ev.agentType,
          kanbanTaskId: ev.kanbanTaskId,
          taskId: ev.taskId,
          coordinationStrategy: normalizeCoordinationStrategy(
            ev.coordinationStrategy || prev.coordinationStrategy,
          ),
          completed: false,
          // Provenance: resume events carry an explicit wokenBy ("agent"
          // or "operator"); brand-new spawns are always agent-initiated
          // (parent created them); root sessions with no parent are
          // operator-initiated. The sidebar surfaces this as a chip.
          wokenBy: ev.resumed
            ? (ev.wokenBy === 'operator' ? 'rewoken-operator' : 'rewoken-agent')
            : ev.parentSessionId
              ? 'agent'
              : 'operator',
        }
        // If the new session happens to be attached to the currently
        // active session, initialize its slice in the archive so events
        // routed to it stream in. Otherwise also archive it.
        const freshSlice = emptySlice(
          snapshot.model,
          childReasoning,
          prev.availableModels,
          snapshot.coordinationStrategy,
        )
        // Seed the slice with an initial user message from `task` so the
        // conversation pane is non-empty the moment you click into a
        // child session. Also stash the system prompt on the slice so
        // the conversation header can render it. Without this, judge /
        // calibrator child panes look empty until the runner's
        // text_delta events catch up.
        if (ev.task && ev.task.trim()) {
          const userMsgId = `${newSessionId}-seed-user`
          freshSlice.messages = [
            {
              id: userMsgId,
              role: 'user',
              parts: [{ type: 'text', text: ev.task }],
              createdAt: ev.createdAt || Date.now(),
            },
          ]
        }
        if (ev.systemPrompt && ev.systemPrompt.trim()) {
          freshSlice.systemPrompt = ev.systemPrompt
        }
        const archive = {
          ...prev.sessionArchive,
          [newSessionId]: freshSlice,
        }
        // Patch parent's childSessionIds list.
        const updatedSessions = prev.sessions.map((s) =>
          s.id === ev.parentSessionId
            ? {
                ...s,
                childSessionIds: [
                  ...(s.childSessionIds ?? []),
                  newSessionId,
                ],
                updatedAt: Date.now(),
              }
            : s,
        )
        // Insert the new snapshot just below its parent if we can find it.
        const parentIdx = updatedSessions.findIndex(
          (s) => s.id === ev.parentSessionId,
        )
        if (parentIdx >= 0) {
          updatedSessions.splice(parentIdx + 1, 0, snapshot)
        } else {
          updatedSessions.unshift(snapshot)
        }
        return {
          ...prev,
          sessionArchive: archive,
          sessions: updatedSessions,
        }
      }
      if (ev.type === 'session_branched') {
        // Bridge has finished cloning transcript files on disk and
        // hands us back a remap of every old id → new id (parent +
        // every cloned subagent). We mirror the same shape in our
        // in-memory snapshot graph so the new branch is immediately
        // browsable from the sidebar, then defer a switch_session
        // command (queueMicrotask so the reducer stays free of
        // direct side-effects) so the bridge restores the cloned
        // transcript on the renderer's behalf.
        const remap = ev.idRemap || {}
        const now = Date.now()
        const cloned: SessionSnapshot[] = []
        for (const [oldId, newId] of Object.entries(remap)) {
          if (typeof oldId !== 'string' || typeof newId !== 'string') continue
          const orig = prev.sessions.find((s) => s.id === oldId)
          if (!orig) continue
          const remappedParent = orig.parentSessionId
            ? remap[orig.parentSessionId] ?? orig.parentSessionId
            : undefined
          const isParent = oldId === ev.originalSessionId
          cloned.push({
            ...orig,
            id: newId,
            title: isParent ? ev.newName : orig.title,
            parentSessionId: isParent ? undefined : remappedParent,
            childSessionIds: undefined,
            createdAt: now,
            updatedAt: now,
          })
        }
        if (cloned.length === 0) return prev
        // Defer the switch so the reducer remains pure: schedule a
        // microtask that asks the store's own switchSession action
        // to load the branched transcript. switchSession handles
        // archiving the current slice, sending switch_session to the
        // bridge, and waking up transcript_restored events.
        queueMicrotask(() => {
          try {
            void useHarness.getState().switchSession(ev.newSessionId)
          } catch {
            // ignore — switchSession reports its own toast on failure
          }
        })
        return {
          ...prev,
          sessions: [...prev.sessions, ...cloned],
          toast: {
            id: nextId('toast'),
            message: `Branched to "${ev.newName}"`,
            tone: 'ok',
            at: now,
          },
        }
      }
      if (ev.type === 'session_completed') {
        const artifactPath = (ev as any).artifactPath as string | undefined
        const createdFiles = ((ev as any).createdFiles as string[] | undefined) ?? []
        const sub = prev.subagents[ev.sessionId!]
        // If the completed session has an artifact, add to parent's artifacts
        let nextArtifacts = prev.artifacts
        const addCompletedArtifact = (path: string, operation: string) => {
          if (!path || !sub || nextArtifacts.some((a) => a.path === path)) return
          const filename = path.split('/').pop() ?? path
          const ext = filename.includes('.') ? filename.split('.').pop()?.toLowerCase() ?? '' : ''
          nextArtifacts = [
            ...nextArtifacts,
            {
              id: `${ev.sessionId!}:${path}`,
              path,
              filename,
              creator: ev.sessionId!,
              creatorLabel: sub.label ?? 'Subagent',
              createdAt: Date.now(),
              fileType: ext,
              operation,
            },
          ]
        }
        if (artifactPath) {
          addCompletedArtifact(artifactPath, 'subagent_artifact')
        }
        for (const path of createdFiles) {
          addCompletedArtifact(path, path === artifactPath ? 'subagent_artifact' : 'write')
        }
        // Also scan the child's archived slice for write_file calls
        const childSlice = prev.sessionArchive[ev.sessionId!]
        if (childSlice && sub) {
          for (const changeSet of childSlice.fileChanges) {
            nextArtifacts = upsertArtifactsFromChangeSet(
              nextArtifacts,
              changeSet,
              ev.sessionId!,
              sub.label ?? 'Subagent',
            )
          }
          const childTcs = Object.values(childSlice.toolCalls)
          for (const tc of childTcs) {
            if (FILE_WRITE_TOOLS.has(tc.name) && !tc.isError && tc.arguments) {
              const args = tc.arguments as Record<string, unknown>
              const fp = String(args.path ?? args.file_path ?? args.file ?? '')
              if (fp && !nextArtifacts.some((a) => a.path === fp)) {
                const fn = fp.split('/').pop() ?? fp
                const ext2 = fn.includes('.') ? fn.split('.').pop()?.toLowerCase() ?? '' : ''
                nextArtifacts = [
                  ...nextArtifacts,
                  {
                    id: tc.id,
                    path: fp,
                    filename: fn,
                    creator: ev.sessionId!,
                    creatorLabel: sub.label ?? 'Subagent',
                    createdAt: Date.now(),
                    fileType: ext2,
                    operation: 'write',
                  },
                ]
              }
            }
          }
        }
        return {
          ...prev,
          artifacts: nextArtifacts,
          sessions: prev.sessions.map((s) =>
            s.id === ev.sessionId
              ? {
                  ...s,
                  completed: true,
                  completedAt: Date.now(),
                  success: ev.success,
                  totalInputTokens: ev.inputTokens ?? s.totalInputTokens,
                  totalOutputTokens: ev.outputTokens ?? s.totalOutputTokens,
                }
              : s,
          ),
        }
      }
      // ─── Computer-use events ──────────────────────────────────
      if (ev.type === 'computer_session_start') {
        const sessionId = ev.sessionId!
        releaseFrame(prev.computerSessions[sessionId]?.latestFrame)
        const next: ComputerSessionState = {
          sessionId,
          parentSessionId: ev.parentSessionId,
          goal: ev.goal,
          targetApp: ev.targetApp,
          status: 'running',
          history: [],
          frameCount: 0,
        }
        return {
          ...prev,
          computerSessions: { ...prev.computerSessions, [sessionId]: next },
          computerActive: true,
        }
      }
      if (ev.type === 'screenshot_frame') {
        const sessionId = ev.sessionId!
        const existing =
          prev.computerSessions[sessionId] ?? {
            sessionId,
            goal: '',
            status: 'running' as const,
            history: [],
            frameCount: 0,
          }
        const frame = registerFrame({
          pngBase64: ev.pngBase64,
          mimeType: ev.mimeType ?? 'image/png',
          width: ev.width,
          height: ev.height,
          takenAt: ev.takenAt,
          reason: ev.reason,
        }, sessionId)
        // Find the currently-running tool call on THIS session (either
        // the active slice or an archived one) and attach the frame to
        // its record so the conversation chip can render it inline.
        const isActiveSession = sessionId === prev.activeSessionId
        const attachFrameToLatestTool = (
          toolCalls: Record<string, ToolCallRecord>,
          toolCallOrder: string[],
        ): Record<string, ToolCallRecord> => {
          // Find the most recent running tool call. Fall back to the
          // most recent overall if none are running (the tool may
          // already have returned by the time the frame lands due to
          // event ordering across the wire).
          let targetId: string | undefined
          for (let i = toolCallOrder.length - 1; i >= 0; i--) {
            const tc = toolCalls[toolCallOrder[i]]
            if (tc?.status === 'running') {
              targetId = tc.id
              break
            }
          }
          if (!targetId && toolCallOrder.length > 0) {
            targetId = toolCallOrder[toolCallOrder.length - 1]
          }
          if (!targetId) return toolCalls
          const existingCall = toolCalls[targetId]
          if (!existingCall) return toolCalls
          releaseFrame(existingCall.frame as FrameRef | undefined)
          retainFrame(frame)
          return {
            ...toolCalls,
            [targetId]: { ...existingCall, frame },
          }
        }
        if (isActiveSession) {
          releaseFrame(existing.latestFrame)
          retainFrame(frame)
          return {
            ...prev,
            toolCalls: attachFrameToLatestTool(prev.toolCalls, prev.toolCallOrder),
            computerSessions: {
              ...prev.computerSessions,
              [sessionId]: {
                ...existing,
                frameCount: existing.frameCount + 1,
                latestFrame: frame,
              },
            },
          }
        }
        // Non-active session: also poke the archived slice so when the
        // user switches to it later, the frames are visible inline.
        const archivedSlice = prev.sessionArchive[sessionId]
        const nextArchive = archivedSlice
          ? {
              ...prev.sessionArchive,
              [sessionId]: {
                ...archivedSlice,
                toolCalls: attachFrameToLatestTool(
                  archivedSlice.toolCalls,
                  archivedSlice.toolCallOrder,
                ),
              },
            }
          : prev.sessionArchive
        releaseFrame(existing.latestFrame)
        retainFrame(frame)
        return {
          ...prev,
          sessionArchive: nextArchive,
          computerSessions: {
            ...prev.computerSessions,
            [sessionId]: {
              ...existing,
              frameCount: existing.frameCount + 1,
              latestFrame: frame,
            },
          },
        }
      }
      if (ev.type === 'action_planned') {
        const sessionId = ev.sessionId!
        const existing = prev.computerSessions[sessionId]
        if (!existing) return prev
        return {
          ...prev,
          computerSessions: {
            ...prev.computerSessions,
            [sessionId]: {
              ...existing,
              plannedAction: {
                action: ev.action,
                description: ev.description,
                x: ev.x,
                y: ev.y,
                w: ev.w,
                h: ev.h,
                plannedAt: Date.now(),
              },
            },
          },
        }
      }
      if (ev.type === 'action_executed') {
        const sessionId = ev.sessionId!
        const existing = prev.computerSessions[sessionId]
        if (!existing) return prev
        return {
          ...prev,
          computerSessions: {
            ...prev.computerSessions,
            [sessionId]: {
              ...existing,
              plannedAction: undefined,
              history: [
                ...existing.history,
                {
                  action: ev.action,
                  success: ev.success,
                  durationMs: ev.durationMs,
                  at: Date.now(),
                },
              ].slice(-50),
            },
          },
        }
      }
      if (ev.type === 'computer_session_end') {
        const sessionId = ev.sessionId!
        const existing = prev.computerSessions[sessionId]
        if (!existing) return prev
        const next = {
          ...existing,
          status:
            ev.outcome === 'done'
              ? ('done' as const)
              : ev.outcome === 'cancelled'
                ? ('cancelled' as const)
                : ('failed' as const),
          summary: ev.summary,
        }
        const updatedMap = { ...prev.computerSessions, [sessionId]: next }
        const stillActive = Object.values(updatedMap).some(
          (s) => s.status === 'running',
        )
        return {
          ...prev,
          computerSessions: updatedMap,
          computerActive: stillActive,
        }
      }
      if (ev.type === 'emergency_stop') {
        // Flip every running session to cancelled. The bridge will
        // then emit computer_session_end events as the sub-agents
        // actually unwind.
        const nextMap: Record<string, ComputerSessionState> = {}
        for (const [id, s] of Object.entries(prev.computerSessions)) {
          nextMap[id] =
            s.status === 'running' ? { ...s, status: 'cancelled' } : s
        }
        return {
          ...prev,
          computerSessions: nextMap,
          computerActive: false,
          toast: {
            id: nextId('toast'),
            message: `Emergency stop (${ev.stopped ?? 0} tasks)`,
            tone: 'warn',
            at: Date.now(),
          },
        }
      }
      if (ev.type === 'permission_request') {
        return {
          ...prev,
          permissionQueue: [
            {
              requestId: ev.requestId,
              sessionId: ev.sessionId,
              level: ev.level,
              prompt: ev.prompt,
              reason: ev.reason,
              details: ev.details,
            },
            ...prev.permissionQueue.filter((p) => p.requestId !== ev.requestId),
          ],
        }
      }
      if (ev.type === 'subagent_event') {
        // Ignored at the session level for now; future: render in subagent
        // detail modal.
        return prev
      }
      if (ev.type === 'message_stop') return prev

      // Session-scoped events: route to the right slice.
      const sessionId = (ev as any).sessionId as string | undefined
      const isActive = !sessionId || sessionId === prev.activeSessionId

      if (isActive) {
        const nextSlice = applyEventToSlice(sliceFromState(prev), ev)
        // Update the session snapshot's summary numbers when usage lands.
        let sessions = prev.sessions
        if (ev.type === 'usage' || ev.type === 'usage_snapshot') {
          sessions = prev.sessions.map((s) =>
            s.id === prev.activeSessionId
              ? {
                  ...s,
                  updatedAt: Date.now(),
                  totalInputTokens: nextSlice.usage.totalInputTokens,
                  totalOutputTokens: nextSlice.usage.totalOutputTokens,
                  cacheReadTokens: nextSlice.usage.totalCacheReadTokens,
                  totalCost: nextSlice.usage.totalCost,
                  messageCount: nextSlice.messages.length,
                }
              : s,
          )
        } else if (ev.type === 'turn_complete') {
          sessions = prev.sessions.map((s) =>
            s.id === prev.activeSessionId
              ? {
                  ...s,
                  updatedAt: Date.now(),
                  messageCount: nextSlice.messages.length,
                }
              : s,
          )
        }
        return { ...prev, ...nextSlice, sessions }
      }

      // Non-active: update or create the archived slice, and also
      // bump the session row's messageCount / updatedAt so the
      // sidebar list reflects background sub-agent activity AND so
      // targeted persistence has accurate metadata for the row.
      // Without this the swarm panel rows stayed at "empty" and were
      // dropped from persistence entirely.
      const sessionSnapshot = prev.sessions.find((s) => s.id === sessionId)
      const existingArchive =
        prev.sessionArchive[sessionId!] ??
        emptySlice(
          sessionSnapshot?.model || prev.model,
          sessionSnapshot?.reasoningLevel || prev.reasoningLevel,
          prev.availableModels,
          normalizeCoordinationStrategy(
            sessionSnapshot?.coordinationStrategy || prev.coordinationStrategy,
          ),
        )
      const updated = applyEventToSlice(existingArchive, ev)
      const updatedSessions = prev.sessions.map((s) =>
        s.id === sessionId
          ? {
              ...s,
              messageCount: updated.messages.length,
              updatedAt: Date.now(),
              totalInputTokens: updated.usage.totalInputTokens,
              totalOutputTokens: updated.usage.totalOutputTokens,
              cacheReadTokens: updated.usage.totalCacheReadTokens,
              totalCost: updated.usage.totalCost,
            }
          : s,
      )
      return {
        ...prev,
        sessions: updatedSessions,
        sessionArchive: { ...prev.sessionArchive, [sessionId!]: updated },
      }
    })
  },

  setInputDraft(v) {
    set({ inputDraft: v })
  },

  async sendMessage(content) {
    const state = useHarness.getState()
    if (!content.trim() && state.pendingAttachments.length === 0) return
    const id = nextId('msg')
    const parts: MessagePart[] = []
    if (content.trim()) parts.push({ type: 'text', text: content })
    const attachments = state.pendingAttachments
    set((prev) => ({
      messages: [
        ...prev.messages,
        {
          id,
          role: 'user',
          parts,
          createdAt: Date.now(),
          attachments: attachments.length > 0
            ? attachments.map((a) => ({ id: a.id, type: a.type, previewUrl: a.previewUrl }))
            : undefined,
        } as Message,
      ],
      inputDraft: '',
      pendingAttachments: [],
    }))
    const api = (window as any).harness
    const cmd: any = {
      type: 'send_message',
      sessionId: state.activeSessionId,
      content,
      // Always include the model so the bridge uses the correct
      // provider — especially important for resumed sessions where
      // the bridge may have been restarted since the session was last
      // active and doesn't remember the model choice.
      model: state.model,
      reasoningLevel: state.reasoningLevel,
      coordinationStrategy: state.coordinationStrategy,
    }
    const activeSnapshot = state.sessions.find((s) => s.id === state.activeSessionId)
    if (activeSnapshot?.parentSessionId && state.messages.length > 0) {
      const contextSummary = extractConversationSummary(state.messages, state.toolCalls)
      if (contextSummary) {
        cmd.contextSummary = contextSummary
      }
    }
    if (attachments.length > 0) {
      cmd.attachments = attachments.map((a) => ({
        type: a.type,
        mimeType: a.mimeType,
        dataBase64: a.dataBase64,
      }))
    }
    if (api) {
      await api.sendCommand(cmd)
    } else {
      ;(window as any).__harnessDemo?.send(content)
    }
  },

  async operatorTalk(sessionId, content, force = false) {
    const text = (content ?? '').trim()
    if (!sessionId || !text) return
    const api = (window as any).harness
    if (!api) return
    await api.sendCommand({
      type: 'operator_talk',
      sessionId,
      content: text,
      force: !!force,
    })
  },

  async cancelTurn() {
    // Mark ALL in-flight state as cancelled so spinners stop, tool
    // chips show error, sub-agents show cancelled. We do NOT flip
    // `isStreaming` to false so the cancel button stays visible as
    // a recovery path. The real `turn_complete` from the bridge
    // handles that.
    set((prev) => {
      // Stop any spinning tool calls
      const nextToolCalls = { ...prev.toolCalls }
      for (const [id, tc] of Object.entries(prev.toolCalls)) {
        if (tc.status === 'running') {
          nextToolCalls[id] = {
            ...tc,
            status: 'error',
            result: 'Cancelled',
            isError: true,
            durationMs: tc.durationMs ?? Math.round(Date.now() - tc.startedAt),
          }
        }
      }
      // Stop any running sub-agents
      const nextSubs = { ...prev.subagents }
      for (const [id, rec] of Object.entries(prev.subagents)) {
        if (rec.state === 'running' || rec.state === 'pending') {
          nextSubs[id] = { ...rec, state: 'cancelled' }
        }
      }
      // Stop computer sessions
      const nextComp: Record<string, ComputerSessionState> = {}
      for (const [id, s] of Object.entries(prev.computerSessions)) {
        nextComp[id] = s.status === 'running' ? { ...s, status: 'cancelled' } : s
      }
      return {
        ...prev,
        toolCalls: nextToolCalls,
        subagents: nextSubs,
        computerSessions: nextComp,
        computerActive: false,
      }
    })
    const api = (window as any).harness
    if (api)
      await api.sendCommand({
        type: 'force_cancel',
        sessionId: useHarness.getState().activeSessionId,
      })
  },

  async setModel(model, reasoningLevel) {
    const current = useHarness.getState()
    const nextReasoning = normalizeReasoningFor(
      model,
      reasoningLevel ?? (model === current.model ? current.reasoningLevel : undefined),
      current.availableModels,
    )
    set((prev) => ({
      model,
      reasoningLevel: nextReasoning,
      usage: { ...prev.usage, contextWindow: contextWindowFor(model) },
      sessions: prev.sessions.map((s) =>
        s.id === prev.activeSessionId
          ? { ...s, model, reasoningLevel: nextReasoning }
          : s,
      ),
    }))
    const api = (window as any).harness
    if (api)
      await api.sendCommand({
        type: 'set_model',
        sessionId: useHarness.getState().activeSessionId,
        model,
        reasoningLevel: nextReasoning,
        coordinationStrategy: useHarness.getState().coordinationStrategy,
      })
  },

  async setReasoningLevel(reasoningLevel) {
    const state = useHarness.getState()
    await state.setModel(state.model, reasoningLevel)
  },

  async setCoordinationStrategy(strategy) {
    const normalized = normalizeCoordinationStrategy(strategy)
    const current = useHarness.getState()
    set((prev) => ({
      coordinationStrategy: normalized,
      sessions: prev.sessions.map((s) =>
        s.id === prev.activeSessionId
          ? { ...s, coordinationStrategy: normalized, updatedAt: Date.now() }
          : s,
      ),
    }))
    const api = (window as any).harness
    if (api) {
      await api.sendCommand({
        type: 'set_coordination_strategy',
        sessionId: current.activeSessionId,
        coordinationStrategy: normalized,
        model: current.model,
        reasoningLevel: current.reasoningLevel,
      })
    }
  },

  openSubagent(id) {
    set({ activeSubagentId: id })
  },

  toggleCommandPalette(open) {
    set((prev) => ({ commandPaletteOpen: open ?? !prev.commandPaletteOpen }))
  },

  toggleMissionDashboard(open, tab) {
    set((prev) => ({
      missionDashboardOpen: open ?? !prev.missionDashboardOpen,
      missionDashboardTab: tab ?? prev.missionDashboardTab,
    }))
  },

  toggleMetricsDashboard(open) {
    set((prev) => ({ metricsDashboardOpen: open ?? !prev.metricsDashboardOpen }))
  },

  toggleModelPicker(open) {
    set((prev) => ({ modelPickerOpen: open ?? !prev.modelPickerOpen }))
  },

  setFocusedPanel(p) {
    set({ focusedPanel: p })
  },

  async requestDemoBurst() {
    const api = (window as any).harness
    if (api) await api.requestDemoBurst()
  },

  async newSession(model, reasoningLevel) {
    const newSessionId = `session-${Date.now().toString(36)}`
    const state = useHarness.getState()
    const chosenModel = model || state.model
    const chosenReasoning = normalizeReasoningFor(
      chosenModel,
      reasoningLevel ?? (model ? undefined : state.reasoningLevel),
      state.availableModels,
    )
    const chosenStrategy = normalizeCoordinationStrategy(state.coordinationStrategy)

    set((prev) => {
      // Archive the current slice unless it's empty (nothing to save).
      const currentSlice = sliceFromState(prev)
      const hasContent =
        currentSlice.messages.length > 0 || currentSlice.isStreaming
      const archive = hasContent
        ? { ...prev.sessionArchive, [prev.activeSessionId]: currentSlice }
        : prev.sessionArchive

      const freshSlice = emptySlice(
        chosenModel,
        chosenReasoning,
        prev.availableModels,
        chosenStrategy,
      )
      const existingPanes = prev.sessionPanes.length > 0
        ? prev.sessionPanes
        : [{ id: 'pane-main', sessionId: prev.activeSessionId, createdAt: Date.now() }]
      const activePaneId = prev.activePaneId || existingPanes[0]?.id || 'pane-main'
      const sessionPanes = existingPanes.map((pane) =>
        pane.id === activePaneId ? { ...pane, sessionId: newSessionId } : pane,
      )
      return {
        ...prev,
        ...freshSlice,
        activeSessionId: newSessionId,
        sessionArchive: archive,
        sessionPanes,
        activePaneId,
        sessions: [
          {
            id: newSessionId,
            title: 'New session',
            workspace: prev.sessions[0]?.workspace ?? '~/',
            model: chosenModel,
            reasoningLevel: chosenReasoning,
            coordinationStrategy: chosenStrategy,
            createdAt: Date.now(),
            updatedAt: Date.now(),
            messageCount: 0,
            totalInputTokens: 0,
            totalOutputTokens: 0,
            cacheReadTokens: 0,
          },
          ...prev.sessions.filter((s) => s.id !== prev.activeSessionId || hasContent),
        ],
        pendingAttachments: [],
        toast: {
          id: nextId('toast'),
          message: `New session (${chosenStrategy} · ${chosenModel.replace('claude-', '')})`,
          tone: 'info',
          at: Date.now(),
        },
      }
    })

    const api = (window as any).harness
    if (api)
      await api.sendCommand({
        type: 'new_session',
        sessionId: newSessionId,
        model: chosenModel,
        reasoningLevel: chosenReasoning,
        coordinationStrategy: chosenStrategy,
      })
    else (window as any).__harnessDemo?.stop?.()
  },

  async switchSession(sessionId) {
    const prev = useHarness.getState()
    if (sessionId === prev.activeSessionId) return
    let archivedSlice = prev.sessionArchive[sessionId]
    if (!archivedSlice) {
      // Try to load from disk.
      const loaded = await prev.loadPersistedSessionIntoArchive(sessionId)
      if (loaded) {
        archivedSlice = useHarness.getState().sessionArchive[sessionId]
      }
    }
    if (!archivedSlice) {
      prev.showToast('Session not found', 'warn')
      return
    }
    const targetSnapshot = prev.sessions.find((s) => s.id === sessionId)
    archivedSlice = {
      ...archivedSlice,
      reasoningLevel: normalizeReasoningFor(
        archivedSlice.model,
        archivedSlice.reasoningLevel || targetSnapshot?.reasoningLevel,
        prev.availableModels,
      ),
      coordinationStrategy: normalizeCoordinationStrategy(
        archivedSlice.coordinationStrategy || targetSnapshot?.coordinationStrategy,
      ),
    }
    set((p) => {
      // Archive the current slice before swapping in the target.
      const currentSlice = sliceFromState(p)
      const newArchive = { ...p.sessionArchive, [p.activeSessionId]: currentSlice }
      // Remove the target from archive since it's now the active one.
      delete newArchive[sessionId]
      const existingPanes = p.sessionPanes.length > 0
        ? p.sessionPanes
        : [{ id: 'pane-main', sessionId: p.activeSessionId, createdAt: Date.now() }]
      const activePaneId = p.activePaneId || existingPanes[0]?.id || 'pane-main'
      const sessionPanes = existingPanes.map((pane) =>
        pane.id === activePaneId ? { ...pane, sessionId } : pane,
      )
      return {
        ...p,
        ...archivedSlice,
        activeSessionId: sessionId,
        sessionArchive: newArchive,
        sessionPanes,
        activePaneId,
        pendingAttachments: [],
      }
    })
    // Route through showToast so it gets the standard 2.6s auto-dismiss
    // timer instead of sticking around until the user clicks it.
    useHarness.getState().showToast('Switched to session', 'info')

    // Tell the bridge which model this session was using so it creates
    // (or reconfigures) the _BridgeSession with the right provider.
    // Without this the bridge defaults to claude-sonnet-4-6 for every
    // resumed session, even if the user had picked gpt-5.4 before.
    const restoredModel = useHarness.getState().model
    const restoredReasoning = useHarness.getState().reasoningLevel
    const restoredStrategy = useHarness.getState().coordinationStrategy
    const sessionSnapshot = useHarness
      .getState()
      .sessions.find((s) => s.id === sessionId)
    const modelForBridge = sessionSnapshot?.model || restoredModel
    const reasoningForBridge = normalizeReasoningFor(
      modelForBridge,
      sessionSnapshot?.reasoningLevel || restoredReasoning,
      useHarness.getState().availableModels,
    )
    const strategyForBridge = normalizeCoordinationStrategy(
      sessionSnapshot?.coordinationStrategy || restoredStrategy,
    )

    const api = (window as any).harness
    if (api) {
      await api.sendCommand({
        type: 'switch_session',
        sessionId,
        model: modelForBridge,
        reasoningLevel: reasoningForBridge,
        coordinationStrategy: strategyForBridge,
      })
    }
  },

  async openSessionPane(sessionId, mode = 'replace') {
    const state = useHarness.getState()
    const snapshot = state.sessions.find((session) => session.id === sessionId)
    if (!snapshot) {
      state.showToast('Session not found', 'warn')
      return
    }

    if (mode === 'replace') {
      await state.switchSession(sessionId)
      return
    }

    if (sessionId !== state.activeSessionId && !state.sessionArchive[sessionId]) {
      await state.loadPersistedSessionIntoArchive(sessionId)
    }

    set((prev) => {
      const existing = prev.sessionPanes.find((pane) => pane.sessionId === sessionId)
      if (existing) {
        return { activePaneId: existing.id }
      }
      const id = nextId('pane')
      return {
        sessionPanes: [
          ...prev.sessionPanes,
          { id, sessionId, createdAt: Date.now() },
        ],
        activePaneId: id,
      }
    })
    useHarness.getState().showToast('Opened split pane', 'info')
  },

  async closeSessionPane(paneId) {
    const state = useHarness.getState()
    const panes = state.sessionPanes.length > 0
      ? state.sessionPanes
      : [{ id: 'pane-main', sessionId: state.activeSessionId, createdAt: Date.now() }]
    if (panes.length <= 1) {
      state.showToast('Keep at least one session pane open', 'warn')
      return
    }
    const closing = panes.find((pane) => pane.id === paneId)
    if (!closing) return
    const remaining = panes.filter((pane) => pane.id !== paneId)
    const nextActivePaneId =
      state.activePaneId === paneId
        ? remaining[remaining.length - 1]?.id
        : state.activePaneId
    set({
      sessionPanes: remaining,
      activePaneId: nextActivePaneId || remaining[0].id,
    })
    if (closing.sessionId === state.activeSessionId) {
      const replacement = remaining.find((pane) => pane.id === nextActivePaneId) ?? remaining[0]
      if (replacement) {
        await useHarness.getState().switchSession(replacement.sessionId)
      }
    }
  },

  setActiveSessionPane(paneId) {
    set((prev) => {
      if (!prev.sessionPanes.some((pane) => pane.id === paneId)) return {}
      return { activePaneId: paneId }
    })
  },

  async attachImage(file) {
    try {
      const sourceMime = (file as any).type || 'image/png'
      // Anthropic caps image base64 at 5 MiB. Aim for <= 3.5 MiB raw so
      // base64 (~33% inflation) stays well under the limit. Downscale +
      // re-encode as JPEG only when the original is over budget — small
      // images keep their native format/quality.
      const RAW_TARGET = 3_500_000
      let bytes: Uint8Array = new Uint8Array(await file.arrayBuffer())
      let mimeType = sourceMime
      if (bytes.length > RAW_TARGET && sourceMime.startsWith('image/')) {
        const downscaled = await downscaleImageForLLM(file, RAW_TARGET)
        if (downscaled) {
          bytes = downscaled.bytes
          mimeType = downscaled.mimeType
        }
      }
      let binary = ''
      for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i])
      const dataBase64 = btoa(binary)
      const previewUrl = `data:${mimeType};base64,${dataBase64}`
      set((prev) => ({
        pendingAttachments: [
          ...prev.pendingAttachments,
          {
            id: nextId('att'),
            type: 'image',
            mimeType,
            dataBase64,
            previewUrl,
          },
        ],
      }))
    } catch (err) {
      useHarness.getState().showToast('Failed to read image', 'warn')
    }
  },

  removeAttachment(id) {
    set((prev) => ({
      pendingAttachments: prev.pendingAttachments.filter((a) => a.id !== id),
    }))
  },

  async editUserMessage(messageId, content) {
    const trimmed = content.trim()
    if (!trimmed) return
    const api = (window as any).harness
    const state = useHarness.getState()
    const sessionId = state.activeSessionId
    if (!api || !sessionId) return
    const ordinal = messageOrdinalById(state.messages, messageId)
    if (ordinal < 0) return
    // Local truncate: drop everything from the edited message onward and
    // re-insert a new user message with the new content. Streaming
    // response will append a fresh assistant turn.
    set((prev) => {
      const idx = prev.messages.findIndex((m) => m.id === messageId)
      if (idx < 0) return {}
      const before = prev.messages.slice(0, idx)
      const newId = nextId('msg')
      const newMessage: Message = {
        id: newId,
        role: 'user',
        parts: [{ type: 'text', text: trimmed }],
        createdAt: Date.now(),
      }
      return {
        messages: [...before, newMessage],
        currentStreamingMessageId: null,
        currentTurnId: null,
        thinking: '',
        isStreaming: false,
      }
    })
    await api.sendCommand({
      type: 'edit_user_message',
      sessionId,
      messageOrdinal: ordinal,
      content: trimmed,
    })
  },

  async rerunUserMessage(messageId) {
    const api = (window as any).harness
    const state = useHarness.getState()
    const sessionId = state.activeSessionId
    if (!api || !sessionId) return
    const ordinal = messageOrdinalById(state.messages, messageId)
    if (ordinal < 0) return
    const target = state.messages.find((m) => m.id === messageId)
    if (!target || target.role !== 'user') return
    // Drop the user message + everything after locally; the bridge
    // will re-add the user message via a fresh turn so it reappears
    // in stream order.
    set((prev) => {
      const idx = prev.messages.findIndex((m) => m.id === messageId)
      if (idx < 0) return {}
      return {
        messages: prev.messages.slice(0, idx),
        currentStreamingMessageId: null,
        currentTurnId: null,
        thinking: '',
        isStreaming: false,
      }
    })
    await api.sendCommand({
      type: 'rerun_user_message',
      sessionId,
      messageOrdinal: ordinal,
    })
  },

  async deleteMessagesFrom(messageId) {
    const api = (window as any).harness
    const state = useHarness.getState()
    const sessionId = state.activeSessionId
    if (!api || !sessionId) return
    const ordinal = messageOrdinalById(state.messages, messageId)
    if (ordinal < 0) return
    set((prev) => {
      const idx = prev.messages.findIndex((m) => m.id === messageId)
      if (idx < 0) return {}
      return {
        messages: prev.messages.slice(0, idx),
        currentStreamingMessageId: null,
        currentTurnId: null,
        thinking: '',
        isStreaming: false,
      }
    })
    await api.sendCommand({
      type: 'delete_messages_from',
      sessionId,
      messageOrdinal: ordinal,
    })
  },

  async toggleEntryPin(messageId, pinned) {
    const api = (window as any).harness
    const state = useHarness.getState()
    const sessionId = state.activeSessionId
    if (!api || !sessionId) return
    const ordinal = messageOrdinalById(state.messages, messageId)
    if (ordinal < 0) return
    // Optimistic update — bridge confirms via entry_pin_changed event.
    set((prev) => ({
      messages: prev.messages.map((m) =>
        m.id === messageId ? { ...m, pinned } : m,
      ),
    }))
    await api.sendCommand({
      type: 'toggle_entry_pin',
      sessionId,
      messageOrdinal: ordinal,
      pinned,
    })
  },

  async branchSessionFrom(messageId, newName) {
    const api = (window as any).harness
    const state = useHarness.getState()
    const sessionId = state.activeSessionId
    if (!api || !sessionId) return
    const ordinal = messageOrdinalById(state.messages, messageId)
    if (ordinal < 0) return
    // Walk every descendant of the active session in the in-memory
    // snapshot graph so the bridge can clone every subagent transcript
    // on disk in one shot. The bridge doesn't track parent-child
    // links itself — that graph lives in the renderer's metadata.
    const childIds = collectDescendantSessionIds(state.sessions, sessionId)
    await api.sendCommand({
      type: 'branch_session',
      sessionId,
      messageOrdinal: ordinal,
      newName,
      childSessionIds: childIds,
    })
  },

  async requestFileMatches(query) {
    const api = (window as any).harness
    if (!api) return
    await api.sendCommand({
      type: 'list_files',
      sessionId: useHarness.getState().activeSessionId,
      query,
      limit: 40,
    })
  },

  async hydrateFromDisk() {
    const api = (window as any).harness
    if (!api?.sessionList) return
    const res = await api.sessionList()
    if (!res?.ok || !Array.isArray(res.sessions) || res.sessions.length === 0) return
    set((prev) => {
      // Build a unified sessions list (persisted + current), de-duped by id.
      // Critically: restore parentSessionId / completed / childSessionIds
      // etc. so the "swarm" panel can reconstruct the subagent tree
      // after an app restart. Previously we dropped everything except
      // the basic display metadata, which made the post-restart swarm
      // panel permanently empty and un-clickable.
      const persistedSnapshots: SessionSnapshot[] = res.sessions.map((s: any) => ({
        id: s.id,
        title: s.title || 'Session',
        workspace: s.workspace || prev.sessions[0]?.workspace || '~/',
        model: s.model || 'claude-sonnet-4-6',
        reasoningLevel: normalizeReasoningFor(
          s.model || 'claude-sonnet-4-6',
          s.reasoningLevel,
          prev.availableModels,
        ),
        coordinationStrategy: normalizeCoordinationStrategy(s.coordinationStrategy),
        createdAt: s.createdAt,
        updatedAt: s.updatedAt,
        messageCount: s.messageCount ?? 0,
        totalInputTokens: s.totalInputTokens ?? 0,
        totalOutputTokens: s.totalOutputTokens ?? 0,
        cacheReadTokens: s.cacheReadTokens ?? 0,
        parentSessionId: s.parentSessionId,
        childSessionIds: s.childSessionIds,
        task: s.task,
        agentType: s.agentType,
        completed: s.completed,
        completedAt: s.completedAt,
        success: s.success,
      }))
      const existingIds = new Set(prev.sessions.map((s) => s.id))
      const merged: SessionSnapshot[] = [...prev.sessions]
      for (const s of persistedSnapshots) {
        if (!existingIds.has(s.id)) merged.push(s)
      }
      // Sort by updatedAt desc, but keep the active session at the top.
      const active = merged.find((s) => s.id === prev.activeSessionId)
      const rest = merged
        .filter((s) => s.id !== prev.activeSessionId)
        .sort((a, b) => b.updatedAt - a.updatedAt)
      return {
        ...prev,
        sessions: active ? [active, ...rest] : rest,
      }
    })
  },

  async persistSession(sessionId) {
    const state = useHarness.getState()
    const api = (window as any).harness
    if (!api?.sessionSave) return
    const session = state.sessions.find((s) => s.id === sessionId)
    if (!session) return
    const slice = sliceForSession(state, sessionId)
    if (!slice || !hasPersistableSessionState(session, slice)) return
    const startedAt = performance.now()
    const res = await api.sessionSave(persistedPayloadFromSession(session, slice))
    const durationMs = performance.now() - startedAt
    if (!res?.ok) {
      throw new Error(res?.error ?? 'session save failed')
    }
    if (durationMs > 100) {
      console.debug(
        `[persistence] renderer save request for ${sessionId} took ${Math.round(durationMs)}ms`,
      )
    }
  },

  async persistSessionIndex() {
    const state = useHarness.getState()
    const api = (window as any).harness
    if (!api?.sessionIndexSave) return
    const rows = state.sessions
      .map((session) => {
        const slice = sliceForSession(state, session.id)
        if (!hasPersistableSessionState(session, slice)) return null
        return persistedMetaFromSession(session, slice)
      })
      .filter(Boolean)
    const startedAt = performance.now()
    const res = await api.sessionIndexSave(rows)
    const durationMs = performance.now() - startedAt
    if (!res?.ok) {
      throw new Error(res?.error ?? 'session index save failed')
    }
    if (durationMs > 100) {
      console.debug(
        `[persistence] renderer index save request took ${Math.round(durationMs)}ms`,
      )
    }
  },

  async persistActiveSession() {
    await useHarness.getState().persistSession(useHarness.getState().activeSessionId)
  },

  async persistAllSessions() {
    const state = useHarness.getState()
    for (const session of state.sessions) {
      await useHarness.getState().persistSession(session.id)
    }
    await useHarness.getState().persistSessionIndex()
  },

  async loadPersistedSessionIntoArchive(sessionId: string) {
    const api = (window as any).harness
    if (!api?.sessionLoad) return false
    const res = await api.sessionLoad(sessionId)
    if (!res?.ok || !res.session?.slice) return false
    const slice = normalizePersistedFrames(res.session.slice as SessionSlice)
    set((prev) => ({
      sessionArchive: {
        ...prev.sessionArchive,
        [sessionId]: slice,
      },
    }))
    return true
  },

  toggleSettings(open) {
    set((prev) => ({ settingsOpen: open ?? !prev.settingsOpen }))
  },

  async hydrateSettings() {
    const api = (window as any).harness
    if (!api?.settingsGet) return
    const res = await api.settingsGet()
    if (res?.ok && res.settings) {
      set({ settings: res.settings as DesktopSettings })
    }
  },

  async setPermissionTier(tier) {
    // Optimistic update in the store so the UI reflects the change
    // instantly; the IPC handler writes to disk and pushes down to the
    // bridge. If the write fails we fall back to the old value.
    const prev = useHarness.getState().settings
    set({
      settings: {
        ...prev,
        permissions: { ...prev.permissions, autoApprove: tier },
      },
    })
    const api = (window as any).harness
    if (api?.settingsUpdate) {
      const res = await api.settingsUpdate({
        permissions: { autoApprove: tier },
      })
      if (!res?.ok) {
        set({ settings: prev })
        useHarness.getState().showToast('Failed to save settings', 'warn')
      } else if (res.settings) {
        set({ settings: res.settings as DesktopSettings })
      }
    }
  },

  async escalateSessionPolicy(tier) {
    // Scoped to just the current session — does NOT touch global settings.
    const api = (window as any).harness
    if (!api) return
    await api.sendCommand({
      type: 'set_permission_policy',
      sessionId: useHarness.getState().activeSessionId,
      autoApprove: tier,
    })
  },

  async switchToParent() {
    const state = useHarness.getState()
    const active = state.sessions.find((s) => s.id === state.activeSessionId)
    if (!active?.parentSessionId) return
    await state.switchSession(active.parentSessionId)
  },

  renameSession(sessionId, title) {
    set((prev) => ({
      sessions: prev.sessions.map((s) =>
        s.id === sessionId ? { ...s, title } : s,
      ),
    }))
    // Persist immediately so the new title survives app restart.
    setTimeout(() => {
      const state = useHarness.getState()
      state.persistSession(sessionId).catch(() => {})
      state.persistSessionIndex().catch(() => {})
    }, 100)
  },

  async deleteSession(sessionId) {
    const state = useHarness.getState()
    // Don't allow deleting the currently active session — switch first.
    if (sessionId === state.activeSessionId) {
      state.showToast("Can't delete the active session", 'warn')
      return
    }
    // Cascade through every subagent / nested-subagent session of the
    // target so the user doesn't end up with orphaned transcripts on
    // disk after a parent is removed.
    const descendants = collectDescendantSessionIds(state.sessions, sessionId)
    if (descendants.includes(state.activeSessionId)) {
      state.showToast(
        "Can't delete — the active session is a subagent of this one. Switch first.",
        'warn',
      )
      return
    }
    const idsToDelete = new Set<string>([sessionId, ...descendants])

    // Remove from store: sessions list, archive, any panes pointing at
    // a removed id. Frame caches get released for each archived slice.
    set((prev) => {
      const nextArchive = { ...prev.sessionArchive }
      for (const id of idsToDelete) {
        releaseSliceFrames(nextArchive[id])
        delete nextArchive[id]
      }
      const nextPanes = prev.sessionPanes.filter(
        (pane) => !idsToDelete.has(pane.sessionId),
      )
      const activePaneStillThere = nextPanes.some((p) => p.id === prev.activePaneId)
      return {
        ...prev,
        sessions: prev.sessions.filter((s) => !idsToDelete.has(s.id)),
        sessionArchive: nextArchive,
        sessionPanes: nextPanes,
        activePaneId: activePaneStillThere
          ? prev.activePaneId
          : (nextPanes[0]?.id ?? prev.activePaneId),
      }
    })

    // Remove from disk: renderer-side metadata file (`{id}.json`) +
    // bridge-side transcript file (`{id}.transcript.json`) +
    // bridge in-memory `_BridgeState.sessions` entries.
    const api = (window as any).harness
    const allIds = [sessionId, ...descendants]
    if (api?.sessionDelete) {
      await Promise.all(allIds.map((id) => api.sessionDelete(id).catch(() => {})))
    }
    if (api?.sendCommand) {
      await api
        .sendCommand({
          type: 'delete_session',
          sessionId,
          cascadeSessionIds: descendants,
        })
        .catch(() => {})
    }
    await useHarness.getState().persistSessionIndex().catch(() => {})

    if (descendants.length === 0) {
      state.showToast('Session deleted', 'info')
    } else {
      const noun = descendants.length === 1 ? 'subagent' : 'subagents'
      state.showToast(
        `Session deleted — ${descendants.length} ${noun} also removed`,
        'info',
      )
    }
  },

  async downloadSession(sessionId) {
    // Helper hoisted inside the function so we don't pollute the module
    // surface — only used by this exporter.
    //
    // EVERYTHING IS CHRONOLOGICAL (oldest → newest) — matches `verdictHistory`
    // on the bridge and reads top-to-bottom like a session transcript.
    // Future analyzers (including any LLM asked to summarize a goal run)
    // should iterate the verdicts array in array order to follow the loop
    // forward in time.
    function collectGoalLoopForExport(events: SystemEventRecord[] | undefined): {
      goalState: Record<string, unknown> | null
      brief: Record<string, unknown> | null
      // Last 12 verdicts in chronological order (matches the bridge's
      // verdict_history field). Provided as a stable per-event snapshot.
      verdictHistory: Record<string, unknown>[]
      // Every goal_judge event's full verdict, chronological. Preserved
      // even if the rolling 100-event renderer buffer dropped early ones,
      // because the bridge's verdictHistory carries them through.
      verdicts: Array<{
        at: number
        turnsUsed: number | null
        verdict: Record<string, unknown>
      }>
      // Convenience: every goal event in chronological order. Useful for
      // reconstructing the timeline (set / continue / pause / done) when
      // analyzing a session externally.
      events: Array<{ at: number; subtype: string; message: string }>
    } | null {
      if (!events || events.length === 0) return null
      const goalEvents = events
        .filter((e) => (e.subtype ?? '').startsWith('goal_'))
        .sort((a, b) => a.at - b.at) // chronological
      if (goalEvents.length === 0) return null
      const latest = goalEvents[goalEvents.length - 1]
      const latestDetails = (latest.details as Record<string, unknown> | undefined) ?? {}
      const judgeEvents = goalEvents.filter((e) => e.subtype === 'goal_judge')
      return {
        goalState: (latestDetails.goalState as Record<string, unknown> | null) ?? null,
        brief: (latestDetails.judgeRules as Record<string, unknown> | null) ?? null,
        verdictHistory:
          (latestDetails.verdictHistory as Record<string, unknown>[] | undefined) ?? [],
        verdicts: judgeEvents.map((e) => {
          const d = (e.details as Record<string, unknown> | undefined) ?? {}
          const gs = d.goalState as Record<string, unknown> | undefined
          return {
            at: e.at,
            turnsUsed: typeof gs?.turnsUsed === 'number' ? (gs.turnsUsed as number) : null,
            verdict: (d.verdict as Record<string, unknown>) ?? {},
          }
        }),
        events: goalEvents.map((e) => ({
          at: e.at,
          subtype: e.subtype ?? '',
          message: e.message ?? '',
        })),
      }
    }

    const state = useHarness.getState()
    // Build the export payload: session snapshot + full slice + metadata
    const snapshot = state.sessions.find((s) => s.id === sessionId)
    if (!snapshot) {
      state.showToast('Session not found', 'warn')
      return
    }
    // Get the slice — either from the active state or the archive
    let slice: SessionSlice | undefined
    if (sessionId === state.activeSessionId) {
      slice = sliceFromState(state)
    } else {
      slice = state.sessionArchive[sessionId]
      if (!slice) {
        // Try loading from disk
        const loaded = await state.loadPersistedSessionIntoArchive(sessionId)
        if (loaded) {
          slice = useHarness.getState().sessionArchive[sessionId]
        }
      }
    }
    if (!slice) {
      state.showToast('Session data not found', 'warn')
      return
    }

    // Build a rich export with all the data needed for training
    // Build per-tool success/failure stats for training data quality
    const toolStats: Record<string, { count: number; success: number; failure: number }> = {}
    for (const tc of Object.values(slice.toolCalls)) {
      if (!toolStats[tc.name]) toolStats[tc.name] = { count: 0, success: 0, failure: 0 }
      toolStats[tc.name].count += 1
      if (tc.isError) toolStats[tc.name].failure += 1
      else if (tc.status === 'success') toolStats[tc.name].success += 1
    }

    // Extract the initial user message as the "task description"
    // (used for RFT pair generation and RL reward assignment)
    const firstUserMsg = slice.messages.find((m) => m.role === 'user')
    const taskDescription = firstUserMsg?.parts
      .filter((p) => p.type === 'text')
      .map((p) => p.text)
      .join('\n') ?? ''

    // Extract thinking traces from message parts
    const thinkingTraces = slice.messages
      .filter((m) => m.role === 'assistant')
      .flatMap((m) =>
        m.parts
          .filter((p) => p.type === 'thinking' && p.text)
          .map((p) => ({ messageId: m.id, thinking: p.text })),
      )

    const exportData = {
      version: 3,
      exportedAt: new Date().toISOString(),
      app: 'freyja',
      // ─── System prompt (critical for SFT — the model needs to
      //     know what instructions it was following) ─────────────
      systemPrompt: slice.systemPrompt ?? null,
      // ─── Session metadata ────────────────────────────────────
      session: {
        id: snapshot.id,
        title: snapshot.title,
        model: snapshot.model,
        reasoningLevel: slice.reasoningLevel || snapshot.reasoningLevel,
        workspace: snapshot.workspace,
        createdAt: snapshot.createdAt,
        updatedAt: snapshot.updatedAt,
        parentSessionId: snapshot.parentSessionId,
        childSessionIds: snapshot.childSessionIds,
        messageCount: snapshot.messageCount,
        totalInputTokens: snapshot.totalInputTokens,
        totalOutputTokens: snapshot.totalOutputTokens,
        cacheReadTokens: snapshot.cacheReadTokens,
      },
      // ─── Training-relevant fields ────────────────────────────
      // Initial task/prompt for RFT pairing and reward assignment
      taskDescription,
      // Per-tool success/failure for quality filtering (Hermes pattern)
      toolStats,
      // Thinking traces (ATLaS: training on reasoning = 3x better)
      thinkingTraces,
      // ─── Full conversation ───────────────────────────────────
      messages: slice.messages.map((m) => ({
        id: m.id,
        role: m.role,
        parts: m.parts,
        createdAt: m.createdAt,
        inputTokens: m.inputTokens,
        outputTokens: m.outputTokens,
        attachments: m.attachments,
      })),
      // ─── Tool calls (trajectory actions) ─────────────────────
      toolCalls: Object.values(slice.toolCalls).map((tc) => ({
        id: tc.id,
        name: tc.name,
        arguments: tc.arguments,
        status: tc.status,
        result: tc.result,
        isError: tc.isError,
        durationMs: tc.durationMs,
        startedAt: tc.startedAt,
      })),
      toolCallOrder: slice.toolCallOrder,
      // ─── Sub-agents (multi-agent trajectories) ───────────────
      subagents: Object.values(slice.subagents).map((sa) => ({
        id: sa.id,
        label: sa.label,
        mode: sa.mode,
        state: sa.state,
        task: sa.task,
        agentType: sa.agentType,
        artifactPath: sa.artifactPath,
        startedAt: sa.startedAt,
        elapsedMs: sa.elapsedMs,
        tokensIn: sa.tokensIn,
        tokensOut: sa.tokensOut,
        toolsCalled: sa.toolsCalled,
        result: sa.result,
      })),
      // ─── Aggregated metrics ──────────────────────────────────
      usage: slice.usage,
      systemEvents: slice.systemEvents,
      // ─── Goal loop snapshot ──────────────────────────────────
      // Lifts goal state / brief / verdict history out of the
      // event stream into a dedicated block so they survive the
      // rolling 100-event buffer and analyzers don't have to scan
      // systemEvents to find them. Pulled from the most recent
      // goal_* event's payload (every goal event includes them).
      goalLoop: collectGoalLoopForExport(slice.systemEvents),
    }

    // Create a downloadable file
    const blob = new Blob(
      [JSON.stringify(exportData, null, 2)],
      { type: 'application/json' },
    )
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    const safeName = (snapshot.title || 'session')
      .replace(/[^a-zA-Z0-9-_ ]/g, '')
      .replace(/\s+/g, '-')
      .slice(0, 50)
    a.href = url
    a.download = `freyja-${safeName}-${sessionId.slice(-8)}.json`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
    state.showToast('Session downloaded', 'ok')
  },

  async setComputerEnabled(enabled) {
    // Optimistic: flip the toggle in the store, persist via settings
    // IPC, and let main.ts forward the flag down to the bridge.
    const prevSettings = useHarness.getState().settings
    const nextComputer = {
      ...prevSettings.computer,
      enabled,
      // First time they turn it on, queue the wizard.
      wizardState:
        enabled && prevSettings.computer.wizardState === 'never'
          ? ('never' as const)
          : prevSettings.computer.wizardState,
    }
    set({
      settings: { ...prevSettings, computer: nextComputer },
      computerWizardOpen: enabled && prevSettings.computer.wizardState === 'never',
    })
    const api = (window as any).harness
    if (api?.settingsUpdate) {
      const res = await api.settingsUpdate({
        computer: { enabled, wizardState: nextComputer.wizardState },
      })
      if (!res?.ok) {
        // Roll back on failure.
        set({ settings: prevSettings, computerWizardOpen: false })
        useHarness.getState().showToast('Failed to toggle computer control', 'warn')
      }
    }
  },

  openComputerWizard(open) {
    set((prev) => ({ computerWizardOpen: open ?? !prev.computerWizardOpen }))
  },

  async emergencyStopComputer(reason) {
    const api = (window as any).harness
    if (!api) return
    // Optimistic: mark sessions cancelled immediately so the UI
    // reflects the stop even before the bridge acks.
    set((prev) => {
      const nextMap: Record<string, ComputerSessionState> = {}
      for (const [id, s] of Object.entries(prev.computerSessions)) {
        nextMap[id] =
          s.status === 'running' ? { ...s, status: 'cancelled' } : s
      }
      return {
        ...prev,
        computerSessions: nextMap,
        computerActive: false,
        toast: {
          id: nextId('toast'),
          message: 'Emergency stop fired',
          tone: 'warn',
          at: Date.now(),
        },
      }
    })
    await api.sendCommand({
      type: 'computer.emergency_stop',
      reason: reason || 'user',
    })
  },

  async answerPermission(requestId, approved) {
    const state = useHarness.getState()
    const req = state.permissionQueue.find((p) => p.requestId === requestId)
    set((prev) => ({
      permissionQueue: prev.permissionQueue.filter((p) => p.requestId !== requestId),
    }))
    const api = (window as any).harness
    if (api) {
      await api.sendCommand({
        type: 'permission_response',
        sessionId: req?.sessionId ?? state.activeSessionId,
        requestId,
        approved,
      })
    }
  },

  async listTools() {
    const api = (window as any).harness
    if (api) await api.sendCommand({ type: 'list_tools' })
  },

  async updateMemory(id, patch) {
    const api = (window as any).harness
    if (!api) return
    const sessionId = useHarness.getState().activeSessionId
    await api.sendCommand({
      type: 'memory_update',
      sessionId,
      id,
      ...patch,
    })
  },

  async deleteMemory(id, note) {
    const api = (window as any).harness
    if (!api) return
    const sessionId = useHarness.getState().activeSessionId
    await api.sendCommand({
      type: 'memory_delete',
      sessionId,
      id,
      note,
    })
  },

  async restoreMemory(id, note) {
    const api = (window as any).harness
    if (!api) return
    const sessionId = useHarness.getState().activeSessionId
    await api.sendCommand({
      type: 'memory_restore',
      sessionId,
      id,
      note,
    })
  },

  async mergeMemories(ids, text, opts) {
    const api = (window as any).harness
    if (!api) return
    const sessionId = useHarness.getState().activeSessionId
    await api.sendCommand({
      type: 'memory_merge',
      sessionId,
      ids,
      text,
      ...(opts ?? {}),
    })
  },

  toggleDebug(open) {
    set((prev) => ({ debugOpen: open ?? !prev.debugOpen }))
  },

  updateJudgeRules(rules) {
    const state = get()
    if (!state.activeSessionId) return
    const api = (window as any).harness
    if (!api) return
    api.sendCommand({
      type: 'goal_control',
      sessionId: state.activeSessionId,
      action: 'set_rules',
      rules,
    })
  },

  recalibrateJudge() {
    const state = get()
    if (!state.activeSessionId) return
    const api = (window as any).harness
    if (!api) return
    api.sendCommand({
      type: 'goal_control',
      sessionId: state.activeSessionId,
      action: 'recalibrate_judge',
    })
  },

  acceptCalibratorProposal() {
    const state = get()
    if (!state.activeSessionId) return
    const api = (window as any).harness
    if (!api) return
    api.sendCommand({
      type: 'goal_control',
      sessionId: state.activeSessionId,
      action: 'accept_calibration',
    })
  },

  dismissCalibratorProposal() {
    const state = get()
    if (!state.activeSessionId) return
    const api = (window as any).harness
    if (!api) return
    api.sendCommand({
      type: 'goal_control',
      sessionId: state.activeSessionId,
      action: 'dismiss_calibration',
    })
  },

  toggleSidebar(collapsed) {
    set((prev) => {
      const next = collapsed ?? !prev.sidebarCollapsed
      try {
        localStorage.setItem('ah.sidebarCollapsed', next ? '1' : '0')
      } catch {}
      return { sidebarCollapsed: next, focusMode: false, preFocusPanelState: null }
    })
  },

  toggleActivityPanel(collapsed) {
    set((prev) => {
      const next = collapsed ?? !prev.activityPanelCollapsed
      try {
        localStorage.setItem('ah.activityPanelCollapsed', next ? '1' : '0')
      } catch {}
      return { activityPanelCollapsed: next, focusMode: false, preFocusPanelState: null }
    })
  },

  setActivityPanelWidth(width) {
    // Clamp: the activity panel has to stay wide enough for the
    // context meter + tool-call rows to render without wrapping
    // everything into useless 1-char columns, and narrow enough that
    // the conversation area still has room on a 13" MBP.
    const clamped = Math.max(260, Math.min(900, Math.round(width)))
    set({ activityPanelWidth: clamped })
    try {
      localStorage.setItem('ah.activityPanelWidth', String(clamped))
    } catch {}
  },

  setSidebarWidth(width) {
    // Lower bound: workspace header (label + "+ new" + collapse) still
    // fits without clipping. Upper bound: leaves room for the
    // conversation column on common laptop widths.
    const clamped = Math.max(220, Math.min(640, Math.round(width)))
    set({ sidebarWidth: clamped })
    try {
      localStorage.setItem('ah.sidebarWidth', String(clamped))
    } catch {}
  },

  focusToolCall(id) {
    set((prev) => ({
      focusedToolCallId: id,
      focusedToolCallSerial: prev.focusedToolCallSerial + 1,
      focusedPanel: 'conversation',
    }))
  },

  toggleFocusMode(focus) {
    // Focus mode hides both side panels completely and remembers the
    // previous panel state so leaving focus mode restores the workspace.
    set((prev) => {
      const entering = focus ?? !prev.focusMode
      if (!entering) {
        const restored = prev.preFocusPanelState ?? {
          sidebarCollapsed: false,
          activityPanelCollapsed: false,
        }
        try {
          localStorage.setItem('ah.sidebarCollapsed', restored.sidebarCollapsed ? '1' : '0')
          localStorage.setItem(
            'ah.activityPanelCollapsed',
            restored.activityPanelCollapsed ? '1' : '0',
          )
        } catch {}
        return {
          focusMode: false,
          preFocusPanelState: null,
          sidebarCollapsed: restored.sidebarCollapsed,
          activityPanelCollapsed: restored.activityPanelCollapsed,
        }
      }
      try {
        localStorage.setItem('ah.sidebarCollapsed', '1')
        localStorage.setItem('ah.activityPanelCollapsed', '1')
      } catch {}
      return {
        focusMode: true,
        preFocusPanelState: {
          sidebarCollapsed: prev.sidebarCollapsed,
          activityPanelCollapsed: prev.activityPanelCollapsed,
        },
        sidebarCollapsed: true,
        activityPanelCollapsed: true,
      }
    })
  },

  showToast(message, tone = 'info') {
    const id = nextId('toast')
    set({ toast: { id, message, tone, at: Date.now() } })
    setTimeout(() => {
      if (useHarness.getState().toast?.id === id) {
        useHarness.setState({ toast: null })
      }
    }, 2600)
  },

  clearToast() {
    set({ toast: null })
  },

  runSlashCommand(name, args) {
    const state = useHarness.getState()
    const show = (m: string, t: 'info' | 'ok' | 'warn' | 'danger' = 'info') =>
      state.showToast(m, t)

    switch (name) {
      case '/help':
      case '/docs':
        state.toggleCommandPalette(true)
        return true
      case '/dashboard':
      case '/mission':
        state.toggleMissionDashboard(true, 'overview')
        return true
      case '/profiles':
      case '/agents':
        state.toggleMissionDashboard(true, 'profiles')
        return true
      case '/telemetry':
      case '/metrics':
        state.toggleMissionDashboard(true, 'telemetry')
        return true
      case '/clear':
      case '/new':
      case '/reset':
        state.newSession()
        return true
      case '/usage': {
        const u = state.usage
        const msg = `context ${u.totalInputTokens} · output ${u.totalOutputTokens} · cache ${u.totalCacheReadTokens} · $${u.totalCost.toFixed(4)}`
        show(msg)
        return true
      }
      case '/model': {
        const next = (args ?? '').trim()
        if (!next) {
          show(`current model: ${state.model}`, 'info')
        } else {
          state.setModel(next)
          show(`model → ${next}`, 'ok')
        }
        return true
      }
      case '/session':
      case '/sessions':
        state.setFocusedPanel('sidebar')
        show(`${state.sessions.length} session(s)`, 'info')
        return true
      case '/skills':
        state.setFocusedPanel('sidebar')
        show(`${Object.keys(state.skills).length} skill(s) loaded`, 'info')
        return true
      case '/subagents': {
        state.toggleMissionDashboard(true, 'overview')
        return true
      }
      case '/tools':
        state.listTools()
        show('loading tool catalog…', 'info')
        return true
      case '/compact':
      case '/compaction': {
        const api = (window as any).harness
        if (!api) {
          show('no bridge', 'warn')
          return true
        }
        api
          .sendCommand({
            type: 'compact',
            sessionId: state.activeSessionId,
            model: state.model,
            reasoningLevel: state.reasoningLevel,
          })
          .then((res: { ok: boolean; error?: string } | undefined) => {
            if (res && !res.ok) show(res.error || 'compaction request failed', 'warn')
          })
          .catch(() => show('compaction request failed', 'warn'))
        show('compaction requested', 'info')
        return true
      }
      case '/goal': {
        const api = (window as any).harness
        if (!api) {
          show('no bridge', 'warn')
          return true
        }
        const raw = (args ?? '').trim()
        const [verb, ...rest] = raw.split(/\s+/)
        const lower = (verb || '').toLowerCase()
        const action =
          lower === 'pause' ||
          lower === 'resume' ||
          lower === 'clear' ||
          lower === 'stop' ||
          lower === 'done' ||
          lower === 'status'
            ? lower
            : raw
              ? 'set'
              : 'status'
        const goal = action === 'set' ? raw : rest.join(' ')
        api.sendCommand({
          type: 'goal_control',
          sessionId: state.activeSessionId,
          action,
          goal,
          reason: goal || undefined,
          model: state.model,
          reasoningLevel: state.reasoningLevel,
          coordinationStrategy: state.coordinationStrategy,
        })
        if (action === 'set') show('goal loop armed', 'ok')
        else show(`goal ${action}`, 'info')
        return true
      }
      case '/autopilot': {
        // /autopilot on|off — toggle kanban auto-dispatch for the
        // active session. Bridge ignores the request when the session
        // isn't in kanban mode, so it's safe to call from anywhere.
        const api = (window as any).harness
        if (!api) {
          show('no bridge', 'warn')
          return true
        }
        const raw = (args ?? '').trim().toLowerCase()
        const enabled = raw === '' ? true : raw === 'on' || raw === 'enable' || raw === 'true'
        api.sendCommand({
          type: 'kanban_autopilot',
          sessionId: state.activeSessionId,
          enabled,
        })
        show(`autopilot ${enabled ? 'on' : 'off'}`, 'info')
        return true
      }
      case '/memory':
        state.setFocusedPanel('sidebar')
        show(`${Object.keys(state.memories).length} memory item(s) loaded`, 'info')
        return true
      case '/export': {
        // Persist whatever's in the current store to disk first so the
        // exported file reflects the live session (the bridge also
        // autosaves periodically but we want a fresh snapshot). Then
        // ask the main process to show a save dialog and write the
        // JSON + a companion .trace.txt that's easy to grep/read.
        const api = (window as any).harness
        if (!api?.sessionExport) {
          show('export unavailable in this build', 'warn')
          return true
        }
        ;(async () => {
          try {
            await state.persistActiveSession()
          } catch {}
          const sid = useHarness.getState().activeSessionId
          const res = await api.sessionExport(sid)
          if (res?.cancelled) return
          if (res?.ok && res.jsonPath) {
            show(`exported → ${res.jsonPath}`, 'info')
          } else {
            show(`export failed: ${res?.error ?? 'unknown'}`, 'warn')
          }
        })()
        return true
      }
      case '/debug':
        state.toggleDebug()
        return true
      case '/settings':
      case '/permissions':
        state.toggleSettings(true)
        return true
      case '/burst': {
        const api = (window as any).harness
        if (api) {
          state.requestDemoBurst()
        } else {
          ;(window as any).__harnessDemo?.burst()
        }
        return true
      }
      case '/diagnose': {
        const api = (window as any).harness
        if (!api) {
          show('no bridge', 'warn')
          return true
        }
        api.sendCommand({ type: 'diagnose' })
        show(
          'diagnose requested -- check ~/.freyja/bridge-diagnose.txt',
          'info',
        )
        return true
      }
      case '/restart-bridge':
      case '/restart': {
        // Kill + respawn the Python bridge subprocess. Use this
        // after editing anything under bridge/ or engine/ --
        // Python doesn't hot-reload, so the running process
        // otherwise keeps the old code in memory. The UI and
        // on-disk session state are preserved; only the in-memory
        // bridge state resets.
        const api = (window as any).harness
        if (!api?.restartBridge) {
          show('restart not available in this build', 'warn')
          return true
        }
        show('restarting bridge…', 'info')
        ;(async () => {
          const res = await api.restartBridge()
          if (res?.ok) {
            show('bridge restarted', 'ok')
          } else {
            show(`restart failed: ${res?.error ?? 'unknown'}`, 'warn')
          }
        })()
        return true
      }
      case '/computer':
      case '/screen': {
        const goal = (args ?? '').trim()
        if (!goal) {
          show('usage: /computer <goal>', 'info')
          return true
        }
        if (!state.settings.computer.enabled) {
          show('computer control is disabled — enable it in Settings', 'warn')
          state.toggleSettings(true)
          return true
        }
        // Synthesize a user message asking the parent agent to run
        // computer_use with the provided goal. This is intentionally
        // a normal chat turn (not a direct tool injection) so the
        // parent transcript tells the full story of what happened.
        state.sendMessage(
          `Use the \`computer_use\` tool with goal: "${goal}". Watch mode is on.`,
        )
        show('computer sub-agent queued', 'ok')
        return true
      }
      default:
        return false
    }
  },
}))
