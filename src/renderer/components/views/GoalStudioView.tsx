import React, { useMemo, useState } from 'react'
import type {
  AgentView,
  JudgeRules,
  GoalStateView,
  GoalVerdict,
  TelemetryEventView,
  VerdictCriterion,
} from '../shared/types'
import type { ToolCallRecord } from '../../../shared/events'
import { useHarness } from '../../state/store'

interface Props {
  goalState: GoalStateView | null
  agents: AgentView[]
  contextPct: number
  cost: number
  onOpenJudgeBrief: () => void
}

/**
 * Goal mode dashboard, redesigned as a turn-correlated timeline.
 *
 * Layout: a compact header strip (mission + counters), then a confidence
 * trajectory strip, then a scrollable vertical timeline of judge verdicts
 * (newest at top). Each verdict card carries the full reason prose
 * (selectable, never truncated), criteria status diffs vs. the prior turn,
 * open questions, and a reserved "Judge inspected" slot for Phase 3's
 * tool-using judge.
 *
 * Replaces the 3-column layout that wasted real estate on the mission
 * objective and crammed all 9 verdicts into a sidebar column too narrow
 * to read or select.
 */
export function GoalStudioView({ goalState, agents, contextPct, cost, onOpenJudgeBrief }: Props) {
  if (!goalState) {
    return (
      <div className="flex flex-1 items-center justify-center font-mono text-[12px] tracking-[0.06em] text-fg-3">
        no goal set · type <code className="text-fg-0">/goal &lt;objective&gt;</code> to start
      </div>
    )
  }

  // Verdict history is newest-last from the bridge; reverse for display
  // so the latest sits at the top of the timeline.
  const verdicts = useMemo(() => {
    const h = goalState.verdictHistory ?? []
    if (h.length > 0) return h
    return goalState.lastVerdict ? [goalState.lastVerdict] : []
  }, [goalState.verdictHistory, goalState.lastVerdict])

  const judgeEvents = useMemo(
    () => goalState.events.filter((e) => e.subtype === 'goal_judge').sort((a, b) => a.at - b.at),
    [goalState.events],
  )

  // Cumulative criteria state across all verdicts (latest status wins),
  // plus a per-criterion journey: status at every turn it was mentioned.
  // Missing entries in any turn collapse to a tombstone in the journey so
  // the side-rail strip shows when a criterion went silent.
  const criteriaIndex = useMemo(() => buildCriteriaIndex(verdicts), [verdicts])

  const [scrollToTurn, setScrollToTurn] = useState<number | null>(null)

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <HeaderStrip
        goalState={goalState}
        agents={agents}
        contextPct={contextPct}
        cost={cost}
        onOpenJudgeBrief={onOpenJudgeBrief}
      />

      <TrajectoryStrip
        verdicts={verdicts}
        onJumpTo={(turn) => setScrollToTurn(turn)}
      />

      <div className="grid min-h-0 flex-1 grid-cols-[minmax(0,1fr)_320px] overflow-hidden">
        <main className="min-h-0 overflow-y-auto px-10 py-8">
          {verdicts.length === 0 ? (
            <div className="py-14 text-center font-mono text-[12px] tracking-[0.06em] text-fg-3">
              no verdicts yet — the judge will fire after the first agent turn.
            </div>
          ) : (
            <div className="mx-auto flex max-w-[820px] flex-col gap-5">
              {verdicts
                .map((v, i) => ({ v, turn: i + 1, ev: judgeEvents[i] }))
                .reverse()
                .map(({ v, turn, ev }, idxFromTop, allRev) => {
                  // Prior verdict = the turn before this one (lower index in
                  // chronological order = next-down in reversed list).
                  const prior = allRev[idxFromTop + 1]?.v ?? null
                  return (
                    <TurnCard
                      key={turn}
                      turn={turn}
                      totalTurns={verdicts.length}
                      maxTurns={goalState.maxTurns}
                      verdict={v}
                      prior={prior}
                      eventAt={ev?.at}
                      profile={goalState.judgeRules?.judgeProfile ?? 'standard'}
                      scrollToHere={scrollToTurn === turn}
                      onConsumeScroll={() => setScrollToTurn(null)}
                    />
                  )
                })}
            </div>
          )}
        </main>

        <SideRail
          goalState={goalState}
          criteriaIndex={criteriaIndex}
          onOpenJudgeBrief={onOpenJudgeBrief}
        />
      </div>
    </div>
  )
}

// ============ HEADER STRIP (compact) ============

function HeaderStrip({
  goalState,
  agents,
  contextPct,
  cost,
  onOpenJudgeBrief,
}: {
  goalState: GoalStateView
  agents: AgentView[]
  contextPct: number
  cost: number
  onOpenJudgeBrief: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const running = agents.filter((a) => a.status === 'running').length
  const profile = goalState.judgeRules?.judgeProfile ?? 'standard'
  const rigor = goalState.judgeRules?.rigorScore ?? 6
  return (
    <header className="border-b border-white/[0.06] px-10 py-5">
      <div className="flex flex-wrap items-baseline gap-4">
        <span className="font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">goal</span>
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="group flex-1 cursor-text select-text text-left"
          title={goalState.goal}
        >
          <h1
            className={`m-0 font-serif font-light leading-[1.45] tracking-[-0.005em] text-fg-1 ${
              expanded ? 'text-[17px]' : 'text-[16px] line-clamp-2'
            }`}
          >
            {goalState.goal || <span className="italic text-fg-3">no objective set</span>}
          </h1>
        </button>
        <button
          type="button"
          onClick={onOpenJudgeBrief}
          className="rounded-md border border-accent/[0.22] bg-accent/[0.06] px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.14em] text-accent transition hover:bg-accent/[0.12]"
        >
          Judge Rules
        </button>
      </div>
      <div className="mt-3 flex flex-wrap gap-4 font-mono text-[11.5px] tracking-[0.06em] text-fg-2">
        <span>
          <span className="tabular-nums text-fg-0">turn {goalState.turnsUsed}</span>{' '}
          <span className="text-fg-3">of</span>{' '}
          <span className="tabular-nums text-fg-0">{goalState.maxTurns}</span>
        </span>
        <StatusBadge status={goalState.status} />
        <span>
          judge: <span className="text-fg-0">{profile}</span>{' '}
          <span className="text-fg-3">· rigor {rigor}/10</span>
        </span>
        <span className="text-fg-3">·</span>
        <span>
          <span className="tabular-nums text-fg-0">{running}</span> agents
        </span>
        <span>
          <span className="tabular-nums text-fg-0">{contextPct}%</span> ctx
        </span>
        <span>
          <span className="tabular-nums text-fg-0">${cost.toFixed(2)}</span> spend
        </span>
      </div>
      {goalState.pauseReason ? (
        <div className="mt-2 select-text font-mono text-[12px] text-warn">
          paused · {goalState.pauseReason}
        </div>
      ) : null}
    </header>
  )
}

function StatusBadge({ status }: { status: string }) {
  const isDone = status === 'done'
  const isPaused = status === 'paused'
  const color = isDone
    ? 'text-ok border-ok/[0.32] bg-ok/[0.08]'
    : isPaused
    ? 'text-warn border-warn/[0.32] bg-warn/[0.08]'
    : 'text-accent border-accent/[0.32] bg-accent/[0.08]'
  const dot = isDone
    ? 'bg-ok'
    : isPaused
    ? 'bg-warn'
    : 'animate-pulse-soft bg-accent shadow-[0_0_6px_rgba(168,212,252,0.6)]'
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-[10px] border px-2 py-0.5 font-mono text-[10.5px] uppercase tracking-[0.14em] ${color}`}
    >
      <span className={`h-1 w-1 rounded-full ${dot}`} />
      {status}
    </span>
  )
}

// ============ TRAJECTORY STRIP ============

function TrajectoryStrip({
  verdicts,
  onJumpTo,
}: {
  verdicts: GoalVerdict[]
  onJumpTo: (turn: number) => void
}) {
  if (verdicts.length === 0) return null
  const conf = verdicts.map((v) => (typeof v.confidence === 'number' ? v.confidence : 0))
  const w = 100
  const h = 32
  const xStep = verdicts.length > 1 ? w / (verdicts.length - 1) : w
  const points = conf
    .map((c, i) => `${(i * xStep).toFixed(2)},${(h - c * h).toFixed(2)}`)
    .join(' ')

  return (
    <div className="border-b border-white/[0.06] bg-black/[0.18] px-10 py-3">
      <div className="mb-2 flex items-baseline justify-between font-mono text-[10px] uppercase tracking-[0.14em] text-fg-3">
        <span>
          confidence trajectory · <span className="font-mono normal-case tracking-normal text-fg-1 tabular-nums">{verdicts.length}</span> verdicts
        </span>
        <span className="font-mono normal-case tracking-normal text-fg-3">
          dashed line is the 0.85 done-threshold
        </span>
      </div>
      <div className="flex items-center gap-3">
        <svg
          viewBox={`0 0 ${w} ${h}`}
          preserveAspectRatio="none"
          className="h-12 flex-1"
          style={{ overflow: 'visible' }}
        >
          {/* done threshold */}
          <line
            x1="0"
            y1={h - 0.85 * h}
            x2={w}
            y2={h - 0.85 * h}
            stroke="rgba(136,214,127,0.25)"
            strokeDasharray="2,2"
            strokeWidth="0.5"
            vectorEffect="non-scaling-stroke"
          />
          <polyline
            points={points}
            fill="none"
            stroke="rgba(168,212,252,0.85)"
            strokeWidth="1.2"
            strokeLinejoin="round"
            strokeLinecap="round"
            vectorEffect="non-scaling-stroke"
          />
          {verdicts.map((v, i) => {
            const c = typeof v.confidence === 'number' ? v.confidence : 0
            const done = !!v.done
            return (
              <g key={i} className="cursor-pointer" onClick={() => onJumpTo(i + 1)}>
                <circle
                  cx={(i * xStep).toFixed(2)}
                  cy={(h - c * h).toFixed(2)}
                  r={1.6}
                  fill={done ? 'rgb(168,176,168)' : 'rgb(168,212,252)'}
                  vectorEffect="non-scaling-stroke"
                />
                <title>{`turn ${i + 1} · conf ${c.toFixed(2)} · ${done ? 'done' : 'continue'}`}</title>
              </g>
            )
          })}
        </svg>
        <div className="flex gap-1.5">
          {verdicts.map((v, i) => {
            const done = !!v.done
            return (
              <button
                key={i}
                type="button"
                onClick={() => onJumpTo(i + 1)}
                className={`rounded px-1.5 py-0.5 font-mono text-[10px] tabular-nums transition hover:bg-white/[0.06] ${
                  done ? 'text-ok' : 'text-fg-2'
                }`}
              >
                t{i + 1}
              </button>
            )
          })}
        </div>
      </div>
    </div>
  )
}

// ============ TURN CARD ============

function TurnCard({
  turn,
  totalTurns,
  maxTurns,
  verdict,
  prior,
  eventAt,
  profile,
  scrollToHere,
  onConsumeScroll,
}: {
  turn: number
  totalTurns: number
  maxTurns: number
  verdict: GoalVerdict
  prior: GoalVerdict | null
  eventAt?: number
  profile: string
  scrollToHere: boolean
  onConsumeScroll: () => void
}) {
  const ref = React.useRef<HTMLDivElement>(null)
  const conf = typeof verdict.confidence === 'number' ? verdict.confidence : 0
  const isLatest = turn === totalTurns

  // Pull the judge's child session slice (if this verdict was produced by
  // the deep profile and the subagent path didn't crash). We subscribe
  // narrowly so unrelated session updates don't rerender every turn card.
  const judgeSessionId = verdict.judgeSessionId ?? null
  const judgeSlice = useHarness((s) =>
    judgeSessionId
      ? s.activeSessionId === judgeSessionId
        ? null  // unusual: judge as the active session — fall through to props
        : s.sessionArchive[judgeSessionId]
      : null,
  )
  const openSessionPane = useHarness((s) => s.openSessionPane)
  const judgeToolCalls: ToolCallRecord[] = useMemo(() => {
    if (!judgeSlice) return []
    return judgeSlice.toolCallOrder
      .map((id) => judgeSlice.toolCalls[id])
      .filter((c): c is ToolCallRecord => !!c)
  }, [judgeSlice])

  React.useEffect(() => {
    if (scrollToHere && ref.current) {
      ref.current.scrollIntoView({ behavior: 'smooth', block: 'center' })
      onConsumeScroll()
    }
  }, [scrollToHere, onConsumeScroll])

  // Compute criteria deltas vs the prior turn so the operator sees what
  // actually changed this turn (new criteria, status transitions).
  const deltas = useMemo(() => computeCriteriaDelta(prior, verdict), [prior, verdict])

  return (
    <div
      ref={ref}
      className={`overflow-hidden rounded-lg border ${
        verdict.done
          ? 'border-ok/[0.25] bg-ok/[0.04]'
          : isLatest
          ? 'border-accent/[0.22] bg-accent/[0.03]'
          : 'border-white/[0.06] bg-white/[0.018]'
      }`}
    >
      <header className="flex flex-wrap items-baseline gap-3 border-b border-white/[0.06] px-5 py-3">
        <span className="font-mono text-[12px] text-fg-0">
          Turn <span className="tabular-nums">{turn}</span>{' '}
          <span className="text-fg-3">of {maxTurns}</span>
        </span>
        <VerdictBadge done={verdict.done} />
        <span className="font-mono text-[11px] tabular-nums text-fg-2">
          conf <span className={confidenceColor(conf)}>{conf.toFixed(2)}</span>
        </span>
        {prior && typeof prior.confidence === 'number' ? (
          <span className="font-mono text-[10.5px] tabular-nums text-fg-3">
            {confDeltaLabel(prior.confidence, conf)}
          </span>
        ) : null}
        <span className="text-fg-3">·</span>
        <span className="font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
          {profile}
        </span>
        {eventAt ? (
          <span className="ml-auto font-mono text-[10.5px] tabular-nums text-fg-3">
            {fmtTime(eventAt)}
          </span>
        ) : null}
      </header>

      <div className="grid grid-cols-[minmax(0,1fr)_300px] gap-0">
        {/* LEFT: judge prose (selectable, no truncation) */}
        <section className="border-r border-white/[0.06] px-5 py-4">
          <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-3">
            Reason
          </div>
          <p className="m-0 select-text whitespace-pre-wrap font-mono text-[13px] leading-[1.7] text-fg-1">
            {verdict.reason || <span className="italic text-fg-3">no reason supplied</span>}
          </p>

          {(verdict.openQuestions?.length ?? 0) > 0 ? (
            <div className="mt-4">
              <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-3">
                Open questions · {verdict.openQuestions?.length}
              </div>
              <ul className="m-0 flex list-none flex-col gap-1.5 p-0">
                {verdict.openQuestions?.map((q, i) => (
                  <li
                    key={i}
                    className="select-text rounded-md border border-warn/[0.18] bg-warn/[0.04] px-3 py-1.5 font-mono text-[12px] leading-[1.55] text-fg-1"
                  >
                    {q}
                  </li>
                ))}
              </ul>
            </div>
          ) : null}
        </section>

        {/* RIGHT: criteria delta + judge-inspected placeholder */}
        <aside className="flex flex-col gap-4 bg-black/[0.10] px-4 py-4">
          <div>
            <div className="mb-2 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-3">
              Criteria this turn
            </div>
            <CriteriaDeltaList deltas={deltas} />
          </div>
          <div>
            <div className="mb-2 flex items-baseline justify-between font-mono text-[10px] uppercase tracking-[0.14em] text-fg-3">
              <span>Judge inspected</span>
              {judgeSessionId ? (
                <button
                  type="button"
                  onClick={() => openSessionPane(judgeSessionId)}
                  className="rounded px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-[0.14em] text-fg-3 transition hover:bg-white/[0.06] hover:text-accent"
                  title="Open the judge's own session pane to see its transcript"
                >
                  open ↗
                </button>
              ) : null}
            </div>
            {verdict.fallbackFrom ? (
              <div className="mb-2 select-text rounded-md border border-warn/[0.22] bg-warn/[0.06] px-2 py-1.5 font-mono text-[10.5px] leading-[1.5] text-warn">
                judge-fallback · {verdict.fallbackFrom}
              </div>
            ) : null}
            {judgeToolCalls.length > 0 ? (
              <ul className="m-0 flex list-none flex-col gap-1 p-0">
                {judgeToolCalls.map((call) => (
                  <JudgeToolRow key={call.id} call={call} />
                ))}
              </ul>
            ) : judgeSessionId ? (
              <div className="font-mono text-[11px] italic leading-[1.55] text-fg-3">
                judge spawned but used no tools — reached a verdict from
                reasoning alone.
              </div>
            ) : (
              <div className="font-mono text-[11px] italic leading-[1.55] text-fg-3">
                {profile === 'deep'
                  ? 'no judge session attached — verdict may pre-date the deep profile'
                  : 'no tools — single-call profile uses text reasoning only'}
              </div>
            )}
          </div>
        </aside>
      </div>
    </div>
  )
}

function VerdictBadge({ done }: { done?: boolean }) {
  return (
    <span
      className={`inline-flex items-center rounded-[10px] border px-2 py-0.5 font-mono text-[10.5px] uppercase tracking-[0.14em] ${
        done
          ? 'border-ok/[0.32] bg-ok/[0.08] text-ok'
          : 'border-accent/[0.22] bg-accent/[0.06] text-accent'
      }`}
    >
      {done ? 'done' : 'continue'}
    </span>
  )
}

function JudgeToolRow({ call }: { call: ToolCallRecord }) {
  // Status dot: running=accent (in-flight), success=ok, error=warn.
  const dot =
    call.status === 'success'
      ? 'bg-ok'
      : call.status === 'error'
      ? 'bg-warn'
      : 'bg-accent animate-pulse-soft'
  // Args preview: short single-line glimpse so the operator can see what
  // the judge actually looked at without expanding the child session.
  const preview = useMemo(() => previewArgs(call.arguments), [call.arguments])
  return (
    <li className="flex items-baseline gap-2 rounded-md border border-white/[0.05] bg-white/[0.014] px-2 py-1">
      <span className={`mt-1 h-1.5 w-1.5 shrink-0 rounded-full ${dot}`} />
      <div className="min-w-0 flex-1">
        <div className="font-mono text-[11.5px] text-fg-1">
          <span className="text-fg-0">{call.name}</span>
          {preview && (
            <span className="ml-1.5 text-fg-3">
              · <span className="text-fg-2">{preview}</span>
            </span>
          )}
        </div>
      </div>
    </li>
  )
}

function previewArgs(input: unknown): string {
  if (!input || typeof input !== 'object') return ''
  const obj = input as Record<string, unknown>
  // Prefer the most diagnostic field per common tool. Fall back to the
  // first non-trivial string value otherwise.
  const candidates = ['query', 'path', 'pattern', 'url', 'command', 'cmd']
  for (const key of candidates) {
    const v = obj[key]
    if (typeof v === 'string' && v.trim()) {
      return v.length > 60 ? v.slice(0, 57) + '…' : v
    }
  }
  for (const v of Object.values(obj)) {
    if (typeof v === 'string' && v.trim()) {
      return v.length > 60 ? v.slice(0, 57) + '…' : v
    }
  }
  return ''
}

function confidenceColor(c: number): string {
  if (c >= 0.85) return 'text-ok'
  if (c >= 0.5) return 'text-accent'
  return 'text-warn'
}

function confDeltaLabel(prev: number, curr: number): string {
  const d = curr - prev
  if (Math.abs(d) < 0.005) return '(unchanged)'
  const sign = d > 0 ? '▲' : '▼'
  return `${sign} ${Math.abs(d).toFixed(2)}`
}

function fmtTime(at: number): string {
  const d = new Date(at)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

// ============ CRITERIA DELTA ============

interface CriteriaDelta {
  text: string
  priority: VerdictCriterion['priority']
  status: VerdictCriterion['status']
  change: 'new' | 'unchanged' | 'progressed' | 'regressed'
  prevStatus?: VerdictCriterion['status']
}

function computeCriteriaDelta(prior: GoalVerdict | null, current: GoalVerdict): CriteriaDelta[] {
  const priorById = new Map<string, VerdictCriterion>()
  for (const c of prior?.criteria ?? []) priorById.set(c.id, c)
  const rank: Record<VerdictCriterion['status'], number> = { missing: 0, partial: 1, met: 2 }
  const out: CriteriaDelta[] = []
  for (const c of current.criteria ?? []) {
    const prev = priorById.get(c.id)
    let change: CriteriaDelta['change'] = 'unchanged'
    if (!prev) change = 'new'
    else if (rank[c.status] > rank[prev.status]) change = 'progressed'
    else if (rank[c.status] < rank[prev.status]) change = 'regressed'
    out.push({
      text: c.text,
      priority: c.priority,
      status: c.status,
      change,
      prevStatus: prev?.status,
    })
  }
  // Order: changes first, then unchanged
  out.sort((a, b) => {
    const order = { new: 0, progressed: 1, regressed: 2, unchanged: 3 }
    return order[a.change] - order[b.change]
  })
  return out
}

function CriteriaDeltaList({ deltas }: { deltas: CriteriaDelta[] }) {
  if (deltas.length === 0) {
    return (
      <div className="font-mono text-[11.5px] italic text-fg-3">
        no criteria tracked yet
      </div>
    )
  }
  // Only surface what *changed* this turn — new, progressed, regressed.
  // Unchanged criteria pile up otherwise; the operator wants to see what
  // moved, not re-read what didn't.
  const changes = deltas.filter((d) => d.change !== 'unchanged')
  const unchanged = deltas.length - changes.length
  if (changes.length === 0) {
    return (
      <div className="font-mono text-[11.5px] italic leading-[1.55] text-fg-3">
        no criteria moved this turn
        <br />
        <span className="text-fg-4">
          ({unchanged} criteria held their prior status)
        </span>
      </div>
    )
  }
  return (
    <>
      <ul className="m-0 flex list-none flex-col gap-1 p-0">
        {changes.map((d, i) => (
          <li
            key={i}
            className="select-text rounded px-1.5 py-1 font-mono text-[11.5px] leading-[1.5]"
          >
            <div className="flex items-center gap-1.5">
              <StatusGlyph status={d.status} />
              <span className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-fg-3">
                {d.priority}
              </span>
              <span
                className={`font-mono text-[9.5px] uppercase tracking-[0.14em] ${changeColor(
                  d.change,
                )}`}
              >
                {d.change}
              </span>
            </div>
            <div className="ml-5 mt-0.5 text-fg-1">{d.text}</div>
            {d.change === 'progressed' || d.change === 'regressed' ? (
              <div className="ml-5 mt-0.5 font-mono text-[10px] text-fg-3">
                was {d.prevStatus} → now {d.status}
              </div>
            ) : null}
          </li>
        ))}
      </ul>
      {unchanged > 0 ? (
        <div className="mt-1.5 font-mono text-[10px] text-fg-4">
          + {unchanged} unchanged
        </div>
      ) : null}
    </>
  )
}

function changeColor(change: CriteriaDelta['change']): string {
  if (change === 'new') return 'text-accent'
  if (change === 'progressed') return 'text-ok'
  if (change === 'regressed') return 'text-warn'
  return 'text-fg-3'
}

function StatusGlyph({ status }: { status: VerdictCriterion['status'] }) {
  const glyph = status === 'met' ? '✓' : status === 'partial' ? '◐' : '○'
  const color =
    status === 'met' ? 'text-ok' : status === 'partial' ? 'text-accent' : 'text-fg-3'
  return <span className={`font-mono text-[11px] ${color}`}>{glyph}</span>
}

// ============ SIDE RAIL ============

interface CriterionJourney {
  id: string
  text: string
  priority: VerdictCriterion['priority']
  // Status at each turn, in chronological order. `null` = the criterion
  // wasn't mentioned that turn (the judge stopped tracking it).
  journey: Array<VerdictCriterion['status'] | null>
  latestStatus: VerdictCriterion['status']
  lastSeenTurn: number
}

function buildCriteriaIndex(verdicts: GoalVerdict[]): CriterionJourney[] {
  // First pass: collect every unique criterion ID and remember the text +
  // priority from its most recent mention (in case the judge re-worded it).
  const meta = new Map<string, { text: string; priority: VerdictCriterion['priority'] }>()
  for (const v of verdicts) {
    for (const c of v.criteria ?? []) {
      meta.set(c.id, { text: c.text, priority: c.priority })
    }
  }
  // Second pass: build per-criterion journey across all turns.
  const out: CriterionJourney[] = []
  for (const [id, m] of meta.entries()) {
    const journey: Array<VerdictCriterion['status'] | null> = []
    let latestStatus: VerdictCriterion['status'] = 'missing'
    let lastSeenTurn = 0
    verdicts.forEach((v, idx) => {
      const c = (v.criteria ?? []).find((x) => x.id === id)
      if (c) {
        journey.push(c.status)
        latestStatus = c.status
        lastSeenTurn = idx + 1
      } else {
        journey.push(null)
      }
    })
    out.push({ id, text: m.text, priority: m.priority, journey, latestStatus, lastSeenTurn })
  }
  // Order: priority (must > should > may), then by latest status (missing
  // first so the operator's eye lands on what needs work).
  const priRank: Record<VerdictCriterion['priority'], number> = { must: 0, should: 1, may: 2 }
  const statRank: Record<VerdictCriterion['status'], number> = { missing: 0, partial: 1, met: 2 }
  out.sort((a, b) => {
    if (priRank[a.priority] !== priRank[b.priority]) {
      return priRank[a.priority] - priRank[b.priority]
    }
    return statRank[a.latestStatus] - statRank[b.latestStatus]
  })
  return out
}

function JourneyStrip({ journey }: { journey: Array<VerdictCriterion['status'] | null> }) {
  // Tiny horizontal strip of dots — one dot per turn, colored by status.
  // Lets the operator see a criterion's history at a glance.
  return (
    <div className="flex items-center gap-0.5">
      {journey.map((s, i) => {
        const color =
          s === 'met'
            ? 'bg-ok'
            : s === 'partial'
            ? 'bg-accent'
            : s === 'missing'
            ? 'bg-warn'
            : 'bg-fg-4'
        return (
          <span
            key={i}
            className={`h-1.5 w-1.5 rounded-full ${color}`}
            title={`turn ${i + 1}: ${s ?? 'not tracked'}`}
          />
        )
      })}
    </div>
  )
}

function SideRail({
  goalState,
  criteriaIndex,
  onOpenJudgeBrief,
}: {
  goalState: GoalStateView
  criteriaIndex: CriterionJourney[]
  onOpenJudgeBrief: () => void
}) {
  const brief = goalState.judgeRules
  const met = criteriaIndex.filter((c) => c.latestStatus === 'met').length
  const partial = criteriaIndex.filter((c) => c.latestStatus === 'partial').length
  const missing = criteriaIndex.filter((c) => c.latestStatus === 'missing').length
  const totalTurns =
    goalState.verdictHistory?.length ?? (goalState.lastVerdict ? 1 : 0)

  return (
    <aside className="flex flex-col gap-7 overflow-y-auto border-l border-white/[0.06] bg-black/[0.10] px-6 py-7">
      <section>
        <div className="mb-2 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
          Criteria · journey
        </div>
        {criteriaIndex.length === 0 ? (
          <div className="font-mono text-[11.5px] italic text-fg-3">
            no criteria tracked yet — the judge will surface them as the loop runs.
          </div>
        ) : (
          <>
            <div className="mb-3 flex gap-3 font-mono text-[11px] tabular-nums">
              <span>
                <span className="text-ok">{met}</span>{' '}
                <span className="text-fg-3">met</span>
              </span>
              <span>
                <span className="text-accent">{partial}</span>{' '}
                <span className="text-fg-3">partial</span>
              </span>
              <span>
                <span className="text-warn">{missing}</span>{' '}
                <span className="text-fg-3">missing</span>
              </span>
            </div>
            <ul className="m-0 flex list-none flex-col gap-2 p-0">
              {criteriaIndex.map((c) => (
                <li
                  key={c.id}
                  className="select-text rounded px-1.5 py-1 font-mono text-[11.5px] leading-[1.5] hover:bg-white/[0.02]"
                >
                  <div className="flex items-center gap-1.5">
                    <StatusGlyph status={c.latestStatus} />
                    <span className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-fg-3">
                      {c.priority}
                    </span>
                    {c.lastSeenTurn !== totalTurns && c.lastSeenTurn > 0 ? (
                      <span
                        className="font-mono text-[9.5px] uppercase tracking-[0.14em] text-fg-3"
                        title={`Judge stopped mentioning at turn ${c.lastSeenTurn}`}
                      >
                        stale t{c.lastSeenTurn}
                      </span>
                    ) : null}
                  </div>
                  <div className="ml-5 mt-0.5 text-fg-1">{c.text}</div>
                  {c.journey.length > 1 ? (
                    <div className="ml-5 mt-1.5">
                      <JourneyStrip journey={c.journey} />
                    </div>
                  ) : null}
                </li>
              ))}
            </ul>
          </>
        )}
      </section>

      <section>
        <div className="mb-2 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
          Judge rules
        </div>
        <BriefSummary brief={brief} />
        <button
          type="button"
          onClick={onOpenJudgeBrief}
          className="mt-3 w-full rounded-md border border-accent/[0.22] bg-accent/[0.06] px-3 py-2 font-mono text-[11px] uppercase tracking-[0.14em] text-accent transition hover:bg-accent/[0.12]"
        >
          Open Judge Rules →
        </button>
      </section>
    </aside>
  )
}

function BriefSummary({ brief }: { brief?: JudgeRules | null }) {
  if (!brief) {
    return (
      <div className="font-mono text-[11.5px] italic leading-[1.55] text-fg-3">
        defaults — no operator brief set.
      </div>
    )
  }
  const mustCount = brief.criteria.filter((c) => c.priority === 'must').length
  const shouldCount = brief.criteria.filter((c) => c.priority === 'should').length
  const mayCount = brief.criteria.filter((c) => c.priority === 'may').length
  return (
    <div className="select-text rounded-md border border-white/[0.06] bg-white/[0.018] px-3 py-2 font-mono text-[11.5px] leading-[1.55] text-fg-1">
      <div className="flex flex-wrap gap-2 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
        <span>
          profile <span className="font-mono normal-case tracking-normal text-fg-0">{brief.judgeProfile}</span>
        </span>
        <span>
          rigor <span className="font-mono normal-case tracking-normal tabular-nums text-fg-0">{brief.rigorScore}/10</span>
        </span>
      </div>
      {brief.voice ? (
        <p className="m-0 mt-2 line-clamp-3 select-text italic text-fg-2">{brief.voice}</p>
      ) : null}
      {mustCount + shouldCount + mayCount > 0 ? (
        <div className="mt-2 font-mono text-[10.5px] tracking-[0.04em] text-fg-3">
          {mustCount} must · {shouldCount} should · {mayCount} may
        </div>
      ) : null}
    </div>
  )
}
