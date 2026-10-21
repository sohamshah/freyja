/**
 * The seam: one drawn line that keeps the shape of a stretch of work.
 *
 * When the agent starts its next paragraph, the steps between the two
 * paragraphs fold into a seam, written left to right in the order things
 * happened:
 *
 *   · thinking lifts the thread a little, in blue;
 *   · a read, a search or a command ripples it up, and an edit ripples it
 *     down (where the pen writes); a slow step makes a bigger ripple;
 *   · calls that ran together open into a spindle, one strand each;
 *   · helpers (sub-agents) make a spindle of rippling strands;
 *   · a step that failed snaps the thread: two frayed red ends, a little
 *     out of line; a failed call in a group snaps its strand;
 *   · a helper still working in the background keeps a blue strand until
 *     it reports back, then its strand is drawn in.
 *
 * This module is pure geometry: units in, SVG path data out. Seam.tsx
 * draws it, WorkStretch.tsx folds the lines into it.
 */

export type StrandState = 'ok' | 'err' | 'out' | 'back'

export type SeamUnit =
  | { k: 'think'; secs: number; label?: string }
  | { k: 'up' | 'down'; ms: number; label?: string }
  | { k: 'fail'; label?: string }
  | { k: 'calls' | 'helpers'; n: number; strands?: StrandState[]; label?: string }

export interface SeamPath {
  d: string
  /** Space-separated classes from: ln, think, err, str, helpers, out, back, brk, fray. */
  cls: string
  /** The stroke a pen would follow through this piece. */
  main?: boolean
  /** Inside its piece, this stroke draws over this span of the piece's
   *  progress, so a break is drawn end, gap, end, in order. */
  range?: [number, number]
  /** Thinner strands when a spindle is crowded. */
  width?: number
  key?: string
}

export interface SeamPiece {
  x0: number
  x1: number
  /** Index of the unit this piece draws, or -1 for a joining stretch. */
  unit: number
  paths: SeamPath[]
}

export interface SeamUnitGeom {
  x0: number
  x1: number
  cx: number
  /** The y of each strand at its widest, for marks that land on a spindle. */
  strandY: number[]
  label: string
}

export interface SeamGeom {
  width: number
  height: number
  pieces: SeamPiece[]
  units: SeamUnitGeom[]
}

/** Baseline and box height. Ripples rise to about 6px above the line and
 *  spindles spread about 8px either side. */
export const SEAM_Y = 12
export const SEAM_H = 26
const K = 1.15
const GAP = 4
const LEAD = 3
const TAIL = 13
const STRAND_CAP = 12

const r2 = (n: number): number => Math.round(n * 100) / 100

export function unitWidth(u: SeamUnit): number {
  switch (u.k) {
    case 'think':
      return K * (8 + Math.min(24, u.secs * 2.4))
    case 'up':
    case 'down':
      return K * (9 + Math.min(4, Math.log10(1 + Math.max(1, u.ms) / 250) * 2.2))
    case 'fail':
      return K * 12
    case 'calls':
      return K * Math.min(46, 14 + Math.min(u.n, STRAND_CAP) * 2.2)
    case 'helpers':
      return K * Math.min(62, 26 + Math.min(u.n, STRAND_CAP) * 3)
  }
}

/** A point on strand `i` (offset `o` from the line at its widest) at t. */
function strandPoint(kind: 'calls' | 'helpers', x: number, w: number, o: number, i: number, t: number): [number, number] {
  const mt = 1 - t
  const bx = x + w * (3 * mt * mt * t * 0.32 + 3 * mt * t * t * 0.68 + t * t * t)
  const by = SEAM_Y + (3 * mt * mt * t + 3 * mt * t * t) * o * 1.33
  // A helper did its own work, so its strand ripples a little.
  const wob = kind === 'helpers' ? Math.sin(t * Math.PI * 5 + i * 1.7) * 0.5 * Math.sin(Math.PI * t) : 0
  return [bx, by + wob]
}

function strandPath(kind: 'calls' | 'helpers', x: number, w: number, o: number, i: number, t0: number, t1: number): string {
  if (kind === 'calls' && t0 === 0 && t1 === 1) {
    return `M${r2(x)} ${SEAM_Y}C${r2(x + 0.32 * w)} ${r2(SEAM_Y + o * 1.33)} ${r2(x + 0.68 * w)} ${r2(SEAM_Y + o * 1.33)} ${r2(x + w)} ${SEAM_Y}`
  }
  const n = Math.max(8, Math.round((w * (t1 - t0)) / 1.1))
  let d = ''
  for (let j = 0; j <= n; j++) {
    const p = strandPoint(kind, x, w, o, i, t0 + ((t1 - t0) * j) / n)
    d += `${j ? 'L' : 'M'}${r2(p[0])} ${r2(p[1])}`
  }
  return d
}

/** A frayed end: three short, uneven fibres. The far end of a break uses a
 *  different set, so the two ends never mirror into a shape. */
function fray(ex: number, ey: number, th: number, far: boolean): string {
  const set: Array<[number, number]> = far
    ? [[0.9, 1.1], [0.15, 1.5], [-0.6, 0.95]]
    : [[-0.95, 1.2], [-0.2, 1.6], [0.55, 1]]
  return set
    .map(([da, len]) => `M${r2(ex)} ${r2(ey)}L${r2(ex + Math.cos(th + da) * len)} ${r2(ey + Math.sin(th + da) * len)}`)
    .join('')
}

function unitPaths(u: SeamUnit, x: number, w: number): { paths: SeamPath[]; strandY?: number[] } {
  const X = (v: number) => r2(x + v * w)
  const Y = SEAM_Y
  if (u.k === 'think') {
    // A long, low rise in blue.
    const h = K * (1.5 + Math.min(1.2, u.secs / 7))
    let d = `M${X(0)} ${Y}`
    for (let j = 1; j <= 24; j++) {
      const q = j / 24
      d += `L${X(q)} ${r2(Y - (h * (1 - Math.cos(2 * Math.PI * q))) / 2)}`
    }
    return { paths: [{ d, cls: 'think', main: true }] }
  }
  if (u.k === 'up' || u.k === 'down') {
    // One swing and a smaller echo; the start eases off the line.
    const a = (K * (2.6 + Math.min(3.6, Math.log10(1 + Math.max(1, u.ms) / 200) * 1.7))) / 0.66
    const sign = u.k === 'up' ? -1 : 1
    let d = `M${X(0)} ${Y}`
    for (let j = 1; j <= 30; j++) {
      const q = j / 30
      d += `L${X(q)} ${r2(Y + sign * a * Math.sin(1.6 * Math.PI * q) * Math.pow(1 - q, 1.2) * (1 - Math.exp(-q * 13)))}`
    }
    return { paths: [{ d, cls: 'ln', main: true }] }
  }
  if (u.k === 'fail') {
    // The thread snaps: two frayed red ends, a little out of line, and the
    // line carries on after.
    const g = 3.8
    const xa = x + w / 2 - g / 2
    const xb = x + w / 2 + g / 2
    const red = 2.8
    const ya = Y + 1.1
    const yb = Y - 0.9
    return {
      paths: [
        { d: `M${X(0)} ${Y}H${r2(xa - red)}`, cls: 'ln', main: true, range: [0, 0.36] },
        { d: `M${r2(xa - red)} ${Y}Q${r2(xa - 0.8)} ${Y} ${r2(xa)} ${r2(ya)}${fray(xa, ya, 0.5, false)}`, cls: 'err brk', main: true, range: [0.36, 0.5] },
        { d: `${fray(xb, yb, Math.PI + 0.45, true)}M${r2(xb)} ${r2(yb)}Q${r2(xb + 0.8)} ${Y} ${r2(xb + red)} ${Y}`, cls: 'err brk', range: [0.52, 0.64] },
        { d: `M${r2(xb + red)} ${Y}H${X(1)}`, cls: 'ln', main: true, range: [0.64, 1] },
      ],
    }
  }
  const spindle = u as Extract<SeamUnit, { k: 'calls' | 'helpers' }>
  const kind = spindle.k
  const m = Math.min(spindle.n, STRAND_CAP)
  const hs = Math.min(kind === 'helpers' ? 7.8 : 7.2, 1.7 + m * 0.58)
  // Past eight strands each one thins, so a crowd reads as a tone, not a blot.
  const width = m > 8 ? r2(Math.max(0.5, 0.85 - (m - 8) * 0.08)) : undefined
  const paths: SeamPath[] = []
  const strandY: number[] = []
  for (let i = 0; i < m; i++) {
    const o = m === 1 ? 0 : -hs + (2 * hs * i) / (m - 1)
    const st = spindle.strands?.[i] ?? 'ok'
    const main = i === Math.floor((m - 1) / 2)
    const base = `str ${kind}`
    strandY.push(Y + o)
    if (st === 'err') {
      // A call that failed snaps its strand: two frayed ends across a gap.
      const tA = 0.4
      const tB = 0.62
      const end = (t: number, back: boolean) => {
        const e = strandPoint(kind, x, w, o, i, t)
        const b = strandPoint(kind, x, w, o, i, back ? t + 0.05 : t - 0.05)
        return fray(e[0], e[1], Math.atan2(e[1] - b[1], e[0] - b[0]), back)
      }
      paths.push({ d: strandPath(kind, x, w, o, i, 0, tA), cls: `${base} err`, main, range: [0, 0.42], width })
      paths.push({ d: end(tA, false), cls: 'err fray', range: [0.4, 0.5] })
      paths.push({ d: end(tB, true), cls: 'err fray', range: [0.58, 0.66] })
      paths.push({ d: strandPath(kind, x, w, o, i, tB, 1), cls: `${base} err`, range: [0.6, 1], width })
    } else if (st === 'out') {
      paths.push({ d: strandPath(kind, x, w, o, i, 0, 1), cls: `${base} out`, main, width })
    } else if (st === 'back') {
      paths.push({ d: strandPath(kind, x, w, o, i, 0, 1), cls: `${base} back`, main, width, key: `back${i}` })
    } else {
      paths.push({ d: strandPath(kind, x, w, o, i, 0, 1), cls: base, main, width })
    }
  }
  return { paths, strandY }
}

/** Lay out a seam. `maxWidth` squeezes the units (never below 42%) so a
 *  long stretch still fits beside its summary. */
export function layoutSeam(units: SeamUnit[], maxWidth?: number): SeamGeom {
  const widths = units.map(unitWidth)
  const natural = LEAD + widths.reduce((a, b) => a + b, 0) + GAP * Math.max(0, units.length - 1)
  const k = maxWidth && natural + TAIL > maxWidth ? Math.max(0.42, (maxWidth - TAIL) / natural) : 1
  const pieces: SeamPiece[] = []
  const geoms: SeamUnitGeom[] = []
  let x = LEAD * k
  pieces.push({ x0: 0, x1: x, unit: -1, paths: [{ d: `M0 ${SEAM_Y}H${r2(x)}`, cls: 'ln', main: true }] })
  units.forEach((u, i) => {
    const w = widths[i] * k
    const g = unitPaths(u, x, w)
    pieces.push({ x0: x, x1: x + w, unit: i, paths: g.paths })
    geoms.push({ x0: x, x1: x + w, cx: x + w / 2, strandY: g.strandY ?? [SEAM_Y], label: u.label ?? '' })
    x += w
    if (i < units.length - 1) {
      pieces.push({ x0: x, x1: x + GAP * k, unit: -1, paths: [{ d: `M${r2(x)} ${SEAM_Y}H${r2(x + GAP * k)}`, cls: 'ln', main: true }] })
      x += GAP * k
    }
  })
  pieces.push({ x0: x, x1: x + TAIL, unit: -1, paths: [{ d: `M${r2(x)} ${SEAM_Y}H${r2(x + TAIL)}`, cls: 'ln', main: true }] })
  return { width: x + TAIL, height: SEAM_H, pieces, units: geoms }
}

/** Fold a long list of strand states into at most `cap` strands: a strand
 *  is in trouble if any call it stands for is. */
export function sampleStrands(states: StrandState[], cap = STRAND_CAP): StrandState[] {
  const n = states.length
  const m = Math.min(n, cap)
  const out: StrandState[] = []
  for (let j = 0; j < m; j++) {
    const mine = states.filter((_, i) => Math.floor((i * m) / n) === j)
    out.push(mine.includes('err') ? 'err' : mine.includes('out') ? 'out' : mine.includes('back') ? 'back' : 'ok')
  }
  return out
}

/** Which strand a call's mark lands on when its group folds. */
export function strandIndex(i: number, n: number, cap = STRAND_CAP): number {
  const m = Math.min(n, cap)
  return Math.floor((i * m) / n)
}
