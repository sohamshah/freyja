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

/** Python-compatible sanitizer. Mirrors
 *  ``bridge/transcript_persistence.py:_transcript_path`` which STRIPS
 *  invalid chars rather than replacing them with ``_``. Used when
 *  looking up files written by the Python daemon (transcripts +
 *  any legacy slice files written before sanitizeId was changed
 *  to the underscore-replacement scheme).
 *
 *  Keep this in lockstep with the Python version — if either side
 *  drifts, transcript lookups silently return null and the
 *  conversation pane shows "Session not found" for valid sessions.
 */
function sanitizeIdLegacyStrip(id: string): string {
  return id.replace(/[^a-zA-Z0-9._-]/g, '').slice(0, 160)
}

/** Resolve a file path under SESSIONS_DIR with backward-compat: try
 *  the current sanitizer first, then the legacy strip-style sanitizer.
 *  Returns the first path that exists, or the modern path if neither
 *  exists (so callers can use it for error messages). */
function resolveSessionPath(id: string, suffix: string): string {
  const modern = path.join(SESSIONS_DIR, `${sanitizeId(id)}${suffix}`)
  if (fs.existsSync(modern)) return modern
  const legacy = path.join(SESSIONS_DIR, `${sanitizeIdLegacyStrip(id)}${suffix}`)
  if (fs.existsSync(legacy)) return legacy
  return modern
}

function sessionFile(id: string): string {
  // Backward-compat: slice files written before sanitizeId was changed
  // to underscore-replace are stored under the legacy strip-style
  // filename. Use the resolver so loadSession finds them either way.
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
  /** Top-level preset id this session was started under (e.g.
   *  "claude-code", "codex"). Persisted so the preset's persona +
   *  tool surface can be re-applied on resume. */
  presetId?: string
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

// Cache the parsed _index.json keyed by file mtime. The index can grow
// to ~2MB on a userwith many sessions, and listSessions is in the
// hot path (called from the renderer's 30s sidebar refresh poll +
// every focus event). Re-reading + JSON-parsing 2MB on every poll
// for no semantic gain is wasteful.
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
    // Cheap fast-path: the cached rows array is sorted+deduped already.
    // Clone the array to avoid callers mutating cache state.
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

/** One-shot migration: rename session files written with the legacy
 *  strip-style sanitizer (colons / other invalid chars stripped) to
 *  the modern underscore-replace style. After running once, the
 *  legacy `resolveSessionPath` fallback becomes a no-op because all
 *  files are at the modern path.
 *
 *  Idempotent + safe to run on every app start — only renames files
 *  whose modern target path doesn't already exist (so a half-done
 *  migration can resume cleanly).
 *
 *  We need to read each candidate file to extract its session_id
 *  before we can compute the modern target path. Cheap because we
 *  only do it for files whose CURRENT name doesn't contain an
 *  underscore (modern names always do for gateway sessions), giving
 *  us a fast filter that skips already-migrated files.
 */
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
    // Only consider session-shaped files.
    if (!f.endsWith('.transcript.json') && !f.endsWith('.json')) continue
    if (f.endsWith('.tmp')) continue
    if (f === SESSION_INDEX_FILE) continue
    const fullPath = path.join(SESSIONS_DIR, f)
    // Read the session_id from inside the file. Both slice files and
    // transcript files carry their id internally.
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
    // Compute the expected modern + legacy filenames; rename if the
    // current file is at the legacy path AND the modern path is free.
    const suffix = f.endsWith('.transcript.json') ? '.transcript.json' : '.json'
    const modern = path.join(SESSIONS_DIR, `${sanitizeId(sessionId)}${suffix}`)
    const legacy = path.join(SESSIONS_DIR, `${sanitizeIdLegacyStrip(sessionId)}${suffix}`)
    if (fullPath !== legacy) {
      // Current file isn't at the legacy path — already migrated or
      // some other naming. Skip.
      skipped += 1
      continue
    }
    if (fullPath === modern) {
      // Legacy and modern collapse to the same string (no special
      // chars in the id). Nothing to do.
      skipped += 1
      continue
    }
    if (fs.existsSync(modern)) {
      // Modern target already exists. Don't overwrite — leave the
      // legacy file in place; the resolver will pick one of them.
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
    // Cache invalidated by file moves.
    _gatewayMirrorCache.clear()
    _indexCache = null
  }
  return { renamed, skipped }
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

// Cache of parsed gateway-transcript metadata keyed by absolute file
// path. Each entry remembers the file's mtime at the time of parsing;
// on the next scan, we skip the file's read+parse if mtime hasn't
// changed. Without this cache, the renderer's 30s polling scans +
// JSON-parses every transcript file in ~/.freyja/sessions/ on every
// poll — including hundreds of unrelated desktop / sub-agent
// transcripts. On a userwith 300+ transcript files (totaling
// hundreds of MB), that's ~300ms of disk I/O + parse on every
// poll. With this cache, repeat polls are O(N stat() calls) +
// re-parse only the files that actually changed.
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
    // Fast-path filter: gateway session ids look like
    // ``freyja:slack:T...:dm:D...``, which sanitizes to a filename
    // starting with ``freyjaslack`` (or future ``freyja<platform>``).
    // 99% of transcripts on a typical install are desktop / sub-agent
    // sessions whose filenames don't start with ``freyja``; skipping
    // them BEFORE we read+parse saves the expensive work.
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
      // File unchanged since last scan — reuse the cached metadata.
      if (cached.meta && !known.has(cached.meta.id)) {
        out.push(cached.meta)
      }
      continue
    }
    // Parse path: file is new or modified since last scan.
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
    // Try to extract a friendly title — for slack sessions the id
    // already carries the workspace + chat. Strip the freyja: prefix
    // for display.
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
      // Mark gateway-mirrored sessions so the sidebar can render them
      // with a [slack] / [telegram] / etc badge. We piggyback on the
      // optional `task` field which is also where regular subagent
      // sessions store their spawn task.
      task: '(gateway-mirrored — read-only mirror of an external chat)',
      // Tag as gateway-mirrored so the renderer's sidebar can detect
      // it. New custom field — see PersistedSession type.
      agentType: 'gateway-slack',
    }
    _gatewayMirrorCache.set(fullPath, { mtimeMs, meta })
    if (!known.has(sessionId)) out.push(meta)
  }
  // Evict cache entries for files that have been deleted off disk so
  // the cache doesn't grow unbounded (e.g. when the operator manually
  // cleans ~/.freyja/sessions/).
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
    // Fall back to synthesizing from the bridge-owned transcript file.
    // Gateway-originated sessions (Slack DMs, channel threads) never get
    // a renderer-owned slice on disk because they're persisted by the
    // daemon, not by the desktop runner. Build a read-only slice from
    // the raw transcript so the desktop can at least show the
    // conversation when the user clicks a `gateway-slack` row in the
    // sidebar.
    return synthesizeSessionFromTranscript(id)
  }
}

/** Quick heuristic: did this tool result represent an error? Our tool
 *  layer's convention is to return text that begins with "Error" when
 *  things fail (image_generation_tool.py, file tools, etc.). Not 100%
 *  reliable — some tools return success-prefixed text describing a
 *  caught error — but good enough for the synthesizer's "show a red
 *  chip vs. green chip" decision. */
function _looksLikeToolError(content: string): boolean {
  if (!content) return false
  const head = content.trimStart().toLowerCase()
  return head.startsWith('error') || head.startsWith('exception')
}

/** Parse "File saved to `<path>`" references out of a tool's result
 *  text and read each one (if it's an image, exists, and is small
 *  enough) as a data URL for inline rendering in the conversation
 *  pane. Same regex pattern the Slack stream consumer uses to detect
 *  generated-image outputs. */
const _FILE_SAVED_RE = /File saved to `([^`]+)`/g
const _IMAGE_EXTS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'])
const _MAX_INLINE_IMAGE_BYTES = 8 * 1024 * 1024  // 8MB — cap to keep slice JSON manageable

function _extractImagesFromResultText(
  resultText: string,
  takenAtMs: number,
): Array<Record<string, unknown>> {
  if (!resultText) return []
  const out: Array<Record<string, unknown>> = []
  _FILE_SAVED_RE.lastIndex = 0  // reset (global regex shared across calls)
  let m: RegExpExecArray | null
  while ((m = _FILE_SAVED_RE.exec(resultText)) !== null) {
    const filePath = m[1]
    const ext = path.extname(filePath).toLowerCase()
    if (!_IMAGE_EXTS.has(ext)) continue
    try {
      const st = fs.statSync(filePath)
      if (!st.isFile()) continue
      if (st.size > _MAX_INLINE_IMAGE_BYTES) continue
      const bytes = fs.readFileSync(filePath)
      const mimeType = _extToMime(ext)
      const dataUrl = `data:${mimeType};base64,${bytes.toString('base64')}`
      out.push({
        pngBase64: dataUrl,
        mimeType,
        width: 0,   // unknown without parsing the image header
        height: 0,
        takenAt: takenAtMs,
        byteSize: st.size,
        label: path.basename(filePath),
      })
    } catch {
      // File missing or unreadable — skip silently.
    }
  }
  return out
}

function _extToMime(ext: string): string {
  switch (ext) {
    case '.png': return 'image/png'
    case '.jpg':
    case '.jpeg': return 'image/jpeg'
    case '.gif': return 'image/gif'
    case '.webp': return 'image/webp'
    case '.bmp': return 'image/bmp'
    default: return 'application/octet-stream'
  }
}

/** Build a read-only PersistedSession from a gateway-owned transcript
 *  file. Used when the renderer asks for a session that only exists
 *  as `<id>.transcript.json` (no slice file produced by the desktop
 *  runner). Reconstructs:
 *
 *    · user + assistant + thinking turns as messages
 *    · tool calls as proper ToolCallRecord entries (so the
 *      conversation pane renders chips, arguments, durations,
 *      isError states, etc. instead of `[tool_use: name]` text stubs)
 *    · tool results matched to their tool_call_id from later
 *      ``role: "tool_result"`` transcript entries
 *    · generated images extracted from result text (``File saved to
 *      `<path>` ``) — read from disk and inlined as data URLs into
 *      ToolCallRecord.resultImages so the viewer shows the actual
 *      image, not just the prose
 *    · token totals aggregated across all assistant turns
 *
 *  Sub-agents, kanban, bus messages, etc. are left empty — the
 *  transcript doesn't carry them and they're not meaningful for an
 *  external chat reflection. */
function synthesizeSessionFromTranscript(id: string): PersistedSession | null {
  // Use the legacy-aware resolver — Python writes the transcript with
  // the STRIP-style sanitizer, while the renderer's current
  // sanitizeId uses underscore replacement. Without this resolver,
  // every gateway-mirrored session click would compute the wrong
  // filename and silently fall through to "session not found".
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

  // Pass 1 — collect all tool_result entries keyed by tool_call_id so
  // we can stitch them onto the assistant message that fired the call.
  // tool_result entries come AFTER the assistant message in transcript
  // order; we need them resolved before we walk the assistant turns
  // (otherwise we'd emit ToolCallRecord with status=running for tools
  // that already finished).
  const toolResultsById = new Map<string, { content: string; timestampMs: number }>()
  for (const e of entries) {
    const msg = e?.message
    if (msg?.role === 'tool_result' && typeof msg.tool_call_id === 'string') {
      toolResultsById.set(msg.tool_call_id, {
        content: typeof msg.content === 'string' ? msg.content : '',
        timestampMs: Math.floor((e.timestamp ?? 0) * 1000),
      })
    }
  }

  // Pass 2 — walk assistant + user entries and synthesize messages +
  // ToolCallRecord map. tool_result entries are skipped here since
  // they were absorbed in pass 1.
  const messages: any[] = []
  const toolCalls: Record<string, any> = {}
  const toolCallOrder: string[] = []
  let totalInput = 0, totalOutput = 0, totalCacheR = 0, totalCacheW = 0

  for (const e of entries) {
    const msg = e?.message
    if (!msg) continue
    if (msg.role === 'tool_result') continue
    if (msg.role !== 'user' && msg.role !== 'assistant') continue

    const entryTimeMs = Math.floor((e.timestamp ?? 0) * 1000)
    const parts: any[] = []

    // Thinking blocks render first (above the text + tool calls). The
    // desktop view shows thinking as a foldable block above the
    // assistant's response.
    if (Array.isArray(msg.thinking_blocks)) {
      for (const tb of msg.thinking_blocks) {
        if (typeof tb?.thinking === 'string' && tb.thinking.length > 0) {
          parts.push({ type: 'thinking', text: tb.thinking })
        }
      }
    }

    // Body text. Content is usually a string in the transcript;
    // handle the legacy array-of-blocks shape defensively in case
    // older transcripts use it.
    if (typeof msg.content === 'string' && msg.content.length > 0) {
      parts.push({ type: 'text', text: msg.content })
    } else if (Array.isArray(msg.content)) {
      for (const block of msg.content) {
        if (typeof block === 'string') {
          parts.push({ type: 'text', text: block })
        } else if (block?.type === 'text' && typeof block?.text === 'string') {
          parts.push({ type: 'text', text: block.text })
        }
        // tool_use / tool_result blocks in content are legacy /
        // anthropic-shape; we handle the modern tool_calls field
        // below. Don't double-render here.
      }
    }

    // Tool calls — OpenAI-style array on assistant messages.
    if (msg.role === 'assistant' && Array.isArray(msg.tool_calls)) {
      for (const tc of msg.tool_calls) {
        if (!tc?.id || typeof tc.id !== 'string') continue
        const result = toolResultsById.get(tc.id)
        const resultContent = result?.content ?? ''
        const isError = _looksLikeToolError(resultContent)
        const durationMs = result
          ? Math.max(0, result.timestampMs - entryTimeMs)
          : undefined
        const resultImages = _extractImagesFromResultText(resultContent, entryTimeMs)
        const record = {
          id: tc.id,
          name: typeof tc.name === 'string' ? tc.name : 'unknown',
          arguments: tc.arguments && typeof tc.arguments === 'object'
            ? tc.arguments
            : undefined,
          status: result
            ? (isError ? 'error' : 'success')
            : 'success',  // missing result = optimistic success; transcript was probably truncated
          result: resultContent || undefined,
          isError,
          durationMs,
          startedAt: entryTimeMs,
          ...(resultImages.length > 0 ? { resultImages } : {}),
        }
        toolCalls[tc.id] = record
        toolCallOrder.push(tc.id)
        parts.push({ type: 'tool_call', toolCallId: tc.id })
      }
    }

    if (parts.length === 0) continue
    if (msg.role === 'assistant') {
      totalInput += msg.input_tokens ?? 0
      totalOutput += msg.output_tokens ?? 0
      totalCacheR += msg.cache_read_tokens ?? 0
      totalCacheW += msg.cache_write_tokens ?? 0
    }
    messages.push({
      id: e.id || String(messages.length),
      role: msg.role,
      parts,
      createdAt: entryTimeMs,
      inputTokens: msg.input_tokens,
      outputTokens: msg.output_tokens,
      cacheReadTokens: msg.cache_read_tokens,
      cacheWriteTokens: msg.cache_write_tokens,
    })
  }

  // Build a slice that contains EVERY required SessionSlice field
  // (see ``src/renderer/state/store.ts:73``). Missing fields cause
  // the renderer's `setActive` to crash silently when it spreads
  // ``archivedSlice`` onto root state — the conversation pane then
  // shows the LAST active session's content instead of the one the
  // user clicked, which looks like "the click did nothing".
  const modelId = parsed?.metadata?.model_id || 'claude-sonnet-4-6'
  const reasoning = parsed?.metadata?.reasoning_level || 'auto'
  const strategy = parsed?.metadata?.coordination_strategy || 'bus'
  const slice = {
    messages,
    currentStreamingMessageId: null,
    currentTurnId: null,
    thinking: '',
    isStreaming: false,
    toolCalls,
    toolCallOrder,
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
    // These four were missing — renderer requires them as
    // non-optional. autoDispatchEnabled gates a kanban-board
    // toggle; the other three populate the title-bar pill state
    // and the bridge-side session reconfigure on switch_session.
    autoDispatchEnabled: false,
    model: modelId,
    reasoningLevel: reasoning,
    coordinationStrategy: strategy,
  }

  return {
    version: 1,
    id,
    title: id.replace(/^freyja:/, ''),
    model: parsed?.metadata?.model_id ?? '',
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
