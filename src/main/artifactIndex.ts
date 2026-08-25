import fs from 'node:fs'
import fsp from 'node:fs/promises'
import path from 'node:path'
import os from 'node:os'
import crypto from 'node:crypto'

import { extractArtifactMeta } from '../shared/artifactMeta.js'
import type {
  ArtifactIndexRow,
  ArtifactIndexFacets,
  ArtifactIndexStats,
  ArtifactQuery,
  ArtifactQueryResult,
  ArtifactRevisionRow,
} from '../shared/events.js'

/**
 * Global artifact index — every file every session ever produced, in one
 * queryable list.
 *
 * Why this lives in the main process and not in the renderer or the bridge
 * ─────────────────────────────────────────────────────────────────────────
 * The renderer's `s.artifacts` is session-scoped: it is reconstructed from
 * live bridge events and persisted inside that session's slice JSON. Cold
 * sessions have nothing, and the slice JSONs total 1.8 GB across ~1 250 files
 * — enumerating them is orders of magnitude slower than the alternative.
 *
 * The real record is python's append-only `manifest.jsonl`, one per project
 * dir. Measured on this machine: 512 project dirs, 143 with a manifest, 7 814
 * rows collapsing to 1 834 unique paths, and a full `cat` of every manifest in
 * ~80 ms. That is cheap enough to scan from node directly, so no SQLite (which
 * Electron 33 can't offer anyway — Node 20.18, no `node:sqlite`) and no python
 * subprocess.
 *
 * What actually costs something
 * ─────────────────────────────
 * Not the scan — the per-artifact title/excerpt. The session-scoped viewer
 * gets those by firing one `artifact:read` per artifact from `useArtifactMeta`,
 * which at global scale is ~1 700 IPC round trips over 323 MB. So this index
 * extracts title/excerpt ONCE during ingest, caches them keyed by the file's
 * sha256, and hands the renderer rows that are already renderable. Full
 * content stays lazy: it is read only when a row is opened.
 *
 * Freshness model
 * ───────────────
 * Ingest is incremental. Each project dir's manifest is fingerprinted by
 * (size, mtimeMs); unchanged manifests are skipped entirely, so a steady-state
 * refresh is ~512 stat calls (single-digit ms). A changed manifest is re-read
 * whole — the largest is 658 KB — and that project's contribution to the index
 * is replaced wholesale.
 */

// Honours FREYJA_HOME the same way main.ts's skills root does, which is also
// what lets the index test point at a fixture tree instead of the real one.
const FREYJA_DIR = process.env.FREYJA_HOME ?? path.join(os.homedir(), '.freyja')
const PROJECTS_DIR = path.join(FREYJA_DIR, 'projects')
const SESSIONS_DIR = path.join(FREYJA_DIR, 'sessions')
const CACHE_FILE = path.join(FREYJA_DIR, 'artifact-index.json')

const CACHE_VERSION = 1

/** Bump when the meta extractor changes shape, so cached excerpts are redone. */
const META_VERSION = 1

/** How much of a file's head is enough to pull a title + first paragraph. */
const META_READ_BYTES = 64 * 1024

/** Concurrent meta reads. Matches the renderer hook's pool, doubled — main
 *  has no frame budget to protect. */
const META_CONCURRENCY = 12

/**
 * Hard ceiling on rows returned by one query.
 *
 * The browser pages by growing `limit` and keeps one contiguous array, which
 * keeps the list simple; the cost is that the cap is also the furthest the
 * operator can scroll. 20 000 rows is ~8 MB over IPC — beyond that the answer
 * is a better filter, not a bigger payload, and `cappedAt` says so out loud.
 */
const MAX_PAGE_ROWS = 20_000

/** A query that arrives within this window of the last ingest skips re-statting
 *  the manifests. Keeps rapid keystroke-driven queries free. */
const INGEST_MIN_INTERVAL_MS = 4_000

/** File types worth reading for a title/excerpt. Everything else gets its
 *  filename as the title and no excerpt. Mirrors `useArtifactMeta`'s TEXTUAL
 *  set plus the extensions the agents actually produce. */
const TEXTUAL_TYPES = new Set([
  'md', 'markdown', 'txt', 'log', 'json', 'yaml', 'yml', 'toml',
  'csv', 'tsv', 'html', 'htm', 'svg', 'xml', 'tex',
  'ts', 'tsx', 'js', 'jsx', 'mjs', 'cjs', 'py', 'rs', 'go', 'java',
  'c', 'h', 'cpp', 'hpp', 'cs', 'rb', 'php', 'swift', 'kt', 'scala',
  'css', 'scss', 'less', 'sh', 'bash', 'zsh', 'fish', 'sql', 'glsl',
  'ini', 'cfg', 'conf', 'env', 'gitignore', 'dockerfile', 'makefile',
])

/** One row as python's `SessionArtifactStore.record_file` writes it. Field
 *  names are python's, NOT the renderer's `ArtifactRecord` (which calls
 *  `creatorId` `creator`). Do not conflate the two. */
interface ManifestRow {
  id?: string
  sessionId?: string
  creatorId?: string
  creatorLabel?: string
  operation?: string
  source?: string
  path?: string
  filename?: string
  fileType?: string
  createdAt?: number
  toolCallId?: string | null
  changeSetId?: string | null
  metadata?: Record<string, unknown> | null
  exists?: boolean
  bytes?: number
  lines?: number | null
  sha256?: string | null
}

interface MetaCacheEntry {
  v: number
  /** Content fingerprint the excerpt was taken from. */
  sha256: string | null
  /** Fallback fingerprint when the manifest carried no hash. */
  mtimeMs: number
  size: number
  title: string
  excerpt: string
  agentType?: string
  task?: string
}

interface ProjectEntry {
  projectId: string
  sessionId: string
  size: number
  mtimeMs: number
  /** One row per unique path within this project, newest revision folded in. */
  rows: ArtifactIndexRow[]
}

interface IndexCache {
  version: number
  builtAt: number
  projects: Record<string, ProjectEntry>
  meta: Record<string, MetaCacheEntry>
}

// ── module state ─────────────────────────────────────────────────────────

let cache: IndexCache | null = null
let flat: ArtifactIndexRow[] = []
/** Lowercased search haystack per row, parallel to `flat`. */
let haystacks: string[] = []
let lastIngestAt = 0
/** Set when something known-external changed the tree; forces the next pass. */
let invalidated = false
let ingesting: Promise<void> | null = null
let ingestingForced = false
let sessionTitles: Map<string, string> | null = null
let sessionTitlesStamp: { size: number; mtimeMs: number } | null = null
let cacheDirty = false

// ── small helpers ────────────────────────────────────────────────────────

function rowId(filePath: string): string {
  return 'ax_' + crypto.createHash('sha1').update(filePath).digest('hex').slice(0, 16)
}

function num(v: unknown, fallback = 0): number {
  const n = typeof v === 'number' ? v : Number(v)
  return Number.isFinite(n) ? n : fallback
}

function str(v: unknown, fallback = ''): string {
  return typeof v === 'string' ? v : fallback
}

/** Split a query into lowercase terms. Every term must match (AND). */
function queryTerms(text: string): string[] {
  return text.toLowerCase().split(/\s+/).map((t) => t.trim()).filter(Boolean)
}

function buildHaystack(row: ArtifactIndexRow): string {
  return [
    row.filename,
    row.path,
    row.title,
    row.excerpt,
    row.fileType,
    row.creatorLabel,
    row.sessionTitle ?? '',
    row.sessionId,
    row.task ?? '',
    row.agentType ?? '',
  ].join(' ').toLowerCase()
}

// ── session titles ───────────────────────────────────────────────────────

/**
 * Join human session titles from the renderer's session index. Best-effort:
 * the index is a single 7.4 MB JSON, so it is read once per process and only
 * re-read when the ingest finds a session id it has never seen.
 */
function loadSessionTitles(force = false): Map<string, string> {
  const indexFile = path.join(SESSIONS_DIR, '_index.json')
  // 7.5 MB of JSON. Re-reading it on every ingest that touched any manifest is
  // most of an otherwise-cheap incremental pass, so fingerprint it: a forced
  // reload still only re-reads when the file actually moved.
  let stamp: { size: number; mtimeMs: number } | null = null
  try {
    const st = fs.statSync(indexFile)
    stamp = { size: st.size, mtimeMs: st.mtimeMs }
  } catch {
    stamp = null
  }
  const unchanged =
    stamp != null &&
    sessionTitlesStamp != null &&
    stamp.size === sessionTitlesStamp.size &&
    stamp.mtimeMs === sessionTitlesStamp.mtimeMs
  if (sessionTitles && (!force || unchanged)) return sessionTitles

  const out = new Map<string, string>()
  try {
    const raw = fs.readFileSync(indexFile, 'utf8')
    const parsed = JSON.parse(raw)
    const rows: unknown[] = Array.isArray(parsed)
      ? parsed
      : Array.isArray(parsed?.sessions)
      ? parsed.sessions
      : []
    for (const entry of rows) {
      if (!entry || typeof entry !== 'object') continue
      const e = entry as Record<string, unknown>
      const id = str(e.id)
      const title = str(e.title)
      if (id && title) out.set(id, title)
    }
  } catch {
    // No index yet, or unreadable. Rows just fall back to their session id.
  }
  sessionTitles = out
  sessionTitlesStamp = stamp
  return out
}

// ── cache load / save ────────────────────────────────────────────────────

function emptyCache(): IndexCache {
  return { version: CACHE_VERSION, builtAt: 0, projects: {}, meta: {} }
}

function loadCache(): IndexCache {
  if (cache) return cache
  try {
    const raw = fs.readFileSync(CACHE_FILE, 'utf8')
    const parsed = JSON.parse(raw) as IndexCache
    if (parsed && parsed.version === CACHE_VERSION && parsed.projects && parsed.meta) {
      cache = parsed
      rebuildFlat()
      return cache
    }
  } catch {
    // Missing or stale cache — a full ingest rebuilds it.
  }
  cache = emptyCache()
  return cache
}

async function saveCache(): Promise<void> {
  if (!cache || !cacheDirty) return
  const tmp = `${CACHE_FILE}.tmp-${process.pid}`
  try {
    await fsp.mkdir(FREYJA_DIR, { recursive: true })
    await fsp.writeFile(tmp, JSON.stringify(cache), 'utf8')
    await fsp.rename(tmp, CACHE_FILE)
    cacheDirty = false
  } catch (err) {
    console.warn('[artifactIndex] cache save failed', err)
    try {
      await fsp.unlink(tmp)
    } catch {
      /* nothing to clean up */
    }
  }
}

/**
 * Flatten every project's rows into one list sorted newest-first, deduping by
 * path. A path can legitimately appear in two manifests — a session that
 * adopted another session's project dir mid-run writes into both — and the
 * newest revision wins.
 */
function rebuildFlat(): void {
  const byPath = new Map<string, ArtifactIndexRow>()
  for (const project of Object.values(cache?.projects ?? {})) {
    for (const row of project.rows) {
      const existing = byPath.get(row.path)
      if (!existing) {
        byPath.set(row.path, { ...row })
        continue
      }
      // Same path in two manifests. That happens when a session adopts another
      // session's project dir mid-run, and both wrote to the file. Keeping only
      // the newer row would report a fraction of the real revision count and
      // the wrong first-seen date, so merge the histories and let the newer
      // row win on attribution.
      const merged = row.createdAt >= existing.createdAt ? { ...row } : { ...existing }
      merged.revisions = existing.revisions + row.revisions
      merged.firstSeenAt = Math.min(existing.firstSeenAt, row.firstSeenAt)
      byPath.set(row.path, merged)
    }
  }
  flat = Array.from(byPath.values()).sort((a, b) => b.createdAt - a.createdAt)
  haystacks = flat.map(buildHaystack)
}

// ── ingest ───────────────────────────────────────────────────────────────

/**
 * Fold one manifest's rows into per-path index rows.
 *
 * The manifest is append-only and deliberately noisy: a `write_file` that also
 * produced a FileChangeSet lands twice with different ids, and every edit adds
 * a row. 7 814 rows collapse to 1 834 paths. Never surface raw rows.
 */
function foldManifest(
  rows: ManifestRow[],
  projectId: string,
  fallbackSessionId: string,
): ArtifactIndexRow[] {
  const byPath = new Map<string, ArtifactIndexRow>()
  for (const raw of rows) {
    const filePath = str(raw.path)
    if (!filePath) continue
    const createdAt = num(raw.createdAt)
    const existing = byPath.get(filePath)

    if (!existing) {
      byPath.set(filePath, {
        id: rowId(filePath),
        path: filePath,
        filename: str(raw.filename) || path.basename(filePath),
        fileType: str(raw.fileType).toLowerCase(),
        sessionId: str(raw.sessionId) || fallbackSessionId,
        projectId,
        creatorId: str(raw.creatorId),
        creatorLabel: str(raw.creatorLabel) || 'Main agent',
        operation: str(raw.operation),
        source: str(raw.source),
        createdAt,
        firstSeenAt: createdAt,
        revisions: 1,
        bytes: num(raw.bytes),
        lines: raw.lines == null ? null : num(raw.lines),
        sha256: raw.sha256 ?? null,
        exists: raw.exists !== false,
        title: '',
        excerpt: '',
        toolCallId: raw.toolCallId ?? null,
      })
      continue
    }

    existing.revisions += 1
    if (createdAt < existing.firstSeenAt) existing.firstSeenAt = createdAt
    // Only the newest revision's attribution and stats are kept — that is what
    // "who touched this last" means, and it is what a note routes back to.
    if (createdAt >= existing.createdAt) {
      existing.createdAt = createdAt
      existing.creatorId = str(raw.creatorId) || existing.creatorId
      existing.creatorLabel = str(raw.creatorLabel) || existing.creatorLabel
      existing.operation = str(raw.operation) || existing.operation
      existing.source = str(raw.source) || existing.source
      existing.sessionId = str(raw.sessionId) || existing.sessionId
      existing.bytes = num(raw.bytes, existing.bytes)
      existing.lines = raw.lines == null ? existing.lines : num(raw.lines)
      existing.sha256 = raw.sha256 ?? existing.sha256
      existing.exists = raw.exists !== false
      existing.toolCallId = raw.toolCallId ?? existing.toolCallId
    }
  }
  return Array.from(byPath.values())
}

async function readManifest(file: string): Promise<ManifestRow[]> {
  let raw: string
  try {
    raw = await fsp.readFile(file, 'utf8')
  } catch {
    return []
  }
  const out: ManifestRow[] = []
  for (const line of raw.split('\n')) {
    const trimmed = line.trim()
    if (!trimmed) continue
    try {
      out.push(JSON.parse(trimmed) as ManifestRow)
    } catch {
      // Append-only logs can end in a torn line from an interrupted write.
    }
  }
  return out
}

/**
 * Fill in title/excerpt for rows whose content changed since the last ingest.
 *
 * Keyed on sha256 when the manifest carried one, and on (mtimeMs, size)
 * otherwise — a user edit through the browser rewrites the file and appends a
 * fresh manifest row, so both fingerprints move.
 */
async function hydrateMeta(rows: ArtifactIndexRow[]): Promise<void> {
  const meta = cache!.meta
  const pending: ArtifactIndexRow[] = []
  const restatOnly: ArtifactIndexRow[] = []

  for (const row of rows) {
    const cached = meta[row.path]
    if (
      cached &&
      cached.v === META_VERSION &&
      ((cached.sha256 && cached.sha256 === row.sha256) ||
        (!row.sha256 && cached.size === row.bytes))
    ) {
      // Content is unchanged, so the excerpt still holds — but existence is
      // NOT a property of the manifest. A file deleted outside the app leaves
      // its manifest untouched, so trusting the cached row here would keep
      // reporting a dead link as present. Re-stat it; skip only the read.
      applyMeta(row, cached)
      restatOnly.push(row)
      continue
    }
    pending.push(row)
  }

  await pool(pending, hydrateOne)
  await pool(restatOnly, restat)
}

/** Run `fn` over `items` with a bounded worker pool. */
async function pool<T>(items: T[], fn: (item: T) => Promise<void>): Promise<void> {
  if (items.length === 0) return
  let cursor = 0
  const worker = async (): Promise<void> => {
    for (;;) {
      const i = cursor++
      if (i >= items.length) return
      await fn(items[i])
    }
  }
  await Promise.all(
    Array.from({ length: Math.min(META_CONCURRENCY, items.length) }, worker),
  )
}

/** Refresh only what the filesystem knows: does it exist, and how big is it. */
async function restat(row: ArtifactIndexRow): Promise<void> {
  let stat: fs.Stats | null = null
  try {
    stat = await fsp.stat(row.path)
  } catch {
    stat = null
  }
  row.exists = stat != null && stat.isFile()
  if (stat?.isFile()) {
    row.bytes = stat.size
    row.modifiedAt = Math.round(stat.mtimeMs)
  }
}

function applyMeta(row: ArtifactIndexRow, entry: MetaCacheEntry): void {
  row.title = entry.title
  row.excerpt = entry.excerpt
  if (entry.agentType) row.agentType = entry.agentType
  if (entry.task) row.task = entry.task
}

async function hydrateOne(row: ArtifactIndexRow): Promise<void> {
  // Re-stat rather than trusting the manifest's `exists`, which is a snapshot
  // from record time. ~6 % of indexed paths are already gone.
  let stat: fs.Stats | null = null
  try {
    stat = await fsp.stat(row.path)
  } catch {
    stat = null
  }
  row.exists = stat != null && stat.isFile()
  if (stat?.isFile()) {
    row.bytes = stat.size
    row.modifiedAt = Math.round(stat.mtimeMs)
  }

  const fallbackTitle = row.filename
  if (!row.exists || !TEXTUAL_TYPES.has(row.fileType)) {
    const entry: MetaCacheEntry = {
      v: META_VERSION,
      sha256: row.sha256,
      mtimeMs: stat ? stat.mtimeMs : 0,
      size: row.bytes,
      title: cleanFilenameTitle(fallbackTitle),
      excerpt: '',
    }
    cache!.meta[row.path] = entry
    applyMeta(row, entry)
    return
  }

  let head = ''
  try {
    const fh = await fsp.open(row.path, 'r')
    try {
      const size = Math.min(stat!.size, META_READ_BYTES)
      const buf = Buffer.alloc(size)
      await fh.read(buf, 0, size, 0)
      head = buf.toString('utf8')
    } finally {
      await fh.close()
    }
  } catch {
    head = ''
  }

  const extracted = extractArtifactMeta(head, row.fileType, fallbackTitle)
  const entry: MetaCacheEntry = {
    v: META_VERSION,
    sha256: row.sha256,
    mtimeMs: stat ? stat.mtimeMs : 0,
    size: row.bytes,
    title: extracted.title,
    excerpt: extracted.excerpt,
    agentType: extracted.agentType,
    task: extracted.task,
  }
  cache!.meta[row.path] = entry
  applyMeta(row, entry)
}

/** The non-markdown fallback title, matching `artifactMeta.cleanFilename`. */
function cleanFilenameTitle(name: string): string {
  const base = name.replace(/\.[^.]+$/, '')
  const subMatch = base.match(/^sub_[a-z0-9]+_(\d+)$/)
  if (subMatch) return `Subagent #${subMatch[1]}`
  return base.replace(/[-_]/g, ' ')
}

/**
 * Bring the index up to date.
 *
 * `force` means "the operator asked for a rescan": skip the min-interval
 * guard, re-read every manifest regardless of its fingerprint, and re-stat
 * every artifact. Callers on the query path leave it off, so a burst of
 * keystroke-driven queries costs nothing.
 *
 * A forced call arriving while an unforced pass is in flight must NOT simply
 * join it — that pass will finish having done none of the extra work the
 * operator asked for. It waits, then runs its own.
 */
export async function ingest(force = false): Promise<void> {
  if (ingesting) {
    if (!force || ingestingForced) return ingesting
    await ingesting.catch(() => undefined)
    return ingest(true)
  }
  if (!force && !invalidated && Date.now() - lastIngestAt < INGEST_MIN_INTERVAL_MS) {
    return
  }
  invalidated = false
  ingestingForced = force
  ingesting = (async () => {
    const started = Date.now()
    const c = loadCache()

    let dirents: fs.Dirent[]
    try {
      dirents = await fsp.readdir(PROJECTS_DIR, { withFileTypes: true })
    } catch (err) {
      if ((err as NodeJS.ErrnoException)?.code === 'ENOENT') {
        // Fresh install, or the tree was wiped. Either way the index is empty
        // — drop whatever the cache still holds so we don't serve artifacts
        // that provably have no manifest behind them.
        if (Object.keys(c.projects).length > 0) {
          c.projects = {}
          c.meta = {}
          c.builtAt = Date.now()
          cacheDirty = true
          rebuildFlat()
          await saveCache()
        }
      } else {
        // A transient failure (permissions, a race with a mount) must NOT
        // clear a good index — serve the cache and retry on the next pass.
        console.warn('[artifactIndex] projects dir unreadable', err)
      }
      lastIngestAt = Date.now()
      return
    }

    const seen = new Set<string>()
    let changedProjects = 0
    const changedRows: ArtifactIndexRow[] = []

    for (const dirent of dirents) {
      if (!dirent.isDirectory()) continue
      const projectId = dirent.name
      const manifest = path.join(PROJECTS_DIR, projectId, 'manifest.jsonl')
      let stat: fs.Stats
      try {
        stat = await fsp.stat(manifest)
      } catch {
        // Most project dirs never wrote a file. Ordinary, not an error.
        continue
      }
      seen.add(projectId)

      const prev = c.projects[projectId]
      // The (size, mtimeMs) fingerprint is the whole reason a steady-state
      // refresh is ~500 stat calls. It is also blind to an append that lands in
      // the same millisecond at the same size, and to anything that changed on
      // disk WITHOUT touching the manifest. A forced rescan is the escape
      // hatch: it re-reads everything.
      if (!force && prev && prev.size === stat.size && prev.mtimeMs === stat.mtimeMs) {
        continue
      }

      const rows = await readManifest(manifest)
      const sessionId = str(rows.find((r) => r.sessionId)?.sessionId) || projectId
      const folded = foldManifest(rows, projectId, sessionId)
      c.projects[projectId] = {
        projectId,
        sessionId,
        size: stat.size,
        mtimeMs: stat.mtimeMs,
        rows: folded,
      }
      changedProjects += 1
      changedRows.push(...folded)
    }

    // Drop projects whose manifest disappeared.
    for (const projectId of Object.keys(c.projects)) {
      if (!seen.has(projectId)) {
        delete c.projects[projectId]
        changedProjects += 1
      }
    }

    // A forced rescan re-checks every artifact's existence, not just the ones
    // whose manifest moved — that is what makes the "rescan" button able to
    // notice a file deleted outside the app.
    const toHydrate = force
      ? Object.values(c.projects).flatMap((project) => project.rows)
      : changedRows

    if (toHydrate.length > 0) {
      await hydrateMeta(toHydrate)
      // Prune meta for paths no longer referenced anywhere, so the cache file
      // doesn't grow without bound across renamed/deleted artifacts.
      const live = new Set<string>()
      for (const project of Object.values(c.projects)) {
        for (const row of project.rows) live.add(row.path)
      }
      for (const key of Object.keys(c.meta)) {
        if (!live.has(key)) delete c.meta[key]
      }
    }

    if (changedProjects > 0 || force) {
      // A new project dir usually means a new session — refresh titles.
      loadSessionTitles(true)
      const titles = loadSessionTitles()
      for (const project of Object.values(c.projects)) {
        for (const row of project.rows) {
          row.sessionTitle = titles.get(row.sessionId) ?? titles.get(row.projectId)
        }
      }
      c.builtAt = Date.now()
      cacheDirty = true
      rebuildFlat()
      await saveCache()
      console.log(
        `[artifactIndex] ingest: ${changedProjects} project(s) changed, ` +
          `${flat.length} artifacts, ${Date.now() - started}ms`,
      )
    } else if (flat.length === 0 && Object.keys(c.projects).length > 0) {
      // Cache loaded but the flat view was never built (cold start path).
      rebuildFlat()
    }

    lastIngestAt = Date.now()
  })()
  try {
    await ingesting
  } finally {
    ingesting = null
    ingestingForced = false
  }
}

// ── query ────────────────────────────────────────────────────────────────

function compare(a: ArtifactIndexRow, b: ArtifactIndexRow, sort: string): number {
  switch (sort) {
    case 'oldest':
      return a.createdAt - b.createdAt
    case 'name':
      return a.filename.localeCompare(b.filename)
    case 'title':
      return (a.title || a.filename).localeCompare(b.title || b.filename)
    case 'size':
      return b.bytes - a.bytes
    case 'revisions':
      return b.revisions - a.revisions
    case 'session':
      return (
        (a.sessionTitle ?? a.sessionId).localeCompare(b.sessionTitle ?? b.sessionId) ||
        b.createdAt - a.createdAt
      )
    default:
      return b.createdAt - a.createdAt
  }
}

function computeFacets(rows: ArtifactIndexRow[]): ArtifactIndexFacets {
  const types = new Map<string, number>()
  const sessions = new Map<string, { id: string; title: string; count: number }>()
  const creators = new Map<string, { id: string; label: string; count: number }>()
  for (const row of rows) {
    const t = row.fileType || '—'
    types.set(t, (types.get(t) ?? 0) + 1)

    const s = sessions.get(row.sessionId)
    if (s) s.count += 1
    else
      sessions.set(row.sessionId, {
        id: row.sessionId,
        title: row.sessionTitle || row.sessionId,
        count: 1,
      })

    const key = row.creatorId || 'parent'
    const c = creators.get(key)
    if (c) c.count += 1
    else creators.set(key, { id: key, label: row.creatorLabel || 'Main agent', count: 1 })
  }
  return {
    types: Array.from(types.entries())
      .map(([key, count]) => ({ key, count }))
      .sort((a, b) => b.count - a.count || a.key.localeCompare(b.key)),
    sessions: Array.from(sessions.values()).sort((a, b) => b.count - a.count),
    creators: Array.from(creators.values()).sort((a, b) => b.count - a.count),
  }
}

export function stats(): ArtifactIndexStats {
  const c = loadCache()
  let revisions = 0
  const sessions = new Set<string>()
  for (const row of flat) {
    revisions += row.revisions
    sessions.add(row.sessionId)
  }
  return {
    artifacts: flat.length,
    revisions,
    sessions: sessions.size,
    projects: Object.keys(c.projects).length,
    missing: flat.reduce((n, row) => n + (row.exists ? 0 : 1), 0),
    builtAt: c.builtAt,
  }
}

/**
 * Filter + sort + page, entirely in main. The renderer only ever holds one
 * page, so the list stays cheap no matter how far the index grows.
 */
export async function query(q: ArtifactQuery): Promise<ArtifactQueryResult> {
  try {
    await ingest(q.forceRefresh === true)
  } catch (err) {
    console.warn('[artifactIndex] ingest failed', err)
  }
  loadCache()

  const terms = queryTerms(q.text ?? '')
  const types = q.types && q.types.length > 0 ? new Set(q.types) : null
  const sessionIds = q.sessionIds && q.sessionIds.length > 0 ? new Set(q.sessionIds) : null
  const creators = q.creators && q.creators.length > 0 ? new Set(q.creators) : null

  const matched: ArtifactIndexRow[] = []
  for (let i = 0; i < flat.length; i++) {
    const row = flat[i]
    if (types && !types.has(row.fileType || '—')) continue
    if (sessionIds && !sessionIds.has(row.sessionId)) continue
    if (creators && !creators.has(row.creatorId || 'parent')) continue
    if (q.onlyExisting && !row.exists) continue
    if (terms.length > 0) {
      const hay = haystacks[i]
      let ok = true
      for (const term of terms) {
        if (!hay.includes(term)) {
          ok = false
          break
        }
      }
      if (!ok) continue
    }
    matched.push(row)
  }

  const sort = q.sort ?? 'newest'
  matched.sort((a, b) => compare(a, b, sort))

  const offset = Math.max(0, q.offset ?? 0)
  const limit = Math.max(1, Math.min(q.limit ?? 200, MAX_PAGE_ROWS))
  const rows = matched.slice(offset, offset + limit)

  // Never truncate silently. The browser pages by growing `limit`, so a cap it
  // doesn't know about would simply make everything past it unreachable by
  // scrolling, with no sign that anything was missing.
  const truncated = offset + limit < matched.length && limit >= MAX_PAGE_ROWS
  if (truncated) {
    console.log(
      `[artifactIndex] query capped at ${MAX_PAGE_ROWS} of ${matched.length} ` +
        'matches — narrow the filters to reach the rest',
    )
  }

  return {
    ok: true,
    rows,
    total: matched.length,
    /** Rows beyond this cannot be paged to; the operator must filter. */
    cappedAt: truncated ? MAX_PAGE_ROWS : undefined,
    // Facets describe the whole index, not the current page, so the filter
    // rail doesn't collapse to whatever the search already narrowed to.
    facets: computeFacets(flat),
    stats: stats(),
  }
}

/** Every manifest revision of one path, newest first. Read on demand. */
export async function revisions(filePath: string): Promise<ArtifactRevisionRow[]> {
  loadCache()
  const row = flat.find((r) => r.path === filePath)
  if (!row) return []
  const manifest = path.join(PROJECTS_DIR, row.projectId, 'manifest.jsonl')
  const rows = await readManifest(manifest)
  return rows
    .filter((r) => str(r.path) === filePath)
    .map((r) => ({
      id: str(r.id),
      createdAt: num(r.createdAt),
      operation: str(r.operation),
      source: str(r.source),
      creatorId: str(r.creatorId),
      creatorLabel: str(r.creatorLabel) || 'Main agent',
      bytes: num(r.bytes),
      lines: r.lines == null ? null : num(r.lines),
      sha256: r.sha256 ?? null,
      toolCallId: r.toolCallId ?? null,
      additions: typeof r.metadata?.additions === 'number' ? r.metadata.additions : null,
      deletions: typeof r.metadata?.deletions === 'number' ? r.metadata.deletions : null,
    }))
    .sort((a, b) => b.createdAt - a.createdAt)
}

/** The indexed row for one path, or null. Used to resolve a note's target. */
export function rowForPath(filePath: string): ArtifactIndexRow | null {
  loadCache()
  return flat.find((r) => r.path === filePath) ?? null
}

/**
 * Record a human edit into the owning project's manifest so bytes/lines/sha256
 * don't go stale.
 *
 * `artifact:write` has existed since the viewer was built and never had a
 * caller, so nothing ever appended a row for an out-of-band write. An edited
 * artifact whose manifest still claims the pre-edit hash would keep serving a
 * stale cached excerpt forever.
 */
export async function recordUserEdit(
  filePath: string,
  opts: { bytes: number; sha256: string; lines: number | null },
): Promise<void> {
  const row = rowForPath(filePath)
  if (!row) {
    // Not an indexed artifact — the editor was pointed at some other file
    // under the home directory. There is no manifest that owns it, and
    // inventing one would create a bogus project dir.
    return
  }
  const manifest = path.join(PROJECTS_DIR, row.projectId, 'manifest.jsonl')
  const entry = {
    id: 'art_' + crypto.createHash('sha1').update(`${filePath}:${Date.now()}`).digest('hex').slice(0, 12),
    sessionId: row.sessionId,
    creatorId: 'user',
    creatorLabel: 'Operator',
    operation: 'user_edit',
    source: 'artifact_browser',
    path: filePath,
    filename: path.basename(filePath),
    fileType: path.extname(filePath).replace('.', '').toLowerCase(),
    createdAt: Date.now(),
    toolCallId: null,
    changeSetId: null,
    metadata: {},
    exists: true,
    bytes: opts.bytes,
    lines: opts.lines,
    sha256: opts.sha256,
  }
  try {
    await fsp.mkdir(path.dirname(manifest), { recursive: true })
    await fsp.appendFile(
      manifest,
      JSON.stringify(entry, Object.keys(entry).sort()) + '\n',
      'utf8',
    )
  } catch (err) {
    console.warn('[artifactIndex] could not record user edit', err)
    return
  }
  // Make the next query pick the edit up rather than waiting out the
  // min-interval guard. A flag rather than `lastIngestAt = 0`: an ingest that
  // is already in flight stamps lastIngestAt when it finishes, which would
  // clobber the invalidation and leave the edit invisible for four seconds.
  invalidated = true
}

/** Reset in-memory state. Exercised by the index test script. */
export function _resetForTests(): void {
  cache = null
  flat = []
  haystacks = []
  lastIngestAt = 0
  invalidated = false
  sessionTitlesStamp = null
  sessionTitles = null
  cacheDirty = false
}
