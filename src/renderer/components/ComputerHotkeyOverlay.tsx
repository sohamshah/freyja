import { useEffect, useRef, useState } from 'react'
import { useHarness } from '../state/store'

/**
 * Floating ⌘⇧K overlay — Vy-style "summon" prompt for a computer task.
 *
 * Opens in response to a `system_event` of subtype
 * `open_computer_hotkey` (main.ts dispatches that when the global
 * shortcut fires). One input, two buttons: run (enter) or cancel
 * (escape). On run, synthesizes a `/computer <goal>` message in the
 * active session so the parent agent spins up a computer_use
 * sub-agent.
 *
 * If computer control is disabled, the overlay shows an inline
 * enable-it affordance instead of the input. Nudges the user toward
 * Settings with a single click.
 */
export function ComputerHotkeyOverlay() {
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const enabled = useHarness((s) => s.settings.computer.enabled)
  const sendMessage = useHarness((s) => s.sendMessage)
  const toggleSettings = useHarness((s) => s.toggleSettings)

  // Listen for the main-process-dispatched open event. The bridge IPC
  // already pipes system_event into the store, but we also need the
  // component to react. Subscribe to the store's system event stream.
  useEffect(() => {
    const unsub = useHarness.subscribe((state, prevState) => {
      if (state.systemEvents === prevState.systemEvents) return
      const last = state.systemEvents[state.systemEvents.length - 1]
      if (last && last.subtype === 'open_computer_hotkey') {
        setOpen(true)
      }
    })
    return unsub
  }, [])

  useEffect(() => {
    if (open) {
      setDraft('')
      setTimeout(() => inputRef.current?.focus(), 50)
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        setOpen(false)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  if (!open) return null

  const run = () => {
    const goal = draft.trim()
    if (!goal) return
    if (!enabled) {
      toggleSettings(true)
      setOpen(false)
      return
    }
    sendMessage(
      `Use the \`computer_use\` tool with goal: "${goal}". Watch mode is on.`,
    )
    setOpen(false)
  }

  return (
    <div className="fixed inset-0 z-[70] flex items-start justify-center pt-[14vh]">
      <div className="absolute inset-0 bg-black/40 backdrop-blur-[3px]" onClick={() => setOpen(false)} />
      <div className="relative w-[560px] overflow-hidden rounded-2xl glass-strong shadow-2xl ring-hairline-strong">
        <div className="flex items-center gap-3 px-5 py-3 hairline-b">
          <span className="label text-warn">computer</span>
          <span className="label text-fg-2">what should it do?</span>
          <span className="ml-auto font-mono text-[9px] text-fg-3">⌘⇧K</span>
        </div>
        <div className="px-5 py-4">
          {enabled ? (
            <input
              ref={inputRef}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  run()
                }
              }}
              placeholder="e.g. open Linear, filter to P1 issues, read me the top 3…"
              className="w-full rounded-md bg-black/40 px-3 py-2.5 font-mono text-[12.5px] text-fg-0 placeholder:text-fg-3 ring-hairline focus:outline-none focus:ring-1 focus:ring-warn/50"
              autoFocus
            />
          ) : (
            <div className="rounded-md bg-warn/10 p-3 text-[12px] leading-[1.55] text-fg-1 ring-1 ring-warn/25">
              Computer control is disabled. Open Settings to enable it
              (requires Screen Recording + Accessibility permissions).
              <div className="mt-3 flex gap-2">
                <button
                  onClick={() => setOpen(false)}
                  className="rounded-md bg-white/[0.04] px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.08em] text-fg-1 ring-hairline hover:bg-white/[0.08]"
                >
                  cancel
                </button>
                <button
                  onClick={() => {
                    toggleSettings(true)
                    setOpen(false)
                  }}
                  className="rounded-md bg-warn/15 px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.08em] text-warn ring-1 ring-warn/40 hover:bg-warn/25"
                >
                  open settings →
                </button>
              </div>
            </div>
          )}
          {enabled && (
            <div className="mt-3 flex items-center justify-between gap-2">
              <div className="font-mono text-[10px] text-fg-3">
                <kbd className="kbd">↵</kbd> run · <kbd className="kbd">⎋</kbd> cancel
              </div>
              <button
                onClick={run}
                disabled={!draft.trim()}
                className="rounded-md bg-warn/15 px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.08em] text-warn ring-1 ring-warn/40 hover:bg-warn/25 disabled:opacity-40"
              >
                run ↵
              </button>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
