import { useEffect, useMemo, useState } from 'react'
import { useHarness } from '../state/store'
import {
  ChildLabel,
  DatelineTS,
  GalleyArtifact,
  SectionSlug,
  StatusPip,
  type PipStatus,
} from './memory/primitives'

/**
 * Working Memory — the centerpiece of Grounded Memory, in THE LEDGER-CARDS
 * direction. The agent's structured, durable notes (workstream / decision /
 * finding / open_thread / artifact_note) rendered as discrete .glass-raised
 * cards. The single ACTIVE workstream's card gets the accent ring + the
 * council-tile is-running pulse (the only moving thing — "the ink is still
 * wet"); paused cards dim to fg-2 with warn pips; finished workstreams fold
 * to one quiet sage line at the bottom.
 *
 * Reads the ONE working_memory.json doc via window.harness.getWorkingMemory,
 * refetched on the activityTick (systemEvents.length), exactly cloning the
 * ActionLedgerSection fetch idiom. Every visual atom comes from the shared
 * memory primitives so the panel reads as one crafted system.
 */

/** One entity from working_memory.json's `entities` map (bridge/working_memory.py). */
type Entity = {
  id: string
  type: 'workstream' | 'decision' | 'finding' | 'open_thread' | 'artifact_note'
  status?: string
  title?: string
  text?: string
  request?: string
  rationale?: string
  source?: string
  path?: string
  note?: string
  workstreamId?: string
  additions?: number | null
  deletions?: number | null
  diff?: string | null
  diffTruncated?: boolean
  createdAt?: number
  updatedAt?: number
}

type Grouped = {
  decisions: Entity[]
  findings: Entity[]
  notes: Entity[]
  threads: Entity[]
}

/** Map a workstream status to a StatusPip status (active|paused|done). */
function wsPip(status?: string): PipStatus {
  if (status === 'paused') return 'paused'
  if (status === 'done') return 'done'
  return 'active'
}

function openFile(path?: string) {
  if (!path) return
  const api = (window as any).harness
  if (api?.openExternal) api.openExternal(`file://${path}`)
}

export function WorkingMemorySection({ topOffset = 0 }: { topOffset?: number }) {
  const activeSessionId = useHarness((s) => s.activeSessionId)
  const openRecallDrawer = useHarness((s) => s.openRecallDrawer)
  // Refetch when new activity lands (cheap, avoids polling) — same tick as the ledger.
  const activityTick = useHarness((s) => s.systemEvents.length)
  const [entities, setEntities] = useState<Record<string, Entity>>({})
  const [loaded, setLoaded] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState(true)
  const [showDone, setShowDone] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoaded(false)
    const run = async () => {
      const api = (window as any).harness
      if (!api?.getWorkingMemory || !activeSessionId) {
        setEntities({})
        setLoaded(true)
        return
      }
      try {
        const res = await api.getWorkingMemory(activeSessionId)
        if (cancelled) return
        if (!res?.ok) {
          setError(res?.error ?? 'failed to load')
          setEntities({})
          setLoaded(true)
          return
        }
        setError(null)
        setEntities((res.entities ?? {}) as Record<string, Entity>)
        setLoaded(true)
      } catch (err) {
        if (!cancelled) {
          setError(String(err))
          setLoaded(true)
        }
      }
    }
    void run()
    return () => {
      cancelled = true
    }
  }, [activeSessionId, activityTick])

  const { workstreams, doneStreams, childrenOf } = useMemo(() => {
    const list = Object.values(entities)
    const byType: Record<string, Entity[]> = {
      workstream: [],
      decision: [],
      finding: [],
      open_thread: [],
      artifact_note: [],
    }
    for (const e of list) {
      if (e && e.type && byType[e.type]) byType[e.type].push(e)
    }
    // Stable order: newest workstreams first so the live edition leads.
    const sortDesc = (a: Entity, b: Entity) =>
      (b.createdAt ?? 0) - (a.createdAt ?? 0)
    const allStreams = [...byType.workstream].sort(sortDesc)
    const live = allStreams.filter((w) => w.status !== 'done')
    // Live first: active before paused, each newest-first.
    live.sort((a, b) => {
      const rank = (w: Entity) => (w.status === 'paused' ? 1 : 0)
      const r = rank(a) - rank(b)
      return r !== 0 ? r : (b.createdAt ?? 0) - (a.createdAt ?? 0)
    })
    const done = allStreams.filter((w) => w.status === 'done')

    const childrenOf = (wsId: string): Grouped => {
      const within = (arr: Entity[]) =>
        arr
          .filter((e) => e.workstreamId === wsId)
          .sort((a, b) => (a.createdAt ?? 0) - (b.createdAt ?? 0))
      return {
        decisions: within(byType.decision),
        findings: within(byType.finding),
        notes: within(byType.artifact_note),
        threads: within(byType.open_thread),
      }
    }
    return { workstreams: live, doneStreams: done, childrenOf }
  }, [entities])

  const total = workstreams.length + doneStreams.length

  return (
    <div className="hairline-b">
      <SectionSlug
        kicker="working memory"
        count={total > 0 ? total : undefined}
        expanded={expanded}
        onToggle={() => setExpanded((v) => !v)}
        topOffset={topOffset}
      >
        <button
          type="button"
          onClick={() => openRecallDrawer()}
          title="Search this session's full transcript"
          className="rounded bg-white/[0.04] px-1.5 py-[2px] font-mono text-[9px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
        >
          history ↗
        </button>
      </SectionSlug>

      {!expanded ? null : !loaded ? (
        <LoadingGhost />
      ) : error ? (
        <div className="px-4 pb-3 pt-2 font-mono text-[11px] text-fg-3">
          memory unavailable: {error}
        </div>
      ) : total === 0 ? (
        <div className="px-4 pb-3 pt-2 font-mono text-[11px] italic text-fg-3">
          No workstreams set this session.
        </div>
      ) : (
        <div className="space-y-2.5 px-4 pb-3 pt-2">
          {workstreams.map((ws) => (
            <WorkstreamCard
              key={ws.id}
              workstream={ws}
              groups={childrenOf(ws.id)}
            />
          ))}

          {doneStreams.length > 0 && (
            <DoneFold
              streams={doneStreams}
              expanded={showDone}
              onToggle={() => setShowDone((v) => !v)}
              childrenOf={childrenOf}
            />
          )}
        </div>
      )}
    </div>
  )
}

/* ──────────────────────────────────────────────────────────────────────
   A single workstream Ledger-Card.
   ────────────────────────────────────────────────────────────────────── */

function WorkstreamCard({
  workstream,
  groups,
}: {
  workstream: Entity
  groups: Grouped
}) {
  const status = workstream.status ?? 'active'
  const isPaused = status === 'paused'
  // The single live card: accent ring + the council-tile pulse. Paused cards
  // dim and lose the ring/pulse so exactly one thing moves on screen.
  const isLive = !isPaused && status !== 'done'
  const statusWord = isPaused ? 'paused' : status === 'done' ? 'done' : 'active'
  const ts = workstream.updatedAt ?? workstream.createdAt ?? Date.now()

  return (
    <div
      className={`glass-raised animate-fade-in rounded-lg p-3 ${
        isLive ? 'memory-card--live' : ''
      } ${isPaused ? 'opacity-70' : ''}`}
    >
      {/* Card head: status pip + task title + status word + dateline */}
      <div className="flex items-baseline gap-2">
        <StatusPip status={wsPip(status)} className="translate-y-[-1px]" />
        <span
          className={`min-w-0 flex-1 truncate font-mono text-[12px] font-medium leading-tight ${
            isPaused ? 'text-fg-2' : 'text-fg-0'
          }`}
        >
          {workstream.title || 'Untitled task'}
        </span>
        <span
          className={`shrink-0 font-mono text-[9px] uppercase tracking-[0.12em] ${
            isPaused ? 'text-warn' : isLive ? 'text-accent' : 'text-fg-3'
          }`}
        >
          {statusWord}
        </span>
        <DatelineTS ts={ts} />
      </div>

      {/* The goal/request — a readable mono line under a left rule. */}
      {workstream.request && (
        <div
          className={`mt-1.5 border-l-2 pl-2 font-mono text-[10.5px] leading-snug ${
            isPaused ? 'border-fg-4 text-fg-2' : 'border-fg-3 text-fg-1'
          }`}
        >
          {workstream.request}
        </div>
      )}

      {hasChildren(groups) && (
        <>
          <div className="memory-dash-rule my-2" />
          <div className="space-y-1.5">
            {groups.decisions.map((d) => (
              <DecisionRow key={d.id} entity={d} />
            ))}
            {groups.findings.map((f) => (
              <FindingRow key={f.id} entity={f} />
            ))}
            {groups.notes.map((n) => (
              <GalleyArtifact
                key={n.id}
                path={n.path ?? n.id}
                note={n.note}
                additions={n.additions}
                deletions={n.deletions}
                diff={n.diff}
                diffTruncated={n.diffTruncated}
                onOpen={n.path ? () => openFile(n.path) : undefined}
              />
            ))}
            {groups.threads.map((t) => (
              <ThreadRow key={t.id} entity={t} />
            ))}
          </div>
        </>
      )}
    </div>
  )
}

function hasChildren(g: Grouped): boolean {
  return (
    g.decisions.length > 0 ||
    g.findings.length > 0 ||
    g.notes.length > 0 ||
    g.threads.length > 0
  )
}

/* ──────────────────────────────────────────────────────────────────────
   Typed-child rows — a labeled left column (decided / found / open) so each
   entry says what it is, content aligned to a common left edge.
   ────────────────────────────────────────────────────────────────────── */

function DecisionRow({ entity }: { entity: Entity }) {
  return (
    <div className="flex items-baseline gap-2">
      <ChildLabel text="decided" />
      <div className="min-w-0 flex-1">
        <div className="font-mono text-[10.5px] leading-[1.5] text-fg-1">{entity.title}</div>
        {entity.rationale && (
          <div className="mt-0.5 font-mono text-[10px] leading-[1.5] text-fg-2">
            {entity.rationale}
          </div>
        )}
      </div>
    </div>
  )
}

function FindingRow({ entity }: { entity: Entity }) {
  return (
    <div className="flex items-baseline gap-2">
      <ChildLabel text="found" />
      <div className="min-w-0 flex-1">
        <div className="font-mono text-[10.5px] leading-[1.5] text-fg-1">{entity.text}</div>
        {entity.source && (
          // The `source` is free-text attribution (how the finding was learned),
          // NOT a link — render it muted on its own line so it doesn't read as
          // a broken clickable citation.
          <div className="mt-0.5 font-mono text-[9.5px] leading-[1.5] text-fg-3">
            source · {entity.source}
          </div>
        )}
      </div>
    </div>
  )
}

function ThreadRow({ entity }: { entity: Entity }) {
  const resolved = entity.status === 'resolved'
  return (
    <div className="flex items-baseline gap-2">
      <ChildLabel text="open" color={resolved ? 'text-fg-3' : 'text-warn'} />
      <span
        className={`min-w-0 flex-1 font-mono text-[10.5px] leading-[1.5] ${
          resolved ? 'text-fg-3 line-through' : 'text-fg-1'
        }`}
      >
        {entity.text}
      </span>
    </div>
  )
}

/* ──────────────────────────────────────────────────────────────────────
   Done fold — every finished workstream collapses to one quiet sage line.
   ────────────────────────────────────────────────────────────────────── */

function DoneFold({
  streams,
  expanded,
  onToggle,
  childrenOf,
}: {
  streams: Entity[]
  expanded: boolean
  onToggle: () => void
  childrenOf: (wsId: string) => Grouped
}) {
  const titles = streams
    .map((w) => w.title || 'untitled')
    .filter(Boolean)
  return (
    <div className="memory-dash-rule--t pt-2">
      <button
        type="button"
        onClick={onToggle}
        className="flex w-full items-baseline gap-1.5 text-left font-mono text-[10.5px] leading-[1.5] text-ok transition-colors hover:text-fg-1"
      >
        <span className="shrink-0">✓</span>
        <span className="shrink-0 tabular-nums">{streams.length} done</span>
        <span className="min-w-0 flex-1 truncate text-fg-3">
          · {titles.join(', ')}
        </span>
        <span className="shrink-0 text-[9px] text-fg-3">{expanded ? '▾' : '▸'}</span>
      </button>

      {expanded && (
        <div className="mt-2 space-y-2.5">
          {streams.map((ws) => (
            <WorkstreamCard key={ws.id} workstream={ws} groups={childrenOf(ws.id)} />
          ))}
        </div>
      )}
    </div>
  )
}

/* ──────────────────────────────────────────────────────────────────────
   Loading state — ghost rows + one shimmer (the single moving thing while
   the doc loads).
   ────────────────────────────────────────────────────────────────────── */

function LoadingGhost() {
  return (
    <div className="space-y-2.5 px-4 pb-3 pt-2" aria-hidden="true">
      <div className="glass-raised rounded-lg p-3">
        <div className="flex items-center gap-2">
          <span className="h-[5px] w-[5px] shrink-0 rounded-full bg-fg-4" />
          {/* The single shimmer — the shipped animate-shimmer overlay idiom. */}
          <div className="relative h-3 w-1/2 overflow-hidden rounded bg-white/[0.04]">
            <div
              className="absolute inset-y-0 left-0 w-1/3 animate-shimmer rounded-full"
              style={{
                backgroundImage:
                  'linear-gradient(90deg, transparent 0%, rgba(168,212,252,0.45) 50%, transparent 100%)',
                backgroundSize: '200% 100%',
              }}
            />
          </div>
        </div>
        <div className="mt-2 h-2.5 w-3/4 rounded bg-white/[0.025]" />
        <div className="memory-dash-rule my-2" />
        <div className="space-y-1.5 pl-1">
          <div className="h-2.5 w-2/3 animate-pulse rounded bg-white/[0.025]" />
          <div className="h-2.5 w-1/2 animate-pulse rounded bg-white/[0.025]" />
        </div>
      </div>
      <div className="glass-raised rounded-lg p-3 opacity-60">
        <div className="flex items-center gap-2">
          <span className="h-[5px] w-[5px] shrink-0 rounded-full bg-fg-4" />
          <div className="h-3 w-2/5 animate-pulse rounded bg-white/[0.03]" />
        </div>
        <div className="mt-2 h-2.5 w-1/2 animate-pulse rounded bg-white/[0.02]" />
      </div>
    </div>
  )
}
