import { useHarness } from '../state/store'
import { formatTokens, formatCost } from '../lib/format'
import { Spinner } from '../lib/spinner'

export function TitleBar() {
  const mode = useHarness((s) => s.mode)
  const modeDetail = useHarness((s) => s.modeDetail)
  const model = useHarness((s) => s.model)
  const usage = useHarness((s) => s.usage)
  const sessionId = useHarness((s) => s.activeSessionId)
  const isStreaming = useHarness((s) => s.isStreaming)
  const ctxPct = Math.min(100, Math.round((usage.totalInputTokens / usage.contextWindow) * 100))

  return (
    <div className="drag hairline-b flex h-[44px] shrink-0 items-center gap-4 pl-[82px] pr-4 text-[12px] text-fg-1">
      <div className="flex items-center gap-2 text-fg-0">
        <TopographicMark />
        <span
          className="text-fg-0"
          style={{
            background:
              'linear-gradient(180deg, #7ae6ff 0%, #a8d4fc 40%, #b4b0ff 80%, #c89aff 100%)',
            WebkitBackgroundClip: 'text',
            WebkitTextFillColor: 'transparent',
            backgroundClip: 'text',
          }}
        >
          freyja
        </span>
      </div>
      <div className="h-[14px] w-px bg-white/10" />
      <Pill tone={mode === 'live' ? 'ok' : mode === 'demo' ? 'warn' : 'danger'}>
        <span className="inline-block h-[6px] w-[6px] rounded-full bg-current animate-pulse-soft" />
        <span className="ml-1.5">{modeDetail}</span>
      </Pill>
      <button
        className="no-drag flex items-center rounded-md bg-white/[0.035] px-2 py-[3px] text-fg-1 ring-hairline hover:bg-white/[0.07]"
        onClick={() => useHarness.getState().toggleModelPicker(true)}
        title="Switch model"
      >
        <span className="text-fg-2">model</span>
        <span className="ml-1.5 font-mono text-fg-0">{model}</span>
        <span className="ml-1.5 text-fg-3">▾</span>
      </button>
      <Pill>
        <span className="text-fg-2">ctx</span>
        <span className="ml-1.5 font-mono text-fg-0">
          {formatTokens(usage.totalInputTokens)}
          <span className="text-fg-2">/{formatTokens(usage.contextWindow)}</span>
        </span>
        <ProgressBar pct={ctxPct} className="ml-2 w-[44px]" />
      </Pill>
      <Pill>
        <span className="text-fg-2">spend</span>
        <span className="ml-1.5 font-mono text-fg-0">{formatCost(usage.totalCost)}</span>
      </Pill>
      <div className="ml-auto flex items-center gap-3 text-fg-2">
        {isStreaming && (
          <span className="flex items-center gap-1.5 font-mono text-[10.5px] uppercase tracking-[0.12em]">
            <Spinner name="braillewave" className="text-accent" />
            streaming
          </span>
        )}
        <span className="font-mono text-[10.5px] text-fg-2">{sessionId.slice(0, 14)}</span>
      </div>
    </div>
  )
}

function Pill({
  children,
  tone = 'neutral',
}: {
  children: React.ReactNode
  tone?: 'neutral' | 'ok' | 'warn' | 'danger'
}) {
  const toneClass =
    tone === 'ok'
      ? 'text-ok'
      : tone === 'warn'
        ? 'text-warn'
        : tone === 'danger'
          ? 'text-danger'
          : 'text-fg-1'
  return (
    <div
      className={`no-drag flex items-center rounded-md bg-white/[0.035] px-2 py-[3px] ring-hairline ${toneClass}`}
    >
      {children}
    </div>
  )
}

function ProgressBar({ pct, className = '' }: { pct: number; className?: string }) {
  return (
    <span className={`relative block h-1 overflow-hidden rounded-full bg-white/10 ${className}`}>
      <span
        className="absolute left-0 top-0 h-full bg-accent/70"
        style={{ width: `${pct}%` }}
      />
    </span>
  )
}

/**
 * TopographicMark — concentric closed-contour lines rendered as SVG,
 * inspired by the drift/ring topographies in `workflow-dither-concepts.html`
 * and the organic tree-ring/elevation references. The shape is
 * generated deterministically from a sum-of-sines perturbation so it's
 * stable across reloads and scales cleanly at any size.
 */
function TopographicMark({ size = 22 }: { size?: number }) {
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
