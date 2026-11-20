import { Worker } from 'node:worker_threads'

import type { SessionSearchResult } from '../shared/events.js'

/**
 * Full-text search over every session on disk, for the sidebar's search box.
 *
 * The renderer can only search transcripts it has loaded, so a word that is
 * only in an old, never-opened session found nothing. This searches the files.
 *
 * What is searched
 * ────────────────
 * The same text the sidebar searches in memory: the title plus every `text`
 * part of every message in the slice (`<id>.json`). Thinking, tool calls and
 * images are not text parts, so they are left out. A gateway session (Slack)
 * has no slice, only `<id>.transcript.json`; its text is taken the way
 * `synthesizeSessionFromTranscript` builds the messages the app shows.
 *
 * Match rule
 * ──────────
 * The sidebar's `matchesAllTokens`: every whitespace token of the query must
 * be a prefix of some word, where words are the lowercased text split on
 * `[\s\W_]+`. Each file is kept as its unique words joined by single spaces,
 * with a leading space, so "token is a prefix of some word" is exactly
 * `hay.includes(' ' + token)`. Words never hold a space, so a hit always
 * starts at a word.
 *
 * Cost
 * ────
 * Session files run to tens of MB, about 70 % base64 images, and there are
 * over a thousand. Reading, parsing and flattening them happens on a worker
 * thread, never on this one (it routes the window's input). The worker keeps
 * one entry per file keyed by (mtimeMs, size), so a repeat query only re-reads
 * the files that changed since the last one. The entries are also saved to
 * `cacheFile`, at most once a minute, so the first search after a restart
 * does not read every session again.
 *
 * The worker answers one query at a time. While it is busy, only the newest
 * query waits; an older waiting query is answered `superseded` (the renderer
 * drops stale answers anyway).
 */

// Plain JS, run with `eval: true` like the persistence writer: the main
// process is bundled to one file, so a worker file would need its own build
// entry and packaging. String.raw keeps the regex backslashes as written.
const WORKER_SOURCE = String.raw`
const { parentPort, workerData } = require('node:worker_threads')
const fs = require('node:fs')
const path = require('node:path')

const CACHE_VERSION = 1
const SAVE_EVERY_MS = 60000
const TRANSCRIPT = '.transcript.json'
// Per-session sidecars that sit next to the slice and are not sessions.
const SIDECAR_RE = /\.(transcript|subagent|inbox|goal|mcp|cadence)\.json$/
// persistence.ts SYNTHETIC_WAKE_PREFIXES: wake prompts the app does not show.
const WAKE_PREFIXES = [
  '[system] One or more inter-agent messages were just delivered',
  '[system] Background sub-agent work you dispatched just finished',
  '[system] New items arrived in your inbox while you were idle',
]
const APPSHOT_RE = /<appshot((?:\s+[a-z-]+="[^"]*")*)\s*>\n?([\s\S]*?)\n?<\/appshot>/g

const sessionsDir = workerData.sessionsDir
const cacheFile = workerData.cacheFile || null

/** file name -> { mtimeMs, size, id, hay } */
let files = null
let dirty = false
let lastSaveAt = 0
let saveTimer = null

/** The unique words of the text, as the sidebar splits it, as ' w1 w2 ...'. */
function words(text) {
  const seen = new Set(text.toLowerCase().split(/[\s\W_]+/))
  seen.delete('')
  return seen.size > 0 ? ' ' + Array.from(seen).join(' ') : ''
}

/** Sidebar.tsx sessionSearchIndex: the title and every text part. */
function sliceText(parsed) {
  const out = [typeof parsed.title === 'string' ? parsed.title : '']
  const messages = parsed.slice && Array.isArray(parsed.slice.messages) ? parsed.slice.messages : []
  for (const m of messages) {
    const parts = m && Array.isArray(m.parts) ? m.parts : null
    if (!parts) continue
    for (const p of parts) {
      if (p && p.type === 'text' && typeof p.text === 'string' && p.text) out.push(p.text)
    }
  }
  return out.join('\n')
}

function unescapeXml(s) {
  return s.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&amp;/g, '&')
}

/** shared/appshot.ts stripAppshotBlocks: a block shows as one placeholder line. */
function stripAppshots(text) {
  if (text.indexOf('<appshot') < 0) return text
  return text.replace(APPSHOT_RE, function (_block, attrs) {
    const a = {}
    const re = /([a-z-]+)="([^"]*)"/g
    let m
    while ((m = re.exec(attrs))) a[m[1]] = unescapeXml(m[2])
    return ' [Appshot: ' + (a['app'] || '') + ', ' + (a['window-title'] || '') + '] '
  })
}

/** persistence.ts synthesizeSessionFromTranscript: the text parts it builds. */
function transcriptText(parsed) {
  const out = [parsed.session_id.replace(/^freyja:/, '')]
  const entries = parsed.transcript && Array.isArray(parsed.transcript.entries) ? parsed.transcript.entries : []
  for (const e of entries) {
    const msg = e && e.message
    if (!msg || (msg.role !== 'user' && msg.role !== 'assistant')) continue
    let texts = []
    if (typeof msg.content === 'string') texts.push(msg.content)
    else if (Array.isArray(msg.content)) {
      for (const b of msg.content) {
        if (typeof b === 'string') texts.push(b)
        else if (!b || typeof b !== 'object') continue
        else if (b.type === 'text' && typeof b.text === 'string') texts.push(b.text)
        else if (b.type === 'tool_use') texts.push('[tool_use: ' + (b.name || 'unknown') + ']')
        else if (b.type === 'tool_result') texts.push(typeof b.content === 'string' ? b.content : '[tool_result]')
      }
    }
    if (msg.role === 'user') {
      texts = texts
        .map((t) => stripAppshots(t.replace(/<system-reminder>[\s\S]*?<\/system-reminder>\s*/g, '')))
        .filter((t) => t.trim())
      if (texts.length === 1 && WAKE_PREFIXES.some((p) => texts[0].startsWith(p))) continue
    }
    for (const t of texts) out.push(t)
  }
  return out.join('\n')
}

function indexFile(file, kind) {
  const none = { id: null, hay: '' }
  let parsed
  try {
    parsed = JSON.parse(fs.readFileSync(file, 'utf8'))
  } catch {
    // Unreadable, torn, or too big for one string. Kept as an empty entry
    // under this (mtime, size), so it is retried only when the file changes.
    return none
  }
  if (!parsed || typeof parsed !== 'object') return none
  if (kind === 'slice') {
    if (parsed.version !== 1 || typeof parsed.id !== 'string' || !parsed.id) return none
    return { id: parsed.id, hay: words(sliceText(parsed)) }
  }
  if (typeof parsed.session_id !== 'string' || !parsed.session_id) return none
  return { id: parsed.session_id, hay: words(transcriptText(parsed)) }
}

function loadCache() {
  files = new Map()
  if (!cacheFile) return
  try {
    const parsed = JSON.parse(fs.readFileSync(cacheFile, 'utf8'))
    if (!parsed || parsed.version !== CACHE_VERSION || parsed.dir !== sessionsDir) return
    const rows = parsed.files && typeof parsed.files === 'object' ? parsed.files : {}
    for (const name of Object.keys(rows)) {
      const e = rows[name]
      if (!e || typeof e.mtimeMs !== 'number' || typeof e.size !== 'number' || typeof e.hay !== 'string') continue
      files.set(name, { mtimeMs: e.mtimeMs, size: e.size, id: typeof e.id === 'string' ? e.id : null, hay: e.hay })
    }
  } catch {
    // No cache yet, or a stale one: this pass reads every file.
  }
}

function save() {
  saveTimer = null
  lastSaveAt = Date.now()
  if (!dirty || !cacheFile) return
  const tmp = cacheFile + '.' + process.pid + '.' + Date.now() + '.tmp'
  try {
    const rows = {}
    for (const [name, e] of files) rows[name] = e
    fs.writeFileSync(tmp, JSON.stringify({ version: CACHE_VERSION, dir: sessionsDir, files: rows }), 'utf8')
    fs.renameSync(tmp, cacheFile)
    dirty = false
  } catch {
    try { fs.rmSync(tmp, { force: true }) } catch {}
  }
}

/** After the reply is posted, never before; at most once a minute. */
function scheduleSave() {
  if (!cacheFile || saveTimer) return
  saveTimer = setTimeout(save, Math.max(0, lastSaveAt + SAVE_EVERY_MS - Date.now()))
}

/** Re-read only the files whose (mtime, size) moved; drop the ones gone. */
function refresh() {
  let names = []
  try {
    names = fs.readdirSync(sessionsDir)
  } catch {
    names = []
  }
  const present = new Set(names)
  const seen = new Set()
  let reread = 0
  for (const name of names) {
    let kind
    if (name.endsWith(TRANSCRIPT)) {
      // A session the app saved is searched by its slice: that is what the
      // app shows, and a desktop transcript can be far bigger.
      if (present.has(name.slice(0, -TRANSCRIPT.length) + '.json')) continue
      kind = 'transcript'
    } else if (name.endsWith('.json') && name[0] !== '_' && !SIDECAR_RE.test(name)) {
      kind = 'slice'
    } else {
      continue
    }
    const file = path.join(sessionsDir, name)
    let st
    try {
      st = fs.statSync(file)
    } catch {
      continue
    }
    if (!st.isFile()) continue
    seen.add(name)
    const prev = files.get(name)
    if (prev && prev.mtimeMs === st.mtimeMs && prev.size === st.size) continue
    const entry = indexFile(file, kind)
    files.set(name, { mtimeMs: st.mtimeMs, size: st.size, id: entry.id, hay: entry.hay })
    reread += 1
    dirty = true
  }
  for (const name of Array.from(files.keys())) {
    if (!seen.has(name)) {
      files.delete(name)
      dirty = true
    }
  }
  return reread
}

function search(query) {
  if (!files) loadCache()
  const reread = refresh()
  if (dirty) scheduleSave()
  const tokens = String(query || '').toLowerCase().split(/\s+/).filter(Boolean).map((t) => ' ' + t)
  const ids = new Set()
  let sessions = 0
  for (const e of files.values()) {
    if (!e.id) continue
    sessions += 1
    if (tokens.length === 0 || !e.hay) continue
    let ok = true
    for (const t of tokens) {
      if (e.hay.indexOf(t) < 0) {
        ok = false
        break
      }
    }
    if (ok) ids.add(e.id)
  }
  return { ok: true, ids: Array.from(ids), sessions, reread }
}

parentPort.on('message', (msg) => {
  let reply
  try {
    reply = search(msg.query)
  } catch (err) {
    reply = { ok: false, ids: [], error: String((err && err.stack) || err) }
  }
  parentPort.postMessage(Object.assign({ id: msg.id }, reply))
})
`

/** A worker that keeps dying (a file too big for its heap, say) is not
 *  respawned forever: after this many, search answers ok:false and the
 *  sidebar keeps its in-memory matches. */
const MAX_WORKER_FAILURES = 3

type Waiter = (result: SessionSearchResult) => void

export interface SessionSearcher {
  /** Ids of sessions whose title or text holds every token of `query`. An
   *  empty query brings the index up to date and returns no ids. */
  search(query: string): Promise<SessionSearchResult>
  /** Stop the worker. Waiting queries are answered ok:false. */
  close(): Promise<void>
}

export function createSessionSearcher(opts: {
  sessionsDir: string
  /** Where the worker saves its per-file entries; null keeps them in memory. */
  cacheFile?: string | null
}): SessionSearcher {
  let worker: Worker | null = null
  let failures = 0
  let closed = false
  let nextId = 0
  let inflight: { id: number; waiters: Waiter[] } | null = null
  let queued: { query: string; waiters: Waiter[] } | null = null

  const fail = (waiters: Waiter[], error: string) => {
    for (const w of waiters) w({ ok: false, ids: [], error })
  }

  const spawn = (): Worker | null => {
    if (worker) return worker
    if (closed || failures >= MAX_WORKER_FAILURES) return null
    try {
      const w = new Worker(WORKER_SOURCE, {
        eval: true,
        workerData: { sessionsDir: opts.sessionsDir, cacheFile: opts.cacheFile ?? null },
      })
      // Never holds the app open; a cache save it had pending is redone
      // from the files next time.
      w.unref()
      w.on('message', (msg: SessionSearchResult & { id: number }) => {
        if (!inflight || msg.id !== inflight.id) return
        const { waiters } = inflight
        inflight = null
        const { id: _id, ...result } = msg
        for (const done of waiters) done(result)
        pump()
      })
      const died = (err: unknown) => {
        if (worker !== w) return
        worker = null
        failures += 1
        console.warn('[sessionSearch] worker stopped', err)
        if (inflight) {
          const { waiters } = inflight
          inflight = null
          fail(waiters, `session search worker stopped: ${String(err)}`)
        }
        pump()
      }
      w.on('error', died)
      w.on('exit', (code) => died(new Error(`exit ${code}`)))
      worker = w
      return w
    } catch (err) {
      failures += 1
      console.warn('[sessionSearch] could not start worker', err)
      return null
    }
  }

  const pump = () => {
    if (inflight || !queued) return
    const job = queued
    queued = null
    const w = spawn()
    if (!w) {
      fail(job.waiters, 'session search unavailable')
      return
    }
    const id = ++nextId
    inflight = { id, waiters: job.waiters }
    w.postMessage({ id, query: job.query })
  }

  return {
    search(query: string) {
      return new Promise<SessionSearchResult>((resolve) => {
        // A warm-up (empty query) only asks for a refresh, and the query
        // already running or waiting does one. It must never push a real
        // query out of the queue.
        if (!query.trim() && (queued || inflight)) {
          resolve({ ok: true, ids: [] })
          return
        }
        if (queued) {
          if (queued.query === query) {
            queued.waiters.push(resolve)
            return
          }
          fail(queued.waiters, 'superseded')
        }
        queued = { query, waiters: [resolve] }
        pump()
      })
    },
    async close() {
      closed = true
      const w = worker
      worker = null
      if (queued) fail(queued.waiters, 'closed')
      if (inflight) fail(inflight.waiters, 'closed')
      queued = null
      inflight = null
      if (w) await w.terminate()
    },
  }
}
