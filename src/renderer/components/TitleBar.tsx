import { useHarness } from '../state/store'
import { formatTokens, formatCost } from '../lib/format'
import { Spinner } from '../lib/spinner'

export function TitleBar() {
  const mode = useHarness((s) => s.mode)
  const modeDetail = useHarness((s) => s.modeDetail)
  const model = useHarness((s) => s.model)
  const reasoningLevel = useHarness((s) => s.reasoningLevel)
  const usage = useHarness((s) => s.usage)
  const sessionId = useHarness((s) => s.activeSessionId)
  const isStreaming = useHarness((s) => s.isStreaming)
  const toggleMissionDashboard = useHarness((s) => s.toggleMissionDashboard)
  const contextKnown = usage.currentContextTokens > 0 || usage.totalInputTokens <= usage.contextWindow
  const contextTokens = usage.currentContextTokens > 0
    ? usage.currentContextTokens
    : contextKnown
      ? usage.totalInputTokens
      : 0
  const ctxPct = Math.min(100, Math.round((contextTokens / usage.contextWindow) * 100))

  return (
    <div className="drag hairline-b flex h-[46px] shrink-0 items-center gap-3 pl-[82px] pr-4 text-[12px] text-fg-1">
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
      <BridgeStatus mode={mode} modeDetail={modeDetail} />
      <TitleControl
        variant="aperture"
        className="no-drag hidden h-[30px] px-3 text-[10.5px] sm:inline-flex"
        onClick={() => toggleMissionDashboard(true, 'overview')}
        title="Open mission dashboard (⌘⇧M)"
      >
        <span className="title-aperture-dot" aria-hidden="true" />
        <span className="font-mono uppercase">dashboard</span>
      </TitleControl>
      <TitleControl
        variant="cartridge"
        className="no-drag flex h-[30px] max-w-[min(36vw,380px)] py-0 pl-3 pr-2 text-fg-1"
        onClick={() => useHarness.getState().toggleModelPicker(true)}
        title="Switch model"
      >
        <span className="title-cartridge-kicker">model</span>
        <span className="title-cartridge-name ml-2 min-w-0 truncate font-mono text-fg-0">{model}</span>
        {reasoningLevel && reasoningLevel !== 'none' && (
          <span className="title-cartridge-effort ml-2 font-mono text-[10px]">{reasoningLevel}</span>
        )}
        {reasoningLevel === 'none' && (
          <span className="title-cartridge-effort title-cartridge-effort-muted ml-2 font-mono text-[10px]">
            no-reasoning
          </span>
        )}
        <span className="title-cartridge-chevron ml-2 text-fg-3">▾</span>
      </TitleControl>
      <TitleControl className="flex h-[30px] px-3">
        <span className="text-fg-2">ctx</span>
        <span className="ml-1.5 font-mono text-fg-0">
          {contextKnown ? formatTokens(contextTokens) : 'n/a'}
          <span className="text-fg-2">/{formatTokens(usage.contextWindow)}</span>
        </span>
        <ProgressBar pct={ctxPct} className="ml-2 w-[56px]" />
      </TitleControl>
      <TitleControl className="flex h-[30px] px-3">
        <span className="text-fg-2">spend</span>
        <span className="ml-1.5 font-mono text-fg-0">{formatCost(usage.totalCost)}</span>
      </TitleControl>
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

function BridgeStatus({ mode, modeDetail }: { mode: string; modeDetail: string }) {
  const toneClass =
    mode === 'live'
      ? 'text-ok'
      : mode === 'demo'
        ? 'text-warn'
        : 'text-danger'
  const label = mode === 'live' ? 'live' : mode === 'demo' ? 'demo' : 'offline'

  return (
    <div
      className={`title-readout title-readout-status flex h-[30px] items-center gap-1.5 px-1 font-mono text-[10px] uppercase ${toneClass}`}
      title={modeDetail}
    >
      <span className="title-status-dot inline-block h-[6px] w-[6px] rounded-full bg-current" />
      <span>{label}</span>
    </div>
  )
}

function TitleControl({
  children,
  className = '',
  onClick,
  title,
  accent = false,
  variant,
}: {
  children: React.ReactNode
  className?: string
  onClick?: () => void
  title?: string
  accent?: boolean
  variant?: 'aperture' | 'cartridge'
}) {
  const surfaceClass = onClick
    ? `title-control title-control-button ${accent ? 'title-control-accent' : ''} ${
        variant ? `title-control-${variant}` : ''
      }`
    : 'title-readout'
  const controlClass = `${surfaceClass} items-center ${className}`

  if (onClick) {
    return (
      <button className={controlClass} onClick={onClick} title={title} type="button">
        {children}
      </button>
    )
  }

  return (
    <div className={controlClass} title={title}>
      {children}
    </div>
  )
}

function ProgressBar({ pct, className = '' }: { pct: number; className?: string }) {
  return (
    <span className={`title-progress relative block ${className}`}>
      <span
        className="absolute left-0 top-0 h-full"
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
