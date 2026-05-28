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

function sessionFile(id: string): string {
  return path.join(SESSIONS_DIR, `${sanitizeId(id)}.json`)
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

function readIndexSync(): PersistedSessionMeta[] | null {
  ensureDir()
  try {
    const raw = fs.readFileSync(sessionIndexFile(), 'utf8')
    const parsed = JSON.parse(raw) as PersistedSessionIndex
    if (!parsed || parsed.version !== 1 || !Array.isArray(parsed.sessions)) {
      return null
    }
    return sortSessions(dedupeSessions(parsed.sessions))
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
  for (const f of files) {
    if (!f.endsWith('.transcript.json')) continue
    const fullPath = path.join(SESSIONS_DIR, f)
    let raw: string
    try {
      raw = fs.readFileSync(fullPath, 'utf8')
    } catch {
      continue
    }
    let parsed: any
    try {
      parsed = JSON.parse(raw)
    } catch {
      continue
    }
    const sessionId: string | undefined = parsed?.session_id
    if (!sessionId) continue
    // Only mirror gateway-shaped IDs; don't re-surface every random
    // transcript in case the user has old files lying around.
    if (!sessionId.startsWith('freyja:')) continue
    if (known.has(sessionId)) continue
    let stat: fs.Stats | null = null
    try {
      stat = fs.statSync(fullPath)
    } catch {
      // ignore
    }
    const messageCount = Array.isArray(parsed?.transcript?.entries)
      ? parsed.transcript.entries.filter((e: any) => e?.message).length
      : 0
    // Try to extract a friendly title — for slack sessions the id
    // already carries the workspace + chat. Strip the freyja: prefix
    // for display.
    const title = sessionId.replace(/^freyja:/, '')
    out.push({
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
    })
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
    return null
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
