import { useHarness } from '../state/store'
import { formatDuration, formatTokens } from '../lib/format'

export function SubagentDetail({ id }: { id: string }) {
  const sub = useHarness((s) => s.subagents[id])
  const close = useHarness((s) => s.openSubagent)
  if (!sub) return null

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/50 backdrop-blur-[2px]" onClick={() => close(null)} />
      <div className="relative w-[640px] max-w-[90vw] overflow-hidden rounded-2xl glass-strong shadow-2xl ring-hairline-strong">
        <div className="flex items-center gap-3 px-5 py-4 hairline-b">
          <div className="flex items-center gap-2">
            <span className="block h-2 w-2 rounded-full bg-accent" />
            <span className="label">
              subagent
            </span>
            <span className="font-mono text-[11px] text-fg-1">{sub.mode}</span>
            <span className="font-mono text-[11px] text-fg-1">{sub.state}</span>
          </div>
          <div className="ml-auto flex items-center gap-2">
            <button
              onClick={() => close(null)}
              className="rounded bg-white/[0.05] px-2.5 py-[3px] text-[11px] text-fg-1 ring-hairline hover:bg-white/[0.08]"
            >
              close ✕
            </button>
          </div>
        </div>
        <div className="px-5 py-5">
          <div className="mb-4">
            <div className="mb-1 label">label</div>
            <div className="text-[13px] text-fg-0">{sub.label}</div>
          </div>
          <div className="mb-4">
            <div className="mb-1 label">task</div>
            <div className="selectable rounded-md bg-black/45 p-3 text-[12.5px] leading-[1.55] text-fg-1 ring-hairline">
              {sub.task}
            </div>
          </div>
          <div className="mb-4 grid grid-cols-4 gap-2">
            <Stat label="elapsed" value={formatDuration(sub.elapsedMs)} />
            <Stat label="tokens in" value={formatTokens(sub.tokensIn)} />
            <Stat label="tokens out" value={formatTokens(sub.tokensOut)} />
            <Stat label="tools" value={String(sub.toolsCalled)} />
          </div>
          {sub.result && (
            <div>
              <div className="mb-1 label">
                result
              </div>
              <div className="selectable rounded-md bg-black/45 p-3 text-[12.5px] leading-[1.6] text-fg-0 ring-hairline">
                {sub.result}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded bg-white/[0.03] px-2 py-2 ring-hairline">
      <div className="text-[9.5px] uppercase tracking-[0.12em] text-fg-3">{label}</div>
      <div className="mt-1 font-mono text-[13px] text-fg-0">{value}</div>
    </div>
  )
}
