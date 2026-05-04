import { useHarness } from '../state/store'
import { Spinner } from '../lib/spinner'

/**
 * Shown at the top of the Conversation panel whenever the active session
 * has a `parentSessionId`. Mirrors Slate's `SubagentSessionIndicator` —
 * tells the user they're nested inside a sub-agent's session and gives a
 * one-click (or `⌘B`) return to the parent.
 */
export function ChildSessionBreadcrumb() {
  const activeSessionId = useHarness((s) => s.activeSessionId)
  const sessions = useHarness((s) => s.sessions)
  const switchToParent = useHarness((s) => s.switchToParent)

  const active = sessions.find((s) => s.id === activeSessionId)
  if (!active?.parentSessionId) return null
  const parent = sessions.find((s) => s.id === active.parentSessionId)

  return (
    <div className="mx-auto w-full max-w-[820px] px-8 pt-5">
      <button
        onClick={() => switchToParent()}
        className="group flex w-full items-center gap-3 rounded-lg glass-raised px-3 py-2 text-left ring-1 ring-accent/20 hover:ring-accent/40"
      >
        <Spinner name="scan" className="text-accent" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="label text-accent">sub-agent session</span>
            <span className="text-fg-3">·</span>
            <span className="font-mono text-[10.5px] text-fg-1">
              {active.title}
            </span>
            {active.completed && (
              <span className="label ml-1 text-ok">done</span>
            )}
          </div>
          <div className="mt-[2px] flex items-center gap-1.5 text-[10.5px] text-fg-2">
            <span className="font-mono text-fg-3">parent</span>
            <span className="truncate font-mono text-fg-1">
              {parent?.title ?? active.parentSessionId}
            </span>
            <span>·</span>
            <span className="font-mono">{active.model.replace('claude-', '')}</span>
          </div>
        </div>
        <div className="flex items-center gap-1.5 text-[10px] text-fg-3">
          <kbd className="kbd">⌘</kbd>
          <kbd className="kbd">B</kbd>
          <span>back</span>
        </div>
      </button>
    </div>
  )
}
