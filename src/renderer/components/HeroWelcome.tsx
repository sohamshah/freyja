import { useHarness } from '../state/store'
import { TopoWordmark } from './TopoWordmark'


export function HeroWelcome() {
  const mode = useHarness((s) => s.mode)
  const requestDemoBurst = useHarness((s) => s.requestDemoBurst)
  const setInputDraft = useHarness((s) => s.setInputDraft)
  const toggleMissionDashboard = useHarness((s) => s.toggleMissionDashboard)
  const activeSessionId = useHarness((s) => s.activeSessionId)
  const sessions = useHarness((s) => s.sessions)
  const model = useHarness((s) => s.model)

  const activeSession = sessions.find((session) => session.id === activeSessionId)
  const workspace = compactPath(activeSession?.workspace || '~/')
  const sessionModel = activeSession?.model || model
  const messageCount = activeSession?.messageCount ?? 0

  const suggestions = [
    'Map the architecture and show me how subagents interact with the skills system',
    'Walk me through a single turn of the runner loop',
    'What skills do we have and which are most trusted?',
    'Run a dry pass and show me every tool the harness exposes',
  ]

  const triggerBurst = () => {
    const api = (window as any).harness
    if (api) requestDemoBurst()
    else (window as any).__harnessDemo?.burst()
  }

  return (
    <div className="mx-auto flex w-full max-w-[940px] flex-col items-center px-8 pt-14 pb-10">
      {/* ── Identity block. Kicker + status rows are Departure Mono;
          the wordmark is a live Canvas2D topographic contour mark
          that shares the same noise vocabulary as the logo. */}
      <div className="mb-6 flex w-full flex-col items-center gap-4">
        <TopoWordmark />
      </div>

      <div className="grid w-full grid-cols-1 gap-3 sm:grid-cols-2">
        <Card title="Workspace">
          <div className="truncate font-mono text-[12px] text-fg-0">{workspace}</div>
          <div className="mt-1 text-[11px] text-fg-2">
            {sessionModel} · {messageCount > 0 ? `${messageCount} messages` : 'empty session'}
          </div>
        </Card>
        <Card title="Quick start">
          <div className="text-[12px] text-fg-1">Start from the input below, or open the current session map.</div>
          <button
            onClick={() => toggleMissionDashboard(true, 'overview')}
            className="mt-3 rounded-md bg-accent/10 px-2.5 py-1.5 font-mono text-[10px] uppercase tracking-[0.08em] text-accent ring-1 ring-accent/25 hover:bg-accent/18"
          >
            open mission dashboard
          </button>
        </Card>
        <Card title="Jump-off prompts" wide>
          <div className="space-y-1.5">
            {suggestions.map((s) => (
              <button
                key={s}
                onClick={() => {
                  setInputDraft(s)
                }}
                className="block w-full rounded-lg bg-white/[0.025] px-3 py-2 text-left text-[12px] text-fg-1 ring-hairline hover:bg-white/[0.055] hover:text-fg-0"
              >
                <span className="text-accent">▸ </span>
                {s}
              </button>
            ))}
          </div>
        </Card>
      </div>

      <div className="mt-8 flex w-full flex-col items-center gap-2 text-[11px] text-fg-2">
        <div className="flex items-center gap-2">
          <span className="inline-block h-1 w-1 rounded-full bg-accent" />
          <span>
            Tip: <kbd className="kbd">⌘</kbd> <kbd className="kbd">⇧</kbd> <kbd className="kbd">M</kbd> opens the mission dashboard
          </span>
        </div>
        {mode === 'demo' && (
          <button
            onClick={triggerBurst}
            className="mt-2 rounded-md bg-accent/10 px-3 py-1 text-[11px] text-accent ring-1 ring-accent/30 hover:bg-accent/20"
          >
            ▸ trigger demo burst (⌘B)
          </button>
        )}
      </div>
    </div>
  )
}

function compactPath(path: string): string {
  return path.replace(/^\/Users\/[^/]+/, '~')
}

function Card({ title, children, wide }: { title: string; children: React.ReactNode; wide?: boolean }) {
  return (
    <div
      className={`rounded-xl glass-raised p-4 ${wide ? 'sm:col-span-2' : ''}`}
    >
      <div className="mb-2 flex items-center gap-2 label">
        <span className="inline-block h-1 w-1 rounded-full bg-accent" />
        {title}
      </div>
      {children}
    </div>
  )
}
