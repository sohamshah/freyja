import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useHarness } from '../state/store'
import type { ArtifactRecord, FileChangeSet } from '@shared/events'
import { ArtifactPreview } from './ArtifactPreview'
import { useArtifactMeta } from '../lib/useArtifactMeta'
import type { ArtifactMeta } from '../lib/artifactMeta'
import { relativeTime } from '../lib/format'
import { FileChangeCard } from './FileChangeCard'

/**
 * Artifact Workspace — a "library" view for files and file changes.
 * Multiple view modes (cards / list / details / changes), persistent left
 * filter rail, and lazy-loaded content excerpts so cards show real
 * titles + first paragraphs instead of opaque hash filenames.
 *
 * Keyboard:
 *   esc       — close (or back out of preview)
 *   ⌘F        — focus search
 *   ⌘1/2/3/4  — switch view modes
 */

type ViewMode = 'cards' | 'list' | 'details' | 'changes'
type SortKey = 'newest' | 'oldest' | 'title' | 'creator'

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
}

const AGENT_TYPE_COLORS: Record<string, string> = {
  parent: '#a8d4fc',
  general: '#a8d4fc',
  explore: '#7ab8a3',
  'explore-fast': '#7bd3ec',
  code: '#ffcc66',
  verify: '#88d67f',
  plan: '#b8a7ff',
  review: '#f0a6ca',
  test: '#f5b45d',
  'browser-qa': '#79b3fa',
  performance: '#f07878',
  docs: '#72d0b2',
  'memory-curator': '#c8d67f',
  computer: '#d99bbe',
}

function fileTypeColor(ext: string): string {
  return TYPE_COLORS[ext] ?? '#888'
}

function agentTypeColor(t: string | undefined): string {
  if (!t) return '#888'
  return AGENT_TYPE_COLORS[t] ?? '#888'
}

export function ArtifactWorkspace({
  onClose,
  initialView = 'cards',
}: {
  onClose: () => void
  initialView?: ViewMode
}) {
  const artifacts = useHarness((s) => s.artifacts)
  const fileChanges = useHarness((s) => s.fileChanges)
  const focusToolCall = useHarness((s) => s.focusToolCall)
  const [view, setView] = useState<ViewMode>(initialView)
  const [sort, setSort] = useState<SortKey>('newest')
  const [search, setSearch] = useState('')
  const [selectedTypes, setSelectedTypes] = useState<Set<string>>(new Set())
  const [selectedCreators, setSelectedCreators] = useState<Set<string>>(new Set())
  const [previewPath, setPreviewPath] = useState<string | null>(null)
  const [detailsSelectedId, setDetailsSelectedId] = useState<string | null>(null)
  const searchRef = useRef<HTMLInputElement>(null)

  const { byPath, loading } = useArtifactMeta(artifacts)

  // ── Keyboard ──────────────────────────────────────────────────
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (previewPath) setPreviewPath(null)
        else onClose()
        e.stopPropagation()
        return
      }
      const isMac = navigator.platform.toLowerCase().includes('mac')
      const mod = isMac ? e.metaKey : e.ctrlKey
      if (mod && e.key === 'f') {
        e.preventDefault()
        searchRef.current?.focus()
        return
      }
      if (mod && e.key === '1') { e.preventDefault(); setView('cards') }
      if (mod && e.key === '2') { e.preventDefault(); setView('list') }
      if (mod && e.key === '3') { e.preventDefault(); setView('details') }
      if (mod && e.key === '4') { e.preventDefault(); setView('changes') }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose, previewPath])

  // ── Filter aggregations ───────────────────────────────────────
  const typeBuckets = useMemo(() => {
    const buckets = new Map<string, number>()
    for (const a of artifacts) buckets.set(a.fileType, (buckets.get(a.fileType) ?? 0) + 1)
    return Array.from(buckets.entries()).sort((a, b) => b[1] - a[1])
  }, [artifacts])

  const creatorBuckets = useMemo(() => {
    // Group by creator id; preserve first-seen label
    const buckets = new Map<string, { label: string; count: number }>()
    for (const a of artifacts) {
      const ex = buckets.get(a.creator)
      if (ex) ex.count += 1
      else buckets.set(a.creator, { label: a.creatorLabel, count: 1 })
    }
    return Array.from(buckets.entries())
      .map(([id, { label, count }]) => ({ id, label, count }))
      .sort((a, b) => b.count - a.count)
  }, [artifacts])

  // ── Filtering + sorting ───────────────────────────────────────
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase()
    let list = artifacts.filter((a) => {
      if (selectedTypes.size > 0 && !selectedTypes.has(a.fileType)) return false
      if (selectedCreators.size > 0 && !selectedCreators.has(a.creator)) return false
      if (q) {
        const meta = byPath[a.path]
        const haystack = [
          a.filename, a.creatorLabel, a.fileType,
          meta?.title ?? '', meta?.excerpt ?? '', meta?.task ?? '',
        ].join(' ').toLowerCase()
        if (!haystack.includes(q)) return false
      }
      return true
    })

    list = [...list].sort((a, b) => {
      switch (sort) {
        case 'newest': return b.createdAt - a.createdAt
        case 'oldest': return a.createdAt - b.createdAt
        case 'title': {
          const ta = byPath[a.path]?.title ?? a.filename
          const tb = byPath[b.path]?.title ?? b.filename
          return ta.localeCompare(tb)
        }
        case 'creator':
          return a.creatorLabel.localeCompare(b.creatorLabel)
      }
    })
    return list
  }, [artifacts, selectedTypes, selectedCreators, search, sort, byPath])

  const artifactsByPath = useMemo(() => {
    const map = new Map<string, ArtifactRecord>()
    for (const artifact of artifacts) map.set(artifact.path, artifact)
    return map
  }, [artifacts])

  const filteredChangeSets = useMemo(() => {
    const q = search.trim().toLowerCase()
    let list = fileChanges.filter((changeSet) => {
      if (selectedTypes.size > 0) {
        const hasType = changeSet.files.some((f) => selectedTypes.has(f.fileType))
        if (!hasType) return false
      }
      if (selectedCreators.size > 0) {
        const hasCreator = changeSet.files.some((f) => {
          const artifact = artifactsByPath.get(f.path)
          return artifact ? selectedCreators.has(artifact.creator) : selectedCreators.has('parent')
        })
        if (!hasCreator) return false
      }
      if (q) {
        const haystack = [
          changeSet.summary,
          changeSet.toolName,
          changeSet.source,
          ...changeSet.files.flatMap((f) => [f.path, f.filename, f.fileType, f.operation]),
        ].join(' ').toLowerCase()
        if (!haystack.includes(q)) return false
      }
      return true
    })

    list = [...list].sort((a, b) => {
      switch (sort) {
        case 'newest': return b.createdAt - a.createdAt
        case 'oldest': return a.createdAt - b.createdAt
        case 'title':
          return (a.files[0]?.filename ?? a.toolName).localeCompare(
            b.files[0]?.filename ?? b.toolName,
          )
        case 'creator':
          return a.toolName.localeCompare(b.toolName)
      }
    })
    return list
  }, [artifactsByPath, fileChanges, search, selectedCreators, selectedTypes, sort])

  const toggleType = useCallback((t: string) => {
    setSelectedTypes((prev) => {
      const next = new Set(prev)
      if (next.has(t)) next.delete(t)
      else next.add(t)
      return next
    })
  }, [])

  const toggleCreator = useCallback((c: string) => {
    setSelectedCreators((prev) => {
      const next = new Set(prev)
      if (next.has(c)) next.delete(c)
      else next.add(c)
      return next
    })
  }, [])

  const clearFilters = useCallback(() => {
    setSelectedTypes(new Set())
    setSelectedCreators(new Set())
    setSearch('')
  }, [])

  const hasActiveFilters = selectedTypes.size > 0 || selectedCreators.size > 0 || search.length > 0

  const openExternal = (path: string) => {
    const api = (window as any).harness
    if (api?.openExternal) api.openExternal(`file://${path}`)
  }

  const jumpToTool = (toolCallId: string) => {
    onClose()
    requestAnimationFrame(() => focusToolCall(toolCallId))
  }

  const previewArtifact = previewPath
    ? artifacts.find((a) => a.path === previewPath) ?? null
    : null

  return (
    <div className="fixed inset-0 z-50 flex flex-col bg-[#0c0c10]/95 backdrop-blur-md">
      {/* ── Header ─────────────────────────────────────── */}
      <Header
        total={artifacts.length}
        filteredCount={filtered.length}
        view={view}
        onView={setView}
        sort={sort}
        onSort={setSort}
        search={search}
        onSearch={setSearch}
        searchRef={searchRef}
        onClose={onClose}
        loading={loading}
        hasActiveFilters={hasActiveFilters}
        onClearFilters={clearFilters}
        changeSetCount={fileChanges.length}
      />

      {/* ── Body ───────────────────────────────────────── */}
      {previewArtifact ? (
        <PreviewPane
          artifact={previewArtifact}
          meta={byPath[previewArtifact.path]}
          onBack={() => setPreviewPath(null)}
          onOpenExternal={() => openExternal(previewArtifact.path)}
        />
      ) : (
        <div className="flex flex-1 min-h-0">
          <FilterRail
            typeBuckets={typeBuckets}
            creatorBuckets={creatorBuckets}
            selectedTypes={selectedTypes}
            selectedCreators={selectedCreators}
            onToggleType={toggleType}
            onToggleCreator={toggleCreator}
            onClear={clearFilters}
            hasActiveFilters={hasActiveFilters}
          />
          <div className="flex flex-1 min-w-0">
            {view === 'changes' ? (
              <ChangesWorkspaceView
                changeSets={filteredChangeSets}
                hasAnyChanges={fileChanges.length > 0}
                hasActiveFilters={hasActiveFilters}
                onClear={clearFilters}
                onOpenExternal={openExternal}
                onJumpToTool={jumpToTool}
              />
            ) : filtered.length === 0 ? (
              <EmptyState
                hasArtifacts={artifacts.length > 0}
                hasActiveFilters={hasActiveFilters}
                onClear={clearFilters}
              />
            ) : view === 'cards' ? (
              <CardGrid
                artifacts={filtered}
                byPath={byPath}
                onSelect={(a) => setPreviewPath(a.path)}
              />
            ) : view === 'list' ? (
              <ListView
                artifacts={filtered}
                byPath={byPath}
                onSelect={(a) => setPreviewPath(a.path)}
                onOpenExternal={(a) => openExternal(a.path)}
              />
            ) : (
              <DetailsView
                artifacts={filtered}
                byPath={byPath}
                selectedId={detailsSelectedId}
                onSelectId={setDetailsSelectedId}
                onOpenExternal={openExternal}
              />
            )}
          </div>
        </div>
      )}
    </div>
  )
}

// ──────────────────────────────────────────────────────────────────
// Header
// ──────────────────────────────────────────────────────────────────

function Header({
  total,
  filteredCount,
  view,
  onView,
  sort,
  onSort,
  search,
  onSearch,
  searchRef,
  onClose,
  loading,
  hasActiveFilters,
  onClearFilters,
  changeSetCount,
}: {
  total: number
  filteredCount: number
  view: ViewMode
  onView: (v: ViewMode) => void
  sort: SortKey
  onSort: (s: SortKey) => void
  search: string
  onSearch: (s: string) => void
  searchRef: React.RefObject<HTMLInputElement>
  onClose: () => void
  loading: boolean
  hasActiveFilters: boolean
  onClearFilters: () => void
  changeSetCount: number
}) {
  const title = view === 'changes' ? 'changes workspace' : 'artifact workspace'
  return (
    <div className="drag flex items-center gap-3 border-b border-white/[0.06] bg-[#0a0a0e]/60 pl-[82px] pr-3 py-2.5">
      {/* Title + count */}
      <div className="no-drag flex items-center gap-2">
        <span className="font-mono text-[11px] uppercase tracking-[0.12em] text-fg-1">
          {title}
        </span>
        <span className="font-mono text-[10px] text-fg-3">
          {view === 'changes'
            ? `${changeSetCount} change set${changeSetCount === 1 ? '' : 's'}`
            : hasActiveFilters && filteredCount !== total
            ? `${filteredCount} of ${total}`
            : `${total} file${total !== 1 ? 's' : ''}`}
        </span>
        {loading && (
          <span className="font-mono text-[9px] text-fg-3 italic">
            loading content…
          </span>
        )}
      </div>

      {/* Search */}
      <div className="no-drag relative ml-auto flex items-center">
        <svg
          width="11" height="11" viewBox="0 0 11 11"
          className="absolute left-2.5 text-fg-3"
          fill="none" stroke="currentColor" strokeWidth="1.5"
        >
          <circle cx="4.5" cy="4.5" r="3.2" />
          <path d="M7 7 L9.5 9.5" strokeLinecap="round" />
        </svg>
        <input
          ref={searchRef}
          value={search}
          onChange={(e) => onSearch(e.target.value)}
          placeholder={view === 'changes' ? 'search files, paths, tools…' : 'search title, content, creator…'}
          className="w-[280px] rounded-md bg-white/[0.04] py-1 pl-7 pr-2 font-prose text-[11px] text-fg-0 ring-hairline placeholder:text-fg-3/70 focus:bg-white/[0.06] focus:outline-none focus:ring-1 focus:ring-accent/30"
        />
        {search && (
          <button
            onClick={() => onSearch('')}
            className="absolute right-2 font-mono text-[10px] text-fg-3 hover:text-fg-0"
          >×</button>
        )}
      </div>

      {/* Sort */}
      <SortMenu sort={sort} onSort={onSort} />

      {/* View toggle */}
      <ViewToggle view={view} onView={onView} />

      <span className="h-3 w-px bg-white/10" />

      <button
        onClick={onClose}
        className="no-drag rounded-md px-2 py-1 font-mono text-[10px] text-fg-2 ring-hairline hover:bg-white/[0.06] hover:text-fg-0"
      >
        esc close
      </button>
    </div>
  )
}

function SortMenu({ sort, onSort }: { sort: SortKey; onSort: (s: SortKey) => void }) {
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!open) return
    const onClick = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    window.addEventListener('mousedown', onClick)
    return () => window.removeEventListener('mousedown', onClick)
  }, [open])

  const options: { key: SortKey; label: string }[] = [
    { key: 'newest', label: 'newest first' },
    { key: 'oldest', label: 'oldest first' },
    { key: 'title', label: 'title (a–z)' },
    { key: 'creator', label: 'creator (a–z)' },
  ]
  const active = options.find((o) => o.key === sort)

  return (
    <div ref={ref} className="no-drag relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-1.5 rounded-md px-2 py-1 font-mono text-[10px] text-fg-2 ring-hairline hover:bg-white/[0.06] hover:text-fg-0"
      >
        <span className="text-fg-3">sort:</span>
        <span className="text-fg-1">{active?.label ?? sort}</span>
        <svg width="8" height="6" viewBox="0 0 8 6" fill="currentColor"><path d="M4 6L0 0H8L4 6Z" /></svg>
      </button>
      {open && (
        <div className="absolute right-0 top-full z-30 mt-1 w-[160px] overflow-hidden rounded-md bg-[#15151b] py-1 shadow-xl ring-1 ring-white/10">
          {options.map((o) => (
            <button
              key={o.key}
              onClick={() => { onSort(o.key); setOpen(false) }}
              className={`flex w-full items-center gap-2 px-3 py-1.5 text-left font-mono text-[10px] transition-colors ${
                o.key === sort ? 'bg-accent/10 text-accent' : 'text-fg-1 hover:bg-white/[0.06]'
              }`}
            >
              <span className={`block h-1 w-1 rounded-full ${o.key === sort ? 'bg-accent' : 'bg-transparent'}`} />
              {o.label}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

function ViewToggle({ view, onView }: { view: ViewMode; onView: (v: ViewMode) => void }) {
  const buttons: Array<{ key: ViewMode; icon: string; label: string }> = [
    { key: 'cards', icon: '▦', label: 'cards' },
    { key: 'list', icon: '☰', label: 'list' },
    { key: 'details', icon: '◫', label: 'details' },
    { key: 'changes', icon: '±', label: 'changes' },
  ]
  return (
    <div className="no-drag flex items-center overflow-hidden rounded-md ring-hairline">
      {buttons.map((b) => (
        <button
          key={b.key}
          onClick={() => onView(b.key)}
          title={`${b.label} (⌘${buttons.findIndex((x) => x.key === b.key) + 1})`}
          className={`px-2 py-1 font-mono text-[11px] transition-colors ${
            view === b.key ? 'bg-accent/15 text-accent' : 'text-fg-3 hover:bg-white/[0.04] hover:text-fg-1'
          }`}
        >
          {b.icon}
        </button>
      ))}
    </div>
  )
}

// ──────────────────────────────────────────────────────────────────
// Filter rail
// ──────────────────────────────────────────────────────────────────

function FilterRail({
  typeBuckets,
  creatorBuckets,
  selectedTypes,
  selectedCreators,
  onToggleType,
  onToggleCreator,
  onClear,
  hasActiveFilters,
}: {
  typeBuckets: [string, number][]
  creatorBuckets: { id: string; label: string; count: number }[]
  selectedTypes: Set<string>
  selectedCreators: Set<string>
  onToggleType: (t: string) => void
  onToggleCreator: (c: string) => void
  onClear: () => void
  hasActiveFilters: boolean
}) {
  return (
    <aside className="flex w-[224px] shrink-0 flex-col overflow-y-auto border-r border-white/[0.06] bg-[#08080c]/60 px-3 py-3">
      {/* Active filters / clear */}
      <div className="mb-3 flex items-center justify-between">
        <span className="font-mono text-[9px] uppercase tracking-[0.12em] text-fg-3">
          filters
        </span>
        {hasActiveFilters && (
          <button
            onClick={onClear}
            className="font-mono text-[9px] text-accent/80 hover:text-accent"
          >clear all</button>
        )}
      </div>

      {/* Type filters */}
      <FilterGroup
        label="file type"
        items={typeBuckets.map(([id, count]) => ({
          id, label: `.${id}`, count, color: fileTypeColor(id),
        }))}
        selected={selectedTypes}
        onToggle={onToggleType}
      />

      {/* Creator filters */}
      <FilterGroup
        label="created by"
        items={creatorBuckets.map(({ id, label, count }) => ({
          id, label, count,
          color: id === 'parent' ? '#a8d4fc' : '#7ab8a3',
        }))}
        selected={selectedCreators}
        onToggle={onToggleCreator}
      />
    </aside>
  )
}

function FilterGroup({
  label,
  items,
  selected,
  onToggle,
}: {
  label: string
  items: { id: string; label: string; count: number; color: string }[]
  selected: Set<string>
  onToggle: (id: string) => void
}) {
  if (items.length === 0) return null
  return (
    <div className="mb-4">
      <div className="mb-1.5 font-mono text-[9px] uppercase tracking-[0.1em] text-fg-3">
        {label}
      </div>
      <div className="space-y-[2px]">
        {items.map((item) => {
          const active = selected.has(item.id)
          return (
            <button
              key={item.id}
              onClick={() => onToggle(item.id)}
              className={`flex w-full items-center gap-2 rounded px-2 py-1 text-left transition-colors ${
                active ? 'bg-accent/15 text-accent' : 'text-fg-1 hover:bg-white/[0.04]'
              }`}
            >
              <span
                className="block h-1.5 w-1.5 shrink-0 rounded-full"
                style={{ backgroundColor: active ? item.color : `${item.color}55` }}
              />
              <span className="min-w-0 flex-1 truncate font-prose text-[11px]">
                {item.label}
              </span>
              <span className="shrink-0 font-mono text-[9px] text-fg-3">
                {item.count}
              </span>
            </button>
          )
        })}
      </div>
    </div>
  )
}

// ──────────────────────────────────────────────────────────────────
// Card grid view
// ──────────────────────────────────────────────────────────────────

function CardGrid({
  artifacts,
  byPath,
  onSelect,
}: {
  artifacts: ArtifactRecord[]
  byPath: Record<string, ArtifactMeta>
  onSelect: (a: ArtifactRecord) => void
}) {
  return (
    <div className="flex-1 overflow-y-auto p-4">
      <div
        className="grid gap-3"
        style={{
          gridTemplateColumns: 'repeat(auto-fill, minmax(280px, 1fr))',
        }}
      >
        {artifacts.map((a) => (
          <Card
            key={a.id}
            artifact={a}
            meta={byPath[a.path]}
            onClick={() => onSelect(a)}
          />
        ))}
      </div>
    </div>
  )
}

function Card({
  artifact,
  meta,
  onClick,
}: {
  artifact: ArtifactRecord
  meta: ArtifactMeta | undefined
  onClick: () => void
}) {
  const typeColor = fileTypeColor(artifact.fileType)
  const aColor = agentTypeColor(meta?.agentType)
  const isSubagent = artifact.creator !== 'parent'
  const title = meta?.title ?? artifact.filename
  const excerpt = meta?.excerpt ?? ''
  const loaded = meta !== undefined

  return (
    <button
      onClick={onClick}
      className="group relative flex flex-col overflow-hidden rounded-xl bg-[#11111a] text-left ring-1 ring-white/[0.06] transition-all hover:bg-[#13131c] hover:ring-accent/30"
      style={{ minHeight: 200 }}
    >
      {/* Top accent stripe */}
      <div
        className="h-[3px] w-full"
        style={{ backgroundColor: typeColor, opacity: 0.7 }}
      />

      {/* Body */}
      <div className="flex flex-1 flex-col gap-2 px-4 pt-3 pb-3">
        {/* Top row: file ext + agent type tag */}
        <div className="flex items-center gap-1.5">
          <span
            className="font-mono text-[9px] font-bold uppercase tracking-[0.08em]"
            style={{ color: typeColor }}
          >
            .{artifact.fileType}
          </span>
          {meta?.agentType && (
            <span
              className="font-mono text-[8.5px] uppercase tracking-[0.06em]"
              style={{ color: aColor }}
            >
              · {meta.agentType}
            </span>
          )}
          <span className="ml-auto font-mono text-[9px] text-fg-3">
            {relativeTime(artifact.createdAt)}
          </span>
        </div>

        {/* Title */}
        <div className="font-prose text-[13.5px] font-medium leading-[1.35] text-fg-0 line-clamp-2">
          {loaded ? (
            title
          ) : (
            <ShimmerLine width="80%" />
          )}
        </div>

        {/* Excerpt */}
        <div className="flex-1 font-prose text-[11px] leading-[1.55] text-fg-2 line-clamp-3">
          {loaded ? (
            excerpt || <span className="italic text-fg-3">no preview</span>
          ) : (
            <>
              <ShimmerLine width="92%" />
              <ShimmerLine width="86%" />
              <ShimmerLine width="68%" />
            </>
          )}
        </div>

        {/* Footer */}
        <div className="mt-auto flex items-center gap-2 pt-1.5 border-t border-white/[0.04]">
          {isSubagent ? (
            <CreatorBadge label={artifact.creatorLabel} color={aColor} />
          ) : (
            <CreatorBadge label="Main agent" color="#a8d4fc" />
          )}
          {artifact.operation === 'edit' && (
            <span className="font-mono text-[8.5px] uppercase tracking-[0.04em] text-warn/80">
              edited
            </span>
          )}
        </div>
      </div>
    </button>
  )
}

function CreatorBadge({ label, color }: { label: string; color: string }) {
  return (
    <span
      className="inline-flex items-center gap-1 truncate rounded-full px-2 py-[2px] font-mono text-[9px] font-bold uppercase tracking-[0.04em]"
      style={{
        color,
        backgroundColor: `${color}18`,
        boxShadow: `inset 0 0 0 1px ${color}30`,
      }}
      title={label}
    >
      <span
        className="block h-1 w-1 shrink-0 rounded-full"
        style={{ backgroundColor: color }}
      />
      <span className="truncate">{label}</span>
    </span>
  )
}

function ShimmerLine({ width }: { width: string }) {
  return (
    <div
      className="my-[3px] h-[1em] animate-pulse rounded bg-white/[0.05]"
      style={{ width }}
    />
  )
}

// ──────────────────────────────────────────────────────────────────
// Compact list view
// ──────────────────────────────────────────────────────────────────

function ListView({
  artifacts,
  byPath,
  onSelect,
  onOpenExternal,
}: {
  artifacts: ArtifactRecord[]
  byPath: Record<string, ArtifactMeta>
  onSelect: (a: ArtifactRecord) => void
  onOpenExternal: (a: ArtifactRecord) => void
}) {
  return (
    <div className="flex-1 overflow-y-auto">
      <div className="sticky top-0 z-10 grid items-center gap-3 border-b border-white/[0.06] bg-[#0a0a0e]/95 px-4 py-2 font-mono text-[9px] uppercase tracking-[0.1em] text-fg-3"
        style={{ gridTemplateColumns: '24px 1fr 200px 80px 80px' }}
      >
        <span></span>
        <span>title</span>
        <span>creator</span>
        <span>created</span>
        <span></span>
      </div>
      {artifacts.map((a) => {
        const meta = byPath[a.path]
        const typeColor = fileTypeColor(a.fileType)
        const aColor = agentTypeColor(meta?.agentType)
        return (
          <button
            key={a.id}
            onClick={() => onSelect(a)}
            className="group grid w-full items-center gap-3 border-b border-white/[0.03] px-4 py-2 text-left transition-colors hover:bg-white/[0.03]"
            style={{ gridTemplateColumns: '24px 1fr 200px 80px 80px' }}
          >
            <span
              className="font-mono text-[9px] font-bold uppercase"
              style={{ color: typeColor }}
              title={a.fileType}
            >
              .{a.fileType.slice(0, 4)}
            </span>
            <span className="min-w-0 truncate font-prose text-[12px] text-fg-0">
              {meta?.title ?? a.filename}
            </span>
            <span className="min-w-0">
              <CreatorBadge
                label={a.creator === 'parent' ? 'Main agent' : a.creatorLabel}
                color={a.creator === 'parent' ? '#a8d4fc' : aColor}
              />
            </span>
            <span className="font-mono text-[10px] text-fg-3">
              {relativeTime(a.createdAt)}
            </span>
            <span className="flex justify-end opacity-0 group-hover:opacity-100">
              <button
                onClick={(e) => { e.stopPropagation(); onOpenExternal(a) }}
                className="font-mono text-[9px] text-fg-3 hover:text-fg-0"
                title="Open externally"
              >↗</button>
            </span>
          </button>
        )
      })}
    </div>
  )
}

// ──────────────────────────────────────────────────────────────────
// Details view: list on left, always-on preview on right
// ──────────────────────────────────────────────────────────────────

function DetailsView({
  artifacts,
  byPath,
  selectedId,
  onSelectId,
  onOpenExternal,
}: {
  artifacts: ArtifactRecord[]
  byPath: Record<string, ArtifactMeta>
  selectedId: string | null
  onSelectId: (id: string | null) => void
  onOpenExternal: (path: string) => void
}) {
  const selected = useMemo(
    () => artifacts.find((a) => a.id === selectedId) ?? artifacts[0] ?? null,
    [artifacts, selectedId],
  )
  // Auto-select first artifact
  useEffect(() => {
    if (!selectedId && artifacts.length > 0) onSelectId(artifacts[0].id)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [artifacts.length])

  return (
    <div className="flex flex-1 min-w-0">
      {/* Left: artifact titles */}
      <div className="flex w-[280px] shrink-0 flex-col overflow-y-auto border-r border-white/[0.06]">
        {artifacts.map((a) => {
          const meta = byPath[a.path]
          const isSelected = a.id === selected?.id
          const typeColor = fileTypeColor(a.fileType)
          const aColor = agentTypeColor(meta?.agentType)
          return (
            <button
              key={a.id}
              onClick={() => onSelectId(a.id)}
              className={`flex flex-col gap-0.5 border-b border-white/[0.03] px-4 py-2.5 text-left transition-colors ${
                isSelected ? 'bg-accent/10' : 'hover:bg-white/[0.03]'
              }`}
            >
              <div className="flex items-center gap-2">
                <span className="font-mono text-[8.5px] font-bold uppercase" style={{ color: typeColor }}>
                  .{a.fileType}
                </span>
                <span className="ml-auto font-mono text-[9px] text-fg-3">
                  {relativeTime(a.createdAt)}
                </span>
              </div>
              <div className="font-prose text-[12px] leading-[1.3] text-fg-0 line-clamp-2">
                {meta?.title ?? a.filename}
              </div>
              <CreatorBadge
                label={a.creator === 'parent' ? 'Main agent' : a.creatorLabel}
                color={a.creator === 'parent' ? '#a8d4fc' : aColor}
              />
            </button>
          )
        })}
      </div>

      {/* Right: live preview */}
      <div className="flex flex-1 min-w-0 flex-col">
        {selected ? (
          <>
            <div className="flex items-center gap-3 border-b border-white/[0.06] px-5 py-2">
              <span className="truncate font-prose text-[12px] text-fg-0">
                {byPath[selected.path]?.title ?? selected.filename}
              </span>
              <span className="font-mono text-[9px] uppercase text-fg-3">
                {selected.fileType}
              </span>
              <button
                onClick={() => onOpenExternal(selected.path)}
                className="ml-auto rounded-md px-2 py-1 font-mono text-[10px] text-accent ring-1 ring-accent/30 hover:bg-accent/10"
              >open externally ↗</button>
            </div>
            <div className="flex-1 min-h-0 overflow-hidden">
              <ArtifactPreview path={selected.path} fileType={selected.fileType} />
            </div>
          </>
        ) : (
          <div className="flex flex-1 items-center justify-center text-fg-3">
            Select an artifact
          </div>
        )}
      </div>
    </div>
  )
}

// ──────────────────────────────────────────────────────────────────
// Changes view: grouped diffs with transcript navigation
// ──────────────────────────────────────────────────────────────────

function ChangesWorkspaceView({
  changeSets,
  hasAnyChanges,
  hasActiveFilters,
  onClear,
  onOpenExternal,
  onJumpToTool,
}: {
  changeSets: FileChangeSet[]
  hasAnyChanges: boolean
  hasActiveFilters: boolean
  onClear: () => void
  onOpenExternal: (path: string) => void
  onJumpToTool: (toolCallId: string) => void
}) {
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const selected = useMemo(
    () => changeSets.find((c) => c.id === selectedId) ?? changeSets[0] ?? null,
    [changeSets, selectedId],
  )

  useEffect(() => {
    if (!selectedId && changeSets.length > 0) {
      setSelectedId(changeSets[0].id)
    } else if (selectedId && !changeSets.some((c) => c.id === selectedId)) {
      setSelectedId(changeSets[0]?.id ?? null)
    }
  }, [changeSets, selectedId])

  const totals = useMemo(
    () =>
      changeSets.reduce(
        (acc, changeSet) => ({
          files: acc.files + changeSet.totals.files,
          additions: acc.additions + changeSet.totals.additions,
          deletions: acc.deletions + changeSet.totals.deletions,
        }),
        { files: 0, additions: 0, deletions: 0 },
      ),
    [changeSets],
  )

  if (changeSets.length === 0) {
    return (
      <EmptyState
        hasArtifacts={hasAnyChanges}
        hasActiveFilters={hasActiveFilters}
        onClear={onClear}
        title={hasAnyChanges ? 'No matching changes' : 'No file changes yet'}
        detail={
          hasAnyChanges
            ? 'Try clearing filters or adjusting your search.'
            : 'File edits and writes will appear here with diffs.'
        }
      />
    )
  }

  return (
    <div className="flex flex-1 min-w-0">
      <div className="flex w-[340px] shrink-0 flex-col border-r border-white/[0.06] bg-[#08080c]/40">
        <div className="border-b border-white/[0.06] px-4 py-3">
          <div className="font-mono text-[9px] uppercase tracking-[0.12em] text-fg-3">
            change sets
          </div>
          <div className="mt-1 flex items-center gap-2 font-mono text-[10px] text-fg-2">
            <span>{changeSets.length} turns</span>
            <span>·</span>
            <span>{totals.files} files</span>
            <span>·</span>
            <span className="text-ok">+{totals.additions}</span>
            <span className="text-danger">-{totals.deletions}</span>
          </div>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {changeSets.map((changeSet) => (
            <ChangeSetListButton
              key={changeSet.id}
              changeSet={changeSet}
              selected={changeSet.id === selected?.id}
              onSelect={() => setSelectedId(changeSet.id)}
            />
          ))}
        </div>
      </div>

      <div className="flex min-w-0 flex-1 flex-col">
        {selected && (
          <>
            <div className="flex items-center gap-3 border-b border-white/[0.06] px-5 py-2.5">
              <div className="min-w-0 flex-1">
                <div className="truncate font-prose text-[12px] text-fg-0">
                  {selected.summary}
                </div>
                <div className="mt-0.5 font-mono text-[9px] text-fg-3">
                  {selected.toolName} · {relativeTime(selected.createdAt)}
                </div>
              </div>
              <button
                onClick={() => onJumpToTool(selected.toolCallId)}
                className="rounded-md px-2 py-1 font-mono text-[10px] text-accent ring-1 ring-accent/30 hover:bg-accent/10"
              >
                jump to tool
              </button>
            </div>
            <div className="min-h-0 flex-1 overflow-y-auto p-4">
              <FileChangeCard
                changeSet={selected}
                onOpenFile={onOpenExternal}
                onJumpToTool={onJumpToTool}
                title="selected diff"
              />
            </div>
          </>
        )}
      </div>
    </div>
  )
}

function ChangeSetListButton({
  changeSet,
  selected,
  onSelect,
}: {
  changeSet: FileChangeSet
  selected: boolean
  onSelect: () => void
}) {
  const firstFile = changeSet.files[0]
  const extra = Math.max(0, changeSet.files.length - 1)
  return (
    <button
      onClick={onSelect}
      className={`flex w-full flex-col gap-1 border-b border-white/[0.03] px-4 py-3 text-left transition-colors ${
        selected ? 'bg-accent/10' : 'hover:bg-white/[0.03]'
      }`}
    >
      <div className="flex items-center gap-2">
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${
          changeSet.source === 'bash' ? 'bg-warn' : 'bg-ok'
        }`} />
        <span className="min-w-0 flex-1 truncate font-mono text-[10.5px] text-fg-0">
          {firstFile?.filename ?? changeSet.toolName}
        </span>
        {extra > 0 && (
          <span className="font-mono text-[9px] text-fg-3">+{extra}</span>
        )}
      </div>
      <div className="truncate font-mono text-[9px] text-fg-3">
        {changeSet.summary}
      </div>
      <div className="flex items-center gap-1.5 font-mono text-[8.5px] text-fg-3">
        <span>{changeSet.toolName}</span>
        <span>·</span>
        <span>{relativeTime(changeSet.createdAt)}</span>
      </div>
    </button>
  )
}

// ──────────────────────────────────────────────────────────────────
// Empty state
// ──────────────────────────────────────────────────────────────────

function EmptyState({
  hasArtifacts,
  hasActiveFilters,
  onClear,
  title,
  detail,
}: {
  hasArtifacts: boolean
  hasActiveFilters: boolean
  onClear: () => void
  title?: string
  detail?: string
}) {
  return (
    <div className="flex flex-1 items-center justify-center p-8">
      <div className="max-w-[400px] text-center">
        <div className="mb-3 font-display text-[32px] text-fg-3 opacity-40">
          ◇
        </div>
        {!hasArtifacts ? (
          <>
            <div className="mb-1 font-prose text-[14px] text-fg-1">
              {title ?? 'No artifacts yet'}
            </div>
            <div className="font-mono text-[10.5px] text-fg-3">
              {detail ?? 'Files created by agents will appear here.'}
            </div>
          </>
        ) : hasActiveFilters ? (
          <>
            <div className="mb-1 font-prose text-[14px] text-fg-1">
              {title ?? 'No matches'}
            </div>
            <div className="mb-3 font-mono text-[10.5px] text-fg-3">
              {detail ?? 'Try clearing some filters or adjusting your search.'}
            </div>
            <button
              onClick={onClear}
              className="rounded-md bg-accent/15 px-3 py-1.5 font-mono text-[10.5px] text-accent ring-1 ring-accent/30 hover:bg-accent/25"
            >clear filters</button>
          </>
        ) : null}
      </div>
    </div>
  )
}

// ──────────────────────────────────────────────────────────────────
// Preview pane (full-screen overlay)
// ──────────────────────────────────────────────────────────────────

function PreviewPane({
  artifact,
  meta,
  onBack,
  onOpenExternal,
}: {
  artifact: ArtifactRecord
  meta: ArtifactMeta | undefined
  onBack: () => void
  onOpenExternal: () => void
}) {
  const typeColor = fileTypeColor(artifact.fileType)
  const aColor = agentTypeColor(meta?.agentType)
  return (
    <div className="flex flex-1 min-h-0 flex-col">
      <div className="flex items-center gap-3 border-b border-white/[0.06] px-5 py-2">
        <button
          onClick={onBack}
          className="rounded-md px-2 py-1 font-mono text-[10px] text-fg-2 ring-hairline hover:bg-white/[0.06] hover:text-fg-0"
        >← back</button>
        <span
          className="font-mono text-[9px] font-bold uppercase tracking-[0.08em]"
          style={{ color: typeColor }}
        >
          .{artifact.fileType}
        </span>
        <span className="truncate font-prose text-[12px] text-fg-0">
          {meta?.title ?? artifact.filename}
        </span>
        {meta?.agentType && artifact.creator !== 'parent' && (
          <CreatorBadge label={artifact.creatorLabel} color={aColor} />
        )}
        <span className="ml-auto font-mono text-[10px] text-fg-3">
          {relativeTime(artifact.createdAt)}
        </span>
        <button
          onClick={onOpenExternal}
          className="rounded-md px-2 py-1 font-mono text-[10px] text-accent ring-1 ring-accent/30 hover:bg-accent/10"
        >open externally ↗</button>
      </div>
      <div className="flex-1 min-h-0 overflow-hidden">
        <ArtifactPreview path={artifact.path} fileType={artifact.fileType} />
      </div>
    </div>
  )
}
