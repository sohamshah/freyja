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
  const indexed = readIndexSync()
  if (indexed) return indexed

  const rows: PersistedSessionMeta[] = []
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
  return sortSessions(dedupeSessions(rows))
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
 * Export the raw JSON session file to a user-chosen destination. Also
 * writes a `.trace.txt` sibling with the condensed human-readable trace
 * so future diagnosis is one file-read away.
 */
export function exportSessionToFile(
  id: string,
  destPath: string,
): { ok: true; jsonPath: string; tracePath: string } | { ok: false; error: string } {
  try {
    const sess = loadSession(id)
    if (!sess) return { ok: false, error: 'session not found' }
    const rawJson = JSON.stringify(sess, null, 2)
    fs.writeFileSync(destPath, rawJson, 'utf8')
    const tracePath = destPath.replace(/\.json$/i, '') + '.trace.txt'
    const trace = buildSessionTrace(id) ?? ''
    fs.writeFileSync(tracePath, trace, 'utf8')
    return { ok: true, jsonPath: destPath, tracePath }
  } catch (err) {
    return { ok: false, error: String(err) }
  }
}
