import { useEffect, useRef, useState } from 'react'

/**
 * TopoWordmark — the hero wordmark rendered as nested topographic
 * contour lines that hug the letter shapes. Uses the same sum-of-sines
 * perturbation vocabulary as the TopographicMark logo so the mark and
 * the wordmark feel like siblings from one atlas.
 *
 * Pipeline (one-time on mount, then draw loop):
 *   1. Render `ub` (Inter Black) + italic `α` at 1.15× into a 2×
 *      supersampled offscreen canvas for clean edges.
 *   2. Threshold → binary mask → two-pass chamfer SDF.
 *   3. Marching squares at N signed-distance levels → line segments.
 *   4. Stitch adjacent segments into polylines via endpoint hash.
 *   5. Chaikin smooth each polyline (2 iterations).
 *   6. requestAnimationFrame draw loop perturbs every point with a
 *      sum-of-sines field and strokes the polylines in an ice-blue
 *      gradient (inner rings bright, outer rings fading).
 */
interface Props {
  text?: string
  width?: number
  height?: number
  drift?: boolean
}

const INITIAL_DRIFT_MS = 4500
const INTERACTION_DRIFT_MS = 1800
const WAKE_DRIFT_MS = 1200
const TARGET_FPS = 24
const FRAME_INTERVAL_MS = 1000 / TARGET_FPS

export function TopoWordmark({
  text = 'freyja',
  width = 1100,
  // 340 instead of 260 so the outermost contour rings (up to ~65px
  // outside the letter outline per `LEVELS`) and Departure Mono's
  // descender depth both have room to breathe without clipping.
  height = 340,
  drift = true,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const rafRef = useRef<number>(0)
  const [fontReady, setFontReady] = useState(() => {
    if (typeof document === 'undefined' || !document.fonts) return true
    return document.fonts.check('16px "Departure Mono"')
  })

  useEffect(() => {
    if (fontReady || typeof document === 'undefined' || !document.fonts) return
    let cancelled = false
    document.fonts
      .load('16px "Departure Mono"')
      .then(() => document.fonts.ready)
      .then(() => {
        if (!cancelled) setFontReady(true)
      })
      .catch(() => {
        if (!cancelled) setFontReady(true)
      })
    return () => {
      cancelled = true
    }
  }, [fontReady])

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d', { alpha: true })
    if (!ctx) return
    if (!fontReady) {
      ctx.clearRect(0, 0, canvas.width, canvas.height)
      return
    }

    // ── Display canvas sized to DPR, drawing in CSS pixels ──
    // `width`/`height` are the *drawing buffer* resolution in CSS pixels;
    // the actual display size is controlled by the JSX class (w-full +
    // aspect-ratio) so the wordmark scales down to fit narrower panels
    // without needing to rebuild the marching-squares polylines.
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    canvas.width = Math.floor(width * dpr)
    canvas.height = Math.floor(height * dpr)
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)

    // ── 1. Offscreen text render (2× supersample) ──
    // Supersampling is critical for killing the horizontal-streak
    // artifacts you get from hinted text at modest sizes. Rendering
    // at 2× and scaling segments back down halves the high-frequency
    // pixel noise that marching squares is sensitive to.
    const SS = 2
    const SW = width * SS
    const SH = height * SS

    const off = document.createElement('canvas')
    off.width = SW
    off.height = SH
    const octx = off.getContext('2d')
    if (!octx) return
    octx.fillStyle = '#000'
    octx.fillRect(0, 0, SW, SH)
    octx.fillStyle = '#fff'
    octx.textBaseline = 'alphabetic'

    // Single-run text render. Using Departure Mono (the same font as
    // the rest of the app chrome) ties the wordmark into the overall
    // aesthetic — pixel-hinted mono letterforms produce denser contour
    // rings with more character than a smooth sans like Inter Black.
    // We auto-shrink the font size if the word overflows the canvas.
    const fontStack = '"Departure Mono", "SF Mono", "JetBrains Mono", Menlo, Monaco, monospace'
    const maxTextWidth = SW * 0.82
    // Departure Mono is thinner per-character than Inter Black at the
    // same px size, so we start larger — the mask needs enough stroke
    // thickness for the SDF's inner rings to be visibly different from
    // the outline.
    let baseSize = 240 * SS
    octx.font = `${baseSize}px ${fontStack}`
    let textWidth = octx.measureText(text).width
    if (textWidth > maxTextWidth) {
      baseSize = Math.floor(baseSize * (maxTextWidth / textWidth))
      octx.font = `${baseSize}px ${fontStack}`
      textWidth = octx.measureText(text).width
    }

    // Baseline heuristic — pulls the baseline up so the letter body
    // sits vertically centred in the (now taller) canvas, leaving equal
    // room above ascenders and below descenders for the outer rings.
    const baselineY = SH / 2 + baseSize * 0.28
    const startX = (SW - textWidth) / 2

    octx.textAlign = 'left'
    octx.fillText(text, startX, baselineY)

    // ── 2. Binary mask + signed distance field ──
    const data = octx.getImageData(0, 0, SW, SH).data
    const mask = new Uint8Array(SW * SH)
    for (let i = 0; i < SW * SH; i++) {
      mask[i] = data[i * 4] > 128 ? 1 : 0
    }

    const sdf = computeSDF(mask, SW, SH)

    // ── 3. Marching squares at fixed SDF levels ──
    // Levels are in (supersampled) pixel units. Negative = inside the
    // letter (the "peak"), positive = outside (the fading skirts).
    const LEVELS = [
      -92, -72, -54, -40, -28, -18, -10, -4,
      4, 12, 22, 34, 48, 64, 82, 104, 130,
    ]

    const rawContours = LEVELS.map((lv) => marchingSquares(sdf, SW, SH, lv))

    // ── 4. Stitch segments into polylines ──
    const polyGroups = rawContours.map((segs) => stitchSegments(segs))

    // ── 5. Chaikin-smooth and scale back to display resolution ──
    const smoothed: Float32Array[][] = polyGroups.map((polys) =>
      polys
        .filter((p) => p.length >= 6)
        .map((p) => {
          const smoothPoly = chaikin(p, 2)
          // Scale supersample → display
          for (let i = 0; i < smoothPoly.length; i++) smoothPoly[i] /= SS
          return smoothPoly
        }),
    )

    // ── 6. Draw loop ──
    // Shared sum-of-sines perturbation. The wordmark animates on arrival
    // and interaction, then settles so the empty screen can become truly idle.
    let t = 0
    let disposed = false
    let lastFrame = 0
    let driftUntil = 0
    const reducedMotion = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false
    const canDrift = drift && !reducedMotion

    const draw = (advance: boolean) => {
      ctx.clearRect(0, 0, width, height)
      ctx.lineCap = 'round'
      ctx.lineJoin = 'round'

      for (let i = 0; i < LEVELS.length; i++) {
        const lv = LEVELS[i]
        const polys = smoothed[i]

        // Light ice-blue with a faint blue tint — still cool but
        // brighter than the old `#a8d4fc`. Inner rings solid, outer
        // rings fade out toward the background.
        let op: number
        if (lv <= 0) {
          op = 0.78 - Math.abs(lv) * 0.0018
        } else {
          op = Math.max(0.05, 0.62 - lv * 0.005)
        }

        ctx.strokeStyle = `rgba(224, 236, 255, ${op})`
        ctx.lineWidth = 1.15

        // Perturbation amplitude grows with |lv| so outer rings
        // wobble more than the inner peak — reinforces the "drift"
        // idea and gives the skirts that hand-drawn-topo feel.
        const amp = 1.8 + Math.abs(lv) * 0.075

        for (const poly of polys) {
          if (poly.length < 4) continue

          ctx.beginPath()

          // First point with perturbation
          let px = poly[0]
          let py = poly[1]
          let dx = perturbX(px, py, i, t) * amp
          let dy = perturbY(px, py, i, t) * amp
          ctx.moveTo(px + dx, py + dy)

          for (let k = 2; k < poly.length; k += 2) {
            px = poly[k]
            py = poly[k + 1]
            dx = perturbX(px, py, i, t) * amp
            dy = perturbY(px, py, i, t) * amp
            ctx.lineTo(px + dx, py + dy)
          }
          ctx.stroke()
        }
      }

      if (advance && canDrift) t += 0.012
    }

    const cancelPendingFrame = () => {
      if (rafRef.current) {
        cancelAnimationFrame(rafRef.current)
        rafRef.current = 0
      }
    }

    const scheduleFrame = () => {
      if (disposed || document.hidden || rafRef.current) return
      rafRef.current = requestAnimationFrame(frame)
    }

    const wake = (durationMs: number) => {
      if (disposed) return
      if (!canDrift) {
        draw(false)
        return
      }
      driftUntil = Math.max(driftUntil, performance.now() + durationMs)
      scheduleFrame()
    }

    const frame = (now: number) => {
      rafRef.current = 0
      if (disposed || document.hidden) return

      const active = canDrift && now < driftUntil
      if (!active) {
        draw(false)
        return
      }

      if (now - lastFrame >= FRAME_INTERVAL_MS) {
        lastFrame = now
        draw(true)
      }
      scheduleFrame()
    }

    const wakeFromInteraction = () => wake(INTERACTION_DRIFT_MS)
    const wakeFromVisibility = () => {
      if (document.hidden) {
        cancelPendingFrame()
      } else {
        lastFrame = 0
        wake(WAKE_DRIFT_MS)
      }
    }

    draw(false)
    wake(INITIAL_DRIFT_MS)

    // Re-draw once font metrics settle after app focus or late browser
    // font work. The component waits for Departure Mono before building
    // the contour mask, so this is just a cheap visual refresh.
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(() => {
        if (!disposed) wake(WAKE_DRIFT_MS)
      })
    }

    canvas.addEventListener('pointerenter', wakeFromInteraction)
    canvas.addEventListener('pointermove', wakeFromInteraction)
    window.addEventListener('focus', wakeFromInteraction)
    document.addEventListener('visibilitychange', wakeFromVisibility)

    return () => {
      disposed = true
      cancelPendingFrame()
      canvas.removeEventListener('pointerenter', wakeFromInteraction)
      canvas.removeEventListener('pointermove', wakeFromInteraction)
      window.removeEventListener('focus', wakeFromInteraction)
      document.removeEventListener('visibilitychange', wakeFromVisibility)
    }
  }, [text, width, height, drift, fontReady])

  return (
    <canvas
      ref={canvasRef}
      className="block w-full"
      style={{
        aspectRatio: `${width} / ${height}`,
        maxWidth: `${width}px`,
      }}
      aria-label={`${text} topographic wordmark`}
    />
  )
}

// ─────────────────────────────────────────────────────────────────
//  Helpers — module-level pure functions so they can be tuned /
//  tested without touching the React surface.
// ─────────────────────────────────────────────────────────────────

/**
 * Sum-of-sines horizontal displacement — rings get increasingly
 * perturbed the further they are from the letter interior. Same
 * flavor of noise as `TopographicMark` in TitleBar.tsx so the
 * wordmark and the logo share a visual dialect.
 */
function perturbX(x: number, y: number, ring: number, t: number): number {
  return (
    Math.sin(x * 0.011 + ring * 0.35 + t) +
    Math.cos(y * 0.015 + ring * 0.52 + t * 0.7) * 0.7 +
    Math.sin(x * 0.025 - ring * 0.8 + t * 0.4) * 0.38 +
    Math.cos(x * 0.004 + y * 0.006 + ring * 0.2) * 0.6
  )
}

function perturbY(x: number, y: number, ring: number, t: number): number {
  return (
    Math.cos(x * 0.013 + ring * 0.42 + t * 0.8) +
    Math.sin(y * 0.017 - ring * 0.3 + t * 0.6) * 0.7 +
    Math.cos(y * 0.027 + ring * 0.9 + t * 0.5) * 0.38 +
    Math.sin(x * 0.005 - y * 0.007 + ring * 0.45) * 0.6
  )
}

/**
 * Two-pass chamfer distance transform producing a signed distance
 * field. Inside pixels get negative distances (distance to nearest
 * outside pixel), outside pixels get positive (distance to nearest
 * inside pixel). Cheap O(n) with the diagonal chamfer 1/√2 mask.
 */
function computeSDF(mask: Uint8Array, w: number, h: number): Float32Array {
  const INF = 1e9
  const posField = new Float32Array(w * h)
  const negField = new Float32Array(w * h)

  for (let i = 0; i < w * h; i++) {
    posField[i] = mask[i] ? 0 : INF
    negField[i] = mask[i] ? INF : 0
  }

  chamferPass(posField, w, h)
  chamferPass(negField, w, h)

  const sdf = new Float32Array(w * h)
  for (let i = 0; i < w * h; i++) sdf[i] = posField[i] - negField[i]
  return sdf
}

function chamferPass(field: Float32Array, w: number, h: number): void {
  const d1 = 1.0
  const d2 = 1.4142135623730951

  // forward (top-left → bottom-right)
  for (let y = 1; y < h; y++) {
    for (let x = 1; x < w - 1; x++) {
      const i = y * w + x
      let v = field[i]
      v = Math.min(v, field[(y - 1) * w + (x - 1)] + d2)
      v = Math.min(v, field[(y - 1) * w + x] + d1)
      v = Math.min(v, field[(y - 1) * w + (x + 1)] + d2)
      v = Math.min(v, field[y * w + (x - 1)] + d1)
      field[i] = v
    }
  }

  // backward (bottom-right → top-left)
  for (let y = h - 2; y >= 0; y--) {
    for (let x = w - 2; x >= 1; x--) {
      const i = y * w + x
      let v = field[i]
      v = Math.min(v, field[y * w + (x + 1)] + d1)
      v = Math.min(v, field[(y + 1) * w + (x - 1)] + d2)
      v = Math.min(v, field[(y + 1) * w + x] + d1)
      v = Math.min(v, field[(y + 1) * w + (x + 1)] + d2)
      field[i] = v
    }
  }
}

/**
 * Marching squares scalar contour extraction. Returns an array of
 * line segments `[x1, y1, x2, y2]` representing the iso-line at the
 * given `level`. Saddles (ambiguous cases 5 and 10) are resolved with
 * the asymptotic decider — sampling the cell centre to pick the
 * correct topology instead of defaulting, which used to produce the
 * diagonal "ghost lines" across the letters. The linear lerp is
 * clamped to `[0, 1]` so the rare pathological cell with a zero-diff
 * edge can't spray a segment halfway across the row.
 */
function marchingSquares(
  field: Float32Array,
  w: number,
  h: number,
  level: number,
): Array<[number, number, number, number]> {
  const segments: Array<[number, number, number, number]> = []

  const lerp = (la: number, lb: number): number => {
    const diff = lb - la
    if (Math.abs(diff) < 1e-6) return 0.5
    const t = (level - la) / diff
    return t < 0 ? 0 : t > 1 ? 1 : t
  }

  for (let y = 0; y < h - 1; y++) {
    for (let x = 0; x < w - 1; x++) {
      const tl = field[y * w + x]
      const tr = field[y * w + x + 1]
      const br = field[(y + 1) * w + x + 1]
      const bl = field[(y + 1) * w + x]

      let code = 0
      if (tl > level) code |= 1
      if (tr > level) code |= 2
      if (br > level) code |= 4
      if (bl > level) code |= 8
      if (code === 0 || code === 15) continue

      const topX = x + lerp(tl, tr)
      const topY = y
      const rightX = x + 1
      const rightY = y + lerp(tr, br)
      const botX = x + lerp(bl, br)
      const botY = y + 1
      const leftX = x
      const leftY = y + lerp(tl, bl)

      switch (code) {
        case 1:
          segments.push([leftX, leftY, topX, topY])
          break
        case 2:
          segments.push([topX, topY, rightX, rightY])
          break
        case 3:
          segments.push([leftX, leftY, rightX, rightY])
          break
        case 4:
          segments.push([rightX, rightY, botX, botY])
          break
        case 5: {
          // Saddle. Check centre to decide which pair.
          const center = (tl + tr + br + bl) * 0.25
          if (center > level) {
            segments.push([leftX, leftY, topX, topY])
            segments.push([rightX, rightY, botX, botY])
          } else {
            segments.push([leftX, leftY, botX, botY])
            segments.push([topX, topY, rightX, rightY])
          }
          break
        }
        case 6:
          segments.push([topX, topY, botX, botY])
          break
        case 7:
          segments.push([leftX, leftY, botX, botY])
          break
        case 8:
          segments.push([leftX, leftY, botX, botY])
          break
        case 9:
          segments.push([topX, topY, botX, botY])
          break
        case 10: {
          // Saddle mirror.
          const center = (tl + tr + br + bl) * 0.25
          if (center > level) {
            segments.push([topX, topY, rightX, rightY])
            segments.push([leftX, leftY, botX, botY])
          } else {
            segments.push([leftX, leftY, topX, topY])
            segments.push([rightX, rightY, botX, botY])
          }
          break
        }
        case 11:
          segments.push([topX, topY, rightX, rightY])
          break
        case 12:
          segments.push([leftX, leftY, rightX, rightY])
          break
        case 13:
          segments.push([topX, topY, rightX, rightY])
          break
        case 14:
          segments.push([leftX, leftY, topX, topY])
          break
      }
    }
  }

  return segments
}

/**
 * Greedy endpoint-matching stitcher: chain segments that share
 * endpoints into longer polylines so we can smooth them as
 * continuous curves (Chaikin on a bag of segments would do nothing).
 * Uses a rounded string hash for endpoint matching since marching
 * squares emits floating-point crossings that are bit-identical on
 * shared cell edges.
 */
function stitchSegments(
  segments: Array<[number, number, number, number]>,
): Float32Array[] {
  if (segments.length === 0) return []

  const PREC = 1000 // 0.001 px granularity
  const key = (x: number, y: number): string =>
    `${Math.round(x * PREC)},${Math.round(y * PREC)}`

  const endpointMap = new Map<string, Array<[number, number]>>()
  for (let i = 0; i < segments.length; i++) {
    const s = segments[i]
    const k1 = key(s[0], s[1])
    const k2 = key(s[2], s[3])
    if (!endpointMap.has(k1)) endpointMap.set(k1, [])
    if (!endpointMap.has(k2)) endpointMap.set(k2, [])
    endpointMap.get(k1)!.push([i, 0])
    endpointMap.get(k2)!.push([i, 1])
  }

  const visited = new Uint8Array(segments.length)
  const polylines: Float32Array[] = []

  for (let startIdx = 0; startIdx < segments.length; startIdx++) {
    if (visited[startIdx]) continue
    visited[startIdx] = 1

    const s = segments[startIdx]
    const pts: number[] = [s[0], s[1], s[2], s[3]]

    // Extend forward from the 2nd endpoint.
    let curKey = key(s[2], s[3])
    while (true) {
      const candidates = endpointMap.get(curKey) || []
      let advanced = false
      for (const [idx, end] of candidates) {
        if (visited[idx]) continue
        visited[idx] = 1
        const next = segments[idx]
        const nx = end === 0 ? next[2] : next[0]
        const ny = end === 0 ? next[3] : next[1]
        pts.push(nx, ny)
        curKey = key(nx, ny)
        advanced = true
        break
      }
      if (!advanced) break
    }

    // Extend backward from the 1st endpoint.
    curKey = key(s[0], s[1])
    while (true) {
      const candidates = endpointMap.get(curKey) || []
      let advanced = false
      for (const [idx, end] of candidates) {
        if (visited[idx]) continue
        visited[idx] = 1
        const next = segments[idx]
        const nx = end === 0 ? next[2] : next[0]
        const ny = end === 0 ? next[3] : next[1]
        pts.unshift(nx, ny)
        curKey = key(nx, ny)
        advanced = true
        break
      }
      if (!advanced) break
    }

    polylines.push(new Float32Array(pts))
  }

  return polylines
}

/**
 * Chaikin corner-cutting smoothing. Each iteration replaces every
 * edge (p, q) with two points at p+0.25(q-p) and p+0.75(q-p), pulling
 * the polyline toward a smoother curve. Two iterations usually
 * produce visually round contours without over-softening detail.
 */
function chaikin(poly: Float32Array, iterations: number): Float32Array {
  let current = poly
  for (let it = 0; it < iterations; it++) {
    const n = current.length / 2
    if (n < 3) return current
    const next = new Float32Array((n - 1) * 4 + 4)
    let ni = 0
    next[ni++] = current[0]
    next[ni++] = current[1]
    for (let i = 0; i < n - 1; i++) {
      const x1 = current[i * 2]
      const y1 = current[i * 2 + 1]
      const x2 = current[(i + 1) * 2]
      const y2 = current[(i + 1) * 2 + 1]
      next[ni++] = 0.75 * x1 + 0.25 * x2
      next[ni++] = 0.75 * y1 + 0.25 * y2
      next[ni++] = 0.25 * x1 + 0.75 * x2
      next[ni++] = 0.25 * y1 + 0.75 * y2
    }
    next[ni++] = current[(n - 1) * 2]
    next[ni++] = current[(n - 1) * 2 + 1]
    current = next.subarray(0, ni)
  }
  return current
}
