import { useEffect, useRef } from 'react'

/**
 * AnimatedTopographicMark — concentric topographic rings that breathe
 * and shimmer with time.
 *
 * Designed to NEVER let adjacent rings cross. The previous pass let
 * outer rings wobble hardest, but outer rings have the tightest
 * spacing margin (just 1/9 of total radius between ring 7 and 8), so
 * any decent amplitude there would pull a lobe of one ring through
 * its neighbour. Looked like a tangle of yarn.
 *
 * Radial ordering is now preserved by construction with two rules:
 *
 *   1. Static-noise ring phases are 4× smaller than the title-bar
 *      mark's. Adjacent rings see almost the same noise pattern, so
 *      their per-angle values stay tightly correlated — they can't
 *      diverge enough to cross. (Slight loss of per-ring variety vs
 *      the title bar; barely visible at any size.)
 *
 *   2. The time-driven motion (the "alive" part) uses ring-independent
 *      phase. The dynamic component is the SAME value for every ring
 *      at every angle and time, so it cancels exactly when comparing
 *      ring i vs ring i+1 — it can't break the ordering the static
 *      component already establishes.
 *
 * Variety between rings still comes from the static noise + each
 * ring's own baseRadius. Inner rings (where spacing margin is large)
 * naturally show more relative wobble; outer rings stay calm. Same
 * "stable peak, breathing skirts" reading as the static mark.
 *
 * Path data is rewritten in place via setAttribute each frame; React
 * only owns the <svg> shell, so per-frame cost stays flat with the
 * number of rings and there's no reconciliation overhead.
 */
export function AnimatedTopographicMark({
  size,
  intensity = 1,
  gravityPullRef,
  expandProgressRef,
  className,
}: {
  /** Display size in CSS pixels (the SVG is a 100×100 viewBox internally). */
  size: number
  /** 0–1 multiplier on the time-driven motion. 0 = static; 1 = full breath. */
  intensity?: number
  /** Optional ref to a 0–1 gravity-well factor. Drives a propagating
   *  inward collapse: rings near the centre activate first, outer
   *  rings later, each collapsing to a near-point at peak. */
  gravityPullRef?: React.MutableRefObject<number>
  /** Optional ref to a 0–1 expansion factor — the visual *reverse* of
   *  the gravity well. At expandProgress = 0 every ring sits at a
   *  near-point; as it climbs to 1, inner rings emerge to natural
   *  radius first, propagating outward ring-by-ring until the icon
   *  is fully formed. Use to animate the icon "growing out" from
   *  nothing. */
  expandProgressRef?: React.MutableRefObject<number>
  /** Tailwind class for `currentColor` — controls stroke colour. */
  className?: string
}) {
  const svgRef = useRef<SVGSVGElement>(null)
  const intensityRef = useRef(intensity)
  intensityRef.current = intensity

  useEffect(() => {
    const svg = svgRef.current
    if (!svg) return

    const reducedMotion =
      typeof window !== 'undefined' &&
      window.matchMedia?.('(prefers-reduced-motion: reduce)').matches

    const VB = 100
    const CENTER = VB / 2
    const RINGS = 9
    const STEPS = 48
    const SVG_NS = 'http://www.w3.org/2000/svg'

    const paths: SVGPathElement[] = []
    for (let i = 0; i < RINGS; i++) {
      const path = document.createElementNS(SVG_NS, 'path')
      path.setAttribute('fill', 'none')
      path.setAttribute('stroke', 'currentColor')
      path.setAttribute('stroke-width', '1.4')
      path.setAttribute('stroke-linejoin', 'round')
      path.setAttribute(
        'stroke-opacity',
        String(0.55 + ((RINGS - 1 - i) / (RINGS - 1)) * 0.45),
      )
      svg.appendChild(path)
      paths.push(path)
    }

    // Static noise with 4× reduced ring-phase scaling (vs the title-bar
    // mark). Adjacent rings stay tightly correlated → never cross.
    // Worst-case adjacent gap in noise space ≈ 0.066, comfortably below
    // the 0.111 ordering-margin between ring 7 and 8.
    const staticNoise = (angle: number, ring: number): number =>
      Math.sin(angle * 3 + ring * 0.088) * 0.09 +
      Math.cos(angle * 2 + ring * 0.150 + 1.2) * 0.07 +
      Math.sin(angle * 5 - ring * 0.050 + 0.8) * 0.04 +
      Math.cos(angle + ring * 0.225 + 2.1) * 0.05

    // Dynamic motion — shared across all rings (no ring parameter in
    // phase). Same value at every ring for the same (angle, t), so it
    // cancels in the ring-i-vs-ring-i+1 ordering comparison. Bounded
    // amplitude ≤ 0.04 keeps things modest.
    const dynamicNoise = (angle: number, t: number): number =>
      Math.sin(angle * 2 + t * 0.7) * 0.025 +
      Math.cos(angle * 3 - t * 0.5 + 1.2) * 0.015

    const drawFrame = (t: number) => {
      const k = intensityRef.current
      const gp = gravityPullRef?.current ?? 0
      // Default ep=1 means "fully expanded / natural" — no distortion.
      // Only ep<1 triggers the expansion compression below.
      const ep = expandProgressRef?.current ?? 1
      // Global breathing — single scalar, applied to every ring →
      // uniform scale doesn't break ordering.
      const breath = 1 + 0.018 * Math.sin(t * 0.5) * k

      for (let i = 0; i < RINGS; i++) {
        const ringT = (i + 1) / RINGS
        const baseRadius = VB * 0.44 * ringT
        const pts: string[] = []
        for (let s = 0; s <= STEPS; s++) {
          const angle = (s / STEPS) * Math.PI * 2
          const sBend = staticNoise(angle, i)
          const dBend = dynamicNoise(angle, t) * k
          const r = baseRadius * breath * (1 + sBend + dBend)
          let x = CENTER + Math.cos(angle) * r
          let y = CENTER + Math.sin(angle) * r

          // ── Propagating gravity-well collapse. Each ring has its
          //    own activation threshold on the gp axis, proportional
          //    to its ringT. Inner rings (low ringT) activate at
          //    very low gp — they start collapsing immediately.
          //    Outer rings have high thresholds — they don't begin
          //    collapsing until gp is well past the inner rings'
          //    activation.
          //
          //    The result: at any moment, the icon shows a *collapse
          //    front* somewhere in its rings. Inside the front, rings
          //    are already consumed (essentially points at centre);
          //    outside, rings are still at natural radius. The front
          //    sweeps outward through the rings as gp grows — like a
          //    black hole consuming matter further and further from
          //    its singularity.
          if (gp > 0) {
            const threshold = ringT * 0.72
            const transitionWidth = 0.22
            const tx = Math.max(0, Math.min(1, (gp - threshold) / transitionWidth))
            const activation = tx * tx * (3 - 2 * tx)
            const compression = activation * 0.95
            const ratio = 1 - compression
            x = CENTER + (x - CENTER) * ratio
            y = CENTER + (y - CENTER) * ratio
          } else if (ep < 1) {
            // ── Propagating outward expansion — visual inverse of the
            //    gravity well. Same threshold pattern (innermost
            //    activates first), but compression is INVERTED:
            //    activation 0 means ring is at near-point, activation
            //    1 means ring is at natural radius. As ep climbs 0→1,
            //    inner rings reach their threshold first → grow first.
            //    Outer rings emerge last. The icon "blooms outward"
            //    from a singular dot, ring after ring.
            const threshold = ringT * 0.72
            const transitionWidth = 0.22
            const tx = Math.max(0, Math.min(1, (ep - threshold) / transitionWidth))
            const activation = tx * tx * (3 - 2 * tx)
            const compression = (1 - activation) * 0.95
            const ratio = 1 - compression
            x = CENTER + (x - CENTER) * ratio
            y = CENTER + (y - CENTER) * ratio
          }

          pts.push(`${x.toFixed(2)},${y.toFixed(2)}`)
        }
        paths[i].setAttribute('d', `M${pts[0]} L${pts.slice(1).join(' L')} Z`)
      }
    }

    if (reducedMotion) {
      drawFrame(0)
      return () => {
        for (const p of paths) p.remove()
      }
    }

    let raf = 0
    let disposed = false
    const start = performance.now()
    const loop = (now: number) => {
      if (disposed) return
      drawFrame((now - start) * 0.001)
      raf = requestAnimationFrame(loop)
    }
    raf = requestAnimationFrame(loop)

    return () => {
      disposed = true
      cancelAnimationFrame(raf)
      for (const p of paths) p.remove()
    }
  }, [])

  return (
    <svg
      ref={svgRef}
      width={size}
      height={size}
      viewBox="0 0 100 100"
      // The gravity-well effect can push ring vertices well below
      // y=100; without this, stretched rings get clipped at the
      // viewBox boundary.
      style={{ overflow: 'visible' }}
      className={className}
      aria-hidden
    />
  )
}
