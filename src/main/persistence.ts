import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'

/**
 * Disk-backed session persistence. Lives under Electron's main process so
 * the renderer stays sandboxed. Sessions are stored one JSON file per id
 * in ~/.freyja/sessions/.
 *
 * NOTE: This is display-state persistence only -- we save the renderer's
 * SessionSlice so the UI can show the transcript after a restart. Full
 * transcript restoration (so follow-ups keep LLM context) is a follow-up;
 * the bridge currently starts a fresh LLM conversation when the user
 * returns to a persisted session.
 */

const SESSIONS_DIR = path.join(
  os.homedir(),
  '.freyja',
  'sessions',
)
const SESSION_INDEX_FILE = '_index.json'
let indexWriteQueue: Promise<unknown> = Promise.resolve()

function ensureDir(): void {
  if (!fs.existsSync(SESSIONS_DIR)) {
    fs.mkdirSync(SESSIONS_DIR, { recursive: true })
  }
}

function sanitizeId(id: string): string {
  // Strip anything that isn't safe for a filename.
  return id.replace(/[^a-zA-Z0-9._-]/g, '_').slice(0, 160)
}

/** Legacy sanitizer that STRIPS invalid chars instead of replacing
 *  them with ``_``. Files written before the sanitizer switched to
 *  underscore-replacement use this scheme; the resolver falls back
 *  to it for backward compat.
 *
 *  Mirrors the (also-now-superseded) shape of
 *  ``bridge/transcript_persistence.py:_sanitize_session_id``. The
 *  one-shot ``migrateLegacySessionFiles()`` migration renames any
 *  legacy-style files at startup so this fallback is a no-op going
 *  forward. */
function sanitizeIdLegacyStrip(id: string): string {
  return id.replace(/[^a-zA-Z0-9._-]/g, '').slice(0, 160)
}

/** Resolve a file path under SESSIONS_DIR with backward-compat: try
 *  the current sanitizer first, then the legacy strip-style
 *  sanitizer. Returns the first path that exists, or the modern path
 *  if neither exists (so callers can use it for error messages). */
function resolveSessionPath(id: string, suffix: string): string {
  const modern = path.join(SESSIONS_DIR, `${sanitizeId(id)}${suffix}`)
  if (fs.existsSync(modern)) return modern
  const legacy = path.join(SESSIONS_DIR, `${sanitizeIdLegacyStrip(id)}${suffix}`)
  if (fs.existsSync(legacy)) return legacy
  return modern
}

function sessionFile(id: string): string {
  return resolveSessionPath(id, '.json')
}

function sessionIndexFile(): string {
  return path.join(SESSIONS_DIR, SESSION_INDEX_FILE)
}

export interface PersistedSession {
  version: 1
  id: string
  title: string
  model: string
  workspace: string
  createdAt: number
  updatedAt: number
  messageCount: number
  totalInputTokens: number
  totalOutputTokens: number
  cacheReadTokens: number
  // Sub-agent lineage. Without these, the "swarm" panel in the
  // sidebar is empty after app restart: `childSessions` is derived
  // from `parentSessionId` on each row, and if we don't round-trip
  // that field the nesting disappears and nothing is clickable.
  parentSessionId?: string
  childSessionIds?: string[]
  task?: string
  agentType?: string
  completed?: boolean
  completedAt?: number
  success?: boolean
  /** The full SessionSlice as a JSON-safe object. */
  slice: unknown
}

export type PersistedSessionMeta = Omit<PersistedSession, 'slice'>

interface PersistedSessionIndex {
  version: 1
  updatedAt: number
  sessions: PersistedSessionMeta[]
}

function toMeta(data: PersistedSession): PersistedSessionMeta {
  const { slice: _unused, ...meta } = data
  return meta
}

function sortSessions(rows: PersistedSessionMeta[]): PersistedSessionMeta[] {
  return rows.slice().sort((a, b) => b.updatedAt - a.updatedAt)
}

function dedupeSessions(rows: PersistedSessionMeta[]): PersistedSessionMeta[] {
  const seen = new Set<string>()
  const out: PersistedSessionMeta[] = []
  for (const row of rows) {
    if (!row?.id || seen.has(row.id)) continue
    seen.add(row.id)
    out.push(row)
  }
  return out
}

// Mtime-keyed cache for the parsed _index.json. The index can grow to
// 1-2MB on installs with many sessions, and listSessions runs in the
// hot path (renderer's 30s sidebar refresh poll + every focus event).
// Re-reading + JSON-parsing on every call is wasteful when the index
// hasn't changed. Cache invalidates the moment mtime advances.
let _indexCache: {
  mtimeMs: number
  rows: PersistedSessionMeta[]
} | null = null

function readIndexSync(): PersistedSessionMeta[] | null {
  ensureDir()
  const indexPath = sessionIndexFile()
  let stat: fs.Stats
  try {
    stat = fs.statSync(indexPath)
  } catch {
    return null
  }
  if (_indexCache && _indexCache.mtimeMs === stat.mtimeMs) {
    // Clone to avoid callers mutating cache state.
    return [..._indexCache.rows]
  }
  try {
    const raw = fs.readFileSync(indexPath, 'utf8')
    const parsed = JSON.parse(raw) as PersistedSessionIndex
    if (!parsed || parsed.version !== 1 || !Array.isArray(parsed.sessions)) {
      return null
    }
    const rows = sortSessions(dedupeSessions(parsed.sessions))
    _indexCache = { mtimeMs: stat.mtimeMs, rows }
    return [...rows]
  } catch {
    return null
  }
}

async function readIndexAsync(): Promise<PersistedSessionMeta[] | null> {
  ensureDir()
  try {
    const raw = await fs.promises.readFile(sessionIndexFile(), 'utf8')
    const parsed = JSON.parse(raw) as PersistedSessionIndex
    if (!parsed || parsed.version !== 1 || !Array.isArray(parsed.sessions)) {
      return null
    }
    return sortSessions(dedupeSessions(parsed.sessions))
  } catch {
    return null
  }
}

async function writeJsonAtomic(filePath: string, value: unknown): Promise<number> {
  ensureDir()
  const raw = JSON.stringify(value)
  const bytes = Buffer.byteLength(raw, 'utf8')
  const tmp = `${filePath}.${process.pid}.${Date.now()}.${Math.random()
    .toString(36)
    .slice(2)}.tmp`
  await fs.promises.writeFile(tmp, raw, 'utf8')
  await fs.promises.rename(tmp, filePath)
  return bytes
}

async function writeIndexUnlocked(rows: PersistedSessionMeta[]): Promise<number> {
  const index: PersistedSessionIndex = {
    version: 1,
    updatedAt: Date.now(),
    sessions: sortSessions(dedupeSessions(rows)),
  }
  return writeJsonAtomic(sessionIndexFile(), index)
}

function enqueueIndexWrite<T>(work: () => Promise<T>): Promise<T> {
  const next = indexWriteQueue.then(work, work)
  indexWriteQueue = next.catch(() => {})
  return next
}

export function sessionsDirectory(): string {
  ensureDir()
  return SESSIONS_DIR
}

export function listSessions(): PersistedSessionMeta[] {
  ensureDir()
  let rows: PersistedSessionMeta[]
  const indexed = readIndexSync()
  if (indexed) {
    rows = [...indexed]
  } else {
    rows = []
    let files: string[] = []
    try {
      files = fs.readdirSync(SESSIONS_DIR)
    } catch {
      return rows
    }
    for (const f of files) {
      if (!f.endsWith('.json')) continue
      if (f === SESSION_INDEX_FILE || f.endsWith('.transcript.json')) continue
      try {
        const raw = fs.readFileSync(path.join(SESSIONS_DIR, f), 'utf8')
        const parsed = JSON.parse(raw) as PersistedSession
        if (!parsed || parsed.version !== 1) continue
        // Return only the metadata fields (not the heavy slice)
        rows.push(toMeta(parsed))
      } catch {
        // Corrupt file -- skip silently; user can manually clean up.
      }
    }
  }

  // Mirror gateway-created sessions (Slack, etc.) — sessions whose
  // transcripts live in ~/.freyja/sessions/<id>.transcript.json but
  // that don't have a renderer-owned slice. Synthesize a stub
  // PersistedSessionMeta so the sidebar shows them with their
  // canonical `freyja:slack:*` id. Click-through still works since
  // the bridge will rehydrate from the transcript when the renderer
  // attaches to that id.
  rows = [...rows, ...mirrorGatewaySessions(new Set(rows.map((r) => r.id)))]

  return sortSessions(dedupeSessions(rows))
}

// Per-file mtime cache for parsed gateway-transcript metadata. The
// renderer's session-list refresh runs every 30s + on every window
// focus. Without this cache, each call read+parsed every
// ``.transcript.json`` file in ~/.freyja/sessions/ — and a single
// install can hit 50MB+ desktop transcripts. The fast-path filename
// filter (only files starting with ``freyja``) plus per-file mtime
// caching makes repeat polls O(stat()) instead of O(parse all).
const _gatewayMirrorCache = new Map<
  string,
  { mtimeMs: number; meta: PersistedSessionMeta | null }
>()

/** Scan for bridge-owned transcripts that don't have a renderer-owned
 *  slice, and produce stub meta entries so the gateway-created
 *  sessions show up in the sidebar. */
function mirrorGatewaySessions(known: Set<string>): PersistedSessionMeta[] {
  const out: PersistedSessionMeta[] = []
  let files: string[] = []
  try {
    files = fs.readdirSync(SESSIONS_DIR)
  } catch {
    return out
  }
  const seenPaths = new Set<string>()
  for (const f of files) {
    if (!f.endsWith('.transcript.json')) continue
    // Fast-path filter: gateway session ids sanitize to filenames
    // starting with ``freyja`` (covers both the modern
    // ``freyja_slack_T...`` and legacy ``freyjaslackT...`` styles).
    // Skipping non-matching files BEFORE the read+parse cuts I/O by
    // ~99% on a typical install (most transcripts are desktop or
    // sub-agent sessions whose names don't start with freyja).
    if (!f.startsWith('freyja')) continue
    const fullPath = path.join(SESSIONS_DIR, f)
    seenPaths.add(fullPath)
    let stat: fs.Stats | null = null
    try {
      stat = fs.statSync(fullPath)
    } catch {
      continue
    }
    const mtimeMs = stat.mtimeMs
    const cached = _gatewayMirrorCache.get(fullPath)
    if (cached && cached.mtimeMs === mtimeMs) {
      if (cached.meta && !known.has(cached.meta.id)) {
        out.push(cached.meta)
      }
      continue
    }
    let parsed: any = null
    try {
      const raw = fs.readFileSync(fullPath, 'utf8')
      parsed = JSON.parse(raw)
    } catch {
      _gatewayMirrorCache.set(fullPath, { mtimeMs, meta: null })
      continue
    }
    const sessionId: string | undefined = parsed?.session_id
    if (!sessionId || !sessionId.startsWith('freyja:')) {
      _gatewayMirrorCache.set(fullPath, { mtimeMs, meta: null })
      continue
    }
    const messageCount = Array.isArray(parsed?.transcript?.entries)
      ? parsed.transcript.entries.filter((e: any) => e?.message).length
      : 0
    const title = sessionId.replace(/^freyja:/, '')
    const meta: PersistedSessionMeta = {
      version: 1,
      id: sessionId,
      title,
      model: parsed?.metadata?.model_id ?? '',
      workspace: parsed?.metadata?.workspace ?? '',
      createdAt: stat?.birthtimeMs ?? stat?.mtimeMs ?? Date.now(),
      updatedAt: stat?.mtimeMs ?? Date.now(),
      messageCount,
      totalInputTokens: 0,
      totalOutputTokens: 0,
      cacheReadTokens: 0,
      task: '(gateway-mirrored — read-only mirror of an external chat)',
      agentType: 'gateway-slack',
    }
    _gatewayMirrorCache.set(fullPath, { mtimeMs, meta })
    if (!known.has(sessionId)) out.push(meta)
  }
  // Evict stale cache entries for deleted files.
  for (const cachedPath of [..._gatewayMirrorCache.keys()]) {
    if (!seenPaths.has(cachedPath)) _gatewayMirrorCache.delete(cachedPath)
  }
  return out
}

export function loadSession(id: string): PersistedSession | null {
  ensureDir()
  try {
    const raw = fs.readFileSync(sessionFile(id), 'utf8')
    const parsed = JSON.parse(raw) as PersistedSession
    if (!parsed || parsed.version !== 1) return null
    return parsed
  } catch {
    // Fall back to synthesizing a read-only PersistedSession from the
    // gateway-owned ``.transcript.json`` file. Gateway sessions
    // (Slack DMs, etc.) never get a renderer-owned slice on disk
    // because they're persisted by the daemon, not by the desktop
    // runner; without this fallback every click on a gateway-mirrored
    // sidebar row would fire a "Session not found" toast even though
    // the conversation exists on disk.
    return synthesizeSessionFromTranscript(id)
  }
}

/** Build a minimal read-only PersistedSession from a gateway-owned
 *  transcript file. Used when the renderer asks for a session that
 *  only exists as ``<id>.transcript.json`` (no slice). Populates the
 *  fields the conversation view needs — user + assistant text turns,
 *  basic token totals, model/reasoning/strategy from transcript
 *  metadata. Tool calls, sub-agents, kanban etc. are left empty
 *  (transcripts don't break them out and they're not meaningful for
 *  an external chat). */
function synthesizeSessionFromTranscript(id: string): PersistedSession | null {
  const transcriptPath = resolveSessionPath(id, '.transcript.json')
  let raw: string
  try {
    raw = fs.readFileSync(transcriptPath, 'utf8')
  } catch {
    return null
  }
  let parsed: any
  try {
    parsed = JSON.parse(raw)
  } catch {
    return null
  }
  const entries: any[] = Array.isArray(parsed?.transcript?.entries)
    ? parsed.transcript.entries
    : []
  let stat: fs.Stats | null = null
  try {
    stat = fs.statSync(transcriptPath)
  } catch { /* ignore */ }

  const messages: any[] = []
  let totalInput = 0, totalOutput = 0, totalCacheR = 0, totalCacheW = 0
  for (const e of entries) {
    const msg = e?.message
    if (!msg || (msg.role !== 'user' && msg.role !== 'assistant')) continue
    const parts: any[] = []
    if (typeof msg.content === 'string') {
      parts.push({ type: 'text', text: msg.content })
    } else if (Array.isArray(msg.content)) {
      for (const block of msg.content) {
        if (typeof block === 'string') {
          parts.push({ type: 'text', text: block })
        } else if (block?.type === 'text' && typeof block?.text === 'string') {
          parts.push({ type: 'text', text: block.text })
        } else if (block?.type === 'tool_use') {
          parts.push({
            type: 'text',
            text: `[tool_use: ${block?.name || 'unknown'}]`,
          })
        } else if (block?.type === 'tool_result') {
          const txt = typeof block?.content === 'string'
            ? block.content
            : '[tool_result]'
          parts.push({ type: 'text', text: txt })
        }
      }
    }
    if (Array.isArray(msg.thinking_blocks)) {
      for (const tb of msg.thinking_blocks) {
        if (typeof tb?.thinking === 'string') {
          parts.unshift({ type: 'thinking', text: tb.thinking })
        }
      }
    }
    if (parts.length === 0) continue
    totalInput += msg.input_tokens ?? 0
    totalOutput += msg.output_tokens ?? 0
    totalCacheR += msg.cache_read_tokens ?? 0
    totalCacheW += msg.cache_write_tokens ?? 0
    messages.push({
      id: e.id || String(messages.length),
      role: msg.role,
      parts,
      createdAt: Math.floor((e.timestamp ?? 0) * 1000),
    })
  }

  // Build a slice containing ALL fields required by SessionSlice
  // (see ``src/renderer/state/store.ts:73``). Missing required fields
  // cause the renderer's setActive to silently drop the click — the
  // conversation pane stays on whatever it was showing.
  const modelId = parsed?.metadata?.model_id || 'claude-sonnet-4-6'
  const reasoning = parsed?.metadata?.reasoning_level || 'auto'
  const strategy = parsed?.metadata?.coordination_strategy || 'bus'
  const slice = {
    messages,
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
      currentContextTokens: totalInput,
      totalInputTokens: totalInput,
      totalOutputTokens: totalOutput,
      totalCacheReadTokens: totalCacheR,
      totalCacheWriteTokens: totalCacheW,
      totalCost: 0,
      lastTurnInputTokens: 0,
      lastTurnOutputTokens: 0,
      contextWindow: 200_000,
    },
    systemEvents: [],
    kanbanCards: {},
    busMessages: [],
    inboxEvents: [],
    artifacts: [],
    widgets: {},
    autoDispatchEnabled: false,
    model: modelId,
    reasoningLevel: reasoning,
    coordinationStrategy: strategy,
  }

  return {
    version: 1,
    id,
    title: id.replace(/^freyja:/, ''),
    model: modelId,
    workspace: parsed?.metadata?.workspace ?? '',
    createdAt: (parsed?.created_at ?? 0) * 1000 || stat?.birthtimeMs || Date.now(),
    updatedAt: (parsed?.last_activity ?? 0) * 1000 || stat?.mtimeMs || Date.now(),
    messageCount: messages.length,
    totalInputTokens: totalInput,
    totalOutputTokens: totalOutput,
    cacheReadTokens: totalCacheR,
    agentType: 'gateway-slack',
    slice,
  }
}

/** One-shot migration: rename session files written with the legacy
 *  strip-style sanitizer to the modern underscore-replace style.
 *  Idempotent + cheap (readdir + per-file stat + maybe rename); safe
 *  to call on every app start.
 *
 *  Necessary because the renderer's ``sanitizeId`` was changed from
 *  STRIP to UNDERSCORE-REPLACE after files had already been written
 *  under the old scheme. Until those files are renamed, the
 *  ``resolveSessionPath`` fallback kicks in on every lookup; after
 *  the migration runs once it becomes a pure no-op.
 *
 *  Returns counts so the caller can log how much work was done. */
export function migrateLegacySessionFiles(): { renamed: number; skipped: number } {
  ensureDir()
  let renamed = 0
  let skipped = 0
  let files: string[] = []
  try {
    files = fs.readdirSync(SESSIONS_DIR)
  } catch {
    return { renamed, skipped }
  }
  for (const f of files) {
    if (!f.endsWith('.transcript.json') && !f.endsWith('.json')) continue
    if (f.endsWith('.tmp')) continue
    if (f === SESSION_INDEX_FILE) continue
    const fullPath = path.join(SESSIONS_DIR, f)
    let sessionId: string | undefined
    try {
      const raw = fs.readFileSync(fullPath, 'utf8')
      const parsed = JSON.parse(raw)
      sessionId = parsed?.id || parsed?.session_id
    } catch {
      skipped += 1
      continue
    }
    if (!sessionId) {
      skipped += 1
      continue
    }
    const suffix = f.endsWith('.transcript.json') ? '.transcript.json' : '.json'
    const modern = path.join(SESSIONS_DIR, `${sanitizeId(sessionId)}${suffix}`)
    const legacy = path.join(SESSIONS_DIR, `${sanitizeIdLegacyStrip(sessionId)}${suffix}`)
    if (fullPath !== legacy || fullPath === modern) {
      skipped += 1
      continue
    }
    if (fs.existsSync(modern)) {
      skipped += 1
      continue
    }
    try {
      fs.renameSync(legacy, modern)
      renamed += 1
    } catch {
      skipped += 1
    }
  }
  if (renamed > 0) {
    _gatewayMirrorCache.clear()
    _indexCache = null
  }
  return { renamed, skipped }
}

export async function saveSession(data: PersistedSession): Promise<number> {
  const bytes = await writeJsonAtomic(sessionFile(data.id), data)
  await enqueueIndexWrite(async () => {
    const indexed = (await readIndexAsync()) ?? []
    return writeIndexUnlocked([
      toMeta(data),
      ...indexed.filter((row) => row.id !== data.id),
    ])
  })
  return bytes
}

export async function saveSessionIndex(rows: PersistedSessionMeta[]): Promise<number> {
  return enqueueIndexWrite(() => writeIndexUnlocked(rows))
}

export function deleteSession(id: string): boolean {
  let deleted = false
  try {
    fs.unlinkSync(sessionFile(id))
    deleted = true
  } catch {
    deleted = false
  }
  enqueueIndexWrite(async () => {
    const indexed = (await readIndexAsync()) ?? []
    return writeIndexUnlocked(indexed.filter((row) => row.id !== id))
  }).catch((err) => {
    console.error('[persistence] failed to update session index after delete:', err)
  })
  return deleted
}

/**
 * Produce a compact human-readable trace of a session. Includes every
 * user/assistant turn and every tool call with its arguments and the
 * first ~600 chars of the result text -- enough to diagnose a session
 * without wading through megabytes of base64 frame data. Each tool call
 * also lists its frame dimensions (if any) and duration.
 *
 * The original full JSON file is kept intact on disk; the exporter just
 * reads it and writes a condensed sibling file alongside.
 */
export function buildSessionTrace(id: string): string | null {
  const sess = loadSession(id)
  if (!sess) return null
  const slice = (sess as any).slice ?? {}
  const lines: string[] = []
  lines.push(`# Session ${sess.id}`)
  lines.push(`# title: ${sess.title}`)
  lines.push(`# model: ${sess.model}`)
  lines.push(`# workspace: ${sess.workspace}`)
  lines.push(`# createdAt: ${new Date(sess.createdAt).toISOString()}`)
  lines.push(`# updatedAt: ${new Date(sess.updatedAt).toISOString()}`)
  lines.push(
    `# usage: in=${sess.totalInputTokens} out=${sess.totalOutputTokens} cacheRead=${sess.cacheReadTokens}`,
  )
  lines.push('')

  const messages: any[] = Array.isArray(slice.messages) ? slice.messages : []
  const toolCalls: Record<string, any> = slice.toolCalls ?? {}

  const truncate = (s: string, n: number): string =>
    s.length > n ? s.slice(0, n) + ` ...(+${s.length - n} chars)` : s

  const describeResult = (result: any): string => {
    if (result == null) return ''
    if (typeof result === 'string') return truncate(result, 600)
    if (typeof result === 'object') {
      // List-of-blocks shape (what our new screenshot tool returns)
      if (Array.isArray(result)) {
        const parts: string[] = []
        for (const block of result) {
          if (block && typeof block === 'object') {
            if (block.type === 'text') {
              parts.push(truncate(String(block.text ?? ''), 400))
            } else if (block.type === 'image') {
              parts.push('[image block]')
            } else {
              parts.push(`[${block.type ?? 'block'}]`)
            }
          }
        }
        return parts.join(' | ')
      }
      const c = (result as any).content
      if (typeof c === 'string') return truncate(c, 600)
      if (Array.isArray(c)) return describeResult(c)
    }
    return truncate(String(result), 600)
  }

  for (const msg of messages) {
    const role = msg.role
    const parts: any[] = Array.isArray(msg.parts) ? msg.parts : []
    lines.push(`---- ${role} (${msg.id ?? ''}) ----`)
    if (msg.createdAt) {
      lines.push(`  at: ${new Date(msg.createdAt).toISOString()}`)
    }
    for (const p of parts) {
      if (p.type === 'text') {
        lines.push(`  TEXT: ${truncate(String(p.text ?? ''), 1200)}`)
      } else if (p.type === 'thinking') {
        lines.push(
          `  THINKING: ${truncate(String(p.thinking ?? p.text ?? ''), 600)}`,
        )
      } else if (p.type === 'tool_call') {
        const tcId = p.toolCallId
        const tc = toolCalls[tcId]
        if (!tc) {
          lines.push(`  TOOL_CALL ${tcId}: <missing>`)
          continue
        }
        const args = tc.arguments ?? {}
        const argsJson = JSON.stringify(args)
        const dur = tc.durationMs ?? '?'
        const err = tc.isError ? ' [ERR]' : ''
        lines.push(
          `  TOOL ${tc.name}${err} (${dur}ms) args=${truncate(argsJson, 300)}`,
        )
        const resultText = describeResult(tc.result)
        if (resultText) {
          lines.push(`    -> ${resultText.replace(/\n/g, ' | ')}`)
        }
        const frame = tc.frame
        if (frame && frame.width && frame.height) {
          lines.push(
            `    frame: ${frame.width}x${frame.height} ${frame.mimeType ?? ''} reason=${frame.reason ?? ''}`,
          )
        }
      } else if (p.type === 'image') {
        lines.push(`  IMAGE: [inline image attachment]`)
      } else {
        lines.push(`  ${p.type}: ${truncate(JSON.stringify(p), 300)}`)
      }
    }
    lines.push('')
  }
  return lines.join('\n')
}

/**
 * Three views of a session for export:
 *   - ``raw_transcript``: every Message ever exchanged, sourced from
 *     ``~/.freyja/projects/<sid>/raw_messages.jsonl`` which the bridge
 *     writes append-only on every ``transcript.append_message`` call.
 *     Never truncated by compaction.
 *   - ``live_transcript``: the renderer's current message slice (what
 *     the user sees) — the compacted view the agent is running on.
 *   - ``compactions``: every successful compaction (runtime AND agent
 *     driven) with full summary text, scope, reason, tokens before /
 *     after. Sourced from
 *     ``~/.freyja/projects/<sid>/compactions.jsonl``.
 */
interface SessionExportBundle {
  version: 2
  exportedAt: number
  metadata: {
    id: string
    title: string
    model: string
    workspace: string
    createdAt: number
    updatedAt: number
    messageCount: number
    totalInputTokens: number
    totalOutputTokens: number
    cacheReadTokens: number
    parentSessionId?: string
    childSessionIds?: string[]
    agentType?: string
    task?: string
  }
  raw_transcript: Array<{
    ts: number
    session_id: string
    turn_id: string | null
    message: unknown
  }>
  live_transcript: unknown[]
  compactions: Array<{
    ts: number
    session_id: string
    subtype: string
    trigger: string
    mechanism: string
    tokens_before: number
    tokens_after: number
    summary_text: string
    scope?: string
    reason?: string
    resumed_from_previous?: boolean
    entries_removed?: number
  }>
}

function sanitizeForFsId(id: string): string {
  return id.replace(/[^A-Za-z0-9_.-]+/g, '-').replace(/^[.-]+|[.-]+$/g, '') || 'session'
}

function readJsonl(filePath: string): unknown[] {
  if (!fs.existsSync(filePath)) return []
  try {
    const raw = fs.readFileSync(filePath, 'utf8')
    const rows: unknown[] = []
    for (const line of raw.split('\n')) {
      const trimmed = line.trim()
      if (!trimmed) continue
      try {
        rows.push(JSON.parse(trimmed))
      } catch {
        // Skip malformed lines — append-only logs are robust to a
        // truncated tail line from an interrupted write.
      }
    }
    return rows
  } catch {
    return []
  }
}

function loadSessionExportBundle(id: string): SessionExportBundle | null {
  const sess = loadSession(id)
  if (!sess) return null
  const safeId = sanitizeForFsId(id)
  const projectDir = path.join(os.homedir(), '.freyja', 'projects', safeId)
  const rawPath = path.join(projectDir, 'raw_messages.jsonl')
  const compactionsPath = path.join(projectDir, 'compactions.jsonl')

  const rawRows = readJsonl(rawPath) as Array<{
    ts: number
    session_id: string
    turn_id: string | null
    message: unknown
  }>
  const compactionRows = readJsonl(compactionsPath) as Array<{
    ts: number
    session_id: string
    subtype: string
    trigger: string
    mechanism: string
    tokens_before: number
    tokens_after: number
    summary_text: string
    scope?: string
    reason?: string
    resumed_from_previous?: boolean
    entries_removed?: number
  }>

  const slice = (sess as any).slice ?? {}
  const liveMessages: unknown[] = Array.isArray(slice.messages) ? slice.messages : []

  return {
    version: 2,
    exportedAt: Date.now(),
    metadata: {
      id: sess.id,
      title: sess.title,
      model: sess.model,
      workspace: sess.workspace,
      createdAt: sess.createdAt,
      updatedAt: sess.updatedAt,
      messageCount: sess.messageCount,
      totalInputTokens: sess.totalInputTokens,
      totalOutputTokens: sess.totalOutputTokens,
      cacheReadTokens: sess.cacheReadTokens,
      parentSessionId: (sess as any).parentSessionId,
      childSessionIds: (sess as any).childSessionIds,
      agentType: (sess as any).agentType,
      task: (sess as any).task,
    },
    raw_transcript: rawRows,
    live_transcript: liveMessages,
    compactions: compactionRows,
  }
}

/**
 * Export the session to a user-chosen destination as three files:
 *   - ``<dest>.json`` — the existing renderer-slice payload (back-compat
 *     for anything that reads the v1 format).
 *   - ``<dest>.bundle.json`` — the v2 three-view bundle: raw +
 *     live + compactions with full summary text.
 *   - ``<dest>.trace.txt`` — human-readable condensed trace.
 */
export function exportSessionToFile(
  id: string,
  destPath: string,
): {
  ok: true
  jsonPath: string
  bundlePath: string
  tracePath: string
} | { ok: false; error: string } {
  try {
    const sess = loadSession(id)
    if (!sess) return { ok: false, error: 'session not found' }
    const rawJson = JSON.stringify(sess, null, 2)
    fs.writeFileSync(destPath, rawJson, 'utf8')

    const base = destPath.replace(/\.json$/i, '')
    const bundlePath = `${base}.bundle.json`
    const tracePath = `${base}.trace.txt`

    const bundle = loadSessionExportBundle(id)
    if (bundle) {
      fs.writeFileSync(bundlePath, JSON.stringify(bundle, null, 2), 'utf8')
    } else {
      // Still write a bundle stub so callers can rely on the file existing.
      fs.writeFileSync(
        bundlePath,
        JSON.stringify(
          { version: 2, error: 'session not found', exportedAt: Date.now() },
          null,
          2,
        ),
        'utf8',
      )
    }

    const trace = buildSessionTrace(id) ?? ''
    fs.writeFileSync(tracePath, trace, 'utf8')
    return { ok: true, jsonPath: destPath, bundlePath, tracePath }
  } catch (err) {
    return { ok: false, error: String(err) }
  }
}
