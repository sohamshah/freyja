import { useHarness } from '../state/store'
import { formatTokens, formatCost } from '../lib/format'
import { Spinner } from '../lib/spinner'
import type { CoordinationStrategy } from '@shared/events'

const STRATEGIES: Array<{
  id: CoordinationStrategy
  label: string
  title: string
}> = [
  {
    id: 'bus',
    label: 'bus',
    title: 'Message bus: profile agents can publish/read shared findings',
  },
  {
    id: 'isolated',
    label: 'tasks',
    title: 'Tasks: parent-led work with a visible task ledger and optional workers',
  },
  {
    id: 'kanban',
    label: 'board',
    title: 'Kanban: cards, dependencies, handoffs, and worker progress',
  },
  {
    id: 'goal',
    label: 'goal',
    title: 'Goal loop: keep the same session moving until the active objective is done',
  },
]

export function TitleBar() {
  const mode = useHarness((s) => s.mode)
  const modeDetail = useHarness((s) => s.modeDetail)
  const model = useHarness((s) => s.model)
  const reasoningLevel = useHarness((s) => s.reasoningLevel)
  const coordinationStrategy = useHarness((s) => s.coordinationStrategy)
  const messages = useHarness((s) => s.messages)
  const usage = useHarness((s) => s.usage)
  const sessionId = useHarness((s) => s.activeSessionId)
  const isStreaming = useHarness((s) => s.isStreaming)
  const missionDashboardOpen = useHarness((s) => s.missionDashboardOpen)
  const sidebarCollapsed = useHarness((s) => s.sidebarCollapsed)
  const activityPanelCollapsed = useHarness((s) => s.activityPanelCollapsed)
  const focusMode = useHarness((s) => s.focusMode)
  const toggleMissionDashboard = useHarness((s) => s.toggleMissionDashboard)
  const toggleSidebar = useHarness((s) => s.toggleSidebar)
  const toggleActivityPanel = useHarness((s) => s.toggleActivityPanel)
  const toggleFocusMode = useHarness((s) => s.toggleFocusMode)
  const setCoordinationStrategy = useHarness((s) => s.setCoordinationStrategy)
  const strategyLocked = messages.length > 0 || isStreaming
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
        className="no-drag hidden h-[28px] px-2.5 text-[10px] sm:inline-flex"
        onClick={() => toggleSidebar()}
        title="Toggle workspace sidebar (⌘[)"
        active={!sidebarCollapsed}
      >
        <span className="font-mono uppercase">workspace</span>
      </TitleControl>
      <TitleControl
        className="no-drag h-[28px] px-2.5 text-[10px]"
        onClick={() => toggleMissionDashboard(true, 'overview')}
        title="Open mission dashboard (⌘⇧M)"
        active={missionDashboardOpen}
      >
        <span className="font-mono uppercase">dashboard</span>
      </TitleControl>
      <TitleControl
        className="no-drag flex h-[28px] max-w-[min(36vw,380px)] py-0 pl-2.5 pr-2 text-fg-1"
        onClick={() => useHarness.getState().toggleModelPicker(true)}
        title="Switch model"
      >
        <span className="title-kicker">model</span>
        <span className="ml-2 min-w-0 truncate font-mono text-fg-0">{model}</span>
        {reasoningLevel && reasoningLevel !== 'none' && (
          <span className="title-effort ml-2 font-mono text-[10px]">{reasoningLevel}</span>
        )}
        {reasoningLevel === 'none' && (
          <span className="title-effort title-effort-muted ml-2 font-mono text-[10px]">
            no-reasoning
          </span>
        )}
        <span className="ml-2 text-fg-3">▾</span>
      </TitleControl>
      <div
        className={`no-drag title-strategy hidden h-[28px] items-center gap-0.5 px-1 lg:flex ${
          strategyLocked ? 'title-strategy-locked' : ''
        }`}
        title={
          strategyLocked
            ? 'Coordination strategy is locked after the first message. Start a new session to choose another strategy.'
            : 'Sub-agent coordination strategy for this session'
        }
      >
        {STRATEGIES.map((strategy) => (
          <button
            key={strategy.id}
            className={`title-strategy-option ${
              coordinationStrategy === strategy.id ? 'title-strategy-option-active' : ''
            }`}
            type="button"
            disabled={strategyLocked}
            title={strategy.title}
            onClick={() => setCoordinationStrategy(strategy.id)}
          >
            {strategy.label}
          </button>
        ))}
      </div>
      <TitleControl
        className="no-drag hidden h-[28px] px-2.5 text-[10px] lg:inline-flex"
        onClick={() => toggleFocusMode()}
        title="Focus mode (⌘\\)"
        active={focusMode}
      >
        <span className="font-mono uppercase">{focusMode ? 'restore' : 'focus'}</span>
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
        <TitleControl
          className="no-drag hidden h-[28px] px-2.5 text-[10px] xl:inline-flex"
          onClick={() => toggleActivityPanel()}
          title="Toggle activity panel (⌘])"
          active={!activityPanelCollapsed}
        >
          <span className="font-mono uppercase">activity</span>
        </TitleControl>
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
  active = false,
}: {
  children: React.ReactNode
  className?: string
  onClick?: () => void
  title?: string
  accent?: boolean
  active?: boolean
}) {
  const surfaceClass = onClick
    ? `title-control title-control-button ${accent ? 'title-control-accent' : ''} ${
        active ? 'title-control-active' : ''
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
