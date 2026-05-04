import { useEffect, useState } from 'react'
import { useHarness } from '../state/store'

/**
 * First-run wizard for computer-use permissions.
 *
 * Opens automatically the first time the user toggles
 * `settings.computer.enabled` on. Walks through:
 *
 *   1. What computer control is and why it needs permissions
 *   2. Screen Recording (opens System Settings -> Privacy)
 *   3. Accessibility (opens System Settings -> Privacy)
 *   4. Done -- user confirms everything works
 *
 * Closing mid-wizard leaves `wizardState === 'never'` so the next
 * toggle re-opens it. "Done" sets `wizardState = 'done'`.
 *
 * The wizard is aware that permission changes usually require a
 * restart of the offending binary (the Python bridge subprocess). It
 * prompts the user to reopen the session after granting either
 * permission to pick up the change.
 */
export function ComputerPermissionWizard() {
  const open = useHarness((s) => s.computerWizardOpen)
  const setOpen = useHarness((s) => s.openComputerWizard)
  const settings = useHarness((s) => s.settings)
  const [step, setStep] = useState<0 | 1 | 2 | 3>(0)

  useEffect(() => {
    if (open) setStep(0)
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
  }, [open, setOpen])

  if (!open) return null

  const finish = async () => {
    const api = (window as any).harness
    if (api?.settingsUpdate) {
      await api.settingsUpdate({
        computer: { ...settings.computer, wizardState: 'done' },
      })
      useHarness.setState((prev) => ({
        settings: {
          ...prev.settings,
          computer: { ...prev.settings.computer, wizardState: 'done' },
        },
      }))
    }
    setOpen(false)
  }

  const openSystemSettings = (pane: 'screenRecording' | 'accessibility') => {
    const url =
      pane === 'screenRecording'
        ? 'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture'
        : 'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'
    ;(window as any).harness?.openExternal?.(url)
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-[10vh]">
      <div
        className="absolute inset-0 bg-black/50 backdrop-blur-[2px]"
        onClick={() => setOpen(false)}
      />
      <div className="relative w-[640px] overflow-hidden rounded-2xl glass-strong shadow-2xl ring-hairline-strong">
        <div className="flex items-center gap-3 px-5 py-4 hairline-b">
          <span className="label text-warn">computer control</span>
          <span className="label text-fg-2">first-run setup</span>
          <button
            onClick={() => setOpen(false)}
            className="ml-auto rounded bg-white/[0.05] px-2 py-[3px] font-mono text-[10px] uppercase tracking-[0.08em] text-fg-1 ring-hairline hover:bg-white/[0.08]"
          >
            skip
          </button>
        </div>

        <div className="px-6 py-6">
          <div className="mb-5 flex items-center gap-2">
            {[0, 1, 2, 3].map((i) => (
              <div
                key={i}
                className={`h-1 flex-1 rounded-full ${
                  i <= step ? 'bg-warn' : 'bg-white/10'
                }`}
              />
            ))}
          </div>

          {step === 0 && (
            <div>
              <h3 className="mb-2 font-mono text-[14px] text-fg-0">
                What is computer control?
              </h3>
              <p className="mb-3 text-[12px] leading-[1.6] text-fg-1">
                When enabled, the agent can drive your actual macOS
                desktop -- take screenshots, click, type, scroll, and
                read the accessibility tree of other apps. This is
                powerful and it can be risky, so we guard it with:
              </p>
              <ul className="mb-4 space-y-1.5 text-[11.5px] leading-[1.55] text-fg-1">
                <li>
                  <span className="text-warn">*</span> an amber highlight
                  ring 200ms before every click, so you can see what's
                  about to happen
                </li>
                <li>
                  <span className="text-warn">*</span> a floating emergency
                  stop button visible whenever any session is running
                </li>
                <li>
                  <span className="text-warn">*</span> triple-Esc and
                  Cmd+Shift+Esc as instant kill switches -- press either if
                  you want to take control back
                </li>
                <li>
                  <span className="text-warn">*</span> a blocklist covering
                  password managers and banking apps by default
                </li>
              </ul>
              <p className="text-[11px] leading-[1.55] text-fg-2">
                macOS requires two system-level permissions before any
                of this works. The next two screens walk you through
                granting them.
              </p>
              <div className="mt-5 flex justify-end gap-2">
                <button
                  onClick={() => setOpen(false)}
                  className="rounded-md bg-white/[0.04] px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.08em] text-fg-1 ring-hairline hover:bg-white/[0.08]"
                >
                  not now
                </button>
                <button
                  onClick={() => setStep(1)}
                  className="rounded-md bg-warn/15 px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.08em] text-warn ring-1 ring-warn/40 hover:bg-warn/25"
                >
                  next
                </button>
              </div>
            </div>
          )}

          {step === 1 && (
            <div>
              <h3 className="mb-2 font-mono text-[14px] text-fg-0">
                1/2 -- Screen Recording
              </h3>
              <p className="mb-3 text-[12px] leading-[1.6] text-fg-1">
                We need this to take screenshots of your display. No
                video is ever recorded; frames are captured on demand
                during a computer-use session and streamed to the UI
                only (never uploaded to any server).
              </p>
              <div className="mb-3 rounded-md bg-danger/10 p-2.5 text-[11px] leading-[1.55] text-fg-1 ring-1 ring-danger/25">
                <span className="font-mono text-[9.5px] uppercase text-danger">
                  critical
                </span>
                <div className="mt-1">
                  Without this permission, macOS still returns captures
                  -- but they're silently <span className="text-danger">privacy-filtered</span>:
                  you'll see your desktop wallpaper + menu bar but
                  EVERY other app's window will be invisible. The
                  agent will think the screen is empty when it's full
                  of apps and hallucinate success on actions that
                  didn't land. Grant this carefully and verify.
                </div>
              </div>
              <div className="mb-3 rounded-md bg-accent/10 p-2.5 text-[11px] leading-[1.55] text-fg-1 ring-1 ring-accent/25">
                <span className="font-mono text-[9.5px] uppercase text-accent">
                  dev tip
                </span>
                <div className="mt-1">
                  Iterating with <span className="font-mono">npm run package</span>?
                  Each build produces a new code signature and macOS
                  treats it as a fresh app -- you'll have to regrant
                  every time. For the inner loop, use{' '}
                  <span className="font-mono text-accent">npm run start</span>{' '}
                  instead: it runs the stable Electron binary from{' '}
                  <span className="font-mono">node_modules</span>, which
                  keeps the TCC grant across your code changes. Grant
                  Electron once and iterate freely.
                </div>
              </div>
              <ol className="mb-4 space-y-1 text-[11.5px] leading-[1.55] text-fg-1">
                <li>
                  1. Click{' '}
                  <button
                    onClick={() => openSystemSettings('screenRecording')}
                    className="font-mono text-accent underline decoration-dotted hover:text-accent"
                  >
                    Open System Settings
                  </button>
                </li>
                <li>
                  2. Drag <span className="font-mono text-warn">Freyja.app</span>{' '}
                  from Finder into the list (easiest), or click '+' and navigate to it
                </li>
                <li>3. Toggle it ON</li>
                <li>4. Quit and relaunch the app (TCC takes effect on next start)</li>
                <li>5. Come back and hit Next</li>
              </ol>
              <div className="flex justify-between gap-2">
                <button
                  onClick={() => setStep(0)}
                  className="rounded-md bg-white/[0.04] px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.08em] text-fg-1 ring-hairline hover:bg-white/[0.08]"
                >
                  back
                </button>
                <button
                  onClick={() => setStep(2)}
                  className="rounded-md bg-warn/15 px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.08em] text-warn ring-1 ring-warn/40 hover:bg-warn/25"
                >
                  next
                </button>
              </div>
            </div>
          )}

          {step === 2 && (
            <div>
              <h3 className="mb-2 font-mono text-[14px] text-fg-0">
                2/2 -- Accessibility
              </h3>
              <p className="mb-3 text-[12px] leading-[1.6] text-fg-1">
                Used for two things: injecting mouse clicks and keyboard
                events at OS level (via Enigo / CGEvent), and reading
                the semantic UI tree of other apps (AXUIElement). The
                AX tree is how we avoid shaky pixel-counting against
                screenshots when the target app is AX-friendly.
              </p>
              <div className="mb-3 rounded-md bg-warn/10 p-2.5 text-[11px] leading-[1.55] text-fg-1 ring-1 ring-warn/25">
                <span className="font-mono text-[9.5px] uppercase text-warn">
                  tip
                </span>
                <div className="mt-1">
                  Easiest way: drag{' '}
                  <span className="font-mono text-warn">Freyja.app</span>{' '}
                  straight from Finder into the Accessibility list. No
                  file picker navigation needed. macOS then propagates
                  the permission to the Python bridge subprocess
                  automatically via responsibility inheritance -- you
                  do NOT need to add the python binary separately.
                </div>
              </div>
              <ol className="mb-4 space-y-1 text-[11.5px] leading-[1.55] text-fg-1">
                <li>
                  1. Click{' '}
                  <button
                    onClick={() => openSystemSettings('accessibility')}
                    className="font-mono text-accent underline decoration-dotted hover:text-accent"
                  >
                    Open System Settings
                  </button>
                </li>
                <li>
                  2. Drag{' '}
                  <span className="font-mono text-warn">Freyja.app</span>{' '}
                  into the list
                </li>
                <li>3. Toggle it ON</li>
                <li>4. Quit and relaunch this app (TCC takes effect on restart)</li>
                <li>5. Come back and hit Next</li>
              </ol>
              <div className="flex justify-between gap-2">
                <button
                  onClick={() => setStep(1)}
                  className="rounded-md bg-white/[0.04] px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.08em] text-fg-1 ring-hairline hover:bg-white/[0.08]"
                >
                  back
                </button>
                <button
                  onClick={() => setStep(3)}
                  className="rounded-md bg-warn/15 px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.08em] text-warn ring-1 ring-warn/40 hover:bg-warn/25"
                >
                  next
                </button>
              </div>
            </div>
          )}

          {step === 3 && (
            <div>
              <h3 className="mb-2 font-mono text-[14px] text-fg-0">
                You're set.
              </h3>
              <p className="mb-3 text-[12px] leading-[1.6] text-fg-1">
                Computer control is enabled and the permission prompts
                should be out of the way. You can now:
              </p>
              <ul className="mb-4 space-y-1.5 text-[11.5px] leading-[1.55] text-fg-1">
                <li>
                  * ask the agent to do something that requires driving
                  the computer (it'll call the new atomic tools)
                </li>
                <li>
                  * type <span className="font-mono text-accent">/computer</span> followed by
                  a goal to spawn a dedicated computer-use sub-agent
                </li>
                <li>
                  * hit <span className="font-mono text-warn">Cmd+Shift+K</span> anywhere
                  for the hotkey overlay
                </li>
                <li>
                  * remember:{' '}
                  <span className="font-mono text-danger">triple-Esc</span>{' '}
                  or <span className="font-mono text-danger">Cmd+Shift+Esc</span>{' '}
                  stops everything
                </li>
              </ul>
              <div className="flex justify-end gap-2">
                <button
                  onClick={() => setStep(2)}
                  className="rounded-md bg-white/[0.04] px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.08em] text-fg-1 ring-hairline hover:bg-white/[0.08]"
                >
                  back
                </button>
                <button
                  onClick={finish}
                  className="rounded-md bg-ok/15 px-3 py-1.5 font-mono text-[11px] uppercase tracking-[0.08em] text-ok ring-1 ring-ok/40 hover:bg-ok/25"
                >
                  finish
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
