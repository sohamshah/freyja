import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useHarness } from '../state/store'
import { useEscapeClose } from '../lib/useEscapeClose'
import { useVirtualList } from '../lib/useVirtualList'
import { scrollToReveal, stepSelection } from '../lib/listNavigation'
import { relativeTime } from '../lib/format'
import { ArtifactPreview } from './ArtifactPreview'
import { ArtifactSource } from './ArtifactSource'
import type {
  ArtifactIndexFacets,
  ArtifactIndexRow,
  ArtifactIndexStats,
  ArtifactNote,
  ArtifactQueryResult,
  ArtifactRevisionRow,
  ArtifactSortKey,
} from '@shared/events'

/**
 * Artifacts — every file every session ever produced.
 *
 * The session library (ArtifactLibrary) shows one session's output and reads
 * the renderer's `artifacts` slice. This is the other half: a single index
 * over every `manifest.jsonl` on disk, so a file written by a Slack session
 * four months ago is one search away.
 *
 * Performance shape
 * ─────────────────
 * Nothing here reads a file to draw a row. Titles and excerpts are extracted
 * once during ingest in the main process and travel with the row; the session
 * library's approach — one `artifact:read` per artifact — would be ~1 700
 * round trips at this scale. Filtering, sorting and paging also happen in
 * main, so the renderer holds one page (200 rows) no matter how far the index
 * grows, and the list itself is windowed on top of that.
 *
 * File content is read exactly when a row is opened, and only then.
 *
 * Keyboard
 *   ↑ ↓ ⇞ ⇟ ⌘↑ ⌘↓  — move the selection
 *   ⌘F              — focus search
 *   ⌘1 / ⌘2 / ⌘3 / ⌘4 — preview / source / history / notes
 *   ⌘E              — toggle edit mode
 *   ⌘S              — save an edit
 *   esc             — close (or leave edit mode first)
 */

const ROW_HEIGHT = 56
const PAGE_SIZE = 200

const TYPE_COLORS: Record<string, string> = {
  md: '#a8d4fc', markdown: '#a8d4fc',
  html: '#7ab8a3', htm: '#7ab8a3',
  json: '#ffcc66', yaml: '#ffcc66', yml: '#ffcc66', toml: '#ffcc66',
  py: '#5bbb5b', rs: '#d99b6b', go: '#5bbabe',
  ts: '#a8d4fc', tsx: '#a8d4fc', js: '#ffd966', jsx: '#ffd966',
  css: '#5bbb5b', scss: '#5bbb5b',
  sh: '#7ab8a3', bash: '#7ab8a3', zsh: '#7ab8a3',
  csv: '#ffcc66', tsv: '#ffcc66',
  svg: '#a8d4fc', xml: '#a8d4fc',
  txt: '#999', log: '#999',
  png: '#d99bbe', jpg: '#d99bbe', jpeg: '#d99bbe', gif: '#d99bbe', webp: '#d99bbe',
  tex: '#7a9cd9', latex: '#7a9cd9',
  swift: '#d99b6b', glsl: '#7a9cd9',
}

function typeColor(ext: string): string {
  return TYPE_COLORS[ext] ?? '#888'
}

function formatBytes(n: number): string {
  if (!n) return '0 B'
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`
  return `${(n / (1024 * 1024)).toFixed(1)} MB`
}

const SORTS: Array<{ key: ArtifactSortKey; label: string }> = [
  { key: 'newest', label: 'newest' },
  { key: 'oldest', label: 'oldest' },
  { key: 'title', label: 'title' },
  { key: 'size', label: 'size' },
  { key: 'revisions', label: 'edits' },
  { key: 'session', label: 'session' },
]

type Tab = 'preview' | 'source' | 'history' | 'notes'

const EMPTY_FACETS: ArtifactIndexFacets = { types: [], sessions: [], creators: [] }
const EMPTY_STATS: ArtifactIndexStats = {
  artifacts: 0,
  revisions: 0,
  sessions: 0,
  projects: 0,
  missing: 0,
  builtAt: 0,
}

export function ArtifactsBrowser() {
  const open = useHarness((s) => s.artifactsBrowserOpen)
  const focusPath = useHarness((s) => s.artifactsBrowserFocusPath)
  const toggle = useHarness((s) => s.toggleArtifactsBrowser)
  const openSessionPane = useHarness((s) => s.openSessionPane)

  const [query, setQuery] = useState('')
  const [debounced, setDebounced] = useState('')
  const [sort, setSort] = useState<ArtifactSortKey>('newest')
  const [types, setTypes] = useState<Set<string>>(new Set())
  const [sessionIds, setSessionIds] = useState<Set<string>>(new Set())
  const [creators, setCreators] = useState<Set<string>>(new Set())
  const [onlyExisting, setOnlyExisting] = useState(true)
  const [limit, setLimit] = useState(PAGE_SIZE)

  const [rows, setRows] = useState<ArtifactIndexRow[]>([])
  const [total, setTotal] = useState(0)
  const [facets, setFacets] = useState<ArtifactIndexFacets>(EMPTY_FACETS)
  const [stats, setStats] = useState<ArtifactIndexStats>(EMPTY_STATS)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [cappedAt, setCappedAt] = useState<number | null>(null)
  const [refreshTick, setRefreshTick] = useState(0)

  const [selected, setSelected] = useState(0)
  const [tab, setTab] = useState<Tab>('preview')
  const [editing, setEditing] = useState(false)
  // Raised by the source pane while it holds unsaved text. Moving the
  // selection would reload the pane and drop the buffer without a word, so
  // the list locks until the operator saves or discards.
  const [unsaved, setUnsaved] = useState(false)

  const searchRef = useRef<HTMLInputElement>(null)
  const suppressHover = useRef(false)
  // The source pane parks a handler here so it gets first refusal on Escape.
  // It cannot register its own useEscapeClose: that hook wins by registering
  // EARLIER in capture order, and the source pane mounts later than this
  // always-mounted shell, so its listener would run second and never see the
  // event — this shell swallows it with stopImmediatePropagation.
  const innerEscape = useRef<(() => boolean) | null>(null)
  // The selection is a PATH, not an index. A save re-queries and, under the
  // default newest-first sort, moves the edited row to the top — holding an
  // index would silently swap the detail pane to whatever file landed in that
  // slot. Cleared whenever the filters change, because then index 0 is right.
  const selectedPath = useRef<string | null>(null)
  // Consumed by the next query. Sticky forcing would defeat the index's ingest
  // throttle on every keystroke.
  const forceOnce = useRef(false)
  // A pending deep-link, consumed once the row it names shows up in a page.
  const pendingFocus = useRef<string | null>(null)

  // Escape must be registered unconditionally at mount so it wins the capture
  // race against the ancestor handlers — see useEscapeClose's doc comment.
  // Edit mode swallows the first Escape so an accidental key doesn't discard
  // unsaved text along with the whole view.
  useEscapeClose(open, () => {
    // Innermost first: a half-typed comment must not be thrown away, and
    // neither must an unsaved edit, just because the operator reached for Esc.
    if (innerEscape.current?.()) return
    if (editing) setEditing(false)
    else toggle(false)
  })

  useEffect(() => {
    if (open && focusPath) pendingFocus.current = focusPath
  }, [open, focusPath])

  // ── query ──────────────────────────────────────────────────────
  useEffect(() => {
    const t = window.setTimeout(() => {
      setDebounced(query)
      setLimit(PAGE_SIZE)
    }, 140)
    return () => window.clearTimeout(t)
  }, [query])

  // Every filter change resets the page size IN THE SAME RENDER. Doing it in a
  // separate effect meant the query effect ran once with the old deep limit —
  // a wasted full-size fetch on every keystroke after paging down.
  const resetPaging = useCallback(() => setLimit(PAGE_SIZE), [])

  useEffect(() => {
    if (!open) return
    let cancelled = false
    const api = (window as any).harness
    if (!api?.artifactIndexQuery) {
      setError('Artifact index IPC unavailable — restart the app')
      return
    }
    setLoading(true)
    const force = forceOnce.current
    forceOnce.current = false
    api
      .artifactIndexQuery({
        text: debounced,
        types: Array.from(types),
        sessionIds: Array.from(sessionIds),
        creators: Array.from(creators),
        onlyExisting,
        sort,
        limit,
        forceRefresh: force,
      })
      .then((result: ArtifactQueryResult) => {
        if (cancelled) return
        setLoading(false)
        if (!result?.ok) {
          setError(result?.error ?? 'Index query failed')
          return
        }
        setError(null)
        setRows(result.rows)
        setTotal(result.total)
        setFacets(result.facets)
        setStats(result.stats)
        setCappedAt(result.cappedAt ?? null)
        // Follow the selected artifact to wherever it landed in the new order.
        const want = selectedPath.current
        if (want) {
          const idx = result.rows.findIndex((r) => r.path === want)
          if (idx >= 0) setSelected(idx)
        }
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setLoading(false)
        setError(String(err))
      })
    return () => {
      cancelled = true
    }
  }, [open, debounced, sort, types, sessionIds, creators, onlyExisting, limit, refreshTick])

  // Clamp the selection when the result set shrinks under it.
  useEffect(() => {
    setSelected((i) => Math.min(i, Math.max(0, rows.length - 1)))
  }, [rows.length])

  // Reset to the top on a new search rather than holding a now-meaningless
  // index — and drop the pinned path, since the row it names may not survive
  // the new filter.
  useEffect(() => {
    selectedPath.current = null
    setSelected(0)
  }, [debounced, sort, types, sessionIds, creators, onlyExisting])

  // Consume a deep-link once its row arrives.
  useEffect(() => {
    const want = pendingFocus.current
    if (!want || rows.length === 0) return
    const idx = rows.findIndex((r) => r.path === want)
    if (idx >= 0) {
      pendingFocus.current = null
      setSelected(idx)
    }
  }, [rows])

  const active = rows[selected] ?? null

  useEffect(() => {
    if (active) selectedPath.current = active.path
  }, [active])

  // Leaving a row while editing would silently drop the buffer.
  useEffect(() => {
    setEditing(false)
  }, [active?.path])

  const list = useVirtualList({ count: rows.length, rowHeight: ROW_HEIGHT })
  const { scrollToIndex } = list
  // Reveal on a real selection change only. Keying this on rows.length too
  // meant every page that loaded while the operator was scrolling yanked the
  // list back to the selected row.
  const revealed = useRef(-1)
  useEffect(() => {
    if (rows.length === 0) return
    if (revealed.current === selected) return
    revealed.current = selected
    scrollToIndex(selected)
  }, [selected, rows.length, scrollToIndex])

  // The index refuses to return more than its cap in one query, so stop
  // growing there rather than asking forever for rows it will never send.
  const reachable = cappedAt != null ? Math.min(total, cappedAt) : total

  const loadMore = useCallback(() => {
    setLimit((n) => (n >= reachable ? n : Math.min(reachable, n + PAGE_SIZE)))
  }, [reachable])

  // Pull the next page in when the window nears the end of what we have.
  // Guarded on `loading`: without it, every scroll event while a page is
  // in flight bumps the limit again, so a fast flick asks for the whole
  // index instead of the next page.
  useEffect(() => {
    if (loading || rows.length >= reachable) return
    if (list.end >= rows.length - 20) loadMore()
  }, [list.end, rows.length, reachable, loading, loadMore])

  const selectRow = useCallback(
    (index: number) => {
      if (unsaved) return
      setSelected(index)
    },
    [unsaved],
  )

  const hasMore = rows.length < reachable

  const move = useCallback(
    (e: { key: string; metaKey?: boolean; ctrlKey?: boolean; altKey?: boolean }) => {
      const next = stepSelection(e, {
        selected,
        count: rows.length,
        pageStep: 10,
        // Wrapping past the last LOADED row would jump to the top of a
        // 1 700-row index the operator is only 200 rows into. Page instead.
        wrap: !hasMore,
      })
      if (next == null) return false
      if (unsaved) return true
      if (next === selected && hasMore) {
        loadMore()
        return true
      }
      suppressHover.current = true
      setSelected(next)
      return true
    },
    [selected, rows.length, unsaved, hasMore, loadMore],
  )

  // ── keyboard ───────────────────────────────────────────────────
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      const isMac = navigator.platform.toLowerCase().includes('mac')
      const mod = isMac ? e.metaKey : e.ctrlKey
      const target = e.target as HTMLElement | null
      const typing =
        target?.tagName === 'INPUT' ||
        target?.tagName === 'TEXTAREA' ||
        target?.isContentEditable === true

      if (mod && (e.key === 'f' || e.key === 'F')) {
        e.preventDefault()
        searchRef.current?.focus()
        searchRef.current?.select()
        return
      }
      if (mod && e.key === 's' && editing) {
        // Handled by the source pane; swallow the browser default here.
        return
      }
      if (mod && (e.key === 'e' || e.key === 'E')) {
        e.preventDefault()
        if (active?.exists) {
          setTab('source')
          setEditing((v) => !v)
        }
        return
      }
      if (mod && e.key === 'r' && !e.shiftKey) {
        e.preventDefault()
        forceOnce.current = true
        setRefreshTick((n) => n + 1)
        return
      }
      if (mod && !e.shiftKey && ['1', '2', '3', '4'].includes(e.key)) {
        e.preventDefault()
        // Leaving the source tab unmounts the editor and takes the buffer with
        // it, so an unsaved edit locks the tabs the same way it locks the list.
        if (!unsaved) {
          setTab((['preview', 'source', 'history', 'notes'] as Tab[])[Number(e.key) - 1])
        }
        return
      }
      // Arrow keys belong to the caret while a field has focus — except in
      // the search box, where the list is what the operator is steering.
      if (typing && target !== searchRef.current) return
      if (editing) return
      if (move(e)) e.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, move, editing, unsaved, active?.exists])

  const toggleIn = useCallback(
    (setter: (fn: (prev: Set<string>) => Set<string>) => void, key: string) => {
      resetPaging()
      setter((prev) => {
        const next = new Set(prev)
        if (next.has(key)) next.delete(key)
        else next.add(key)
        return next
      })
    },
    [resetPaging],
  )

  const chooseSort = useCallback(
    (key: ArtifactSortKey) => {
      resetPaging()
      setSort(key)
    },
    [resetPaging],
  )

  const toggleExisting = useCallback(() => {
    resetPaging()
    setOnlyExisting((v) => !v)
  }, [resetPaging])

  const hasFilters =
    types.size > 0 || sessionIds.size > 0 || creators.size > 0 || debounced.trim().length > 0

  const clearFilters = useCallback(() => {
    resetPaging()
    setTypes(new Set())
    setSessionIds(new Set())
    setCreators(new Set())
    setQuery('')
  }, [resetPaging])

  const openOwningSession = useCallback(() => {
    if (!active?.sessionId) return
    toggle(false)
    openSessionPane(active.sessionId, 'replace').catch(() => {})
  }, [active?.sessionId, toggle, openSessionPane])

  if (!open) return null

  return (
    // z-[55]: above the session library (z-50), which can be open underneath
    // when the operator jumps here from the activity panel.
    <div className="fixed inset-0 z-[55] flex flex-col bg-[#0c0c10]/95 backdrop-blur-md">
      <header
        className="drag flex shrink-0 items-center gap-3 border-b border-white/[0.06] bg-[#0a0a0e]/60 pr-3"
        style={{
          paddingLeft: 'var(--titlebar-inset, 82px)',
          minHeight: 'var(--titlebar-height, 46px)',
        }}
      >
        <div className="no-drag flex items-baseline gap-2">
          <span className="font-mono text-[11px] uppercase tracking-[0.12em] text-fg-1">
            artifacts
          </span>
          <span className="font-mono text-[10px] text-fg-3">
            {hasFilters && total !== stats.artifacts
              ? `${total} of ${stats.artifacts}`
              : `${stats.artifacts} file${stats.artifacts === 1 ? '' : 's'}`}
          </span>
          <span className="font-mono text-[9.5px] text-fg-3">
            · {stats.sessions} session{stats.sessions === 1 ? '' : 's'} ·{' '}
            {stats.revisions} revision{stats.revisions === 1 ? '' : 's'}
          </span>
          {loading && (
            <span className="font-mono text-[9px] italic text-fg-3">indexing…</span>
          )}
        </div>

        <div className="no-drag relative ml-auto flex items-center">
          <svg
            width="11" height="11" viewBox="0 0 11 11"
            className="absolute left-2.5 text-fg-3"
            fill="none" stroke="currentColor" strokeWidth="1.5"
          >
            <circle cx="4.5" cy="4.5" r="3.2" />
            <line x1="7" y1="7" x2="9.6" y2="9.6" strokeLinecap="round" />
          </svg>
          <input
            ref={searchRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search every artifact… (⌘F)"
            className="w-[300px] rounded-md bg-white/[0.04] py-1 pl-7 pr-2.5 font-mono text-[11px] text-fg-0 ring-hairline placeholder:text-fg-3 focus:outline-none focus:ring-1 focus:ring-accent/40"
          />
        </div>

        <div className="no-drag flex items-center gap-1">
          {SORTS.map((s) => (
            <button
              key={s.key}
              onClick={() => chooseSort(s.key)}
              className={`rounded px-1.5 py-[2px] font-mono text-[9px] uppercase tracking-[0.08em] ring-hairline ${
                sort === s.key
                  ? 'bg-accent/15 text-accent ring-1 ring-accent/30'
                  : 'bg-white/[0.04] text-fg-2 hover:bg-white/[0.08] hover:text-fg-0'
              }`}
            >
              {s.label}
            </button>
          ))}
        </div>

        <button
          onClick={() => {
            forceOnce.current = true
            setRefreshTick((n) => n + 1)
          }}
          title="Re-scan every manifest (⌘R)"
          className="no-drag rounded bg-white/[0.04] px-1.5 py-[2px] font-mono text-[9px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
        >
          rescan
        </button>
        <button
          onClick={() => toggle(false)}
          className="no-drag rounded bg-white/[0.04] px-2 py-[3px] font-mono text-[9px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
        >
          esc close
        </button>
      </header>

      {error && (
        <div className="shrink-0 bg-danger/[0.08] px-5 py-1.5 font-mono text-[10.5px] text-danger">
          {error}
        </div>
      )}

      <div className="flex min-h-0 flex-1">
        <FilterRail
          facets={facets}
          stats={stats}
          types={types}
          sessionIds={sessionIds}
          creators={creators}
          onlyExisting={onlyExisting}
          hasFilters={hasFilters}
          onToggleType={(k) => toggleIn(setTypes, k)}
          onToggleSession={(k) => toggleIn(setSessionIds, k)}
          onToggleCreator={(k) => toggleIn(setCreators, k)}
          onToggleExisting={toggleExisting}
          onClear={clearFilters}
        />

        {/* Result list */}
        <div className="flex w-[380px] shrink-0 flex-col border-r border-white/[0.06]">
          <div
            ref={list.ref}
            onScroll={list.onScroll}
            onMouseMove={() => {
              suppressHover.current = false
            }}
            className="min-h-0 flex-1 overflow-y-auto"
          >
            {rows.length === 0 ? (
              <div className="p-6 text-center font-mono text-[11px] italic text-fg-3">
                {loading
                  ? 'scanning manifests…'
                  : hasFilters
                  ? 'nothing matches these filters'
                  : 'no artifacts indexed yet'}
              </div>
            ) : (
              <div style={{ height: list.totalHeight, position: 'relative' }}>
                <div style={{ transform: `translateY(${list.offsetY}px)` }}>
                  {rows.slice(list.start, list.end).map((row, i) => {
                    const index = list.start + i
                    return (
                      <ResultRow
                        key={row.id}
                        row={row}
                        index={index}
                        active={index === selected}
                        onHover={() => {
                          if (!suppressHover.current) selectRow(index)
                        }}
                        onSelect={() => selectRow(index)}
                      />
                    )
                  })}
                </div>
              </div>
            )}
          </div>
          <div className="flex shrink-0 items-center justify-between border-t border-white/[0.06] bg-black/25 px-3 py-1.5 font-mono text-[9px] text-fg-3">
            <span>
              {rows.length > 0 ? `${selected + 1} / ${total}` : '—'}
            </span>
            <span className={unsaved || cappedAt != null ? 'text-warn' : undefined}>
              {unsaved
                ? 'unsaved edit — save or discard to move'
                : cappedAt != null && rows.length >= cappedAt
                ? `first ${cappedAt} of ${total} — narrow the filters`
                : rows.length < total
                ? `${rows.length} loaded`
                : 'all loaded'}
            </span>
          </div>
        </div>

        {/* Detail */}
        <div className="flex min-w-0 flex-1 flex-col">
          {active ? (
            <DetailPane
              row={active}
              tab={tab}
              onTab={setTab}
              editing={editing}
              onEditing={setEditing}
              onOpenSession={openOwningSession}
              onIndexChanged={() => setRefreshTick((n) => n + 1)}
              onDirtyChange={setUnsaved}
              escapeSlot={innerEscape}
            />
          ) : (
            <div className="flex h-full items-center justify-center font-mono text-[11px] italic text-fg-3">
              select an artifact
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

// ─── Filter rail ────────────────────────────────────────────────────

function FilterRail({
  facets,
  stats,
  types,
  sessionIds,
  creators,
  onlyExisting,
  hasFilters,
  onToggleType,
  onToggleSession,
  onToggleCreator,
  onToggleExisting,
  onClear,
}: {
  facets: ArtifactIndexFacets
  stats: ArtifactIndexStats
  types: Set<string>
  sessionIds: Set<string>
  creators: Set<string>
  onlyExisting: boolean
  hasFilters: boolean
  onToggleType: (key: string) => void
  onToggleSession: (key: string) => void
  onToggleCreator: (key: string) => void
  onToggleExisting: () => void
  onClear: () => void
}) {
  const [sessionFilter, setSessionFilter] = useState('')
  const visibleSessions = useMemo(() => {
    const q = sessionFilter.trim().toLowerCase()
    const list = q
      ? facets.sessions.filter(
          (s) => s.title.toLowerCase().includes(q) || s.id.toLowerCase().includes(q),
        )
      : facets.sessions
    // 142 sessions is too many to scroll past the type facet; the filter box
    // above is the way to reach the tail.
    return q ? list.slice(0, 40) : list.slice(0, 18)
  }, [facets.sessions, sessionFilter])

  return (
    <aside className="flex w-[224px] shrink-0 flex-col overflow-y-auto border-r border-white/[0.06] bg-[#08080c]/60 px-3 py-3">
      <div className="mb-2 flex items-baseline justify-between">
        <div className="label">filters</div>
        {hasFilters && (
          <button
            onClick={onClear}
            className="font-mono text-[9px] uppercase tracking-[0.08em] text-fg-3 hover:text-fg-0"
          >
            clear
          </button>
        )}
      </div>

      <button
        onClick={onToggleExisting}
        className={`mb-3 flex items-center justify-between rounded px-2 py-1 text-left font-mono text-[10px] ring-hairline ${
          onlyExisting
            ? 'bg-accent/10 text-accent ring-1 ring-accent/25'
            : 'bg-white/[0.03] text-fg-2 hover:bg-white/[0.06]'
        }`}
      >
        <span>on disk only</span>
        <span className="text-fg-3">{stats.missing} gone</span>
      </button>

      <FacetGroup title="file type">
        {facets.types.slice(0, 16).map((t) => (
          <FacetChip
            key={t.key}
            label={t.key}
            count={t.count}
            active={types.has(t.key)}
            dot={typeColor(t.key)}
            onClick={() => onToggleType(t.key)}
          />
        ))}
      </FacetGroup>

      <FacetGroup title="session">
        <input
          value={sessionFilter}
          onChange={(e) => setSessionFilter(e.target.value)}
          placeholder="filter sessions…"
          className="mb-1 w-full rounded bg-white/[0.04] px-2 py-1 font-mono text-[10px] text-fg-0 ring-hairline placeholder:text-fg-3 focus:outline-none focus:ring-1 focus:ring-accent/30"
        />
        {visibleSessions.map((s) => (
          <FacetChip
            key={s.id}
            label={s.title}
            count={s.count}
            active={sessionIds.has(s.id)}
            onClick={() => onToggleSession(s.id)}
          />
        ))}
        {!sessionFilter && facets.sessions.length > visibleSessions.length && (
          <div className="px-1 pt-1 font-mono text-[9px] text-fg-3">
            +{facets.sessions.length - visibleSessions.length} more — use the filter
          </div>
        )}
      </FacetGroup>

      <FacetGroup title="created by">
        {facets.creators.slice(0, 12).map((c) => (
          <FacetChip
            key={c.id}
            label={c.label}
            count={c.count}
            active={creators.has(c.id)}
            onClick={() => onToggleCreator(c.id)}
          />
        ))}
      </FacetGroup>

      {stats.builtAt > 0 && (
        <div className="mt-auto pt-3 font-mono text-[9px] text-fg-3">
          indexed {relativeTime(stats.builtAt)} · {stats.projects} project dirs
        </div>
      )}
    </aside>
  )
}

function FacetGroup({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="mb-3">
      <div className="mb-1 font-mono text-[9px] uppercase tracking-[0.1em] text-fg-3">
        {title}
      </div>
      <div className="space-y-[2px]">{children}</div>
    </div>
  )
}

function FacetChip({
  label,
  count,
  active,
  dot,
  onClick,
}: {
  label: string
  count: number
  active: boolean
  dot?: string
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      title={label}
      className={`flex w-full items-center gap-1.5 rounded px-1.5 py-[3px] text-left font-mono text-[10px] ${
        active ? 'bg-accent/15 text-fg-0' : 'text-fg-2 hover:bg-white/[0.04]'
      }`}
    >
      {dot && (
        <span
          className="block h-1.5 w-1.5 shrink-0 rounded-[1px]"
          style={{ background: dot }}
        />
      )}
      <span className="min-w-0 flex-1 truncate">{label}</span>
      <span className="shrink-0 text-fg-3">{count}</span>
    </button>
  )
}

// ─── Result row ─────────────────────────────────────────────────────

const ResultRow = memo(function ResultRow({
  row,
  index,
  active,
  onHover,
  onSelect,
}: {
  row: ArtifactIndexRow
  index: number
  active: boolean
  onHover: () => void
  onSelect: () => void
}) {
  return (
    <button
      data-artifact-row={index}
      onMouseEnter={onHover}
      onClick={onSelect}
      style={{ height: ROW_HEIGHT }}
      className={`flex w-full items-center gap-2.5 px-3 text-left ${
        active ? 'bg-accent/[0.13]' : 'hover:bg-white/[0.03]'
      }`}
    >
      <span
        className="block h-6 w-[3px] shrink-0 rounded-full"
        style={{ background: row.exists ? typeColor(row.fileType) : '#3a3a3a' }}
      />
      <span className="min-w-0 flex-1">
        <span className="flex items-baseline gap-1.5">
          <span
            className={`truncate font-mono text-[11.5px] ${
              row.exists ? 'text-fg-0' : 'text-fg-3 line-through'
            }`}
          >
            {row.title || row.filename}
          </span>
          {row.noteCount ? (
            <span className="shrink-0 rounded-full bg-warn/20 px-1 font-mono text-[8px] font-bold text-warn">
              {row.noteCount}
            </span>
          ) : null}
        </span>
        <span className="mt-[2px] flex items-baseline gap-1.5 font-mono text-[9px] text-fg-3">
          <span className="truncate">{row.sessionTitle || row.sessionId}</span>
          <span>·</span>
          <span className="shrink-0">{relativeTime(row.createdAt)}</span>
          {row.revisions > 1 && (
            <>
              <span>·</span>
              <span className="shrink-0">{row.revisions} edits</span>
            </>
          )}
        </span>
      </span>
      <span className="shrink-0 rounded-full bg-white/[0.04] px-1.5 py-[1px] font-mono text-[8px] uppercase text-fg-3">
        {row.fileType || '?'}
      </span>
    </button>
  )
})

// ─── Detail pane ────────────────────────────────────────────────────

function DetailPane({
  row,
  tab,
  onTab,
  editing,
  onEditing,
  onOpenSession,
  onIndexChanged,
  onDirtyChange,
  escapeSlot,
}: {
  row: ArtifactIndexRow
  tab: Tab
  onTab: (t: Tab) => void
  editing: boolean
  onEditing: (v: boolean) => void
  onOpenSession: () => void
  onIndexChanged: () => void
  onDirtyChange: (dirty: boolean) => void
  escapeSlot: React.MutableRefObject<(() => boolean) | null>
}) {
  const [notes, setNotes] = useState<ArtifactNote[]>([])
  const [notesTick, setNotesTick] = useState(0)
  const [dirty, setDirty] = useState(false)

  const reportDirty = useCallback(
    (next: boolean) => {
      setDirty(next)
      onDirtyChange(next)
    },
    [onDirtyChange],
  )

  // Refetch on tab switch as well as on selection change: a note can be
  // written from somewhere other than this pane (another window, an earlier
  // visit to the same artifact), and a stale empty state reads as "no
  // comments" rather than "not loaded".
  useEffect(() => {
    let cancelled = false
    const api = (window as any).harness
    if (!api?.artifactNoteList) return
    api.artifactNoteList(row.path).then((r: { ok: boolean; notes: ArtifactNote[] }) => {
      if (!cancelled) setNotes(r?.ok ? r.notes : [])
    })
    return () => {
      cancelled = true
    }
  }, [row.path, notesTick, tab])

  const openNotes = notes.filter((n) => n.status !== 'resolved')

  return (
    <>
      <div className="flex shrink-0 flex-col gap-1.5 border-b border-white/[0.06] px-4 py-2.5">
        <div className="flex items-baseline gap-2">
          <span className="truncate font-mono text-[12.5px] text-fg-0">
            {row.title || row.filename}
          </span>
          {!row.exists && (
            <span className="shrink-0 rounded-full bg-danger/15 px-1.5 py-[1px] font-mono text-[8px] uppercase text-danger ring-1 ring-danger/25">
              missing on disk
            </span>
          )}
        </div>
        <div className="truncate font-mono text-[9.5px] text-fg-3" title={row.path}>
          {row.path}
        </div>
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1 font-mono text-[9px] text-fg-3">
          <button
            onClick={onOpenSession}
            title="Open the session that produced this"
            className="rounded bg-white/[0.04] px-1.5 py-[1px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
          >
            {row.sessionTitle || row.sessionId} ↗
          </button>
          <span>{row.creatorLabel}</span>
          <span>·</span>
          <span>{formatBytes(row.bytes)}</span>
          {row.lines != null && (
            <>
              <span>·</span>
              <span>{row.lines} lines</span>
            </>
          )}
          <span>·</span>
          <span>
            {row.revisions} revision{row.revisions === 1 ? '' : 's'}
          </span>
          <span>·</span>
          <span>last touched {relativeTime(row.createdAt)}</span>
        </div>

        <div className="mt-1 flex items-center gap-1">
          {(['preview', 'source', 'history', 'notes'] as Tab[]).map((t, i) => (
            <button
              key={t}
              onClick={() => {
                if (!dirty) onTab(t)
              }}
              disabled={dirty && t !== tab}
              title={
                dirty && t !== tab ? 'Save or discard the edit first' : `⌘${i + 1}`
              }
              className={`rounded px-2 py-[3px] font-mono text-[9.5px] uppercase tracking-[0.08em] ${
                tab === t
                  ? 'bg-accent/15 text-accent ring-1 ring-accent/30'
                  : dirty
                  ? 'bg-white/[0.02] text-fg-3 ring-hairline'
                  : 'bg-white/[0.03] text-fg-2 ring-hairline hover:bg-white/[0.07] hover:text-fg-0'
              }`}
            >
              {t}
              {t === 'notes' && openNotes.length > 0 ? ` ${openNotes.length}` : ''}
              {t === 'history' && row.revisions > 1 ? ` ${row.revisions}` : ''}
            </button>
          ))}
          <button
            onClick={() => {
              const api = (window as any).harness
              api?.openExternal?.(`file://${row.path}`)
            }}
            className="ml-auto rounded bg-white/[0.04] px-2 py-[3px] font-mono text-[9.5px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
          >
            open externally ↗
          </button>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-hidden">
        {tab === 'preview' && (
          row.exists ? (
            <div className="h-full overflow-y-auto">
              <ArtifactPreview path={row.path} fileType={row.fileType} />
            </div>
          ) : (
            <MissingPane row={row} />
          )
        )}
        {tab === 'source' && (
          row.exists ? (
            <ArtifactSource
              row={row}
              editing={editing}
              onEditing={onEditing}
              onDirtyChange={reportDirty}
              escapeSlot={escapeSlot}
              onSaved={onIndexChanged}
              onNoteCreated={() => {
                setNotesTick((n) => n + 1)
                onIndexChanged()
              }}
            />
          ) : (
            <MissingPane row={row} />
          )
        )}
        {tab === 'history' && <HistoryPane row={row} />}
        {tab === 'notes' && (
          <NotesPane
            notes={notes}
            onResolve={async (id) => {
              const api = (window as any).harness
              await api?.artifactNoteResolve?.(id)
              setNotesTick((n) => n + 1)
              onIndexChanged()
            }}
          />
        )}
      </div>
    </>
  )
}

function MissingPane({ row }: { row: ArtifactIndexRow }) {
  return (
    <div className="flex h-full items-center justify-center p-8">
      <div className="max-w-[480px] text-center">
        <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.1em] text-fg-3">
          file no longer on disk
        </div>
        <div className="mb-3 break-all font-mono text-[11px] text-fg-2">{row.path}</div>
        <div className="font-prose text-[12px] leading-relaxed text-fg-3">
          The manifest still records it — {row.revisions} revision
          {row.revisions === 1 ? '' : 's'}, last touched {relativeTime(row.createdAt)} by{' '}
          {row.creatorLabel}. History and notes still work; the content is gone.
        </div>
      </div>
    </div>
  )
}

// ─── History ────────────────────────────────────────────────────────

function HistoryPane({ row }: { row: ArtifactIndexRow }) {
  const [revs, setRevs] = useState<ArtifactRevisionRow[] | null>(null)

  useEffect(() => {
    let cancelled = false
    const api = (window as any).harness
    if (!api?.artifactIndexRevisions) return
    setRevs(null)
    api
      .artifactIndexRevisions(row.path)
      .then((r: { ok: boolean; rows: ArtifactRevisionRow[] }) => {
        if (!cancelled) setRevs(r?.ok ? r.rows : [])
      })
    return () => {
      cancelled = true
    }
  }, [row.path])

  if (revs == null) {
    return (
      <div className="p-6 font-mono text-[11px] italic text-fg-3">reading manifest…</div>
    )
  }
  if (revs.length === 0) {
    return (
      <div className="p-6 font-mono text-[11px] italic text-fg-3">
        no manifest history for this path
      </div>
    )
  }
  return (
    <div className="h-full overflow-y-auto p-4">
      <div className="space-y-1">
        {revs.map((rev, i) => (
          <div
            key={rev.id || `${rev.createdAt}-${i}`}
            className="flex items-baseline gap-2 rounded-md bg-white/[0.02] px-2.5 py-1.5 ring-hairline"
          >
            <span className="w-[52px] shrink-0 font-mono text-[9px] uppercase tracking-[0.08em] text-fg-3">
              {revs.length - i}
            </span>
            <span
              className={`shrink-0 rounded px-1.5 py-[1px] font-mono text-[8.5px] uppercase tracking-[0.06em] ${
                rev.operation === 'user_edit'
                  ? 'bg-warn/15 text-warn'
                  : rev.operation === 'subagent_artifact'
                  ? 'bg-ok/15 text-ok'
                  : 'bg-white/[0.05] text-fg-2'
              }`}
            >
              {rev.operation || '—'}
            </span>
            <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-fg-2">
              {rev.creatorLabel}
            </span>
            {(rev.additions != null || rev.deletions != null) && (
              <span className="shrink-0 font-mono text-[9px]">
                <span className="text-ok">+{rev.additions ?? 0}</span>{' '}
                <span className="text-danger">-{rev.deletions ?? 0}</span>
              </span>
            )}
            <span className="shrink-0 font-mono text-[9px] text-fg-3">
              {formatBytes(rev.bytes)}
            </span>
            <span className="w-[92px] shrink-0 text-right font-mono text-[9px] text-fg-3">
              {relativeTime(rev.createdAt)}
            </span>
          </div>
        ))}
      </div>
    </div>
  )
}

// ─── Notes ──────────────────────────────────────────────────────────

function NotesPane({
  notes,
  onResolve,
}: {
  notes: ArtifactNote[]
  onResolve: (id: string) => void
}) {
  if (notes.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-8">
        <div className="max-w-[420px] text-center font-prose text-[12px] leading-relaxed text-fg-3">
          No comments yet. Open the <span className="font-mono text-fg-2">source</span>{' '}
          tab, select the lines you want to talk about, and leave a comment — it goes
          straight to the session that last touched this file.
        </div>
      </div>
    )
  }
  return (
    <div className="h-full overflow-y-auto p-4">
      <div className="space-y-2">
        {notes.map((note) => (
          <div
            key={note.id}
            className={`rounded-lg p-3 ring-hairline ${
              note.status === 'resolved' ? 'bg-white/[0.015] opacity-60' : 'bg-white/[0.03]'
            }`}
          >
            <div className="mb-1.5 flex items-center gap-2 font-mono text-[9px] uppercase tracking-[0.08em]">
              <span
                className={
                  note.status === 'delivered'
                    ? 'text-ok'
                    : note.status === 'failed'
                    ? 'text-danger'
                    : note.status === 'resolved'
                    ? 'text-fg-3'
                    : 'text-warn'
                }
              >
                {note.status}
              </span>
              <span className="text-fg-3">→ {note.targetSessionTitle || note.targetSessionId || 'no session'}</span>
              <span className="ml-auto text-fg-3">{relativeTime(note.createdAt)}</span>
              {note.status !== 'resolved' && (
                <button
                  onClick={() => onResolve(note.id)}
                  className="rounded bg-white/[0.05] px-1.5 py-[1px] text-fg-2 hover:bg-white/[0.1] hover:text-fg-0"
                >
                  resolve
                </button>
              )}
            </div>
            {note.anchor && (
              <pre className="mb-1.5 overflow-x-auto rounded bg-black/30 px-2 py-1 font-mono text-[9.5px] leading-relaxed text-fg-2">
                <span className="text-fg-3">
                  {note.anchor.startLine === note.anchor.endLine
                    ? `line ${note.anchor.startLine}`
                    : `lines ${note.anchor.startLine}–${note.anchor.endLine}`}
                  {'\n'}
                </span>
                {note.anchor.quote}
              </pre>
            )}
            <div className="whitespace-pre-wrap font-prose text-[12px] leading-relaxed text-fg-1">
              {note.body}
            </div>
            {note.error && (
              <div className="mt-1.5 font-mono text-[9.5px] text-danger">{note.error}</div>
            )}
          </div>
        ))}
      </div>
    </div>
  )
}
