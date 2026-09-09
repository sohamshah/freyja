import { useEffect, useMemo, useRef, useState } from 'react'
import { sessionCostBreakdown, useHarness } from '../../state/store'
import { ScrambleText } from '../ScrambleText'
import { formatCost, formatDuration, formatTokens } from '../../lib/format'
import type { Message, SubagentRecord, ToolCallRecord } from '@shared/events'

/**
 * SessionReceiptView — the session-specific activity/metrics view.
 *
 * Ported from mockups/dashboard-session-receipt-v5.html. The hero is a
 * "thread braid": one horizontal line is the session's main thread on the
 * true time axis; every sub-agent is a transit-style branch that forks off
 * when spawned, runs on its own lane for exactly as long as it ran, and
 * merges back. Stroke width follows tokens read (log scale). Idle
 * stretches render the main thread dashed. Nothing in the drawing is
 * decorative — every arc, width, gap and mark is a recorded value from
 * the active session's slice.
 *
 * Below the braid: KPI tiles (ScrambleText, same treatment as the metrics
 * view) and four analysis cards — read amplification, wall clock by tool,
 * swarm reading, and what landed on disk.
 */

// ── palette ─────────────────────────────────────────────────────────────
// The ice ramp was validated (dataviz validate_palette.js, ordinal, dark
// surface) for the receipt mocks; sand is the house warn token. Chart
// marks use hex directly; chrome uses the tailwind fg/bg tokens.
const ICE_0 = '#a8d4fc'
const ICE_2 = '#5b8fcf'
const ICE_4 = '#2a4d78'
const SAND = '#b8a078'

const MIN = 60_000

// ── derivation ──────────────────────────────────────────────────────────

interface ThreadView {
  id: string
  label: string
  agentType: string
  state: SubagentRecord['state']
  /** minutes from session start */
  start: number
  /** minutes */
  dur: number
  tokensIn: number
  tokensOut: number
}

interface Anatomy {
  t0: number
  spanMin: number
  activeMin: number
  gaps: Array<[number, number]>
  biggest: [number, number] | null
  threads: ThreadView[]
  waves: Array<{ at: number; threads: ThreadView[] }>
  userMarks: number[]
  artifactMarks: number[]
  toolAgg: Array<{ name: string; n: number; ms: number; maxMs: number; err: number }>
  toolCallCount: number
  toolErrCount: number
}

function deriveAnatomy(
  messages: Message[],
  toolCalls: Record<string, ToolCallRecord>,
  subagents: Record<string, SubagentRecord>,
  artifacts: Array<{ createdAt: number; creator: string; operation: string }>,
  isStreaming: boolean,
  now: number,
): Anatomy | null {
  const tools = Object.values(toolCalls).filter((t) => t.startedAt > 0)
  const subs = Object.values(subagents).filter((s) => s.startedAt > 0)

  let t0 = Infinity
  let tEnd = -Infinity
  for (const m of messages) {
    if (m.createdAt > 0) t0 = Math.min(t0, m.createdAt)
    tEnd = Math.max(tEnd, m.completedAt ?? m.createdAt ?? 0)
  }
  for (const t of tools) {
    t0 = Math.min(t0, t.startedAt)
    tEnd = Math.max(tEnd, t.startedAt + (t.durationMs ?? 0))
  }
  for (const s of subs) {
    t0 = Math.min(t0, s.startedAt)
    tEnd = Math.max(tEnd, s.startedAt + (s.elapsedMs ?? 0))
  }
  if (!Number.isFinite(t0)) return null
  if (isStreaming) tEnd = Math.max(tEnd, now)
  const spanMin = Math.max(1, (tEnd - t0) / MIN)

  // Per-minute occupancy: a minute is live when a tool was IN FLIGHT or a
  // sub-agent was running. Mirrors the mocks so the stall figure matches.
  const N = Math.max(1, Math.ceil(spanMin))
  const busy = new Uint8Array(N + 1)
  const mark = (fromMs: number, toMs: number) => {
    const a = Math.max(0, Math.floor((fromMs - t0) / MIN))
    const b = Math.min(N, Math.ceil((toMs - t0) / MIN))
    for (let i = a; i <= b && i <= N; i++) busy[i] = 1
  }
  for (const t of tools) mark(t.startedAt, t.startedAt + Math.max(t.durationMs ?? 0, 1))
  for (const s of subs) {
    // elapsedMs updates live while an agent genuinely runs, so it is the
    // honest end for running threads too — trusting `now` instead lets a
    // stale crashed "running" record paint days of dead tail as live.
    mark(s.startedAt, s.startedAt + Math.max(s.elapsedMs ?? 0, 30_000))
  }

  const gaps: Array<[number, number]> = []
  let run = -1
  for (let i = 0; i <= N; i++) {
    if (!busy[i] && run < 0) run = i
    if (busy[i] && run >= 0) {
      if (i - run >= 5) gaps.push([run, i])
      run = -1
    }
  }
  // A trailing quiet stretch on a non-streaming session is just "the
  // session ended", not a stall — only count it while still live.
  if (run >= 0 && N - run >= 5 && isStreaming) gaps.push([run, N])
  const activeMin = busy.reduce((a, b) => a + b, 0)
  const biggest = gaps.length
    ? gaps.reduce((a, g) => (g[1] - g[0] > a[1] - a[0] ? g : a))
    : null

  const threads: ThreadView[] = subs
    .map((s) => ({
      id: s.id,
      label: s.label || s.agentType || s.id,
      agentType: s.agentType ?? '',
      state: s.state,
      start: (s.startedAt - t0) / MIN,
      dur: Math.max(0, (s.elapsedMs ?? 0) / MIN),
      tokensIn: s.tokensIn ?? 0,
      tokensOut: s.tokensOut ?? 0,
    }))
    .sort((a, b) => a.start - b.start)

  const waves: Anatomy['waves'] = []
  for (const th of threads) {
    const w = waves[waves.length - 1]
    if (w && Math.abs(th.start - w.at) < 0.5) w.threads.push(th)
    else waves.push({ at: th.start, threads: [th] })
  }

  const userMarks = messages
    .filter((m) => m.role === 'user' && m.createdAt >= t0)
    .map((m) => (m.createdAt - t0) / MIN)
  const artifactMarks = artifacts
    .filter((a) => a.creator === 'parent' && a.createdAt >= t0)
    .map((a) => (a.createdAt - t0) / MIN)

  const agg = new Map<string, { n: number; ms: number; maxMs: number; err: number }>()
  let toolErrCount = 0
  for (const t of tools) {
    const rec = agg.get(t.name) ?? { n: 0, ms: 0, maxMs: 0, err: 0 }
    const ms = t.durationMs ?? 0
    rec.n += 1
    rec.ms += ms
    rec.maxMs = Math.max(rec.maxMs, ms)
    if (t.isError) {
      rec.err += 1
      toolErrCount += 1
    }
    agg.set(t.name, rec)
  }
  const toolAgg = [...agg.entries()]
    .map(([name, r]) => ({ name, ...r }))
    .sort((a, b) => b.ms - a.ms)

  return {
    t0,
    spanMin,
    activeMin,
    gaps,
    biggest,
    threads,
    waves,
    userMarks,
    artifactMarks,
    toolAgg,
    toolCallCount: tools.length,
    toolErrCount,
  }
}

// ── formatting ──────────────────────────────────────────────────────────

const hm = (m: number) => formatDuration(Math.max(0, Math.round(m * MIN)))
/** formatTokens tops out at "1867k"; sessions routinely cross 1M reads. */
const tok = (n: number) => (n >= 1e6 ? `${(n / 1e6).toFixed(2)}M` : formatTokens(n))

// ── braid layout ────────────────────────────────────────────────────────

interface BraidLabel {
  key: string
  text: string
  bold?: string
  suffix?: { text: string; warn?: boolean }
  x?: number
  right?: number
  y: number
  dim?: boolean
  /** thread this label belongs to, for hover-highlight coupling */
  threadId?: string
}

interface BranchPath {
  id: string
  d: string
  width: number
  dash?: string
  color: string
  opacity: number
  /** tooltip lines for the hover layer */
  tipTitle: string
  tipMeta: string
}

interface BraidLayout {
  /** computed stage height — grows with lane depth, clamped */
  height: number
  branches: BranchPath[]
  paths: Array<{ d: string; stroke: string; width: number; dash?: string; opacity?: number }>
  dots: Array<{ cx: number; cy: number; r: number; fill: string }>
  crosses: Array<{ x: number; y: number }>
  squares: Array<{ x: number; y: number }>
  diamonds: Array<{ x: number; y: number }>
  ticks: Array<{ x: number; major: boolean }>
  mainY: number
  X0: number
  X1: number
  /** solid main-thread spans and compressed idle blocks, in x-space */
  mainSegs: Array<{ x0: number; x1: number; gap: boolean }>
  idles: Array<{ xa: number; xb: number; plateY: number; label: string; sub: string | null }>
  /** minutes between ruler ticks, for the footer caption */
  tickStepMin: number
  labels: BraidLabel[]
  /** branches whose labels didn't fit — discoverable via hover */
  unlabelled: number
}

interface Box { x0: number; y0: number; x1: number; y1: number }

function layoutBraid(
  anatomy: Anatomy,
  W: number,
): BraidLayout {
  const { spanMin, waves, userMarks, artifactMarks } = anatomy
  const X0 = 30
  const X1 = W - 30

  // ── piecewise time axis: idle gaps compress to fixed blocks ──────────
  const COMPRESS_MIN = 10
  const bigGaps = anatomy.gaps.filter((g) => g[1] - g[0] >= COMPRESS_MIN)
  const gapTotal = bigGaps.reduce((a, g) => a + (g[1] - g[0]), 0)
  const activeTotal = Math.max(0.5, spanMin - gapTotal)
  let gapPx = 84
  let activePx = X1 - X0 - bigGaps.length * gapPx
  if (activePx < 160 && bigGaps.length > 0) {
    gapPx = Math.max(28, (X1 - X0 - 160) / bigGaps.length)
    activePx = X1 - X0 - bigGaps.length * gapPx
  }
  const scale = activePx / activeTotal // px per ACTIVE minute

  interface Seg { m0: number; m1: number; x0: number; x1: number; gap: boolean }
  const segs: Seg[] = []
  let cm = 0
  let cx = X0
  for (const g of bigGaps) {
    if (g[0] > cm) {
      const x1 = cx + (g[0] - cm) * scale
      segs.push({ m0: cm, m1: g[0], x0: cx, x1, gap: false })
      cx = x1
    }
    segs.push({ m0: g[0], m1: Math.min(g[1], spanMin), x0: cx, x1: cx + gapPx, gap: true })
    cx += gapPx
    cm = g[1]
  }
  if (cm < spanMin) segs.push({ m0: cm, m1: spanMin, x0: cx, x1: X1, gap: false })
  if (segs.length === 0) segs.push({ m0: 0, m1: spanMin, x0: X0, x1: X1, gap: false })
  segs[segs.length - 1].x1 = X1 // absorb float drift

  const tx = (m: number) => {
    const mm = Math.max(0, Math.min(m, spanMin))
    for (const seg of segs) {
      if (mm <= seg.m1) {
        const f = (mm - seg.m0) / Math.max(1e-9, seg.m1 - seg.m0)
        return seg.x0 + Math.max(0, Math.min(1, f)) * (seg.x1 - seg.x0)
      }
    }
    return X1
  }

  // ── lane allocation, UNBOUNDED — height is derived from depth after ──
  // Occupancy is tracked in pixels (idle compression can put waves hours
  // apart only 84px apart on screen).
  const laneEnds: Record<string, number[]> = { '1': [], '-1': [] }
  const alloc = (side: 1 | -1, count: number, atPx: number) => {
    const ends = laneEnds[String(side)]
    let j = 0
    for (;;) {
      let free = true
      for (let k = 0; k < count; k++) {
        if ((ends[j + k] ?? -Infinity) > atPx) { free = false; break }
      }
      if (free) return j
      j++
    }
  }

  interface Placed {
    t: ThreadView
    side: 1 | -1
    lane: number
    xs: number
    xe: number
    waveX: number
  }
  const placed: Placed[] = []
  const maxLane: Record<string, number> = { '1': 0, '-1': 0 }

  waves.forEach((wave, i) => {
    const waveX = tx(wave.at)
    // Prefer alternating sides, but take whichever is shallower at this
    // point — a run of same-shape waves otherwise digs one side deep
    // while the other sits empty.
    const a = alloc(1, wave.threads.length, waveX)
    const b = alloc(-1, wave.threads.length, waveX)
    let side: 1 | -1
    if (Math.abs(a - b) > 1) side = a < b ? 1 : -1
    else side = i % 2 ? -1 : 1
    const j0 = side === 1 ? a : b

    const ends = laneEnds[String(side)]
    wave.threads.forEach((t, k) => {
      const xs = tx(t.start)
      const xe = Math.max(tx(t.start + t.dur), xs + 26)
      // Reserve through the bar plus one label width so the next wave on
      // this lane can't overprint the label.
      ends[j0 + k] = Math.max(ends[j0 + k] ?? -Infinity, xe + 160)
      placed.push({ t, side, lane: j0 + k, xs, xe, waveX })
      maxLane[String(side)] = Math.max(maxLane[String(side)], j0 + k)
    })
  })

  // ── vertical scale: pitch adapts, height grows (within bounds) ───────
  const TOP = 22
  const RULER = 34
  const BASE = 34
  const LANES = maxLane['1'] + maxLane['-1'] + 2
  const MAX_H = 620
  let pitch = 24
  const fixed = TOP + RULER + 2 * BASE + 36 // 36 = label headroom both sides
  if (fixed + LANES * pitch > MAX_H) {
    pitch = Math.max(15, Math.floor((MAX_H - fixed) / Math.max(1, LANES)))
  }
  const aboveExtent = BASE + maxLane['-1'] * pitch + 18
  const belowExtent = BASE + maxLane['1'] * pitch + 18
  let mainY = TOP + aboveExtent
  let height = mainY + belowExtent + RULER
  if (height < 300) {
    mainY += Math.round((300 - height) * 0.48)
    height = 300
  }
  const laneY = (side: 1 | -1, j: number) => mainY + side * (BASE + j * pitch)

  const out: BraidLayout = {
    height,
    branches: [], paths: [], dots: [], crosses: [], squares: [], diamonds: [],
    ticks: [], mainY, X0, X1,
    mainSegs: segs.map((seg) => ({ x0: seg.x0, x1: seg.x1, gap: seg.gap })),
    idles: [], tickStepMin: 30, labels: [], unlabelled: 0,
  }

  // ── label collision resolver ─────────────────────────────────────────
  // Everything that wants ink registers a box; boxes are accepted in
  // priority order and later candidates try fallback positions before
  // degrading to hover-only. Nothing ever overprints.
  const taken: Box[] = []
  const estWidth = (text: string) => text.length * 5.7 + 16
  const collides = (b: Box) =>
    b.y0 < 2 || b.y1 > height - RULER + 6 || b.x0 < 4 || b.x1 > W - 4 ||
    taken.some((o) => b.x0 < o.x1 && b.x1 > o.x0 && b.y0 < o.y1 && b.y1 > o.y0)
  const accept = (b: Box) => { taken.push(b) }

  // idle plates first — highest priority, staggered when adjacent
  for (const seg of segs) {
    if (!seg.gap) continue
    const gm = seg.m1 - seg.m0
    const waiting = userMarks.some((m) => m >= seg.m1 - 3 && m <= seg.m1 + 3)
    const label = `idle ${hm(gm).replace(/\s0s$/, '')}`
    const sub = waiting ? 'waiting on your reply' : null
    const w = Math.max(estWidth(label) + 12, sub ? estWidth(sub) : 0)
    const h = sub ? 34 : 22
    const cxm = (seg.x0 + seg.x1) / 2
    let plateY = mainY - (sub ? 56 : 44)
    // stagger upward until clear of earlier plates
    for (let guard = 0; guard < 6; guard++) {
      const box = { x0: cxm - w / 2 - 4, y0: plateY - 2, x1: cxm + w / 2 + 4, y1: plateY + h + 2 }
      if (!collides(box)) { accept(box); break }
      plateY -= h + 8
    }
    out.idles.push({ xa: seg.x0, xb: seg.x1, plateY, label, sub })
  }

  // threads, most important first: non-done states, then by tokens read
  const wTok = (tk: number) => Math.max(1.2, Math.min(3.4, 0.7 + Math.log10(1 + tk / 2e4)))
  const prio = (t: ThreadView) =>
    (t.state === 'running' || t.state === 'pending' ? 2e12 : t.state !== 'done' ? 1e12 : 0) +
    t.tokensIn
  const ordered = [...placed].sort((p1, p2) => prio(p2.t) - prio(p1.t))

  for (const pl of ordered) {
    const { t, side, lane, xs, xe } = pl
    const y = laneY(side, lane)
    const sgn = side
    const rr = Math.min(10, Math.abs(y - mainY) / 2, (xe - xs) / 2)
    const name = t.label.replace(/\s+—.*$/, '').toLowerCase()

    if (t.state === 'cancelled' || t.state === 'failed') {
      const cy = mainY + sgn * 16
      out.paths.push({ d: `M ${xs} ${mainY} L ${xs} ${cy}`, stroke: SAND, width: 1, opacity: 0.6 })
      out.crosses.push({ x: xs, y: cy + sgn * 5 })
      const text = `${name} · ${t.state}`
      const wpx = estWidth(text)
      const ly = cy + sgn * 5 - 7
      const cands: Array<{ x?: number; right?: number; box: Box }> = [
        { x: xs + 10, box: { x0: xs + 8, y0: ly, x1: xs + 8 + wpx, y1: ly + 15 } },
        { right: W - xs + 8, box: { x0: xs - 8 - wpx, y0: ly, x1: xs - 8, y1: ly + 15 } },
      ]
      const hit = cands.find((c) => !collides(c.box))
      if (hit) {
        accept(hit.box)
        out.labels.push({
          key: t.id, text: '', bold: name, threadId: t.id,
          suffix: { text: t.state, warn: true },
          ...(hit.x != null ? { x: hit.x } : { right: hit.right }), y: ly,
        })
      } else out.unlabelled++
      // cancelled stubs still need a hover surface
      out.branches.push({
        id: t.id, d: `M ${xs} ${mainY} L ${xs} ${cy + sgn * 8}`, width: 1,
        color: SAND, opacity: 0, dash: undefined,
        tipTitle: name, tipMeta: `${t.agentType || 'agent'} · ${t.state}`,
      })
      continue
    }

    const running = t.state === 'running' || t.state === 'pending'
    const branchD = running
      ? `M ${xs} ${mainY} L ${xs} ${y - sgn * rr} Q ${xs} ${y} ${xs + rr} ${y} L ${xe} ${y}`
      : `M ${xs} ${mainY} L ${xs} ${y - sgn * rr} Q ${xs} ${y} ${xs + rr} ${y}` +
        ` L ${xe - rr} ${y} Q ${xe} ${y} ${xe} ${y - sgn * rr} L ${xe} ${mainY}`

    out.branches.push({
      id: t.id, d: branchD, width: running ? 1.2 : wTok(t.tokensIn),
      color: ICE_2, opacity: running ? 0.8 : 0.85, dash: undefined,
      tipTitle: name,
      tipMeta: `${t.agentType || 'agent'} · ${hm(t.dur)} · ${tok(t.tokensIn)} in / ${tok(t.tokensOut)} out${running ? ` · ${t.state}` : ''}`,
    })
    if (running) {
      out.paths.push({
        d: `M ${xe} ${y} L ${Math.min(xe + 26, X1)} ${y}`,
        stroke: ICE_2, width: 1.2, dash: '2 4',
      })
    } else {
      out.dots.push({ cx: xe, cy: mainY, r: 1.8, fill: ICE_2 })
    }

    const text = running
      ? `${name} · ${t.state === 'pending' ? 'starting' : 'still running'}`
      : `${name} · ${hm(t.dur)} · ${tok(t.tokensIn)}`
    const wpx = estWidth(text)
    const besideY = y - 7
    const onBarY = sgn < 0 ? y - 16 : y + 4
    const cands: Array<{ x?: number; right?: number; y: number; box: Box }> = []
    if (!running && xe - xs >= 100) {
      cands.push({ x: xs + rr + 4, y: onBarY, box: { x0: xs + rr + 2, y0: onBarY, x1: xs + rr + 2 + wpx, y1: onBarY + 15 } })
    }
    if (running || xs > W - 240) {
      cands.push({ right: W - xs + 8, y: besideY, box: { x0: xs - 8 - wpx, y0: besideY, x1: xs - 8, y1: besideY + 15 } })
    }
    cands.push({ x: xe + 8, y: besideY, box: { x0: xe + 6, y0: besideY, x1: xe + 6 + wpx, y1: besideY + 15 } })
    cands.push({ right: W - xs + 8, y: besideY, box: { x0: xs - 8 - wpx, y0: besideY, x1: xs - 8, y1: besideY + 15 } })
    // last resort: the far side of the bar
    cands.push({ x: xs + rr + 4, y: sgn < 0 ? y + 4 : y - 16, box: { x0: xs + rr + 2, y0: sgn < 0 ? y + 4 : y - 16, x1: xs + rr + 2 + wpx, y1: (sgn < 0 ? y + 4 : y - 16) + 15 } })

    const hit = cands.find((c) => !collides(c.box))
    if (hit) {
      accept(hit.box)
      out.labels.push({
        key: t.id, threadId: t.id,
        text: running ? '' : `· ${hm(t.dur)} · ${tok(t.tokensIn)}`,
        bold: name,
        suffix: running ? { text: t.state === 'pending' ? 'starting' : 'still running' } : undefined,
        dim: running,
        ...(hit.x != null ? { x: hit.x } : { right: hit.right }), y: hit.y,
      })
    } else {
      out.unlabelled++
    }
  }

  // wave fork dots (drawn after so they sit above branch roots)
  for (const wave of waves) {
    out.dots.push({ cx: tx(wave.at), cy: mainY, r: 2.2, fill: ICE_0 })
  }

  for (const m of artifactMarks) out.squares.push({ x: tx(m), y: mainY })
  for (const m of userMarks) out.diamonds.push({ x: tx(m), y: mainY })

  // ruler ticks on active stretches only
  const NICE = [1, 2, 5, 10, 15, 30, 60, 120, 240, 480]
  const step = NICE.find((n) => n * scale >= 70) ?? 480
  out.tickStepMin = step
  for (const seg of segs) {
    if (seg.gap) continue
    for (let m = Math.ceil(seg.m0 / step) * step; m <= seg.m1; m += step) {
      out.ticks.push({ x: tx(m), major: m % (step * 2) === 0 })
    }
  }

  return out
}

// ── the braid card ──────────────────────────────────────────────────────

function SessionBraid({ anatomy, tip }: { anatomy: Anatomy; tip: TipBind }) {
  const stageRef = useRef<HTMLDivElement | null>(null)
  const [width, setWidth] = useState<number | null>(null)
  const [hovered, setHovered] = useState<string | null>(null)
  // comet easter egg: double-click the stage and a spark rides the main
  // thread end to end. cometX is null when idle.
  const [cometX, setCometX] = useState<number | null>(null)
  const cometRaf = useRef(0)

  useEffect(() => {
    const el = stageRef.current
    if (!el) return
    let pending = 0
    const measure = () => {
      const w = Math.max(1, Math.round(el.getBoundingClientRect().width))
      setWidth((prev) => (prev === w ? prev : w))
    }
    measure()
    const ro = new ResizeObserver(() => {
      window.clearTimeout(pending)
      pending = window.setTimeout(measure, 120)
    })
    ro.observe(el)
    return () => {
      ro.disconnect()
      window.clearTimeout(pending)
    }
  }, [])

  const layout = useMemo(
    () => (width ? layoutBraid(anatomy, width) : null),
    [anatomy, width],
  )

  useEffect(() => () => cancelAnimationFrame(cometRaf.current), [])
  const launchComet = () => {
    if (!layout) return
    cancelAnimationFrame(cometRaf.current)
    const t0 = performance.now()
    const DUR = 1600
    const run = (now: number) => {
      const f = (now - t0) / DUR
      if (f >= 1) { setCometX(null); return }
      // ease-in-out so it lingers mid-flight
      const e = f < 0.5 ? 2 * f * f : 1 - Math.pow(-2 * f + 2, 2) / 2
      setCometX(layout.X0 + e * (layout.X1 - layout.X0))
      cometRaf.current = requestAnimationFrame(run)
    }
    cometRaf.current = requestAnimationFrame(run)
  }

  const H = layout?.height ?? 300

  return (
    <div className="relative overflow-hidden rounded-lg border border-white/[0.06] bg-bg-1">
      <div
        ref={stageRef}
        className="relative"
        style={{ height: H }}
        onDoubleClick={launchComet}
        title=""
      >
        {/* corner registration marks */}
        {(['tl', 'tr', 'bl', 'br'] as const).map((c) => (
          <span
            key={c}
            className="pointer-events-none absolute h-4 w-4 border-accent/40"
            style={{
              top: c[0] === 't' ? 10 : undefined,
              bottom: c[0] === 'b' ? 10 : undefined,
              left: c[1] === 'l' ? 10 : undefined,
              right: c[1] === 'r' ? 10 : undefined,
              borderTopWidth: c[0] === 't' ? 1 : 0,
              borderBottomWidth: c[0] === 'b' ? 1 : 0,
              borderLeftWidth: c[1] === 'l' ? 1 : 0,
              borderRightWidth: c[1] === 'r' ? 1 : 0,
            }}
          />
        ))}

        {layout && width && (
          <svg
            className="absolute inset-0"
            width="100%"
            height="100%"
            viewBox={`0 0 ${width} ${H}`}
            preserveAspectRatio="none"
          >
            <g strokeLinejoin="round" strokeLinecap="round">
              {/* main thread first so branches sit above it */}
              {layout.mainSegs.map((seg, i) =>
                seg.gap ? (
                  <g key={i}>
                    <line
                      x1={seg.x0} y1={layout.mainY} x2={seg.x1} y2={layout.mainY}
                      stroke={SAND} strokeWidth={1} opacity={0.55}
                      strokeDasharray="2 6" strokeLinecap="round"
                    />
                    <line x1={seg.x0} y1={layout.mainY - 12} x2={seg.x0} y2={layout.mainY + 12}
                      stroke={SAND} strokeWidth={1} opacity={0.3} />
                    <line x1={seg.x1} y1={layout.mainY - 12} x2={seg.x1} y2={layout.mainY + 12}
                      stroke={SAND} strokeWidth={1} opacity={0.3} />
                  </g>
                ) : (
                  <g key={i}>
                    <line x1={seg.x0} y1={layout.mainY} x2={seg.x1} y2={layout.mainY}
                      stroke={ICE_0} strokeWidth={3.6} opacity={0.1} />
                    <line x1={seg.x0} y1={layout.mainY} x2={seg.x1} y2={layout.mainY}
                      stroke={ICE_0} strokeWidth={1.4} opacity={0.9} />
                  </g>
                ),
              )}

              {/* branches + generous invisible hit paths for hover */}
              {layout.branches.map((b) => (
                <g key={b.id}>
                  <path
                    d={b.d}
                    fill="none"
                    stroke={hovered === b.id ? ICE_0 : b.color}
                    strokeWidth={hovered === b.id ? b.width + 0.6 : b.width}
                    strokeDasharray={b.dash}
                    opacity={
                      b.opacity === 0
                        ? 0
                        : hovered
                          ? hovered === b.id ? 1 : 0.22
                          : b.opacity
                    }
                  />
                  {(() => {
                    const tp = tip(
                      <span>
                        <span className="text-fg-1">{b.tipTitle}</span>{' '}
                        <span className="text-fg-2">· {b.tipMeta}</span>
                      </span>,
                    )
                    return (
                      <path
                        d={b.d}
                        fill="none"
                        stroke="#fff"
                        strokeOpacity={0}
                        strokeWidth={11}
                        style={{ pointerEvents: 'stroke', cursor: 'crosshair' }}
                        onMouseMove={tp.onMouseMove}
                        onMouseEnter={() => setHovered(b.id)}
                        onMouseLeave={() => {
                          tp.onMouseLeave()
                          setHovered(null)
                        }}
                      />
                    )
                  })()}
                </g>
              ))}

              {layout.paths.map((p, i) => (
                <path
                  key={i}
                  d={p.d}
                  fill="none"
                  stroke={p.stroke}
                  strokeWidth={p.width}
                  strokeDasharray={p.dash}
                  opacity={hovered ? (p.opacity ?? 1) * 0.4 : p.opacity ?? 1}
                />
              ))}

              {layout.dots.map((d, i) => (
                <circle key={i} cx={d.cx} cy={d.cy} r={d.r} fill={d.fill} opacity={0.9} />
              ))}
              {layout.crosses.map((c, i) => (
                <path
                  key={i}
                  d={`M${c.x - 3} ${c.y - 3} l6 6 M${c.x + 3} ${c.y - 3} l-6 6`}
                  stroke={SAND}
                  strokeWidth={1.1}
                  opacity={0.85}
                />
              ))}
              {layout.squares.map((sq, i) => (
                <rect
                  key={i}
                  x={sq.x - 3} y={sq.y - 3} width={6} height={6}
                  fill={ICE_2} stroke="#0a0a0a" strokeWidth={1.4}
                />
              ))}
              {layout.diamonds.map((d, i) => (
                <g key={i} transform={`translate(${d.x},${d.y}) rotate(45)`}>
                  <rect x={-4} y={-4} width={8} height={8} fill={ICE_0} stroke="#0a0a0a" strokeWidth={1.4} />
                </g>
              ))}

              {/* time ruler — ticks on active stretches only */}
              <line
                x1={layout.X0} y1={H - 14} x2={layout.X1} y2={H - 14}
                stroke="rgba(168,212,252,.14)" strokeWidth={1}
              />
              {layout.ticks.map((t, i) => (
                <line
                  key={i}
                  x1={t.x} y1={H - 14} x2={t.x} y2={H - 14 - (t.major ? 7 : 4)}
                  stroke={`rgba(168,212,252,${t.major ? 0.28 : 0.14})`}
                  strokeWidth={1}
                />
              ))}

              {/* the comet */}
              {cometX != null && (
                <g>
                  {[0, 1, 2, 3].map((k) => (
                    <circle
                      key={k}
                      cx={cometX - k * 9}
                      cy={layout.mainY}
                      r={3.4 - k * 0.7}
                      fill={ICE_0}
                      opacity={0.9 - k * 0.22}
                    />
                  ))}
                </g>
              )}
            </g>
          </svg>
        )}

        {/* branch labels — plated; only non-colliding survivors render */}
        {layout?.labels.map((l) => (
          <div
            key={l.key}
            className="pointer-events-none absolute whitespace-nowrap rounded-sm px-1.5 py-px font-mono text-[9px] tracking-[0.02em] transition-opacity"
            style={{
              left: l.x,
              right: l.right,
              top: l.y,
              textAlign: l.right != null ? 'right' : undefined,
              background: 'rgba(5,5,6,.85)',
              color: l.dim ? '#4a4a4a' : '#6e6e6e',
              opacity: hovered && l.threadId !== hovered ? 0.25 : 1,
            }}
          >
            <span className={hovered && l.threadId === hovered ? 'text-accent' : 'text-fg-1'}>
              {l.bold}
            </span>
            {l.text && <span> {l.text}</span>}
            {l.suffix && (
              <span style={{ color: l.suffix.warn ? SAND : undefined }}> · {l.suffix.text}</span>
            )}
          </div>
        ))}

        {/* one compact plate per compressed idle block — pre-staggered */}
        {layout?.idles.map((idle, i) => (
          <div
            key={i}
            className="pointer-events-none absolute -translate-x-1/2 whitespace-nowrap rounded-sm border px-2.5 py-1 text-center transition-opacity"
            style={{
              left: (idle.xa + idle.xb) / 2,
              top: idle.plateY,
              background: 'rgba(5,5,6,.84)',
              borderColor: 'rgba(184,160,120,.22)',
              opacity: hovered ? 0.3 : 1,
            }}
          >
            <div className="font-mono text-[9.5px] uppercase tracking-[0.18em]" style={{ color: SAND }}>
              {idle.label}
            </div>
            {idle.sub && (
              <div className="mt-px font-mono text-[9px] text-fg-3">{idle.sub}</div>
            )}
          </div>
        ))}
      </div>

      {/* furniture lives OUTSIDE the plot so labels never fight it */}
      <div className="flex items-center justify-between gap-4 border-t border-white/[0.04] px-4 py-2">
        <span className="label">
          ticks every{' '}
          {layout
            ? layout.tickStepMin % 60 === 0
              ? `${layout.tickStepMin / 60}h`
              : `${layout.tickStepMin}m`
            : '—'}
          {layout && layout.idles.length > 0 ? ' · idle compressed to blocks' : ''}
          {layout && layout.unlabelled > 0
            ? ` · ${layout.unlabelled} unlabelled — hover the branches`
            : ''}
        </span>
        <div className="flex items-center gap-4 whitespace-nowrap font-mono text-[9px] text-fg-3">
          <span className="flex items-center gap-1.5">
            <i className="h-[2px] w-3.5 rounded-full" style={{ background: ICE_2 }} />
            sub-agent · width = tokens read
          </span>
          <span className="flex items-center gap-1.5">
            <i className="h-1.5 w-1.5" style={{ background: ICE_2 }} />
            artifact
          </span>
          <span className="flex items-center gap-1.5">
            <i className="h-1.5 w-1.5 rotate-45" style={{ background: ICE_0 }} />
            your message
          </span>
          <span className="flex items-center gap-1.5">
            <i
              className="h-px w-3.5"
              style={{
                background: `repeating-linear-gradient(90deg, ${SAND} 0 3px, transparent 3px 6px)`,
              }}
            />
            idle
          </span>
        </div>
      </div>
    </div>
  )
}

// ── cursor tooltip ──────────────────────────────────────────────────────
// House readout instead of the native title tooltip: follows the cursor,
// plated in the panel style. Returns bind() to spread onto hover targets.

interface TipState { x: number; y: number; node: React.ReactNode }

function useCursorTip() {
  const [tip, setTip] = useState<TipState | null>(null)
  const bind = (node: React.ReactNode) => ({
    onMouseMove: (e: React.MouseEvent) => setTip({ x: e.clientX, y: e.clientY, node }),
    onMouseLeave: () => setTip(null),
  })
  const el = tip ? (
    <div
      className="pointer-events-none fixed z-[70] whitespace-nowrap rounded border border-white/[0.08] px-2.5 py-1.5 font-mono text-[10.5px] text-fg-0 shadow-2xl"
      style={{
        left: Math.min(tip.x + 14, window.innerWidth - 300),
        top: Math.min(tip.y + 16, window.innerHeight - 60),
        background: '#101014',
      }}
    >
      {tip.node}
    </div>
  ) : null
  return { bind, el }
}

type TipBind = ReturnType<typeof useCursorTip>['bind']

// ── tiles ───────────────────────────────────────────────────────────────

function Tile({
  k,
  v,
  n,
  sand,
}: {
  k: string
  v: string
  n: string
  sand?: boolean
}) {
  return (
    <div className="bg-bg-1 px-4 pb-3 pt-3.5">
      <span className="label block">{k}</span>
      <div className={`mt-1.5 text-[19px] font-light ${sand ? '' : 'text-fg-0'}`} style={sand ? { color: SAND } : undefined}>
        <ScrambleText value={v} pace={2.4} />
      </div>
      <div className="mt-0.5 font-mono text-[10px] text-fg-2">{n}</div>
    </div>
  )
}

// ── funnel ──────────────────────────────────────────────────────────────

function Funnel({
  rows,
  tip,
}: {
  rows: Array<{ k: string; v: number; c: string }>
  tip: TipBind
}) {
  const total = rows.reduce((a, r) => a + r.v, 0)
  if (total <= 0) return null
  return (
    <>
      <div className="flex h-7 gap-0.5 overflow-hidden rounded">
        {rows.map((r) => (
          <div
            key={r.k}
            {...tip(
              <span>
                <span className="text-fg-1">{r.k}</span> · {r.v.toLocaleString()} tok ·{' '}
                {((r.v / total) * 100).toFixed(1)}%
              </span>,
            )}
            style={{ background: r.c, width: `${Math.max((r.v / total) * 100, 0.4)}%` }}
          />
        ))}
      </div>
      <div className="mt-2.5 flex flex-wrap gap-x-4 gap-y-1 font-mono text-[10.5px] text-fg-1">
        {rows.map((r) => (
          <span key={r.k}>
            <i className="mr-1.5 inline-block h-2 w-2 rounded-sm align-[-1px]" style={{ background: r.c }} />
            {r.k}{' '}
            <span className="text-fg-3">
              {formatTokens(r.v)} · {((r.v / total) * 100).toFixed(1)}%
            </span>
          </span>
        ))}
      </div>
    </>
  )
}

// ── ranked bars ─────────────────────────────────────────────────────────

function RankedBars({
  rows,
  tip,
  onRow,
}: {
  rows: Array<{ id: string; name: string; v: number; vLabel: string; note: string; bad?: boolean }>
  tip: TipBind
  onRow?: (id: string) => void
}) {
  const max = Math.max(...rows.map((r) => r.v), 1)
  return (
    <div>
      {rows.map((r) => (
        <div
          key={r.id}
          {...tip(
            <span>
              <span className="text-fg-1">{r.name}</span> · {r.vLabel} ·{' '}
              <span className="text-fg-2">{r.note}</span>
              {onRow && <span className="text-fg-3"> · click to open</span>}
            </span>,
          )}
          onClick={onRow ? () => onRow(r.id) : undefined}
          className={`grid grid-cols-[120px_1fr_60px_100px] items-center gap-3 border-t border-white/[0.03] py-1.5 text-[11px] first:border-t-0 ${
            onRow ? 'cursor-pointer rounded-sm hover:bg-white/[0.03]' : ''
          }`}
        >
          <div className="truncate font-mono text-fg-1">{r.name}</div>
          <div
            className="relative h-2.5 border-b border-white/[0.03]"
            style={{
              background:
                'repeating-linear-gradient(90deg, rgba(255,255,255,.026) 0 1px, transparent 1px 7px)',
            }}
          >
            <div
              className="absolute left-0 top-0 h-full min-w-[2px] rounded-r-sm"
              style={{
                width: `${Math.max((r.v / max) * 100, 0.7)}%`,
                background: '#3f6da8',
                boxShadow: `inset 0 0 0 1px ${ICE_2}`,
              }}
            />
          </div>
          <div className="text-right font-mono tabular-nums text-fg-0">{r.vLabel}</div>
          <div
            className="whitespace-nowrap font-mono text-[9.5px]"
            style={{ color: r.bad ? SAND : '#4a4a4a' }}
          >
            {r.note}
          </div>
        </div>
      ))}
    </div>
  )
}

// ── card chrome ─────────────────────────────────────────────────────────

function Card({
  title,
  hint,
  children,
}: {
  title: string
  hint?: string
  children: React.ReactNode
}) {
  return (
    <div className="rounded-lg border border-white/[0.06] bg-bg-1 p-5">
      <div className="mb-3.5 flex items-baseline justify-between">
        <span className="label text-fg-2">{title}</span>
        {hint && <span className="label text-fg-3/60">{hint}</span>}
      </div>
      {children}
    </div>
  )
}

// ── activity tab wrapper ────────────────────────────────────────────────

/** The dashboard's activity tab: the receipt is the face, the classic
 *  event log stays one click away. Mode persists across sessions. */
export function SessionActivityTab({ renderLog }: { renderLog: () => React.ReactNode }) {
  const [mode, setMode] = useState<'receipt' | 'log'>(() => {
    try {
      return localStorage.getItem('freyja.activity.viewMode') === 'log' ? 'log' : 'receipt'
    } catch {
      return 'receipt'
    }
  })
  const pick = (next: 'receipt' | 'log') => {
    setMode(next)
    try {
      localStorage.setItem('freyja.activity.viewMode', next)
    } catch {
      /* ignore */
    }
  }
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="flex shrink-0 items-center gap-1.5 border-b border-white/[0.06] px-8 py-2">
        {(['receipt', 'log'] as const).map((m) => (
          <button
            key={m}
            onClick={() => pick(m)}
            className={`rounded px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.14em] transition ${
              mode === m
                ? 'bg-accent/15 text-accent ring-1 ring-accent/30'
                : 'text-fg-3 hover:bg-white/[0.05] hover:text-fg-1'
            }`}
          >
            {m === 'receipt' ? 'receipt' : 'event log'}
          </button>
        ))}
      </div>
      {mode === 'receipt' ? <SessionReceiptView /> : renderLog()}
    </div>
  )
}

// ── main view ───────────────────────────────────────────────────────────

export function SessionReceiptView() {
  const messages = useHarness((s) => s.messages)
  const toolCalls = useHarness((s) => s.toolCalls)
  const subagents = useHarness((s) => s.subagents)
  const usage = useHarness((s) => s.usage)
  const artifacts = useHarness((s) => s.artifacts)
  const fileChanges = useHarness((s) => s.fileChanges)
  const isStreaming = useHarness((s) => s.isStreaming)
  const activeSessionId = useHarness((s) => s.activeSessionId)
  // Identity changes on any session/archive update — that's what keeps
  // the cross-session cost roll-up fresh without a custom subscription.
  const sessions = useHarness((s) => s.sessions)
  const sessionArchive = useHarness((s) => s.sessionArchive)

  const tip = useCursorTip()

  // Coarse clock so a live session's braid keeps growing while the tab is
  // open. 30s resolution is plenty at minutes-per-pixel scale.
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 30_000)
    return () => window.clearInterval(t)
  }, [])

  const anatomy = useMemo(
    () => deriveAnatomy(messages, toolCalls, subagents, artifacts, isStreaming, now),
    [messages, toolCalls, subagents, artifacts, isStreaming, now],
  )

  // ── spend across the session tree ────────────────────────────────
  // The slice's totalCost is THIS session alone; sub-agents spawned as
  // child sessions bill to their own slices. The hero shows the tree
  // total, with the split inline (≤3 children) or as a card (more).
  const spend = useMemo(() => {
    const rows = sessionCostBreakdown(useHarness.getState(), activeSessionId)
    const children = rows.filter((r) => !r.isRoot && r.cost > 0).sort((a, b) => b.cost - a.cost)
    const own = rows.find((r) => r.isRoot)?.cost ?? 0
    // Descendants reporting $0 with no loaded slice are usually sessions
    // persisted before totalCost round-tripped — their real spend is
    // sitting in the slice file. Surface them so the effect below can
    // hydrate their archives once.
    const unresolved = rows
      .filter((r) => !r.isRoot && r.cost === 0)
      .map((r) => r.id)
      .filter((id) => !useHarness.getState().sessionArchive[id])
    return { own, children, unresolved, total: own + children.reduce((a, r) => a + r.cost, 0) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSessionId, sessions, sessionArchive, usage.totalCost])

  // One-shot archive hydration for cost-less descendants. Each id is
  // requested at most once per mount; the store update re-runs `spend`.
  const requestedRef = useRef<Set<string>>(new Set())
  useEffect(() => {
    const load = useHarness.getState().loadPersistedSessionIntoArchive
    for (const id of spend.unresolved) {
      if (requestedRef.current.has(id)) continue
      requestedRef.current.add(id)
      void load(id)
    }
  }, [spend.unresolved])

  const readTotal = usage.totalInputTokens + usage.totalCacheReadTokens
  const idleMin = anatomy ? anatomy.gaps.reduce((a, g) => a + (g[1] - g[0]), 0) : 0
  // busy[] spans N+1 minute buckets (0..N inclusive), so clamp to the
  // span — a 1-minute session with both buckets touched is 100%, not 200%.
  const spanMinutes = anatomy ? Math.max(1, Math.ceil(anatomy.spanMin)) : 1
  const activeShown = anatomy ? Math.min(anatomy.activeMin, spanMinutes) : 0
  const livePct = anatomy ? Math.min(100, Math.round((100 * activeShown) / spanMinutes)) : 0

  const parentArtifacts = useMemo(
    () => artifacts.filter((a) => a.creator === 'parent'),
    [artifacts],
  )
  const subReports = artifacts.length - parentArtifacts.length
  const changeTotals = useMemo(
    () =>
      fileChanges.reduce(
        (acc, c) => ({
          additions: acc.additions + c.totals.additions,
          deletions: acc.deletions + c.totals.deletions,
        }),
        { additions: 0, deletions: 0 },
      ),
    [fileChanges],
  )

  const swarmRows = useMemo(
    () =>
      (anatomy?.threads ?? [])
        .filter((t) => t.tokensIn > 0)
        .sort((a, b) => b.tokensIn - a.tokensIn)
        .slice(0, 12)
        .map((t) => ({
          id: t.id,
          name: t.label.replace(/\s+—.*$/, '').toLowerCase(),
          v: t.tokensIn,
          vLabel: tok(t.tokensIn),
          note: `${hm(t.dur)} · ${t.agentType || t.state}`,
        })),
    [anatomy],
  )
  const swarmTokIn = (anatomy?.threads ?? []).reduce((a, t) => a + t.tokensIn, 0)
  const swarmTokOut = (anatomy?.threads ?? []).reduce((a, t) => a + t.tokensOut, 0)
  const swarmMin = (anatomy?.threads ?? []).reduce((a, t) => a + t.dur, 0)

  const toolRows = useMemo(() => {
    const agg = anatomy?.toolAgg ?? []
    const interesting = agg.filter((t) => t.ms > 500 || t.err > 0)
    // A quick session where every call was fast still deserves rows.
    return (interesting.length > 0 ? interesting : agg)
        .slice(0, 10)
        .map((t) => {
          const rate = t.err / t.n
          return {
            id: t.name,
            name: t.name,
            v: t.ms,
            vLabel: formatDuration(t.ms),
            note: t.err ? `${rate >= 0.25 ? '▲' : '△'} ${t.err}/${t.n} failed` : `${t.n} calls`,
            bad: rate >= 0.25,
          }
        })
  }, [anatomy])

  // Insight sentences, only when the data supports them.
  const slow = anatomy?.toolAgg[0]
  const slowConcentrated = slow && slow.ms > 5 * MIN && slow.maxMs / slow.ms > 0.6
  const worst = anatomy?.toolAgg
    .filter((t) => t.n >= 5 && t.err > 0)
    .sort((a, b) => b.err / b.n - a.err / a.n)[0]

  if (!anatomy) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center">
        <div className="text-center">
          <div className="label mb-2">session receipt</div>
          <div className="font-mono text-[12px] text-fg-2">
            Nothing recorded yet — the receipt draws itself as the session runs.
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-8 pb-16 pt-6">
      <div className="mx-auto max-w-[1320px]">
        {/* ── hero row ─────────────────────────────────────────────── */}
        <div className="mb-4 flex flex-wrap items-end justify-between gap-6">
          <div>
            <div className="flex items-baseline gap-4">
              <div className="text-[44px] font-light leading-none tracking-[-0.02em] text-fg-0">
                <span className="mr-0.5 align-[0.28em] text-[22px] text-fg-2">$</span>
                <ScrambleText value={spend.total > 0 ? spend.total.toFixed(2) : '0.00'} pace={2.4} />
              </div>
              <div className="max-w-[46ch] font-mono text-[10.5px] leading-relaxed text-fg-2">
                {spend.children.length > 0 ? (
                  <>billed across this session and its {spend.children.length} sub-agent
                    session{spend.children.length === 1 ? '' : 's'}. </>
                ) : (
                  <>billed to this session. </>
                )}
                The line below is the main thread over{' '}
                <span className="text-fg-1">{hm(anatomy.spanMin)}</span> — every branch is a
                sub-agent forked off and merged back
                {anatomy.biggest ? '; idle stretches are compressed and labelled' : ''}.
              </div>
            </div>
            {/* spend split: inline for a few children, a card below for many */}
            {spend.children.length > 0 && spend.children.length <= 3 && (
              <div className="mt-2 font-mono text-[10px] text-fg-3">
                this session <span className="text-fg-1">{formatCost(spend.own)}</span>
                {spend.children.map((c) => (
                  <span key={c.id}>
                    {' '}· {c.title.replace(/\s+—.*$/, '').slice(0, 28).toLowerCase()}{' '}
                    <span className="text-fg-1">{formatCost(c.cost)}</span>
                  </span>
                ))}
              </div>
            )}
            {spend.children.length > 3 && (
              <div className="mt-2 font-mono text-[10px] text-fg-3">
                this session <span className="text-fg-1">{formatCost(spend.own)}</span> ·{' '}
                {spend.children.length} sub-agent sessions{' '}
                <span className="text-fg-1">
                  {formatCost(spend.total - spend.own)}
                </span>{' '}
                — split below
              </div>
            )}
          </div>
          <div className="text-right font-mono text-[9.5px] leading-[1.8] text-fg-3">
            session <span className="text-accent/80">{activeSessionId.slice(-13)}</span>
            <br />
            <span className="text-fg-1">{anatomy.threads.length}</span> sub-agents ·{' '}
            <span className="text-fg-1">{anatomy.waves.length}</span> spawn waves
            <br />
            <span className="text-fg-1">{anatomy.toolCallCount}</span> tool calls ·{' '}
            {anatomy.toolErrCount} failed
          </div>
        </div>

        {/* ── the braid ─────────────────────────────────────────────── */}
        <SessionBraid anatomy={anatomy} tip={tip.bind} />

        {/* ── tiles ─────────────────────────────────────────────────── */}
        <div
          className="mt-3 grid gap-px overflow-hidden rounded-lg border border-white/[0.06] bg-white/[0.03]"
          style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))' }}
        >
          <Tile k="wall clock" v={hm(anatomy.spanMin)} n={`${messages.length} messages`} />
          <Tile
            k="live"
            v={`${livePct}%`}
            n={`${activeShown} of ${spanMinutes} minutes`}
          />
          <Tile
            k="silent"
            v={idleMin > 0 ? hm(idleMin) : '0m'}
            n="no calls, no agents"
            sand={idleMin >= 30}
          />
          <Tile k="tokens read" v={tok(readTotal)} n="fresh input + cache reads" />
          <Tile
            k="tokens written"
            v={tok(usage.totalOutputTokens)}
            n="everything it said or did"
          />
          <Tile
            k="swarm reading"
            v={tok(swarmTokIn)}
            n={`across ${anatomy.threads.length} sub-agents`}
          />
        </div>

        {/* ── cards ─────────────────────────────────────────────────── */}
        <div className="mt-3 grid grid-cols-1 gap-3 xl:grid-cols-2">
          {spend.children.length > 3 && (
            <Card title="spend by session" hint={`${spend.children.length + 1} sessions`}>
              <RankedBars
                tip={tip.bind}
                onRow={(id) => {
                  const st = useHarness.getState()
                  if (id === activeSessionId) return
                  void st.openSessionPane(id, 'replace').then(() => st.toggleMissionDashboard(false))
                }}
                rows={[
                  {
                    id: activeSessionId,
                    name: 'this session',
                    v: spend.own,
                    vLabel: formatCost(spend.own),
                    note: `${Math.round((100 * spend.own) / Math.max(spend.total, 1e-9))}% of total`,
                  },
                  ...spend.children.slice(0, 11).map((c) => ({
                    id: c.id,
                    name: c.title.replace(/\s+—.*$/, '').toLowerCase(),
                    v: c.cost,
                    vLabel: formatCost(c.cost),
                    note: `${Math.round((100 * c.cost) / Math.max(spend.total, 1e-9))}% of total`,
                  })),
                ].sort((a, b) => b.v - a.v)}
              />
              {spend.children.length > 11 && (
                <p className="mt-2 font-mono text-[10px] text-fg-3">
                  +{spend.children.length - 11} more sessions in the sidebar tree
                </p>
              )}
            </Card>
          )}
          <Card title="read amplification" hint="funnel">
            <Funnel
              tip={tip.bind}
              rows={[
                { k: 'fresh input', v: usage.totalInputTokens, c: ICE_4 },
                { k: 'cache read', v: usage.totalCacheReadTokens, c: ICE_2 },
                { k: 'output', v: usage.totalOutputTokens, c: ICE_0 },
              ]}
            />
            {usage.totalOutputTokens > 0 && (
              <p className="mt-3.5 border-t border-white/[0.03] pt-3 font-mono text-[10.5px] leading-relaxed text-fg-2">
                <span className="text-fg-1">
                  {Math.round(readTotal / usage.totalOutputTokens)} tokens read for every 1
                  written.
                </span>{' '}
                {readTotal > 0 &&
                  (usage.totalCacheReadTokens / readTotal >= 0.995 ? (
                    <>Essentially every read hit cache.</>
                  ) : (
                    <>
                      {Math.round((usage.totalCacheReadTokens / readTotal) * 100)}% of the reads
                      hit cache — the rest billed at full input rate.
                    </>
                  ))}
              </p>
            )}
          </Card>

          <Card title="what came out" hint="on disk">
            <div className="font-mono text-[11px]">
              {parentArtifacts.slice(-6).map((a) => (
                <div
                  key={a.id}
                  className="flex justify-between gap-3 border-b border-white/[0.03] py-1.5"
                >
                  <span className="truncate text-fg-1">{a.filename}</span>
                  <span className="whitespace-nowrap tabular-nums text-fg-2">
                    {a.operation} +{a.additions ?? 0}/−{a.deletions ?? 0}
                  </span>
                </div>
              ))}
              {subReports > 0 && (
                <div className="flex justify-between gap-3 border-b border-white/[0.03] py-1.5">
                  <span className="text-fg-1">sub-agent reports</span>
                  <span className="tabular-nums text-fg-2">{subReports} files</span>
                </div>
              )}
              <div className="flex justify-between gap-3 border-b border-white/[0.03] py-1.5">
                <span className="text-fg-1">file change sets</span>
                <span className="tabular-nums text-fg-2">
                  {fileChanges.length} +{changeTotals.additions}/−{changeTotals.deletions}
                </span>
              </div>
              {usage.totalCost > 0 && changeTotals.additions > 0 && (
                <div className="mt-2.5 flex justify-between gap-3 pt-1 text-[11.5px]">
                  <span className="text-fg-1">cost per line written</span>
                  <span className="tabular-nums text-accent">
                    {formatCost(usage.totalCost / changeTotals.additions)} ×{' '}
                    {changeTotals.additions} lines
                  </span>
                </div>
              )}
            </div>
          </Card>

          <Card title="where the wall clock went" hint="per tool">
            {toolRows.length > 0 ? (
              <>
                <RankedBars rows={toolRows} tip={tip.bind} />
                {(slowConcentrated || worst) && (
                  <p className="mt-3.5 border-t border-white/[0.03] pt-3 font-mono text-[10.5px] leading-relaxed text-fg-2">
                    {slowConcentrated && slow && (
                      <>
                        <span className="text-fg-1">
                          {slow.name} is the sink — and mostly one call.
                        </span>{' '}
                        Its longest single invocation ran {(slow.maxMs / MIN).toFixed(1)}m,{' '}
                        {Math.round((slow.maxMs / slow.ms) * 100)}% of the tool's whole wall
                        clock.{' '}
                      </>
                    )}
                    {worst && (
                      <span style={{ color: SAND }}>
                        {worst.name} failed {Math.round((100 * worst.err) / worst.n)}% of the
                        time ({worst.err} of {worst.n}).
                      </span>
                    )}
                  </p>
                )}
              </>
            ) : (
              <div className="font-mono text-[11px] text-fg-3">no tool calls yet</div>
            )}
          </Card>

          <Card title="what the swarm read" hint={`${swarmRows.length} agents`}>
            {swarmRows.length > 0 ? (
              <>
                <RankedBars rows={swarmRows} tip={tip.bind} />
                <p className="mt-3.5 border-t border-white/[0.03] pt-3 font-mono text-[10.5px] leading-relaxed text-fg-2">
                  The swarm read <span className="text-fg-1">{tok(swarmTokIn)}</span> to
                  return <span className="text-fg-1">{tok(swarmTokOut)}</span>, over{' '}
                  {hm(swarmMin)} of summed agent time inside a {hm(anatomy.spanMin)} session.
                </p>
              </>
            ) : (
              <div className="font-mono text-[11px] text-fg-3">no sub-agents spawned</div>
            )}
          </Card>
        </div>
      </div>
      {tip.el}
    </div>
  )
}
