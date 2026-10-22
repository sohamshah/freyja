/**
 * TopographicMark — concentric closed-contour lines rendered as SVG,
 * inspired by the drift/ring topographies in `workflow-dither-concepts.html`
 * and the organic tree-ring/elevation references. The shape is
 * generated deterministically from a sum-of-sines perturbation so it's
 * stable across reloads and scales cleanly at any size.
 */
export function TopographicMark({ size = 22 }: { size?: number }) {
  const vb = 100
  const center = vb / 2
  const RINGS = 9

  // Deterministic organic noise — sum of phase-shifted sines and
  // cosines at different frequencies. Keeps inner and outer rings
  // correlated so the whole thing reads as a single landform instead
  // of random noise at each level.
  const noise = (angle: number, ring: number): number =>
    Math.sin(angle * 3 + ring * 0.35) * 0.09 +
    Math.cos(angle * 2 + ring * 0.6 + 1.2) * 0.07 +
    Math.sin(angle * 5 - ring * 0.2 + 0.8) * 0.04 +
    Math.cos(angle + ring * 0.9 + 2.1) * 0.05

  const paths: string[] = []
  for (let i = 0; i < RINGS; i++) {
    const t = (i + 1) / RINGS // 1/9 … 1.0
    const baseRadius = vb * 0.44 * t
    const steps = 48
    const points: string[] = []
    for (let s = 0; s <= steps; s++) {
      const angle = (s / steps) * Math.PI * 2
      const r = baseRadius * (1 + noise(angle, i))
      const x = center + Math.cos(angle) * r
      const y = center + Math.sin(angle) * r
      points.push(`${x.toFixed(2)},${y.toFixed(2)}`)
    }
    paths.push(`M${points[0]} L${points.slice(1).join(' L')} Z`)
  }

  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${vb} ${vb}`}
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinejoin="round"
      className="text-accent"
      aria-hidden
    >
      {paths.map((d, i) => (
        <path
          key={i}
          d={d}
          // Outer rings fade slightly so the mark reads as a peak — the
          // innermost ring is solid, outermost is at ~55% opacity.
          strokeOpacity={0.55 + ((RINGS - 1 - i) / (RINGS - 1)) * 0.45}
        />
      ))}
    </svg>
  )
}
