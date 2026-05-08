import { useEffect, useMemo, useRef, useState } from 'react'

/**
 * Ambient topographic backdrop — port of `topo-mock-v2.html`.
 *
 * Field stack:
 *  • Domain warp on (x, y) for organic curl.
 *  • Sum of Gaussian "mountain" peaks (with ~12% depressions).
 *  • Slow large-scale flow FBM that connects peaks through saddles.
 *  • Medium-frequency base terrain FBM (~80px features) so flat regions
 *    between peaks still have non-zero gradient — kills dead zones.
 *  • High-frequency detail FBM (~25px features), offset 1.3× to break
 *    alignment with base terrain.
 *
 * Then marching squares (asymptotic decider) extracts iso-lines, Chaikin
 * smooths, and a 3-layer hash-based perturbation (with ridged FBM in
 * the coarse layer for sharp geological creases) gives the final lines
 * their hand-drawn-but-irregular character. Closed contours additionally
 * fill with a hypsometric tint keyed to elevation, for subtle depth.
 */
interface Props {
  seed: number
  className?: string
}

interface Contour {
  d: string
  elevation: number
  closed: boolean
}

const CFG = {
  cellSize: 3,
  levels: 40,

  numPeaks: 20,
  peakSigmaMin: 0.13,
  peakSigmaMax: 0.32,
  peakHeightMin: 0.4,
  peakHeightMax: 1.0,
  peakSpacingMin: 0.12,
  depressionChance: 0.12,

  flowAmplitude: 0.8,
  flowFreq: 0.003,
  flowOctaves: 3,
  flowPersistence: 0.55,

  // Base terrain — medium-freq FBM that fills flat zones (~80px features).
  baseAmplitude: 0.35,
  baseFreq: 0.012,
  baseOctaves: 4,
  basePersistence: 0.5,

  // Detail grain — high-freq FBM (~25px features).
  detailAmplitude: 0.15,
  detailFreq: 0.04,
  detailOctaves: 3,
  detailPersistence: 0.45,

  warpStrength: 22,
  warpFreq: 0.005,
  warpOctaves: 2,

  // Uniform line style.
  lineOpacity: 0.09,
  lineStroke: 0.75,
  tintOpacity: 0.008,

  minRawPoints: 6,
  smoothAbove: 10,

  // Hypsometric colour ramp (HSL).
  hueDeep: 228,
  hueHigh: 205,
  satDeep: 25,
  satHigh: 35,
  lightDeep: 55,
  lightHigh: 68,
} as const

export function TopoBackdrop({ seed, className }: Props) {
  const hostRef = useRef<HTMLDivElement>(null)
  const [size, setSize] = useState<{ w: number; h: number } | null>(null)

  useEffect(() => {
    const host = hostRef.current
    if (!host) return
    let pending = 0
    const measure = () => {
      const r = host.getBoundingClientRect()
      const w = Math.max(1, Math.round(r.width))
      const h = Math.max(1, Math.round(r.height))
      setSize((prev) => (prev && prev.w === w && prev.h === h ? prev : { w, h }))
    }
    measure()
    const ro = new ResizeObserver(() => {
      window.clearTimeout(pending)
      pending = window.setTimeout(measure, 120)
    })
    ro.observe(host)
    return () => {
      ro.disconnect()
      window.clearTimeout(pending)
    }
  }, [])

  const contours = useMemo(() => {
    if (!size) return [] as Contour[]
    return buildContours(size.w, size.h, seed)
  }, [size, seed])

  return (
    <div ref={hostRef} className={className} aria-hidden>
      {size && (
        <svg
          viewBox={`0 0 ${size.w} ${size.h}`}
          preserveAspectRatio="xMidYMid slice"
          width="100%"
          height="100%"
          fill="none"
          strokeLinejoin="round"
          strokeLinecap="round"
          style={{ mixBlendMode: 'screen' }}
        >
          {/* Layer 1: hypsometric tint fills under closed contours. */}
          <g>
            {contours.map((c, i) =>
              c.closed ? (
                <path
                  key={`t${i}`}
                  d={c.d + 'Z'}
                  fill={elevColor(c.elevation, CFG.tintOpacity)}
                />
              ) : null,
            )}
          </g>
          {/* Layer 2: contour lines, uniform weight, elevation-tinted. */}
          <g>
            {contours.map((c, i) => (
              <path
                key={`l${i}`}
                d={c.d}
                stroke={elevColor(c.elevation, CFG.lineOpacity)}
                strokeWidth={CFG.lineStroke}
              />
            ))}
          </g>
        </svg>
      )}
    </div>
  )
}

// ─── colour ramp ───────────────────────────────────────────────────

function elevColor(t: number, opacity: number): string {
  const h = CFG.hueDeep + (CFG.hueHigh - CFG.hueDeep) * t
  const s = CFG.satDeep + (CFG.satHigh - CFG.satDeep) * t
  const l = CFG.lightDeep + (CFG.lightHigh - CFG.lightDeep) * t
  return `hsla(${h | 0}, ${s | 0}%, ${l | 0}%, ${opacity})`
}

// ─── contour pipeline ──────────────────────────────────────────────

interface Field {
  data: Float32Array
  cols: number
  rows: number
  cell: number
  min: number
  max: number
}

function buildContours(width: number, height: number, seed: number): Contour[] {
  const field = makeField(width, height, seed)
  const range = field.max - field.min
  const lo = field.min + range * 0.04
  const hi = field.max - range * 0.04
  const pPhase = (seed | 0) * 0.137

  const out: Contour[] = []
  for (let i = 0; i < CFG.levels; i++) {
    const t = i / Math.max(1, CFG.levels - 1)
    const level = lo + (hi - lo) * t
    const segs = march(field, level)
    const polys = stitch(segs)
    for (const poly of polys) {
      if (poly.length < CFG.minRawPoints * 2) continue
      const smoothed = poly.length >= CFG.smoothAbove * 2 ? chaikin(poly, 2) : poly
      const roughened = perturbPoly(smoothed, pPhase)
      const d = polyToD(roughened)
      if (!d) continue
      out.push({ d, elevation: t, closed: isClosed(roughened) })
    }
  }
  return out
}

// ─── seeded RNG ────────────────────────────────────────────────────

function makeRng(seed: number): () => number {
  let s = (seed | 0) || 1
  return () => {
    s = (s * 1664525 + 1013904223) | 0
    return ((s >>> 0) % 1_000_000) / 1_000_000
  }
}

// ─── sum-of-sines noise (for the field) ───────────────────────────

interface Phase {
  a: number
  b: number
  c: number
  d: number
  rot: number
}

function makePhases(r: () => number): Phase {
  return {
    a: r() * Math.PI * 2,
    b: r() * Math.PI * 2,
    c: r() * Math.PI * 2,
    d: r() * Math.PI * 2,
    rot: r() * Math.PI * 2,
  }
}

function oneOctave(px: number, py: number, freq: number, ph: Phase): number {
  const cosR = Math.cos(ph.rot)
  const sinR = Math.sin(ph.rot)
  const rx = px * cosR - py * sinR
  const ry = px * sinR + py * cosR
  return (
    Math.sin(rx * freq + ph.a) * Math.cos(ry * freq * 1.07 + ph.b) +
    0.55 * Math.sin((rx + ry) * freq * 0.7 + ph.c) +
    0.4 * Math.cos((rx - ry) * freq * 0.85 + ph.d)
  )
}

function fbm(
  px: number,
  py: number,
  phases: Phase[],
  freq0: number,
  persistence: number,
): number {
  let v = 0
  let amp = 1
  let freq = freq0
  for (let o = 0; o < phases.length; o++) {
    v += amp * oneOctave(px, py, freq, phases[o])
    amp *= persistence
    freq *= 2.05
  }
  return v
}

// ─── hash-based value noise (for perturbation — non-periodic) ─────

function hash2d(ix: number, iy: number, seed: number): number {
  let n = (ix * 1619 + iy * 31337 + seed * 6971) | 0
  n = (n << 13) ^ n
  n = (n * (n * n * 15731 + 789221) + 1376312589) & 0x7fffffff
  return n / 0x7fffffff
}

function vnoise(x: number, y: number, seed: number): number {
  const ix = Math.floor(x)
  const iy = Math.floor(y)
  const fx = x - ix
  const fy = y - iy
  // Quintic interpolation (C2 continuous).
  const u = fx * fx * fx * (fx * (fx * 6 - 15) + 10)
  const v = fy * fy * fy * (fy * (fy * 6 - 15) + 10)
  const a = hash2d(ix, iy, seed)
  const b = hash2d(ix + 1, iy, seed)
  const c = hash2d(ix, iy + 1, seed)
  const d = hash2d(ix + 1, iy + 1, seed)
  return ((a * (1 - u) + b * u) * (1 - v) + (c * (1 - u) + d * u) * v) * 2 - 1
}

/** FBM with optional ridged mode (`abs` produces sharp geological creases). */
function fbmHash(
  x: number,
  y: number,
  seed: number,
  octaves: number,
  lacunarity: number,
  gain: number,
  ridged: boolean,
): number {
  let sum = 0
  let amp = 1
  let freq = 1
  let maxAmp = 0
  for (let o = 0; o < octaves; o++) {
    let n = vnoise(x * freq, y * freq, seed + o * 5381)
    if (ridged) n = 1 - 2 * Math.abs(n)
    sum += n * amp
    maxAmp += amp
    amp *= gain
    freq *= lacunarity
  }
  return sum / maxAmp
}

// ─── field synthesis ───────────────────────────────────────────────

interface Peak {
  cx: number
  cy: number
  sigma: number
  h: number
}

function makeField(W: number, H: number, seed: number): Field {
  const r = makeRng(seed)
  const minDim = Math.min(W, H)
  const spacing = minDim * CFG.peakSpacingMin

  // Poisson-style peak placement. Allow peaks to land slightly outside
  // the panel so edges don't read as artificially flat.
  const peaks: Peak[] = []
  for (let i = 0; i < CFG.numPeaks; i++) {
    let cx = 0
    let cy = 0
    let best = -1
    for (let tries = 0; tries < 40; tries++) {
      const m = -minDim * 0.08
      const tx = m + r() * (W - 2 * m)
      const ty = m + r() * (H - 2 * m)
      let nearest = Infinity
      for (const p of peaks) {
        const d = Math.hypot(tx - p.cx, ty - p.cy)
        if (d < nearest) nearest = d
      }
      if (nearest > best) {
        best = nearest
        cx = tx
        cy = ty
      }
      if (nearest >= spacing) break
    }
    const sigma =
      minDim *
      (CFG.peakSigmaMin + r() * (CFG.peakSigmaMax - CFG.peakSigmaMin))
    const mag =
      CFG.peakHeightMin + r() * (CFG.peakHeightMax - CFG.peakHeightMin)
    const sign = r() < CFG.depressionChance ? -1 : 1
    peaks.push({ cx, cy, sigma, h: mag * sign })
  }

  const flowPhases = Array.from({ length: CFG.flowOctaves }, () => makePhases(r))
  const warpPhasesX = Array.from({ length: CFG.warpOctaves }, () => makePhases(r))
  const warpPhasesY = Array.from({ length: CFG.warpOctaves }, () => makePhases(r))
  const basePhases = Array.from({ length: CFG.baseOctaves }, () => makePhases(r))
  const detailPhases = Array.from({ length: CFG.detailOctaves }, () =>
    makePhases(r),
  )

  const cell = CFG.cellSize
  const cols = Math.ceil(W / cell) + 1
  const rows = Math.ceil(H / cell) + 1
  const data = new Float32Array(cols * rows)
  let min = Infinity
  let max = -Infinity

  for (let y = 0; y < rows; y++) {
    for (let x = 0; x < cols; x++) {
      const px = x * cell
      const py = y * cell

      // Domain warp.
      const wx =
        fbm(px, py, warpPhasesX, CFG.warpFreq, 0.5) * CFG.warpStrength
      const wy =
        fbm(px, py, warpPhasesY, CFG.warpFreq, 0.5) * CFG.warpStrength
      const sx = px + wx
      const sy = py + wy

      // Gaussian peaks.
      let v = 0
      for (let i = 0; i < peaks.length; i++) {
        const p = peaks[i]
        const dx = sx - p.cx
        const dy = sy - p.cy
        v += p.h * Math.exp(-(dx * dx + dy * dy) / (p.sigma * p.sigma))
      }

      // Slow flow connecting peaks through saddles.
      v +=
        CFG.flowAmplitude *
        fbm(sx, sy, flowPhases, CFG.flowFreq, CFG.flowPersistence)

      // Base terrain — fills flat zones with rolling hills.
      v +=
        CFG.baseAmplitude *
        fbm(sx, sy, basePhases, CFG.baseFreq, CFG.basePersistence)

      // Detail grain — offset 1.3× to break alignment with base layer.
      v +=
        CFG.detailAmplitude *
        fbm(
          sx * 1.3,
          sy * 1.3,
          detailPhases,
          CFG.detailFreq,
          CFG.detailPersistence,
        )

      data[y * cols + x] = v
      if (v < min) min = v
      if (v > max) max = v
    }
  }
  return { data, cols, rows, cell, min, max }
}

// ─── marching squares (asymptotic decider) ─────────────────────────

type Segment = [number, number, number, number]

function march(field: Field, level: number): Segment[] {
  const { data, cols, rows, cell } = field
  const segs: Segment[] = []
  const lerp = (a: number, b: number): number => {
    const d = b - a
    if (Math.abs(d) < 1e-6) return 0.5
    const t = (level - a) / d
    return t < 0 ? 0 : t > 1 ? 1 : t
  }
  for (let y = 0; y < rows - 1; y++) {
    for (let x = 0; x < cols - 1; x++) {
      const tl = data[y * cols + x]
      const tr = data[y * cols + x + 1]
      const br = data[(y + 1) * cols + x + 1]
      const bl = data[(y + 1) * cols + x]
      let c = 0
      if (tl > level) c |= 1
      if (tr > level) c |= 2
      if (br > level) c |= 4
      if (bl > level) c |= 8
      if (c === 0 || c === 15) continue
      const tX = (x + lerp(tl, tr)) * cell
      const tY = y * cell
      const rX = (x + 1) * cell
      const rY = (y + lerp(tr, br)) * cell
      const bX = (x + lerp(bl, br)) * cell
      const bY = (y + 1) * cell
      const lX = x * cell
      const lY = (y + lerp(tl, bl)) * cell
      switch (c) {
        case 1:
          segs.push([lX, lY, tX, tY])
          break
        case 2:
          segs.push([tX, tY, rX, rY])
          break
        case 3:
          segs.push([lX, lY, rX, rY])
          break
        case 4:
          segs.push([rX, rY, bX, bY])
          break
        case 5: {
          const den = tl + br - tr - bl
          const s =
            Math.abs(den) < 1e-9
              ? (tl + tr + br + bl) * 0.25
              : (tl * br - tr * bl) / den
          if (level < s) {
            segs.push([lX, lY, tX, tY])
            segs.push([rX, rY, bX, bY])
          } else {
            segs.push([lX, lY, bX, bY])
            segs.push([tX, tY, rX, rY])
          }
          break
        }
        case 6:
          segs.push([tX, tY, bX, bY])
          break
        case 7:
          segs.push([lX, lY, bX, bY])
          break
        case 8:
          segs.push([lX, lY, bX, bY])
          break
        case 9:
          segs.push([tX, tY, bX, bY])
          break
        case 10: {
          const den = tl + br - tr - bl
          const s =
            Math.abs(den) < 1e-9
              ? (tl + tr + br + bl) * 0.25
              : (tl * br - tr * bl) / den
          if (level < s) {
            segs.push([tX, tY, rX, rY])
            segs.push([lX, lY, bX, bY])
          } else {
            segs.push([lX, lY, tX, tY])
            segs.push([rX, rY, bX, bY])
          }
          break
        }
        case 11:
          segs.push([tX, tY, rX, rY])
          break
        case 12:
          segs.push([lX, lY, rX, rY])
          break
        case 13:
          segs.push([tX, tY, rX, rY])
          break
        case 14:
          segs.push([lX, lY, tX, tY])
          break
      }
    }
  }
  return segs
}

function stitch(segments: Segment[]): Float32Array[] {
  if (!segments.length) return []
  const PREC = 1000
  const key = (x: number, y: number): string =>
    `${Math.round(x * PREC)},${Math.round(y * PREC)}`

  const map = new Map<string, Array<[number, number]>>()
  for (let i = 0; i < segments.length; i++) {
    const s = segments[i]
    const k1 = key(s[0], s[1])
    const k2 = key(s[2], s[3])
    if (!map.has(k1)) map.set(k1, [])
    if (!map.has(k2)) map.set(k2, [])
    map.get(k1)!.push([i, 0])
    map.get(k2)!.push([i, 1])
  }

  const visited = new Uint8Array(segments.length)
  const polys: Float32Array[] = []
  for (let start = 0; start < segments.length; start++) {
    if (visited[start]) continue
    visited[start] = 1
    const s = segments[start]
    const pts: number[] = [s[0], s[1], s[2], s[3]]

    const extend = (forward: boolean) => {
      let cur = forward ? key(s[2], s[3]) : key(s[0], s[1])
      while (true) {
        const cands = map.get(cur) || []
        let next = -1
        let ei = 0
        for (const [idx, end] of cands) {
          if (!visited[idx]) {
            next = idx
            ei = end
            break
          }
        }
        if (next < 0) break
        visited[next] = 1
        const seg = segments[next]
        const nx = ei === 0 ? seg[2] : seg[0]
        const ny = ei === 0 ? seg[3] : seg[1]
        if (forward) pts.push(nx, ny)
        else pts.unshift(nx, ny)
        cur = key(nx, ny)
      }
    }
    extend(true)
    extend(false)
    polys.push(new Float32Array(pts))
  }
  return polys
}

function chaikin(poly: Float32Array, iters: number): Float32Array {
  let cur = poly
  for (let it = 0; it < iters; it++) {
    const n = cur.length / 2
    if (n < 3) return cur
    const out = new Float32Array((n - 1) * 4 + 4)
    let i = 0
    out[i++] = cur[0]
    out[i++] = cur[1]
    for (let k = 0; k < n - 1; k++) {
      const x1 = cur[k * 2]
      const y1 = cur[k * 2 + 1]
      const x2 = cur[(k + 1) * 2]
      const y2 = cur[(k + 1) * 2 + 1]
      out[i++] = 0.75 * x1 + 0.25 * x2
      out[i++] = 0.75 * y1 + 0.25 * y2
      out[i++] = 0.25 * x1 + 0.75 * x2
      out[i++] = 0.25 * y1 + 0.75 * y2
    }
    out[i++] = cur[(n - 1) * 2]
    out[i++] = cur[(n - 1) * 2 + 1]
    cur = out.subarray(0, i)
  }
  return cur
}

// ─── 3-layer hash perturbation with ridged coarse layer ───────────

function perturbPoly(poly: Float32Array, phase: number): Float32Array {
  const out = new Float32Array(poly.length)
  const seedI = Math.round(phase * 1000)

  for (let i = 0; i < poly.length; i += 2) {
    const x = poly[i]
    const y = poly[i + 1]

    // Scale to noise-space (controls feature size in px).
    const sx = x * 0.018
    const sy = y * 0.018

    // Layer 1: broad geological warping — RIDGED for sharp creases.
    // Irrational lacunarity (2.2) keeps octaves from aligning into a grid.
    const wx = fbmHash(sx * 0.35, sy * 0.35, seedI, 3, 2.2, 0.55, true)
    const wy = fbmHash(sx * 0.35, sy * 0.35, seedI + 3001, 3, 2.2, 0.55, true)

    // Layer 2: medium irregular bumps.
    const mx = fbmHash(sx * 0.8, sy * 0.8, seedI + 7001, 4, 2.3, 0.45, false)
    const my = fbmHash(sx * 0.8, sy * 0.8, seedI + 11003, 4, 2.3, 0.45, false)

    // Layer 3: fine grit.
    const gx = fbmHash(sx * 2.2, sy * 2.2, seedI + 17001, 3, 2.0, 0.5, false)
    const gy = fbmHash(sx * 2.2, sy * 2.2, seedI + 23003, 3, 2.0, 0.5, false)

    out[i] = x + wx * 3.8 + mx * 2.2 + gx * 0.9
    out[i + 1] = y + wy * 3.8 + my * 2.2 + gy * 0.9
  }
  return out
}

// ─── helpers ───────────────────────────────────────────────────────

function polyToD(poly: Float32Array): string {
  if (poly.length < 4) return ''
  let d = `M${poly[0].toFixed(1)} ${poly[1].toFixed(1)}`
  for (let i = 2; i < poly.length; i += 2) {
    d += `L${poly[i].toFixed(1)} ${poly[i + 1].toFixed(1)}`
  }
  return d
}

function isClosed(poly: Float32Array): boolean {
  const n = poly.length
  if (n < 8) return false
  const dx = poly[0] - poly[n - 2]
  const dy = poly[1] - poly[n - 1]
  return dx * dx + dy * dy < 36
}
