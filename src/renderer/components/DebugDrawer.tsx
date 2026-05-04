import { useHarness } from '../state/store'
import { relativeTime } from '../lib/format'

export function DebugDrawer() {
  const open = useHarness((s) => s.debugOpen)
  const toggle = useHarness((s) => s.toggleDebug)
  const logs = useHarness((s) => s.logs)
  const mode = useHarness((s) => s.mode)
  const tools = useHarness((s) => s.toolCatalog)
  const subagents = useHarness((s) => s.subagents)

  if (!open) return null

  return (
    <div className="fixed inset-0 z-30 flex justify-end">
      <div className="absolute inset-0 bg-black/40 backdrop-blur-[1px]" onClick={() => toggle(false)} />
      <div className="relative flex h-full w-[480px] flex-col glass-strong ring-hairline-strong">
        <div className="flex items-center justify-between px-4 py-3 hairline-b">
          <div className="label">debug</div>
          <button
            onClick={() => toggle(false)}
            className="rounded bg-white/[0.05] px-2 py-[3px] font-mono text-[10px] uppercase tracking-[0.08em] text-fg-1 ring-hairline hover:bg-white/[0.08]"
          >
            close ✕
          </button>
        </div>
        <div className="flex-1 space-y-4 overflow-y-auto px-4 py-4">
          <Section title={`mode: ${mode}`}>
            <div className="font-mono text-[11px] text-fg-1">
              tool catalog: {Object.keys(tools).length} · subagents: {Object.keys(subagents).length}
            </div>
          </Section>

          <Section title="tool catalog">
            {Object.values(tools).length === 0 && (
              <div className="font-mono text-[11px] italic text-fg-3">
                run /tools to populate
              </div>
            )}
            {Object.values(tools).map((t) => (
              <div key={t.name} className="rounded bg-white/[0.025] px-2 py-1.5 ring-hairline">
                <div className="flex items-baseline gap-2">
                  <span className="font-mono text-[11px] text-accent">{t.name}</span>
                  <span className="font-mono text-[9.5px] uppercase text-fg-3">{t.tier}</span>
                </div>
                <div className="mt-0.5 text-[11px] text-fg-1">{t.summary}</div>
              </div>
            ))}
          </Section>

          <Section title="log stream">
            <div className="space-y-0.5 font-mono text-[10.5px]">
              {logs.slice(-80).reverse().map((l, i) => (
                <div key={i} className="flex gap-2">
                  <span
                    className={
                      l.level === 'error'
                        ? 'text-danger'
                        : l.level === 'warn'
                          ? 'text-warn'
                          : l.level === 'info'
                            ? 'text-ok'
                            : 'text-fg-2'
                    }
                  >
                    {l.level}
                  </span>
                  <span className="flex-1 truncate text-fg-1">{l.message}</span>
                  <span className="text-fg-3">{relativeTime(l.at)}</span>
                </div>
              ))}
            </div>
          </Section>
        </div>
      </div>
    </div>
  )
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <div className="mb-1.5 label">{title}</div>
      <div className="space-y-1">{children}</div>
    </div>
  )
}
