import { Fragment, useEffect, useMemo, useState } from 'react'
import { useHarness } from '../state/store'
import { formatCost, formatDuration, formatTokens } from '../lib/format'
import type { CompactionTelemetryRow } from '@shared/events'

/**
 * Compaction Metrics Dashboard.
 *
 * Cross-session aggregation of every pressure signal, compaction event,
 * and per-call LLM metric the bridge has persisted to ~/.freyja/telemetry/
 * compaction.jsonl. Opens via the header button.
 *
 * Two-layer interactive surface:
 *   1. Aggregated view across all sessions (stat tiles + charts + table)
 *   2. Click a session row → detail drawer slides in from the right,
 *      *squeezing* the main panel to the left rather than overlaying.
 *      Drawer shows per-call timeline, per-call table, compaction log.
 *
 * Filters: time range (24h / 7d / 30d / all), group-by (none / model /
 * day / peak-band), narrow filter (had-compaction / had-thrash / model).
 *
 * All charts hand-rolled SVG. No chart library, no extra deps.
 */
export function MetricsDashboard() {
  const open = useHarness((s) => s.metricsDashboardOpen)
  const toggle = useHarness((s) => s.toggleMetricsDashboard)
  const [rawRows, setRawRows] = useState<CompactionTelemetryRow[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [lastFetchedAt, setLastFetchedAt] = useState<number>(0)

  // View toggle: sessions (default) or profiles (sub-agent profile breakdown).
  // Both views share the same time-range / filter controls.
  const [view, setView] = useState<'sessions' | 'profiles'>('sessions')

  // Filters
  const [timeRange, setTimeRange] = useState<'24h' | '7d' | '30d' | 'all'>('7d')
  const [groupBy, setGroupBy] = useState<'none' | 'model' | 'day' | 'band'>('none')
  const [narrowFilter, setNarrowFilter] = useState<
    'all' | 'has_compaction' | 'has_thrash' | 'has_subagents'
  >('all')

  // Selected entity for the right-side detail drawer.
  const [selectedSessionId, setSelectedSessionId] = useState<string | null>(null)
  const [selectedProfileId, setSelectedProfileId] = useState<string | null>(null)

  const refresh = async () => {
    setLoading(true)
    setError(null)
    try {
      const api = (window as any).harness
      if (!api?.compactionMetrics) {
        setError('Metrics IPC not available')
        return
      }
      const res = await api.compactionMetrics()
      if (!res.ok) {
        setError(res.error || 'Failed to load metrics')
        setRawRows([])
        return
      }
      setRawRows((res.rows || []) as CompactionTelemetryRow[])
      setLastFetchedAt(Date.now())
    } catch (err) {
      setError(String(err))
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (!open) return
    void refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  // Close on Esc; close the drawer first if open.
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        if (selectedProfileId) setSelectedProfileId(null)
        else if (selectedSessionId) setSelectedSessionId(null)
        else toggle(false)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, selectedSessionId, selectedProfileId, toggle])

  // Apply time-range filter to the raw rows before aggregation.
  const filteredRows = useMemo(() => {
    if (timeRange === 'all') return rawRows
    const now = Date.now() / 1000
    const cutoff =
      now -
      (timeRange === '24h' ? 86400 : timeRange === '7d' ? 7 * 86400 : 30 * 86400)
    return rawRows.filter((r) => (r as any).ts >= cutoff)
  }, [rawRows, timeRange])

  const aggregate = useMemo(
    () => aggregateRows(filteredRows, narrowFilter),
    [filteredRows, narrowFilter],
  )

  const profilesAggregate = useMemo(
    () => aggregateProfiles(filteredRows),
    [filteredRows],
  )

  if (!open) return null

  const sessionDrawerOpen = view === 'sessions' && selectedSessionId !== null
  const profileDrawerOpen = view === 'profiles' && selectedProfileId !== null
  const selectedSession = sessionDrawerOpen
    ? aggregate.perSession.find((s) => s.sessionId === selectedSessionId)
    : null
  const selectedProfile = profileDrawerOpen
    ? profilesAggregate.perProfile.find((p) => p.agentType === selectedProfileId)
    : null

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-6">
      <div
        className="absolute inset-0 bg-black/70 backdrop-blur-[6px]"
        onClick={() => toggle(false)}
      />
      <div
        className="relative flex h-[88vh] w-[min(1400px,96vw)] overflow-hidden rounded-2xl glass-strong ring-hairline-strong shadow-2xl"
      >
        {/* Main panel — flex-1, *squeezes* when the drawer opens */}
        <div className="flex min-w-0 flex-1 flex-col">
          {/* Header */}
          <div className="flex shrink-0 items-center gap-4 hairline-b px-6 py-4">
            <span className="label">compaction metrics</span>
            <span className="font-mono text-[10px] text-fg-3">
              {rawRows.length.toLocaleString()} events ·{' '}
              {aggregate.sessions.size.toLocaleString()} sessions
              {timeRange !== 'all' && (
                <>
                  {' · '}
                  <span className="text-fg-3/70">window {timeRange}</span>
                </>
              )}
              {lastFetchedAt > 0 && (
                <>
                  {' · '}
                  <span className="text-fg-3/60">
                    refreshed {new Date(lastFetchedAt).toLocaleTimeString()}
                  </span>
                </>
              )}
            </span>
            <div className="ml-auto flex items-center gap-2">
              <button
                onClick={refresh}
                disabled={loading}
                className="rounded-md bg-white/[0.05] px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.08em] text-fg-1 ring-hairline hover:bg-white/[0.08] hover:text-fg-0 disabled:opacity-50"
              >
                {loading ? 'loading…' : 'refresh'}
              </button>
              <button
                onClick={() => toggle(false)}
                title="Close (Esc)"
                className="rounded-md bg-white/[0.05] px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
              >
                esc close
              </button>
            </div>
          </div>

          {/* Filter bar */}
          <div className="flex shrink-0 flex-wrap items-center gap-3 hairline-b px-6 py-2.5">
            <FilterGroup
              label="view"
              value={view}
              options={[
                ['sessions', 'sessions'],
                ['profiles', 'profiles'],
              ]}
              onChange={(v) => {
                setView(v as typeof view)
                setSelectedSessionId(null)
                setSelectedProfileId(null)
              }}
            />
            <FilterGroup
              label="window"
              value={timeRange}
              options={[
                ['24h', '24h'],
                ['7d', '7d'],
                ['30d', '30d'],
                ['all', 'all'],
              ]}
              onChange={(v) => setTimeRange(v as typeof timeRange)}
            />
            {view === 'sessions' && (
              <>
                <FilterGroup
                  label="group"
                  value={groupBy}
                  options={[
                    ['none', 'none'],
                    ['model', 'model'],
                    ['day', 'day'],
                    ['band', 'peak band'],
                  ]}
                  onChange={(v) => setGroupBy(v as typeof groupBy)}
                />
                <FilterGroup
                  label="filter"
                  value={narrowFilter}
                  options={[
                    ['all', 'all'],
                    ['has_compaction', 'compacted'],
                    ['has_thrash', 'thrash'],
                    ['has_subagents', 'subagents'],
                  ]}
                  onChange={(v) => setNarrowFilter(v as typeof narrowFilter)}
                />
              </>
            )}
          </div>

          {error && (
            <div className="mx-6 mt-3 rounded-md bg-danger/12 px-3 py-2 font-mono text-[11px] text-danger ring-1 ring-danger/30">
              {error}
            </div>
          )}

          {/* Content scroller */}
          {view === 'profiles' ? (
            <ProfilesContent
              data={profilesAggregate}
              selectedId={selectedProfileId}
              onSelect={(id) => setSelectedProfileId(id)}
            />
          ) : (
          <div className="grid min-h-0 flex-1 grid-cols-12 gap-4 overflow-auto p-6">
            {/* Top-row stat tiles — six now, two rows on narrow */}
            <StatTile
              label="total spend"
              value={formatCost(aggregate.totalCost)}
              sub={`${aggregate.calls.toLocaleString()} llm calls`}
              tone="accent"
              className="col-span-6 lg:col-span-2"
            />
            <StatTile
              label="cache reuse"
              value={
                aggregate.cacheHitRate != null
                  ? `${(aggregate.cacheHitRate * 100).toFixed(0)}%`
                  : 'n/a'
              }
              sub={`${formatTokens(aggregate.totalCacheReads)} cached / ${formatTokens(aggregate.totalCacheWrites)} written`}
              tone={
                aggregate.cacheHitRate != null && aggregate.cacheHitRate > 0.5
                  ? 'ok'
                  : 'warn'
              }
              className="col-span-6 lg:col-span-2"
            />
            <StatTile
              label="compactions"
              value={String(aggregate.compactions)}
              sub={`${aggregate.summaryCount} llm · ${aggregate.pruneCount} prune${aggregate.thrashCount > 0 ? ` · ${aggregate.thrashCount} thrash` : ''}`}
              tone={aggregate.thrashCount > 0 ? 'warn' : 'neutral'}
              className="col-span-6 lg:col-span-2"
            />
            <StatTile
              label="tokens saved"
              value={formatTokens(aggregate.tokensSaved)}
              sub={`avg ${formatTokens(aggregate.avgSavedPerCompaction)} / compaction`}
              tone="ok"
              className="col-span-6 lg:col-span-2"
            />
            <StatTile
              label="p95 latency"
              value={
                aggregate.latencyP95 > 0
                  ? formatDuration(aggregate.latencyP95)
                  : 'n/a'
              }
              sub={`p50 ${aggregate.latencyP50 > 0 ? formatDuration(aggregate.latencyP50) : '—'} · p99 ${aggregate.latencyP99 > 0 ? formatDuration(aggregate.latencyP99) : '—'}`}
              className="col-span-6 lg:col-span-2"
            />
            <StatTile
              label="subagent share"
              value={
                aggregate.totalCost > 0
                  ? `${((aggregate.subagentCost / aggregate.totalCost) * 100).toFixed(0)}%`
                  : 'n/a'
              }
              sub={`${formatCost(aggregate.subagentCost)} of ${formatCost(aggregate.totalCost)}`}
              className="col-span-6 lg:col-span-2"
            />

            {/* Band distribution */}
            <section className="col-span-12 flex flex-col gap-3 rounded-xl glass-raised p-4 ring-hairline lg:col-span-7">
              <div className="flex items-center justify-between">
                <span className="label">pressure band distribution</span>
                <span className="font-mono text-[10px] text-fg-3">
                  {aggregate.bandTotal.toLocaleString()} signals
                </span>
              </div>
              <BandDistribution counts={aggregate.bandCounts} />
              <p className="font-mono text-[10px] leading-[1.55] text-fg-3">
                clean &lt; 15% · pruning 15–25% · awareness 25–40% · soft 40–60% ·
                strong 60–80% · fallback 80–95%.
              </p>
            </section>

            {/* Trigger source mix */}
            <section className="col-span-12 flex flex-col gap-3 rounded-xl glass-raised p-4 ring-hairline lg:col-span-5">
              <div className="flex items-center justify-between">
                <span className="label">trigger source</span>
                <span className="font-mono text-[10px] text-fg-3">
                  {aggregate.compactions} total
                </span>
              </div>
              <RankedBars
                rows={objectToSortedEntries(aggregate.triggerCounts)}
                accent="#7fb8e8"
              />
            </section>

            {/* Spend over time */}
            <section className="col-span-12 flex flex-col gap-3 rounded-xl glass-raised p-4 ring-hairline lg:col-span-8">
              <div className="flex items-center justify-between">
                <span className="label">spend over time</span>
                <span className="font-mono text-[10px] text-fg-3">
                  cumulative · {aggregate.calls} calls
                </span>
              </div>
              <SpendChart series={aggregate.spendSeries} />
            </section>

            {/* Compaction effectiveness trend */}
            <section className="col-span-12 flex flex-col gap-3 rounded-xl glass-raised p-4 ring-hairline lg:col-span-4">
              <div className="flex items-center justify-between">
                <span className="label">compaction savings</span>
                <span className="font-mono text-[10px] text-fg-3">
                  per event · most recent →
                </span>
              </div>
              <SavingsTrend points={aggregate.savingsSeries} />
            </section>

            {/* Model mix */}
            <section className="col-span-12 flex flex-col gap-3 rounded-xl glass-raised p-4 ring-hairline lg:col-span-6">
              <div className="flex items-center justify-between">
                <span className="label">model mix</span>
                <span className="font-mono text-[10px] text-fg-3">
                  {Object.keys(aggregate.modelCosts).length} models
                </span>
              </div>
              <ModelMix
                costs={aggregate.modelCosts}
                calls={aggregate.modelCalls}
                total={aggregate.totalCost}
              />
            </section>

            {/* Cost-per-turn distribution */}
            <section className="col-span-12 flex flex-col gap-3 rounded-xl glass-raised p-4 ring-hairline lg:col-span-6">
              <div className="flex items-center justify-between">
                <span className="label">cost per turn</span>
                <span className="font-mono text-[10px] text-fg-3">
                  {aggregate.turnHistogram.reduce((a, b) => a + b.count, 0)} turns
                </span>
              </div>
              <HistogramChart
                bins={aggregate.turnHistogram}
                formatX={(v) => `$${v.toFixed(2)}`}
              />
            </section>

            {/* Per-session table with sparklines */}
            <section className="col-span-12 flex flex-col gap-3 rounded-xl glass-raised p-4 ring-hairline">
              <div className="flex items-center justify-between">
                <span className="label">
                  per session {groupBy !== 'none' && `· grouped by ${groupBy}`}
                </span>
                <span className="font-mono text-[10px] text-fg-3">
                  {aggregate.perSession.length} sessions · click a row for details
                </span>
              </div>
              <SessionTable
                rows={aggregate.perSession}
                groupBy={groupBy}
                onSelect={(id) => setSelectedSessionId(id)}
                selectedId={selectedSessionId}
              />
            </section>
          </div>
          )}
        </div>

        {/* Right-side detail drawer — squeezes the main panel left when open */}
        {sessionDrawerOpen && selectedSession && (
          <SessionDetailDrawer
            session={selectedSession}
            onClose={() => setSelectedSessionId(null)}
          />
        )}
        {profileDrawerOpen && selectedProfile && (
          <ProfileDetailDrawer
            profile={selectedProfile}
            onClose={() => setSelectedProfileId(null)}
            onJumpToSession={(sid) => {
              setSelectedProfileId(null)
              setView('sessions')
              setSelectedSessionId(sid)
            }}
          />
        )}
      </div>
    </div>
  )
}

// ── Aggregation types ─────────────────────────────────────────────

type BandKey = 'clean' | 'pruning' | 'awareness' | 'soft' | 'strong' | 'fallback'

interface PerSessionRow {
  sessionId: string
  isSubagent: boolean
  firstSeenTs: number
  lastSeenTs: number
  calls: number
  totalCost: number
  tokensIn: number
  tokensOut: number
  cacheReads: number
  cacheWrites: number
  compactions: number
  thrashTrips: number
  highestBand: BandKey
  /** Cumulative spend points for the row sparkline. */
  spendPoints: Array<{ t: number; cum: number }>
  /** Raw LLM call rows for this session — used by the detail drawer. */
  callRows: LlmCallRow[]
  /** Compaction event rows for this session — used by the detail drawer. */
  compactionRows: CompactionEventRow[]
  /** Pressure signal rows for this session — used to render band overlay. */
  pressureRows: PressureRow[]
}

interface LlmCallRow {
  ts: number
  turn_id?: string
  model: string
  input_tokens: number
  output_tokens: number
  cache_read_tokens: number
  cache_write_tokens: number
  cost_usd: number
  duration_ms: number
}

interface CompactionEventRow {
  ts: number
  subtype: string
  mechanism: string
  trigger?: string
  tokens_before: number
  tokens_after: number
}

interface PressureRow {
  ts: number
  band: BandKey
  pressure_pct: number
}

interface AggregateView {
  sessions: Set<string>
  calls: number
  totalCost: number
  totalInput: number
  totalOutput: number
  totalCacheReads: number
  totalCacheWrites: number
  cacheHitRate: number | null
  compactions: number
  summaryCount: number
  pruneCount: number
  thrashCount: number
  tokensSaved: number
  avgSavedPerCompaction: number
  subagentCost: number
  latencyP50: number
  latencyP95: number
  latencyP99: number
  bandCounts: Record<BandKey, number>
  bandTotal: number
  triggerCounts: Record<string, number>
  modelCosts: Record<string, number>
  modelCalls: Record<string, number>
  spendSeries: Array<{ t: number; cum: number }>
  savingsSeries: Array<{ ts: number; pct: number }>
  turnHistogram: Array<{ low: number; high: number; count: number }>
  perSession: PerSessionRow[]
}

const BAND_ORDER: BandKey[] = [
  'clean',
  'pruning',
  'awareness',
  'soft',
  'strong',
  'fallback',
]
const BAND_LABEL: Record<BandKey, string> = {
  clean: 'clean',
  pruning: 'pruning',
  awareness: 'awareness',
  soft: 'soft',
  strong: 'strong',
  fallback: 'fallback',
}
const BAND_COLOR: Record<BandKey, string> = {
  clean: '#5a6b6b',
  pruning: '#7a9595',
  awareness: '#7fb8e8',
  soft: '#a8d4fc',
  strong: '#f5b45d',
  fallback: '#f07878',
}
const BAND_RANK: Record<BandKey, number> = {
  clean: 0,
  pruning: 1,
  awareness: 2,
  soft: 3,
  strong: 4,
  fallback: 5,
}

function blankSessionRow(id: string, ts: number): PerSessionRow {
  return {
    sessionId: id,
    isSubagent: id.startsWith('sub_'),
    firstSeenTs: ts,
    lastSeenTs: ts,
    calls: 0,
    totalCost: 0,
    tokensIn: 0,
    tokensOut: 0,
    cacheReads: 0,
    cacheWrites: 0,
    compactions: 0,
    thrashTrips: 0,
    highestBand: 'clean',
    spendPoints: [],
    callRows: [],
    compactionRows: [],
    pressureRows: [],
  }
}

function aggregateRows(
  rows: CompactionTelemetryRow[],
  narrow: 'all' | 'has_compaction' | 'has_thrash' | 'has_subagents',
): AggregateView {
  // First pass: group rows by session and accumulate per-session state.
  const sessionMap = new Map<string, PerSessionRow>()
  const sorted = rows.slice().sort((a: any, b: any) => (a.ts || 0) - (b.ts || 0))

  let totalCost = 0
  let totalInput = 0
  let totalOutput = 0
  let totalCacheReads = 0
  let totalCacheWrites = 0
  let calls = 0
  let compactions = 0
  let summaryCount = 0
  let pruneCount = 0
  let thrashCount = 0
  let tokensSaved = 0
  let subagentCost = 0
  const bandCounts: Record<BandKey, number> = {
    clean: 0, pruning: 0, awareness: 0, soft: 0, strong: 0, fallback: 0,
  }
  const triggerCounts: Record<string, number> = {}
  const modelCosts: Record<string, number> = {}
  const modelCalls: Record<string, number> = {}
  const latencies: number[] = []
  const savingsSeries: Array<{ ts: number; pct: number }> = []
  const spendSeries: Array<{ t: number; cum: number }> = []
  const turnCosts = new Map<string, number>()

  for (const row of sorted as any[]) {
    if (!row || !row.type) continue
    const sid: string | undefined = row.session_id
    const ts: number = row.ts || 0
    const cur = sid ? sessionMap.get(sid) || blankSessionRow(sid, ts) : null
    if (cur && sid) {
      cur.lastSeenTs = ts
      sessionMap.set(sid, cur)
    }

    if (row.type === 'llm_call_metric') {
      calls += 1
      const inTok = row.input_tokens || 0
      const outTok = row.output_tokens || 0
      const crTok = row.cache_read_tokens || 0
      const cwTok = row.cache_write_tokens || 0
      const cost = typeof row.cost_usd === 'number' ? row.cost_usd : 0
      totalInput += inTok
      totalOutput += outTok
      totalCacheReads += crTok
      totalCacheWrites += cwTok
      totalCost += cost
      if (typeof row.duration_ms === 'number') latencies.push(row.duration_ms)
      spendSeries.push({ t: ts, cum: totalCost })
      const m = row.model || 'unknown'
      modelCosts[m] = (modelCosts[m] || 0) + cost
      modelCalls[m] = (modelCalls[m] || 0) + 1
      if (sid?.startsWith('sub_')) subagentCost += cost
      if (cur) {
        cur.calls += 1
        cur.totalCost += cost
        cur.tokensIn += inTok
        cur.tokensOut += outTok
        cur.cacheReads += crTok
        cur.cacheWrites += cwTok
        cur.spendPoints.push({ t: ts, cum: cur.totalCost })
        cur.callRows.push({
          ts,
          turn_id: row.turn_id,
          model: m,
          input_tokens: inTok,
          output_tokens: outTok,
          cache_read_tokens: crTok,
          cache_write_tokens: cwTok,
          cost_usd: cost,
          duration_ms: row.duration_ms || 0,
        })
      }
      if (row.turn_id) {
        const tk = `${sid}:${row.turn_id}`
        turnCosts.set(tk, (turnCosts.get(tk) || 0) + cost)
      }
    } else if (row.type === 'pressure_signal') {
      const band: BandKey = (row.band as BandKey) || 'clean'
      bandCounts[band] = (bandCounts[band] || 0) + 1
      if (cur) {
        if (BAND_RANK[band] > BAND_RANK[cur.highestBand]) cur.highestBand = band
        cur.pressureRows.push({
          ts,
          band,
          pressure_pct: row.pressure_pct || 0,
        })
      }
    } else if (row.type === 'compaction_event') {
      compactions += 1
      const sub = row.subtype || ''
      if (sub === 'compaction_complete') summaryCount += 1
      else if (sub === 'context_pruning' || sub === 'media_pruning') pruneCount += 1
      else if (sub === 'thrash_skip') thrashCount += 1
      const before = row.tokens_before || 0
      const after = row.tokens_after || 0
      if (before > after) tokensSaved += before - after
      if (before > 0) {
        savingsSeries.push({ ts, pct: ((before - after) / before) * 100 })
      }
      const trig = row.trigger || row.mechanism || 'unknown'
      triggerCounts[trig] = (triggerCounts[trig] || 0) + 1
      if (cur) {
        cur.compactions += 1
        if (sub === 'thrash_skip') cur.thrashTrips += 1
        cur.compactionRows.push({
          ts,
          subtype: sub,
          mechanism: row.mechanism || 'unknown',
          trigger: row.trigger,
          tokens_before: before,
          tokens_after: after,
        })
      }
    }
  }

  // Per-session list, sorted by spend.
  let perSession = Array.from(sessionMap.values())
  if (narrow === 'has_compaction') {
    perSession = perSession.filter((s) => s.compactions > 0)
  } else if (narrow === 'has_thrash') {
    perSession = perSession.filter((s) => s.thrashTrips > 0)
  } else if (narrow === 'has_subagents') {
    perSession = perSession.filter((s) => s.isSubagent)
  }
  perSession.sort((a, b) => b.totalCost - a.totalCost)

  const sessions = new Set(perSession.map((s) => s.sessionId))
  const bandTotal = Object.values(bandCounts).reduce((a, b) => a + b, 0)
  // cache% = total cache traffic (reads + writes) / total billable input.
  // All three buckets are disjoint after provider normalization: input is
  // fresh, cache_read is reused from a prior turn, cache_write is what we
  // paid to populate the cache. Higher % = more prompt reuse.
  const cacheHitRate =
    totalInput + totalCacheReads + totalCacheWrites > 0
      ? (totalCacheReads + totalCacheWrites) /
        (totalInput + totalCacheReads + totalCacheWrites)
      : null

  // Latency percentiles
  const sortedLat = latencies.slice().sort((a, b) => a - b)
  const pick = (q: number) =>
    sortedLat.length === 0
      ? 0
      : sortedLat[Math.min(sortedLat.length - 1, Math.floor(q * sortedLat.length))]

  // Cost-per-turn histogram (log-ish bins)
  const turnHistogram = histogramTurnCosts(Array.from(turnCosts.values()))

  return {
    sessions,
    calls,
    totalCost,
    totalInput,
    totalOutput,
    totalCacheReads,
    totalCacheWrites,
    cacheHitRate,
    compactions,
    summaryCount,
    pruneCount,
    thrashCount,
    tokensSaved,
    avgSavedPerCompaction:
      compactions > 0 ? Math.round(tokensSaved / compactions) : 0,
    subagentCost,
    latencyP50: pick(0.5),
    latencyP95: pick(0.95),
    latencyP99: pick(0.99),
    bandCounts,
    bandTotal,
    triggerCounts,
    modelCosts,
    modelCalls,
    spendSeries: downsampleSeries(spendSeries, 200),
    savingsSeries,
    turnHistogram,
    perSession,
  }
}

function downsampleSeries<T>(series: T[], maxPoints: number): T[] {
  if (series.length <= maxPoints) return series
  const step = Math.ceil(series.length / maxPoints)
  const out: T[] = []
  for (let i = 0; i < series.length; i += step) out.push(series[i])
  if (out[out.length - 1] !== series[series.length - 1]) {
    out.push(series[series.length - 1])
  }
  return out
}

function histogramTurnCosts(
  costs: number[],
): Array<{ low: number; high: number; count: number }> {
  if (costs.length === 0) return []
  // Fixed bins: $0-0.01, 0.01-0.05, 0.05-0.20, 0.20-0.50, 0.50-1, 1-3, 3+
  const edges = [0, 0.01, 0.05, 0.2, 0.5, 1, 3, Infinity]
  const bins: Array<{ low: number; high: number; count: number }> = []
  for (let i = 0; i < edges.length - 1; i++) {
    bins.push({ low: edges[i], high: edges[i + 1], count: 0 })
  }
  for (const c of costs) {
    for (let i = 0; i < bins.length; i++) {
      if (c >= bins[i].low && c < bins[i].high) {
        bins[i].count += 1
        break
      }
    }
  }
  return bins
}

function objectToSortedEntries(obj: Record<string, number>): Array<[string, number]> {
  return Object.entries(obj).sort((a, b) => b[1] - a[1])
}

// ── Sub-components ────────────────────────────────────────────────

function FilterGroup({
  label,
  value,
  options,
  onChange,
}: {
  label: string
  value: string
  options: Array<[string, string]>
  onChange: (v: string) => void
}) {
  return (
    <div className="flex items-center gap-1.5">
      <span className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-fg-3">
        {label}
      </span>
      <div className="flex overflow-hidden rounded-md ring-hairline">
        {options.map(([v, lbl]) => {
          const active = v === value
          return (
            <button
              key={v}
              onClick={() => onChange(v)}
              className={`px-2.5 py-1 font-mono text-[10.5px] ${
                active
                  ? 'bg-accent/15 text-accent'
                  : 'bg-white/[0.03] text-fg-2 hover:bg-white/[0.07] hover:text-fg-0'
              }`}
            >
              {lbl}
            </button>
          )
        })}
      </div>
    </div>
  )
}

function StatTile({
  label,
  value,
  sub,
  tone = 'neutral',
  className,
}: {
  label: string
  value: string
  sub: string
  tone?: 'neutral' | 'accent' | 'ok' | 'warn'
  className?: string
}) {
  const toneClass =
    tone === 'accent'
      ? 'text-accent'
      : tone === 'ok'
        ? 'text-ok'
        : tone === 'warn'
          ? 'text-warn'
          : 'text-fg-0'
  return (
    <div className={`flex flex-col rounded-xl glass-raised p-4 ring-hairline ${className ?? ''}`}>
      <span className="label">{label}</span>
      <span className={`mt-2 font-mono text-[20px] leading-none ${toneClass}`}>{value}</span>
      <span className="mt-1 truncate font-mono text-[10px] text-fg-3">{sub}</span>
    </div>
  )
}

function BandDistribution({ counts }: { counts: Record<BandKey, number> }) {
  const total = Object.values(counts).reduce((a, b) => a + b, 0)
  if (total === 0) {
    return (
      <div className="flex h-24 items-center justify-center rounded bg-white/[0.02] font-mono text-[11px] italic text-fg-3 ring-hairline">
        no pressure signals yet — start a session and let it run a few turns
      </div>
    )
  }
  return (
    <div className="flex flex-col gap-2">
      <div className="flex h-6 w-full overflow-hidden rounded-md ring-hairline">
        {BAND_ORDER.map((band) => {
          const pct = (counts[band] / total) * 100
          if (pct === 0) return null
          return (
            <div
              key={band}
              title={`${BAND_LABEL[band]} · ${counts[band]} signals (${pct.toFixed(1)}%)`}
              style={{ width: `${pct}%`, background: BAND_COLOR[band] }}
            />
          )
        })}
      </div>
      <div className="grid grid-cols-3 gap-x-3 gap-y-1.5 font-mono text-[10.5px] md:grid-cols-6">
        {BAND_ORDER.map((band) => {
          const pct = total > 0 ? (counts[band] / total) * 100 : 0
          return (
            <div key={band} className="flex items-center gap-1.5 text-fg-2">
              <span
                className="h-2 w-2 rounded-sm"
                style={{ background: BAND_COLOR[band] }}
              />
              <span className="text-fg-1">{BAND_LABEL[band]}</span>
              <span className="ml-auto text-fg-3">{pct.toFixed(0)}%</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function RankedBars({
  rows,
  accent,
}: {
  rows: Array<[string, number]>
  accent: string
}) {
  const total = rows.reduce((a, [, b]) => a + b, 0)
  if (total === 0) {
    return (
      <div className="flex h-24 items-center justify-center rounded bg-white/[0.02] font-mono text-[11px] italic text-fg-3 ring-hairline">
        no events yet
      </div>
    )
  }
  return (
    <div className="flex flex-col gap-1.5">
      {rows.map(([k, n]) => {
        const pct = (n / total) * 100
        return (
          <div key={k} className="flex items-center gap-2 font-mono text-[10.5px]">
            <span className="w-[110px] truncate text-fg-1" title={k}>{k}</span>
            <div className="relative h-2 flex-1 overflow-hidden rounded-full bg-white/[0.04]">
              <div
                className="absolute left-0 top-0 h-full rounded-full"
                style={{ width: `${pct}%`, background: `${accent}a8` }}
              />
            </div>
            <span className="w-[34px] text-right text-fg-3">{n}</span>
            <span className="w-[42px] text-right text-fg-3/70">
              {pct.toFixed(0)}%
            </span>
          </div>
        )
      })}
    </div>
  )
}

function SpendChart({ series }: { series: Array<{ t: number; cum: number }> }) {
  if (series.length < 2) {
    return (
      <div className="flex h-32 items-center justify-center rounded bg-white/[0.02] font-mono text-[11px] italic text-fg-3 ring-hairline">
        not enough data yet
      </div>
    )
  }
  const W = 800
  const H = 160
  const tMin = series[0].t
  const tMax = series[series.length - 1].t
  const tSpan = Math.max(1, tMax - tMin)
  const yMax = series[series.length - 1].cum
  const path = series.map((p, i) => {
    const x = ((p.t - tMin) / tSpan) * (W - 10) + 5
    const y = H - ((yMax > 0 ? p.cum / yMax : 0) * (H - 20)) - 10
    return `${i === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${y.toFixed(1)}`
  }).join(' ')
  const fillPath = `${path} L ${W - 5} ${H - 10} L 5 ${H - 10} Z`

  return (
    <div className="flex flex-col gap-2">
      <svg viewBox={`0 0 ${W} ${H}`} className="block h-32 w-full" preserveAspectRatio="none">
        <defs>
          <linearGradient id="spend-fill" x1="0%" y1="0%" x2="0%" y2="100%">
            <stop offset="0%" stopColor="#a8d4fc" stopOpacity="0.35" />
            <stop offset="100%" stopColor="#a8d4fc" stopOpacity="0" />
          </linearGradient>
        </defs>
        {[0.25, 0.5, 0.75].map((g) => (
          <line
            key={g}
            x1={5}
            x2={W - 5}
            y1={H - g * (H - 20) - 10}
            y2={H - g * (H - 20) - 10}
            stroke="rgba(255,255,255,0.05)"
            strokeWidth={1}
          />
        ))}
        <path d={fillPath} fill="url(#spend-fill)" />
        <path d={path} fill="none" stroke="#a8d4fc" strokeWidth={1.5} />
      </svg>
      <div className="flex items-center justify-between font-mono text-[10px] text-fg-3">
        <span>{new Date(tMin * 1000).toLocaleDateString()}</span>
        <span className="text-fg-1">cumulative {formatCost(yMax)}</span>
        <span>{new Date(tMax * 1000).toLocaleDateString()}</span>
      </div>
    </div>
  )
}

function SavingsTrend({ points }: { points: Array<{ ts: number; pct: number }> }) {
  if (points.length === 0) {
    return (
      <div className="flex h-32 items-center justify-center rounded bg-white/[0.02] font-mono text-[11px] italic text-fg-3 ring-hairline">
        no compactions yet
      </div>
    )
  }
  const W = 360
  const H = 140
  const tMin = points[0].ts
  const tMax = points[points.length - 1].ts
  const tSpan = Math.max(1, tMax - tMin)
  const yMax = 100
  const path = points.map((p, i) => {
    const x = (points.length === 1 ? 0.5 : (p.ts - tMin) / tSpan) * (W - 10) + 5
    const y = H - (p.pct / yMax) * (H - 20) - 10
    return `${i === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${y.toFixed(1)}`
  }).join(' ')
  const last = points[points.length - 1]
  return (
    <div className="flex flex-col gap-2">
      <svg viewBox={`0 0 ${W} ${H}`} className="block h-32 w-full" preserveAspectRatio="none">
        <line
          x1={5}
          x2={W - 5}
          y1={H - 0.1 * (H - 20) - 10}
          y2={H - 0.1 * (H - 20) - 10}
          stroke="rgba(245,180,93,0.35)"
          strokeWidth={1}
          strokeDasharray="3 4"
        />
        <text
          x={W - 4}
          y={H - 0.1 * (H - 20) - 14}
          textAnchor="end"
          fill="#f5b45d"
          fontSize="9"
          fontFamily="Departure Mono, monospace"
        >
          thrash 10%
        </text>
        {points.map((p, i) => {
          const x = (points.length === 1 ? 0.5 : (p.ts - tMin) / tSpan) * (W - 10) + 5
          const y = H - (p.pct / yMax) * (H - 20) - 10
          return (
            <circle
              key={i}
              cx={x}
              cy={y}
              r={2.5}
              fill={p.pct < 10 ? '#f07878' : '#7fb8e8'}
              fillOpacity="0.85"
            />
          )
        })}
        <path d={path} fill="none" stroke="rgba(127,184,232,0.55)" strokeWidth={1.2} />
      </svg>
      <div className="flex items-center justify-between font-mono text-[10px] text-fg-3">
        <span>{points.length} events</span>
        <span className={last.pct < 10 ? 'text-danger' : 'text-fg-1'}>
          latest {last.pct.toFixed(1)}%
        </span>
      </div>
    </div>
  )
}

function ModelMix({
  costs,
  calls,
  total,
}: {
  costs: Record<string, number>
  calls: Record<string, number>
  total: number
}) {
  const entries = Object.entries(costs).sort((a, b) => b[1] - a[1])
  if (entries.length === 0) {
    return (
      <div className="flex h-24 items-center justify-center rounded bg-white/[0.02] font-mono text-[11px] italic text-fg-3 ring-hairline">
        no data yet
      </div>
    )
  }
  return (
    <div className="flex flex-col gap-1.5">
      {entries.map(([model, cost]) => {
        const pct = total > 0 ? (cost / total) * 100 : 0
        return (
          <div key={model} className="flex items-center gap-2 font-mono text-[10.5px]">
            <span
              className="w-[120px] truncate text-fg-1"
              title={model}
            >
              {model.replace('claude-', '')}
            </span>
            <div className="relative h-2 flex-1 overflow-hidden rounded-full bg-white/[0.04]">
              <div
                className="absolute left-0 top-0 h-full rounded-full bg-accent/65"
                style={{ width: `${pct}%` }}
              />
            </div>
            <span className="w-[58px] text-right text-fg-1">{formatCost(cost)}</span>
            <span className="w-[34px] text-right text-fg-3">{calls[model] ?? 0}</span>
          </div>
        )
      })}
    </div>
  )
}

function HistogramChart({
  bins,
  formatX,
}: {
  bins: Array<{ low: number; high: number; count: number }>
  formatX: (v: number) => string
}) {
  if (bins.length === 0 || bins.every((b) => b.count === 0)) {
    return (
      <div className="flex h-24 items-center justify-center rounded bg-white/[0.02] font-mono text-[11px] italic text-fg-3 ring-hairline">
        no turn data yet
      </div>
    )
  }
  const maxCount = Math.max(...bins.map((b) => b.count))
  return (
    <div className="flex flex-col gap-2">
      <div className="flex h-28 items-end gap-1">
        {bins.map((b, i) => {
          const pct = (b.count / maxCount) * 100
          return (
            <div
              key={i}
              className="flex flex-1 flex-col items-center gap-1"
              title={`${formatX(b.low)} – ${b.high === Infinity ? '∞' : formatX(b.high)} · ${b.count} turns`}
            >
              <div
                className="w-full rounded-t bg-accent/65"
                style={{ height: `${pct}%`, minHeight: b.count > 0 ? 2 : 0 }}
              />
              <span className="font-mono text-[8.5px] text-fg-3">{b.count}</span>
            </div>
          )
        })}
      </div>
      <div className="flex justify-between font-mono text-[9px] text-fg-3">
        {bins.map((b, i) => (
          <span key={i} className="flex-1 text-center">
            {formatX(b.low)}
          </span>
        ))}
        <span className="flex-1 text-center">∞</span>
      </div>
    </div>
  )
}

function SessionTable({
  rows,
  groupBy,
  onSelect,
  selectedId,
}: {
  rows: PerSessionRow[]
  groupBy: 'none' | 'model' | 'day' | 'band'
  onSelect: (id: string) => void
  selectedId: string | null
}) {
  if (rows.length === 0) {
    return (
      <div className="flex h-24 items-center justify-center rounded bg-white/[0.02] font-mono text-[11px] italic text-fg-3 ring-hairline">
        no sessions match the current filter
      </div>
    )
  }

  // Optional grouping. We render section dividers between groups.
  const grouped = groupSessions(rows, groupBy)

  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[920px] font-mono text-[10.5px]">
        <thead>
          <tr className="text-left text-fg-3">
            <th className="px-2 py-1.5 font-normal">session</th>
            <th className="px-2 py-1.5 text-right font-normal">spend</th>
            <th className="px-2 py-1.5 font-normal">spend trajectory</th>
            <th className="px-2 py-1.5 text-right font-normal">calls</th>
            <th
              className="px-2 py-1.5 text-right font-normal"
              title="Fresh input tokens (excludes cache reads/writes)."
            >
              fresh in
            </th>
            <th className="px-2 py-1.5 text-right font-normal">out tok</th>
            <th
              className="px-2 py-1.5 text-right font-normal"
              title="(cache_read + cache_write) / (fresh + cache_read + cache_write). Higher = more of the prompt served from cache."
            >
              cache %
            </th>
            <th className="px-2 py-1.5 text-right font-normal">compact</th>
            <th className="px-2 py-1.5 font-normal">peak band</th>
          </tr>
        </thead>
        <tbody>
          {grouped.map((group, gi) => (
            <Fragment key={`g-${group.label || gi}`}>
              {group.label && (
                <tr>
                  <td colSpan={9} className="pb-1 pt-3 font-mono text-[9.5px] uppercase tracking-[0.14em] text-fg-3">
                    {group.label} <span className="text-fg-3/60">· {group.rows.length}</span>
                  </td>
                </tr>
              )}
              {group.rows.slice(0, 80).map((r) => {
                const cacheTotal = r.cacheReads + r.cacheWrites
                const billable = r.tokensIn + cacheTotal
                const cachePct = billable > 0 ? (cacheTotal / billable) * 100 : 0
                const active = selectedId === r.sessionId
                return (
                  <tr
                    key={r.sessionId}
                    onClick={() => onSelect(r.sessionId)}
                    className={`cursor-pointer border-t border-white/[0.04] ${
                      active ? 'bg-accent/[0.08]' : 'hover:bg-white/[0.025]'
                    }`}
                  >
                    <td className="px-2 py-1.5 text-fg-1" title={r.sessionId}>
                      <div className="flex items-center gap-1.5">
                        {r.isSubagent && (
                          <span className="rounded bg-white/[0.06] px-1 py-[1px] text-[8.5px] uppercase text-fg-3">
                            sub
                          </span>
                        )}
                        <span className="truncate">
                          {r.sessionId.length > 28 ? r.sessionId.slice(0, 25) + '…' : r.sessionId}
                        </span>
                        {r.thrashTrips > 0 && (
                          <span
                            title={`${r.thrashTrips} thrash event${r.thrashTrips === 1 ? '' : 's'}`}
                            className="rounded bg-danger/15 px-1 py-[1px] text-[8.5px] uppercase text-danger ring-1 ring-danger/30"
                          >
                            thrash
                          </span>
                        )}
                      </div>
                    </td>
                    <td className="px-2 py-1.5 text-right text-fg-0">{formatCost(r.totalCost)}</td>
                    <td className="px-2 py-1.5">
                      <Sparkline
                        points={r.spendPoints}
                        pressureRows={r.pressureRows}
                      />
                    </td>
                    <td className="px-2 py-1.5 text-right text-fg-2">{r.calls}</td>
                    <td className="px-2 py-1.5 text-right text-fg-2">{formatTokens(r.tokensIn)}</td>
                    <td className="px-2 py-1.5 text-right text-fg-2">{formatTokens(r.tokensOut)}</td>
                    <td className="px-2 py-1.5 text-right text-fg-2">{cachePct.toFixed(0)}%</td>
                    <td className="px-2 py-1.5 text-right text-fg-2">{r.compactions}</td>
                    <td className="px-2 py-1.5">
                      <span
                        className="inline-flex items-center gap-1.5 rounded px-1.5 py-[1px] text-[9.5px] uppercase tracking-[0.06em]"
                        style={{
                          color: BAND_COLOR[r.highestBand],
                          background: `${BAND_COLOR[r.highestBand]}1f`,
                        }}
                      >
                        <span
                          className="h-1.5 w-1.5 rounded-full"
                          style={{ background: BAND_COLOR[r.highestBand] }}
                        />
                        {BAND_LABEL[r.highestBand]}
                      </span>
                    </td>
                  </tr>
                )
              })}
            </Fragment>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function groupSessions(
  rows: PerSessionRow[],
  groupBy: 'none' | 'model' | 'day' | 'band',
): Array<{ label: string; rows: PerSessionRow[] }> {
  if (groupBy === 'none') return [{ label: '', rows }]
  const groups = new Map<string, PerSessionRow[]>()
  for (const r of rows) {
    let key: string
    if (groupBy === 'day') {
      key = new Date(r.firstSeenTs * 1000).toLocaleDateString()
    } else if (groupBy === 'band') {
      key = BAND_LABEL[r.highestBand]
    } else if (groupBy === 'model') {
      // Most-used model by call count for this session
      const counts: Record<string, number> = {}
      for (const c of r.callRows) counts[c.model] = (counts[c.model] || 0) + 1
      const top = Object.entries(counts).sort((a, b) => b[1] - a[1])[0]
      key = top ? top[0].replace('claude-', '') : 'unknown'
    } else {
      key = ''
    }
    const arr = groups.get(key) || []
    arr.push(r)
    groups.set(key, arr)
  }
  return Array.from(groups.entries()).map(([label, rs]) => ({ label, rows: rs }))
}

function Sparkline({
  points,
  pressureRows,
  width = 120,
  height = 24,
}: {
  points: Array<{ t: number; cum: number }>
  pressureRows: PressureRow[]
  width?: number
  height?: number
}) {
  if (points.length < 2) {
    return <div className="h-6 w-[120px] rounded bg-white/[0.03]" />
  }
  const tMin = points[0].t
  const tMax = points[points.length - 1].t
  const tSpan = Math.max(1, tMax - tMin)
  const yMax = points[points.length - 1].cum
  const path = points
    .map((p, i) => {
      const x = ((p.t - tMin) / tSpan) * (width - 2) + 1
      const y = height - ((yMax > 0 ? p.cum / yMax : 0) * (height - 4)) - 2
      return `${i === 0 ? 'M' : 'L'} ${x.toFixed(1)} ${y.toFixed(1)}`
    })
    .join(' ')

  // Pressure band background — render as colored stripes over time.
  // We sample band transitions and shade the region between them.
  const bandsByTime = pressureRows
    .slice()
    .sort((a, b) => a.ts - b.ts)
    .filter((p) => p.ts >= tMin && p.ts <= tMax)

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      className="block"
      preserveAspectRatio="none"
    >
      {/* Band background */}
      {bandsByTime.map((b, i) => {
        const x = ((b.ts - tMin) / tSpan) * (width - 2) + 1
        const xNext =
          i < bandsByTime.length - 1
            ? ((bandsByTime[i + 1].ts - tMin) / tSpan) * (width - 2) + 1
            : width - 1
        if (b.band === 'clean' || b.band === 'pruning') return null
        return (
          <rect
            key={i}
            x={x}
            y={0}
            width={Math.max(1, xNext - x)}
            height={height}
            fill={BAND_COLOR[b.band]}
            opacity={0.16}
          />
        )
      })}
      {/* Spend line */}
      <path d={path} fill="none" stroke="#a8d4fc" strokeWidth={1.2} />
    </svg>
  )
}

// ── Session detail drawer ────────────────────────────────────────

function SessionDetailDrawer({
  session,
  onClose,
}: {
  session: PerSessionRow
  onClose: () => void
}) {
  // Per-call timeline data
  const calls = session.callRows.slice().sort((a, b) => a.ts - b.ts)
  const compactions = session.compactionRows.slice().sort((a, b) => a.ts - b.ts)
  const pressures = session.pressureRows.slice().sort((a, b) => a.ts - b.ts)
  const tMin = calls[0]?.ts ?? session.firstSeenTs
  const tMax = calls[calls.length - 1]?.ts ?? session.lastSeenTs
  const tSpan = Math.max(1, tMax - tMin)

  const W = 440
  const H = 100
  const callMaxOut = Math.max(1, ...calls.map((c) => c.output_tokens))

  return (
    <aside className="hairline-l flex w-[500px] shrink-0 flex-col overflow-y-auto">
      {/* Drawer header */}
      <div className="sticky top-0 z-10 flex items-center gap-3 hairline-b bg-black/40 px-4 py-3 backdrop-blur-sm">
        <span className="label">session detail</span>
        <button
          onClick={onClose}
          title="Close drawer (Esc)"
          className="ml-auto rounded-md bg-white/[0.05] px-2 py-1 font-mono text-[10px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
        >
          close
        </button>
      </div>

      <div className="flex flex-col gap-4 p-4">
        {/* Identity + headline stats */}
        <div className="flex flex-col gap-1.5">
          <div className="flex items-baseline gap-2">
            {session.isSubagent && (
              <span className="rounded bg-white/[0.06] px-1.5 py-[1px] font-mono text-[9px] uppercase text-fg-3">
                subagent
              </span>
            )}
            <span className="break-all font-mono text-[12px] text-fg-0">
              {session.sessionId}
            </span>
          </div>
          <div className="font-mono text-[10px] text-fg-3">
            {new Date(session.firstSeenTs * 1000).toLocaleString()} →{' '}
            {new Date(session.lastSeenTs * 1000).toLocaleTimeString()}
            <span className="ml-2">· {session.calls} calls</span>
            <span className="ml-2">· {formatCost(session.totalCost)}</span>
          </div>
        </div>

        {/* Mini Gantt: per-call bars, height = output tokens, color = model */}
        <section className="flex flex-col gap-1.5">
          <span className="label">call timeline</span>
          {calls.length === 0 ? (
            <div className="rounded bg-white/[0.02] p-3 font-mono text-[10.5px] italic text-fg-3 ring-hairline">
              no llm calls yet
            </div>
          ) : (
            <div className="flex flex-col gap-1">
              <svg
                viewBox={`0 0 ${W} ${H}`}
                className="block h-24 w-full"
                preserveAspectRatio="none"
              >
                {/* Pressure band background */}
                {pressures.map((p, i) => {
                  const x = ((p.ts - tMin) / tSpan) * (W - 2) + 1
                  const xNext =
                    i < pressures.length - 1
                      ? ((pressures[i + 1].ts - tMin) / tSpan) * (W - 2) + 1
                      : W - 1
                  if (p.band === 'clean' || p.band === 'pruning') return null
                  return (
                    <rect
                      key={`p-${i}`}
                      x={x}
                      y={0}
                      width={Math.max(1, xNext - x)}
                      height={H}
                      fill={BAND_COLOR[p.band]}
                      opacity={0.13}
                    />
                  )
                })}
                {/* Per-call bars */}
                {calls.map((c, i) => {
                  const x = ((c.ts - tMin) / tSpan) * (W - 2) + 1
                  const w = Math.max(1.5, ((c.duration_ms / 1000) / Math.max(60, tSpan)) * (W - 2))
                  const h = (c.output_tokens / callMaxOut) * (H - 12)
                  const fill = modelColor(c.model)
                  return (
                    <g key={i}>
                      <rect
                        x={x}
                        y={H - h - 6}
                        width={w}
                        height={Math.max(2, h)}
                        rx={1}
                        fill={fill}
                        opacity={0.78}
                      >
                        <title>
                          {`${c.model} · ${c.duration_ms}ms · in=${c.input_tokens} out=${c.output_tokens} · $${c.cost_usd.toFixed(4)}`}
                        </title>
                      </rect>
                    </g>
                  )
                })}
                {/* Compaction event markers */}
                {compactions.map((c, i) => {
                  const x = ((c.ts - tMin) / tSpan) * (W - 2) + 1
                  const color =
                    c.subtype === 'compaction_complete'
                      ? '#a8d4fc'
                      : c.subtype === 'thrash_skip'
                        ? '#f07878'
                        : '#f5b45d'
                  return (
                    <g key={`c-${i}`}>
                      <line x1={x} y1={2} x2={x} y2={H - 2} stroke={color} strokeWidth={1.5} strokeOpacity={0.85} />
                      <circle cx={x} cy={4} r={2} fill={color} />
                      <title>{`compaction · ${c.subtype} · ${formatTokens(c.tokens_before)} → ${formatTokens(c.tokens_after)}`}</title>
                    </g>
                  )
                })}
              </svg>
              <div className="flex items-center justify-between font-mono text-[9.5px] text-fg-3">
                <span>{new Date(tMin * 1000).toLocaleTimeString()}</span>
                <span>{calls.length} calls · {compactions.length} compactions</span>
                <span>{new Date(tMax * 1000).toLocaleTimeString()}</span>
              </div>
            </div>
          )}
        </section>

        {/* Per-call table */}
        <section className="flex flex-col gap-1.5">
          <span className="label">per call</span>
          {calls.length === 0 ? (
            <div className="rounded bg-white/[0.02] p-3 font-mono text-[10.5px] italic text-fg-3 ring-hairline">
              —
            </div>
          ) : (
            <div className="max-h-[280px] overflow-y-auto rounded bg-black/30 ring-hairline">
              <table className="w-full font-mono text-[10px]">
                <thead className="sticky top-0 bg-black/60">
                  <tr className="text-left text-fg-3">
                    <th className="px-2 py-1 font-normal">time</th>
                    <th className="px-2 py-1 font-normal">model</th>
                    <th
                      className="px-2 py-1 text-right font-normal"
                      title="Fresh input tokens — new prompt content this turn. Cache reads/writes are billed separately."
                    >
                      fresh
                    </th>
                    <th className="px-2 py-1 text-right font-normal">out</th>
                    <th
                      className="px-2 py-1 text-right font-normal"
                      title="Cache: ↓ reads (charged ~10% of input) + ↑ writes (charged ~1.25× input on Anthropic)."
                    >
                      cache
                    </th>
                    <th
                      className="px-2 py-1 text-right font-normal"
                      title="cache% = (cache_read + cache_write) / (fresh + cache_read + cache_write). 100% = fully served from cache."
                    >
                      %
                    </th>
                    <th className="px-2 py-1 text-right font-normal">cost</th>
                    <th className="px-2 py-1 text-right font-normal">dur</th>
                  </tr>
                </thead>
                <tbody>
                  {calls.map((c, i) => {
                    const cacheTotal = c.cache_read_tokens + c.cache_write_tokens
                    const billable = c.input_tokens + cacheTotal
                    const cachePct = billable > 0 ? (cacheTotal / billable) * 100 : 0
                    return (
                      <tr key={i} className="border-t border-white/[0.04]">
                        <td className="px-2 py-1 text-fg-3">{new Date(c.ts * 1000).toLocaleTimeString()}</td>
                        <td className="px-2 py-1">
                          <span
                            className="inline-block h-1.5 w-1.5 rounded-full"
                            style={{ background: modelColor(c.model) }}
                          />
                          <span className="ml-1.5 text-fg-1">{c.model.replace('claude-', '')}</span>
                        </td>
                        <td className="px-2 py-1 text-right text-fg-2">{formatTokens(c.input_tokens)}</td>
                        <td className="px-2 py-1 text-right text-fg-2">{formatTokens(c.output_tokens)}</td>
                        <td
                          className="px-2 py-1 text-right text-fg-2"
                          title={`↓ read ${formatTokens(c.cache_read_tokens)} · ↑ write ${formatTokens(c.cache_write_tokens)}`}
                        >
                          {formatTokens(cacheTotal)}
                        </td>
                        <td
                          className="px-2 py-1 text-right font-mono"
                          style={{
                            color:
                              cachePct >= 70
                                ? '#88d67f'
                                : cachePct >= 30
                                  ? '#a8d4fc'
                                  : 'rgb(120 130 140)',
                          }}
                        >
                          {cachePct > 0 ? `${cachePct.toFixed(0)}%` : '—'}
                        </td>
                        <td className="px-2 py-1 text-right text-fg-0">${c.cost_usd.toFixed(4)}</td>
                        <td className="px-2 py-1 text-right text-fg-3">{c.duration_ms}ms</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>

        {/* Compaction log */}
        <section className="flex flex-col gap-1.5">
          <span className="label">compactions</span>
          {compactions.length === 0 ? (
            <div className="rounded bg-white/[0.02] p-3 font-mono text-[10.5px] italic text-fg-3 ring-hairline">
              no compactions in this session
            </div>
          ) : (
            <div className="flex flex-col gap-1">
              {compactions.map((c, i) => {
                const savings =
                  c.tokens_before > 0
                    ? ((c.tokens_before - c.tokens_after) / c.tokens_before) * 100
                    : 0
                const color =
                  c.subtype === 'compaction_complete'
                    ? '#a8d4fc'
                    : c.subtype === 'thrash_skip'
                      ? '#f07878'
                      : '#f5b45d'
                return (
                  <div
                    key={i}
                    className="flex items-center gap-2 rounded bg-white/[0.025] px-2.5 py-1.5 font-mono text-[10px] ring-hairline"
                  >
                    <span
                      className="h-1.5 w-1.5 shrink-0 rounded-full"
                      style={{ background: color }}
                    />
                    <span className="text-fg-3">{new Date(c.ts * 1000).toLocaleTimeString()}</span>
                    <span className="text-fg-1">{c.subtype}</span>
                    <span className="text-fg-3">·</span>
                    <span className="text-fg-2">{c.mechanism}</span>
                    {c.trigger && (
                      <>
                        <span className="text-fg-3">·</span>
                        <span className="text-fg-3">{c.trigger}</span>
                      </>
                    )}
                    <span className="ml-auto text-fg-2">
                      {formatTokens(c.tokens_before)} → {formatTokens(c.tokens_after)}
                    </span>
                    <span className={savings < 10 ? 'text-danger' : 'text-ok'}>
                      {savings.toFixed(0)}%
                    </span>
                  </div>
                )
              })}
            </div>
          )}
        </section>
      </div>
    </aside>
  )
}

// Deterministic color per model name. Hash → pick from a small monochrome
// palette so the timeline + per-call table colors are stable.
function modelColor(model: string): string {
  const palette = ['#a8d4fc', '#88d67f', '#f5b45d', '#b8a7ff', '#f0a6ca', '#7fb8e8', '#d99bbe']
  let h = 0
  for (let i = 0; i < model.length; i++) h = (h * 31 + model.charCodeAt(i)) | 0
  return palette[Math.abs(h) % palette.length]
}

// ── Profiles view ────────────────────────────────────────────────

interface ProfileSessionRef {
  sessionId: string
  parentSessionId?: string | null
  spawnedAt: number
  finishedAt: number | null
  outcome: 'success' | 'error' | 'cancelled' | 'running'
  iterations: number
  maxIterations: number
  cost: number
  taskPreview: string
  model: string
}

interface PerProfileRow {
  agentType: string
  isSynthetic: boolean
  invocations: number
  totalCost: number
  callCount: number
  toolCallCount: number
  outcomes: { success: number; error: number; cancelled: number; running: number }
  iterationsUsed: number
  iterationsBudget: number
  modelCosts: Record<string, number>
  toolCounts: Record<string, number>
  /** Per-call latencies for percentile math. */
  latencies: number[]
  /** Sessions tagged with this profile — for the drawer's sub-table. */
  sessions: ProfileSessionRef[]
  /** Per-day spawn count, used to draw the row sparkline. */
  spawnSeries: Array<{ t: number; count: number }>
  /** Recent task previews for the drawer. */
  taskPreviews: Array<{ ts: number; sessionId: string; preview: string }>
}

interface ProfilesAggregate {
  perProfile: PerProfileRow[]
  totalSpawns: number
  totalCost: number
  busiestProfile: string | null
}

function aggregateProfiles(rows: CompactionTelemetryRow[]): ProfilesAggregate {
  // First pass: build session→profile maps from profile_invocation and
  // profile_completion rows. Sessions that don't appear in either map are
  // treated as synthetic "root" — the user's primary chat sessions.
  const invocations = new Map<string, {
    parentSessionId: string
    agentType: string
    model: string
    maxIterations: number
    taskPreview: string
    ts: number
  }>()
  const completions = new Map<string, {
    iterations: number
    outcome: 'success' | 'error' | 'cancelled'
    durationMs: number
    ts: number
  }>()

  // Track which (agentType, sessionId) pairs we've seen across telemetry
  // rows so root sessions also surface in the per-profile table.
  const sessionAgentType = new Map<string, string>()

  // Pull profile rows first so we can resolve agent_type for everything else.
  for (const row of rows as any[]) {
    if (!row || !row.type) continue
    if (row.type === 'profile_invocation') {
      invocations.set(row.session_id, {
        parentSessionId: row.parent_session_id,
        agentType: row.agent_type || 'general',
        model: row.model,
        maxIterations: row.max_iterations || 0,
        taskPreview: row.task_preview || '',
        ts: row.ts || 0,
      })
      sessionAgentType.set(row.session_id, row.agent_type || 'general')
    } else if (row.type === 'profile_completion') {
      completions.set(row.session_id, {
        iterations: row.iterations_used || 0,
        outcome: row.final_outcome || 'success',
        durationMs: row.duration_ms || 0,
        ts: row.ts || 0,
      })
    }
  }

  // Now pull agent_type tags off llm_call_metric / tool_call_metric / pressure_signal
  // rows to fill in subagent sessions whose profile_invocation row may
  // pre-date the time window (legacy data) and to identify root sessions.
  for (const row of rows as any[]) {
    if (!row || !row.session_id) continue
    if (row.type === 'llm_call_metric' || row.type === 'tool_call_metric' || row.type === 'pressure_signal') {
      const at = (row as any).agent_type
      if (typeof at === 'string' && at && !sessionAgentType.has(row.session_id)) {
        sessionAgentType.set(row.session_id, at)
      } else if (!sessionAgentType.has(row.session_id)) {
        sessionAgentType.set(row.session_id, 'root')
      }
    }
  }

  // Build per-profile aggregations.
  const map = new Map<string, PerProfileRow>()
  const ensure = (agentType: string): PerProfileRow => {
    let r = map.get(agentType)
    if (!r) {
      r = {
        agentType,
        isSynthetic: agentType === 'root',
        invocations: 0,
        totalCost: 0,
        callCount: 0,
        toolCallCount: 0,
        outcomes: { success: 0, error: 0, cancelled: 0, running: 0 },
        iterationsUsed: 0,
        iterationsBudget: 0,
        modelCosts: {},
        toolCounts: {},
        latencies: [],
        sessions: [],
        spawnSeries: [],
        taskPreviews: [],
      }
      map.set(agentType, r)
    }
    return r
  }

  // Walk invocations first to populate session refs + budget totals.
  const sessionRefMap = new Map<string, ProfileSessionRef>()
  for (const [sid, inv] of invocations.entries()) {
    const profile = ensure(inv.agentType)
    profile.invocations += 1
    profile.iterationsBudget += inv.maxIterations
    const completion = completions.get(sid)
    const ref: ProfileSessionRef = {
      sessionId: sid,
      parentSessionId: inv.parentSessionId,
      spawnedAt: inv.ts,
      finishedAt: completion ? completion.ts : null,
      outcome: completion ? completion.outcome : 'running',
      iterations: completion ? completion.iterations : 0,
      maxIterations: inv.maxIterations,
      cost: 0,
      taskPreview: inv.taskPreview,
      model: inv.model,
    }
    sessionRefMap.set(sid, ref)
    profile.sessions.push(ref)
    profile.taskPreviews.push({
      ts: inv.ts,
      sessionId: sid,
      preview: inv.taskPreview,
    })
    if (completion) {
      profile.iterationsUsed += completion.iterations
      profile.outcomes[completion.outcome] += 1
    } else {
      profile.outcomes.running += 1
    }
  }

  // Synthesize "root" entries for sessions seen in llm_call_metric rows
  // that have no profile_invocation.
  for (const [sid, agentType] of sessionAgentType.entries()) {
    if (invocations.has(sid)) continue
    const profile = ensure(agentType)
    if (!sessionRefMap.has(sid)) {
      const ref: ProfileSessionRef = {
        sessionId: sid,
        spawnedAt: 0,
        finishedAt: null,
        outcome: 'running',
        iterations: 0,
        maxIterations: 0,
        cost: 0,
        taskPreview: '',
        model: '',
      }
      sessionRefMap.set(sid, ref)
      profile.sessions.push(ref)
    }
  }

  // Second pass: fold llm_call_metric / tool_call_metric stats in.
  for (const row of rows as any[]) {
    if (!row || !row.session_id) continue
    const sid: string = row.session_id
    const agentType = sessionAgentType.get(sid) || 'root'
    const profile = map.get(agentType)
    if (!profile) continue

    if (row.type === 'llm_call_metric') {
      const cost = typeof row.cost_usd === 'number' ? row.cost_usd : 0
      profile.totalCost += cost
      profile.callCount += 1
      const m = row.model || 'unknown'
      profile.modelCosts[m] = (profile.modelCosts[m] || 0) + cost
      if (typeof row.duration_ms === 'number') profile.latencies.push(row.duration_ms)
      const ref = sessionRefMap.get(sid)
      if (ref) ref.cost += cost
      if (ref && !ref.model) ref.model = m
    } else if (row.type === 'tool_call_metric') {
      profile.toolCallCount += 1
      const tn = row.tool_name || 'unknown'
      profile.toolCounts[tn] = (profile.toolCounts[tn] || 0) + 1
    } else if (row.type === 'profile_invocation') {
      // Daily spawn count for the sparkline.
      const day = Math.floor((row.ts || 0) / 86400) * 86400
      const last = profile.spawnSeries[profile.spawnSeries.length - 1]
      if (last && last.t === day) last.count += 1
      else profile.spawnSeries.push({ t: day, count: 1 })
    }
  }

  // Trim previews to the latest 6 per profile. For synthetic profiles
  // (e.g. ``root``) the ``invocations`` counter stayed at 0 because no
  // ``profile_invocation`` row was ever written — those sessions are
  // the user's primary chats, not spawned subagents. Backfill the
  // count from the unique-session set so the table shows a meaningful
  // number rather than 0.
  for (const profile of map.values()) {
    profile.taskPreviews.sort((a, b) => b.ts - a.ts)
    profile.taskPreviews = profile.taskPreviews.slice(0, 6)
    profile.sessions.sort((a, b) => b.spawnedAt - a.spawnedAt)
    if (profile.isSynthetic && profile.invocations === 0) {
      profile.invocations = profile.sessions.length
    }
  }

  const perProfile = Array.from(map.values()).sort(
    (a, b) => b.invocations + b.callCount - (a.invocations + a.callCount),
  )
  // Exclude synthetic profiles from the global spawn count — they're
  // backfilled session counts, not real subagent spawns. Same goes for
  // the "busiest" tile.
  const totalSpawns = perProfile
    .filter((p) => !p.isSynthetic)
    .reduce((acc, p) => acc + p.invocations, 0)
  const totalCost = perProfile.reduce((acc, p) => acc + p.totalCost, 0)
  const busiestCandidates = perProfile.filter((p) => !p.isSynthetic)
  const busiest =
    busiestCandidates.length === 0
      ? null
      : busiestCandidates.slice().sort((a, b) => b.invocations - a.invocations)[0].agentType

  return {
    perProfile,
    totalSpawns,
    totalCost,
    busiestProfile: busiest,
  }
}

function profileColor(agentType: string): string {
  if (agentType === 'root') return '#5a6b6b'
  const palette = [
    '#a8d4fc', '#88d67f', '#f5b45d', '#b8a7ff', '#f0a6ca',
    '#7fb8e8', '#d99bbe', '#fcd96b', '#9eecd2',
  ]
  let h = 0
  for (let i = 0; i < agentType.length; i++) h = (h * 31 + agentType.charCodeAt(i)) | 0
  return palette[Math.abs(h) % palette.length]
}

function ProfilesContent({
  data,
  selectedId,
  onSelect,
}: {
  data: ProfilesAggregate
  selectedId: string | null
  onSelect: (id: string) => void
}) {
  if (data.perProfile.length === 0) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center p-6">
        <span className="font-mono text-[11px] italic text-fg-3">
          no profile activity in this window yet
        </span>
      </div>
    )
  }

  // Pick out budget utilization: avg iterations_used / iterations_budget
  // across profiles that actually completed something. Exclude synthetic
  // profiles (e.g. "root") since they have no notion of budget or
  // success/failure — they're the user's primary chats, not subagent
  // spawns.
  const realProfiles = data.perProfile.filter((p) => !p.isSynthetic)
  const finishedSpawns = realProfiles.reduce(
    (acc, p) => acc + p.outcomes.success + p.outcomes.error + p.outcomes.cancelled,
    0,
  )
  const totalIterationsUsed = realProfiles.reduce(
    (acc, p) => acc + p.iterationsUsed,
    0,
  )
  const totalIterationsBudget = realProfiles.reduce(
    (acc, p) => acc + p.iterationsBudget,
    0,
  )
  const budgetUtilization =
    totalIterationsBudget > 0
      ? (totalIterationsUsed / totalIterationsBudget) * 100
      : 0
  const totalSuccesses = realProfiles.reduce(
    (acc, p) => acc + p.outcomes.success,
    0,
  )
  const successRate = finishedSpawns > 0 ? (totalSuccesses / finishedSpawns) * 100 : 0

  return (
    <div className="grid min-h-0 flex-1 grid-cols-12 gap-4 overflow-auto p-6">
      <StatTile
        label="profile invocations"
        value={String(data.totalSpawns)}
        sub={`${data.perProfile.length} distinct profile${data.perProfile.length === 1 ? '' : 's'}`}
        tone="accent"
        className="col-span-6 lg:col-span-3"
      />
      <StatTile
        label="busiest profile"
        value={data.busiestProfile || '—'}
        sub={
          data.busiestProfile
            ? `${data.perProfile.find((p) => p.agentType === data.busiestProfile)?.invocations || 0} spawns`
            : 'no spawns yet'
        }
        className="col-span-6 lg:col-span-3"
      />
      <StatTile
        label="success rate"
        value={finishedSpawns > 0 ? `${successRate.toFixed(0)}%` : 'n/a'}
        sub={`${totalSuccesses} ok / ${finishedSpawns - totalSuccesses} not ok`}
        tone={successRate >= 80 ? 'ok' : successRate >= 50 ? 'neutral' : 'warn'}
        className="col-span-6 lg:col-span-3"
      />
      <StatTile
        label="budget used"
        value={
          totalIterationsBudget > 0 ? `${budgetUtilization.toFixed(0)}%` : 'n/a'
        }
        sub={`${totalIterationsUsed} of ${totalIterationsBudget} iter`}
        tone={budgetUtilization > 90 ? 'warn' : 'neutral'}
        className="col-span-6 lg:col-span-3"
      />

      <section className="col-span-12 flex flex-col gap-3 rounded-xl glass-raised p-4 ring-hairline">
        <div className="flex items-center justify-between">
          <span className="label">per profile</span>
          <span className="font-mono text-[10px] text-fg-3">
            click a row for details
          </span>
        </div>
        <ProfileTable
          rows={data.perProfile}
          onSelect={onSelect}
          selectedId={selectedId}
        />
      </section>
    </div>
  )
}

function ProfileTable({
  rows,
  onSelect,
  selectedId,
}: {
  rows: PerProfileRow[]
  onSelect: (id: string) => void
  selectedId: string | null
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[820px] font-mono text-[10.5px]">
        <thead>
          <tr className="text-left text-fg-3">
            <th className="px-2 py-1.5 font-normal">profile</th>
            <th className="px-2 py-1.5 text-right font-normal">spawns</th>
            <th className="px-2 py-1.5 text-right font-normal">calls</th>
            <th className="px-2 py-1.5 text-right font-normal">spend</th>
            <th className="px-2 py-1.5 font-normal">spawn trajectory</th>
            <th className="px-2 py-1.5 font-normal">models</th>
            <th className="px-2 py-1.5 text-right font-normal">p95 lat</th>
            <th
              className="px-2 py-1.5 text-right font-normal"
              title="success / (success + error + cancelled)"
            >
              ok rate
            </th>
            <th
              className="px-2 py-1.5 text-right font-normal"
              title="avg iterations used / max_iterations from the agent type definition"
            >
              budget
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const sorted = r.latencies.slice().sort((a, b) => a - b)
            const p95 =
              sorted.length === 0
                ? 0
                : sorted[Math.min(sorted.length - 1, Math.floor(0.95 * sorted.length))]
            const finished = r.outcomes.success + r.outcomes.error + r.outcomes.cancelled
            // Synthetic profiles (e.g. "root") never have completion
            // rows, so a success-rate / budget-utilization number would
            // be meaningless. Display "—" for them rather than 0%.
            const okRate =
              r.isSynthetic
                ? null
                : finished > 0
                  ? (r.outcomes.success / finished) * 100
                  : null
            const budgetUse =
              r.isSynthetic
                ? null
                : r.iterationsBudget > 0
                  ? (r.iterationsUsed / r.iterationsBudget) * 100
                  : null
            const active = selectedId === r.agentType
            const topModels = Object.entries(r.modelCosts)
              .sort((a, b) => b[1] - a[1])
              .slice(0, 2)
              .map(([m]) => m.replace('claude-', '').replace('gpt-', ''))
            return (
              <tr
                key={r.agentType}
                onClick={() => onSelect(r.agentType)}
                className={`cursor-pointer border-t border-white/[0.04] ${
                  active ? 'bg-accent/[0.08]' : 'hover:bg-white/[0.025]'
                }`}
              >
                <td className="px-2 py-1.5">
                  <div className="flex items-center gap-1.5">
                    <span
                      className="h-2 w-2 shrink-0 rounded-full"
                      style={{ background: profileColor(r.agentType) }}
                    />
                    <span className="text-fg-0">{r.agentType}</span>
                    {r.isSynthetic && (
                      <span className="rounded bg-white/[0.06] px-1 py-[1px] text-[8.5px] uppercase text-fg-3">
                        synth
                      </span>
                    )}
                  </div>
                </td>
                <td className="px-2 py-1.5 text-right text-fg-1">{r.invocations}</td>
                <td className="px-2 py-1.5 text-right text-fg-2">{r.callCount}</td>
                <td className="px-2 py-1.5 text-right text-fg-0">{formatCost(r.totalCost)}</td>
                <td className="px-2 py-1.5">
                  <ProfileSparkline
                    points={r.spawnSeries}
                    accent={profileColor(r.agentType)}
                  />
                </td>
                <td className="px-2 py-1.5 text-fg-2">
                  {topModels.length === 0 ? (
                    <span className="text-fg-3">—</span>
                  ) : (
                    <span className="truncate">
                      {topModels.join(' · ')}
                      {Object.keys(r.modelCosts).length > topModels.length && (
                        <span className="text-fg-3">
                          {' '}
                          +{Object.keys(r.modelCosts).length - topModels.length}
                        </span>
                      )}
                    </span>
                  )}
                </td>
                <td className="px-2 py-1.5 text-right text-fg-2">
                  {p95 > 0 ? formatDuration(p95) : '—'}
                </td>
                <td
                  className="px-2 py-1.5 text-right"
                  style={{
                    color:
                      okRate == null
                        ? 'rgb(120 130 140)'
                        : okRate >= 80
                          ? '#88d67f'
                          : okRate >= 50
                            ? '#a8d4fc'
                            : '#f07878',
                  }}
                >
                  {okRate == null ? '—' : `${okRate.toFixed(0)}%`}
                </td>
                <td className="px-2 py-1.5 text-right text-fg-2">
                  {budgetUse == null ? '—' : `${budgetUse.toFixed(0)}%`}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

function ProfileSparkline({
  points,
  accent,
  width = 120,
  height = 22,
}: {
  points: Array<{ t: number; count: number }>
  accent: string
  width?: number
  height?: number
}) {
  if (points.length === 0) {
    return <div className="h-5 w-[120px] rounded bg-white/[0.03]" />
  }
  const yMax = Math.max(...points.map((p) => p.count))
  if (yMax === 0) return <div className="h-5 w-[120px] rounded bg-white/[0.03]" />

  const barW = Math.max(2, Math.floor(width / Math.max(1, points.length)))
  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      width={width}
      height={height}
      className="block"
      preserveAspectRatio="none"
    >
      {points.map((p, i) => {
        const x = i * barW + 1
        const h = (p.count / yMax) * (height - 4)
        return (
          <rect
            key={i}
            x={x}
            y={height - h - 1}
            width={Math.max(1, barW - 1)}
            height={Math.max(1, h)}
            fill={accent}
            opacity={0.75}
          />
        )
      })}
    </svg>
  )
}

function ProfileDetailDrawer({
  profile,
  onClose,
  onJumpToSession,
}: {
  profile: PerProfileRow
  onClose: () => void
  onJumpToSession: (sessionId: string) => void
}) {
  const finished =
    profile.outcomes.success + profile.outcomes.error + profile.outcomes.cancelled
  const outcomeSegments: Array<{ label: string; count: number; color: string }> = [
    { label: 'success', count: profile.outcomes.success, color: '#88d67f' },
    { label: 'error', count: profile.outcomes.error, color: '#f07878' },
    { label: 'cancelled', count: profile.outcomes.cancelled, color: '#f5b45d' },
    { label: 'running', count: profile.outcomes.running, color: '#7fb8e8' },
  ]
  const outcomeTotal = outcomeSegments.reduce((a, b) => a + b.count, 0)

  const topTools = Object.entries(profile.toolCounts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 8)
  const topModels = Object.entries(profile.modelCosts).sort((a, b) => b[1] - a[1])

  const sortedLat = profile.latencies.slice().sort((a, b) => a - b)
  const pick = (q: number) =>
    sortedLat.length === 0
      ? 0
      : sortedLat[Math.min(sortedLat.length - 1, Math.floor(q * sortedLat.length))]

  return (
    <aside className="hairline-l flex w-[500px] shrink-0 flex-col overflow-y-auto">
      <div className="sticky top-0 z-10 flex items-center gap-3 hairline-b bg-black/40 px-4 py-3 backdrop-blur-sm">
        <span className="label">profile detail</span>
        <button
          onClick={onClose}
          title="Close drawer (Esc)"
          className="ml-auto rounded-md bg-white/[0.05] px-2 py-1 font-mono text-[10px] uppercase tracking-[0.08em] text-fg-2 ring-hairline hover:bg-white/[0.08] hover:text-fg-0"
        >
          close
        </button>
      </div>

      <div className="flex flex-col gap-4 p-4">
        {/* Identity */}
        <div className="flex flex-col gap-1.5">
          <div className="flex items-center gap-2">
            <span
              className="h-3 w-3 rounded-full"
              style={{ background: profileColor(profile.agentType) }}
            />
            <span className="font-mono text-[14px] text-fg-0">{profile.agentType}</span>
            {profile.isSynthetic && (
              <span className="rounded bg-white/[0.06] px-1.5 py-[1px] font-mono text-[9px] uppercase text-fg-3">
                synthetic
              </span>
            )}
          </div>
          <div className="font-mono text-[10px] text-fg-3">
            {profile.invocations} spawn{profile.invocations === 1 ? '' : 's'} ·{' '}
            {profile.callCount} llm calls · {profile.toolCallCount} tool calls ·{' '}
            {formatCost(profile.totalCost)}
          </div>
        </div>

        {/* Outcome donut + latency. Synthetic profiles (root) don't
            have a meaningful outcome concept — their sessions are the
            user's primary chats and never "terminate" the way a
            subagent does. Skip the donut for those and let latency
            take the full row. */}
        <section className={`grid ${profile.isSynthetic ? 'grid-cols-1' : 'grid-cols-2'} gap-3`}>
          {!profile.isSynthetic && (
          <div className="flex flex-col gap-2 rounded bg-white/[0.025] p-3 ring-hairline">
            <span className="label">outcomes</span>
            {outcomeTotal === 0 ? (
              <span className="font-mono text-[10.5px] italic text-fg-3">no terminations yet</span>
            ) : (
              <>
                <div className="flex h-4 w-full overflow-hidden rounded-md ring-hairline">
                  {outcomeSegments.map((seg) => {
                    if (seg.count === 0) return null
                    const pct = (seg.count / outcomeTotal) * 100
                    return (
                      <div
                        key={seg.label}
                        title={`${seg.label}: ${seg.count} (${pct.toFixed(0)}%)`}
                        style={{ width: `${pct}%`, background: seg.color }}
                      />
                    )
                  })}
                </div>
                <div className="grid grid-cols-2 gap-x-2 gap-y-1 font-mono text-[10px]">
                  {outcomeSegments.map((seg) => (
                    <div
                      key={seg.label}
                      className="flex items-center gap-1.5 text-fg-2"
                    >
                      <span
                        className="h-1.5 w-1.5 rounded-sm"
                        style={{ background: seg.color }}
                      />
                      <span className="text-fg-1">{seg.label}</span>
                      <span className="ml-auto text-fg-3">{seg.count}</span>
                    </div>
                  ))}
                </div>
              </>
            )}
          </div>
          )}
          <div className="flex flex-col gap-2 rounded bg-white/[0.025] p-3 ring-hairline">
            <span className="label">latency</span>
            {sortedLat.length === 0 ? (
              <span className="font-mono text-[10.5px] italic text-fg-3">no calls yet</span>
            ) : (
              <div className="grid grid-cols-3 gap-1 font-mono text-[10.5px]">
                <div className="flex flex-col">
                  <span className="text-fg-3">p50</span>
                  <span className="text-fg-0">{formatDuration(pick(0.5))}</span>
                </div>
                <div className="flex flex-col">
                  <span className="text-fg-3">p95</span>
                  <span className="text-fg-0">{formatDuration(pick(0.95))}</span>
                </div>
                <div className="flex flex-col">
                  <span className="text-fg-3">p99</span>
                  <span className="text-fg-0">{formatDuration(pick(0.99))}</span>
                </div>
              </div>
            )}
          </div>
        </section>

        {/* Iterations vs budget — synthetic profiles have no budget. */}
        {!profile.isSynthetic && profile.iterationsBudget > 0 && (
          <section className="flex flex-col gap-2 rounded bg-white/[0.025] p-3 ring-hairline">
            <span className="label">iterations used vs budget</span>
            <div className="relative h-3 overflow-hidden rounded-full bg-white/[0.04]">
              <div
                className="absolute left-0 top-0 h-full rounded-full"
                style={{
                  width: `${Math.min(100, (profile.iterationsUsed / profile.iterationsBudget) * 100)}%`,
                  background:
                    profile.iterationsUsed / profile.iterationsBudget > 0.9
                      ? '#f5b45d'
                      : profileColor(profile.agentType),
                }}
              />
            </div>
            <div className="flex justify-between font-mono text-[10px] text-fg-3">
              <span>{profile.iterationsUsed} used</span>
              <span>
                {((profile.iterationsUsed / profile.iterationsBudget) * 100).toFixed(0)}%
              </span>
              <span>{profile.iterationsBudget} budget</span>
            </div>
          </section>
        )}

        {/* Models */}
        {topModels.length > 0 && (
          <section className="flex flex-col gap-1.5">
            <span className="label">models</span>
            <div className="flex flex-col gap-1.5">
              {topModels.map(([m, cost]) => {
                const pct =
                  profile.totalCost > 0 ? (cost / profile.totalCost) * 100 : 0
                return (
                  <div
                    key={m}
                    className="flex items-center gap-2 font-mono text-[10.5px]"
                  >
                    <span
                      className="h-1.5 w-1.5 shrink-0 rounded-full"
                      style={{ background: modelColor(m) }}
                    />
                    <span className="text-fg-1">{m.replace('claude-', '').replace('gpt-', '')}</span>
                    <div className="relative h-1.5 flex-1 overflow-hidden rounded-full bg-white/[0.04]">
                      <div
                        className="absolute left-0 top-0 h-full rounded-full bg-accent/65"
                        style={{ width: `${pct}%` }}
                      />
                    </div>
                    <span className="w-[58px] text-right text-fg-2">{formatCost(cost)}</span>
                  </div>
                )
              })}
            </div>
          </section>
        )}

        {/* Tool histogram */}
        {topTools.length > 0 && (
          <section className="flex flex-col gap-1.5">
            <span className="label">top tools</span>
            <RankedBars
              rows={topTools}
              accent={profileColor(profile.agentType)}
            />
          </section>
        )}

        {/* Task previews */}
        {profile.taskPreviews.length > 0 && (
          <section className="flex flex-col gap-1.5">
            <span className="label">recent tasks</span>
            <div className="flex flex-col gap-1">
              {profile.taskPreviews.map((tp) => (
                <button
                  key={tp.sessionId}
                  onClick={() => onJumpToSession(tp.sessionId)}
                  className="rounded bg-white/[0.025] px-2.5 py-1.5 text-left ring-hairline hover:bg-white/[0.05]"
                >
                  <div className="font-mono text-[10px] text-fg-3">
                    {new Date(tp.ts * 1000).toLocaleString()} · {tp.sessionId}
                  </div>
                  <div className="mt-0.5 font-mono text-[10.5px] leading-[1.45] text-fg-1">
                    {tp.preview || '(no preview captured)'}
                  </div>
                </button>
              ))}
            </div>
          </section>
        )}

        {/* Sessions sub-table */}
        <section className="flex flex-col gap-1.5">
          <span className="label">sessions ({profile.sessions.length})</span>
          {profile.sessions.length === 0 ? (
            <div className="rounded bg-white/[0.02] p-3 font-mono text-[10.5px] italic text-fg-3 ring-hairline">
              no sessions tagged with this profile
            </div>
          ) : (
            <div className="max-h-[260px] overflow-y-auto rounded bg-black/30 ring-hairline">
              <table className="w-full font-mono text-[10px]">
                <thead className="sticky top-0 bg-black/60">
                  <tr className="text-left text-fg-3">
                    <th className="px-2 py-1 font-normal">session</th>
                    {!profile.isSynthetic && (
                      <th className="px-2 py-1 font-normal">outcome</th>
                    )}
                    <th className="px-2 py-1 text-right font-normal">iter</th>
                    <th className="px-2 py-1 text-right font-normal">cost</th>
                  </tr>
                </thead>
                <tbody>
                  {profile.sessions.slice(0, 50).map((s) => {
                    const color =
                      s.outcome === 'success'
                        ? '#88d67f'
                        : s.outcome === 'error'
                          ? '#f07878'
                          : s.outcome === 'cancelled'
                            ? '#f5b45d'
                            : '#7fb8e8'
                    return (
                      <tr
                        key={s.sessionId}
                        onClick={() => onJumpToSession(s.sessionId)}
                        className="cursor-pointer border-t border-white/[0.04] hover:bg-white/[0.025]"
                      >
                        <td className="px-2 py-1 text-fg-1">
                          <span className="truncate">{s.sessionId}</span>
                        </td>
                        {!profile.isSynthetic && (
                          <td className="px-2 py-1">
                            <span
                              className="inline-flex items-center gap-1 rounded px-1.5 py-[1px] text-[9px] uppercase"
                              style={{ color, background: `${color}1f` }}
                            >
                              <span
                                className="h-1.5 w-1.5 rounded-full"
                                style={{ background: color }}
                              />
                              {s.outcome}
                            </span>
                          </td>
                        )}
                        <td className="px-2 py-1 text-right text-fg-2">
                          {s.iterations || (profile.isSynthetic ? '—' : 0)}
                          {s.maxIterations > 0 && (
                            <span className="text-fg-3">/{s.maxIterations}</span>
                          )}
                        </td>
                        <td className="px-2 py-1 text-right text-fg-0">
                          {formatCost(s.cost)}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </aside>
  )
}
