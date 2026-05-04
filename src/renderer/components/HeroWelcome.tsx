import { useHarness } from '../state/store'
import { TopographyMesh } from './TopographyMesh'
import { TopoWordmark } from './TopoWordmark'


export function HeroWelcome() {
  const mode = useHarness((s) => s.mode)
  const requestDemoBurst = useHarness((s) => s.requestDemoBurst)
  const setInputDraft = useHarness((s) => s.setInputDraft)
  const sendMessage = useHarness((s) => s.sendMessage)

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
          <div className="font-mono text-[12px] text-fg-0">~/work/services/freyja</div>
          <div className="mt-1 text-[11px] text-fg-2">main* · 0 unstaged · writable</div>
        </Card>
        <Card title="Quick start">
          <div className="text-[12px] text-fg-1">Type a message below — or try:</div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            {['/help', '/skills', '/subagents', '/usage'].map((c) => (
              <span key={c} className="chip font-mono text-accent/90">
                {c}
              </span>
            ))}
          </div>
        </Card>
        <Card title="Jump-off prompts" wide>
          <div className="space-y-1.5">
            {suggestions.map((s) => (
              <button
                key={s}
                onClick={() => {
                  setInputDraft(s)
                  sendMessage(s)
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
            Tip: <kbd className="kbd">⌘</kbd> <kbd className="kbd">O</kbd> opens the subagent panel
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

/**
 * Diagonal hatched bar — the "////" end-caps flanking the kicker.
 * Pure SVG so the strokes stay crisp regardless of devicePixelRatio.
 * Matches the corner hatching in the CATHEDRAL reference poster.
 */
function HatchBar() {
  return (
    <svg
      width="34"
      height="10"
      viewBox="0 0 34 10"
      className="shrink-0 text-fg-2/70"
      aria-hidden
    >
      {[0, 4, 8, 12, 16, 20, 24, 28].map((x) => (
        <line
          key={x}
          x1={x}
          y1={10}
          x2={x + 8}
          y2={0}
          stroke="currentColor"
          strokeWidth="1"
          strokeLinecap="square"
        />
      ))}
    </svg>
  )
}
