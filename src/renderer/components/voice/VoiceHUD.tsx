import { useEffect, useRef, useState } from 'react'
import type { Receipt } from '@shared/events'
import { useVoiceStore } from '../../state/voice-store'
import type { VoiceEngineState } from '../../voice/engine'
import { VoiceSigil } from './VoiceSigil'

/**
 * VoiceHUD — the Galdr capsule (docs/GALDR-BUILD.md §7.3, dossier §03).
 *
 * A glass capsule bottom-center, the InputDock's sibling: the live
 * sigil, the transcript AS IT FORMS (the repair surface — a mishear
 * dies at a glance), the resolved verb chip, and the receipt. Esc ends
 * the session (handled with priority in App.tsx); typing any printable
 * character opens an inline floor-command input.
 *
 * z-40: above the shell, below PermissionPrompt (z-50).
 */

export function VoiceHUD() {
  const hudOpen = useVoiceStore((s) => s.hudOpen)
  // Body is a separate component so its listeners/effects only exist
  // while the capsule is actually up.
  if (!hudOpen) return null
  return <VoiceHudBody />
}

function VoiceHudBody() {
  const engineState = useVoiceStore((s) => s.engineState)
  const micLevel = useVoiceStore((s) => s.micLevel)
  const userLine = useVoiceStore((s) => s.userLine)
  const assistantLine = useVoiceStore((s) => s.assistantLine)
  const activity = useVoiceStore((s) => s.activity)
  const receipts = useVoiceStore((s) => s.receipts)
  const voiceSessionId = useVoiceStore((s) => s.voiceSessionId)
  const error = useVoiceStore((s) => s.error)
  const usage = useVoiceStore((s) => s.usage)
  const hotkeyLabel = useVoiceStore((s) => s.hotkeyLabel)
  const typedCommand = useVoiceStore((s) => s.typedCommand)
  const confirmPending = useVoiceStore((s) => s.confirmPending)
  const undo = useVoiceStore((s) => s.undo)

  // Inline floor-command input — null = closed, string = current draft.
  const [typed, setTyped] = useState<string | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)

  // Take the keyboard deliberately on open. The InputDock textarea
  // autofocuses on mount and keeps focus after a send, so in the app's
  // default state every printable key would land in the chat draft —
  // "type to command" would be dead and Enter would ship the floor
  // command as a chat turn. Blurring whatever holds focus hands the
  // keys to the listener below; re-focusing an editable after this is
  // an explicit user act the listener still defers to.
  useEffect(() => {
    const el = document.activeElement
    if (el instanceof HTMLElement) el.blur()
  }, [])

  // Any printable key while the HUD is up (and no other field focused)
  // opens the typed-command input seeded with that character. This is
  // the FLOOR lane: the bridge parses it deterministically — no model.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (typed !== null) return
      const t = e.target as HTMLElement | null
      if (
        t &&
        (t.tagName === 'INPUT' ||
          t.tagName === 'TEXTAREA' ||
          t.tagName === 'SELECT' ||
          t.isContentEditable)
      )
        return
      if (e.metaKey || e.ctrlKey || e.altKey) return
      if (e.key.length !== 1) return
      e.preventDefault()
      setTyped(e.key)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [typed])

  // Keep the newest transcript text in view as it streams.
  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [userLine, assistantLine])

  // Latest receipt belonging to THIS session — older receipts stay in
  // the activity-panel section, not on a fresh capsule.
  const sessionReceipt =
    (voiceSessionId &&
      receipts.find((r) => r.voiceSessionId === voiceSessionId)) ||
    null

  const confirming = activity?.status === 'confirm'
  const showError = error !== null && engineState === 'error'
  // Caret while the mic is live — the transcript can still extend.
  const caretLive = engineState === 'listening'

  return (
    // Positioning wrapper and animated capsule are separate elements:
    // the fade-in keyframes animate `transform`, which would clobber
    // -translate-x-1/2 for the duration of the entry animation and
    // make the capsule jump half a width to the right.
    <div
      className="pointer-events-auto fixed bottom-24 left-1/2 z-40 -translate-x-1/2"
      style={{ width: 'min(620px, 88vw)' }}
    >
      <div className="animate-fade-in overflow-hidden rounded-2xl glass-strong shadow-2xl ring-hairline-strong">
        {/* ── main row: sigil · transcript · verb chip ─────────────── */}
        <div className="flex items-center gap-3.5 px-4 py-3">
          <VoiceSigil
            size={56}
            state={engineState}
            level={micLevel}
            className="shrink-0"
          />
          {/* role=status/aria-live: the streaming transcript is the only
              non-visual signal that the mic is hot and what was heard. */}
          <div
            ref={scrollRef}
            role="status"
            aria-live="polite"
            className="max-h-[76px] min-w-0 flex-1 overflow-y-auto"
          >
            {userLine ? (
              <div className="text-[13px] leading-[1.45] text-fg-0">
                {userLine}
                {caretLive && (
                  <span className="ml-[3px] inline-block h-[13px] w-[7px] translate-y-[2px] animate-caret-blink bg-accent" />
                )}
              </div>
            ) : (
              <div className="text-[12px] italic leading-[1.45] text-fg-3">
                {statusLine(engineState)}
              </div>
            )}
            {assistantLine && (
              <div className="mt-1 text-[12.5px] leading-[1.45] text-accent-hi">
                {assistantLine}
              </div>
            )}
          </div>
          {activity ? (
            <span
              className={chipClasses(activity.status, laneOf(activity.verb, sessionReceipt))}
            >
              {activity.verb}
            </span>
          ) : (
            <span className="shrink-0 font-mono text-[9.5px] uppercase tracking-[0.14em] text-fg-3">
              {engineState}
            </span>
          )}
        </div>

        {/* ── confirm row — the verb is gated; go/cancel also voice-answerable ── */}
        {confirming && activity && (
          <div className="flex items-center gap-3 px-4 py-2 hairline-t">
            <span className="shrink-0 font-mono text-[9px] uppercase tracking-[0.14em] text-warn">
              confirm
            </span>
            <span className="min-w-0 flex-1 truncate text-[12px] text-fg-0">
              {activity.summary}
            </span>
            <button
              onClick={() => confirmPending(true)}
              className="shrink-0 rounded-md bg-accent/15 px-3 py-1 font-mono text-[10px] uppercase tracking-[0.1em] text-accent ring-1 ring-accent/40 hover:bg-accent/25"
            >
              go
            </button>
            <button
              onClick={() => confirmPending(false)}
              className="shrink-0 rounded-md px-3 py-1 font-mono text-[10px] uppercase tracking-[0.1em] text-danger ring-1 ring-danger/30 hover:bg-danger/15"
            >
              cancel
            </button>
          </div>
        )}

        {/* ── receipt row — the latest outcome of this exchange ────── */}
        {!confirming && sessionReceipt && (
          <div className="flex items-center gap-2.5 px-4 py-2 hairline-t">
            <span
              className={`min-w-0 flex-1 truncate text-[11px] ${
                sessionReceipt.ok ? 'text-fg-1' : 'text-danger/90'
              } ${sessionReceipt.undone ? 'line-through opacity-60' : ''}`}
            >
              {sessionReceipt.summary}
            </span>
            <span className="shrink-0 font-mono text-[10px] tabular-nums text-fg-3">
              {hhmm(sessionReceipt.ts)}
            </span>
            {sessionReceipt.undoable && !sessionReceipt.undone && (
              <button
                onClick={() => undo(sessionReceipt.id)}
                className="shrink-0 border-b border-dotted border-accent/60 font-mono text-[10px] text-accent hover:border-accent hover:text-accent-hi"
              >
                undo
              </button>
            )}
            {sessionReceipt.undone && (
              <span className="shrink-0 font-mono text-[9px] uppercase tracking-[0.1em] text-fg-3">
                undone
              </span>
            )}
          </div>
        )}

        {/* ── error row — mint/transport failure with the retry path ── */}
        {showError && (
          <div className="flex items-center gap-2.5 bg-danger/[0.05] px-4 py-2 hairline-t">
            <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-danger">
              {error}
            </span>
            <span className="shrink-0 font-mono text-[10px] text-fg-3">
              {hotkeyLabel ? `${hotkeyLabel} to retry` : 'click the sigil to retry'}
            </span>
          </div>
        )}

        {/* ── inline floor-command input (typed lane) ──────────────── */}
        {typed !== null && (
          <div className="flex items-center gap-2 px-4 py-2 hairline-t">
            <span className="shrink-0 font-mono text-[10px] text-ok">▸</span>
            <input
              autoFocus
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter') {
                  typedCommand(typed)
                  setTyped(null)
                } else if (e.key === 'Escape') {
                  // First Esc dismisses the input; the next one reaches
                  // App.tsx and ends the session.
                  e.stopPropagation()
                  setTyped(null)
                }
              }}
              placeholder="floor command — pause · next · volume 40 · mute"
              aria-label="floor command"
              spellCheck={false}
              className="w-full bg-transparent font-mono text-[12px] text-fg-0 outline-none placeholder:text-fg-3"
            />
            <span className="shrink-0 font-mono text-[9px] uppercase tracking-[0.14em] text-ok/80">
              floor
            </span>
          </div>
        )}

        {/* ── footer microcopy ─────────────────────────────────────── */}
        <div className="flex items-center justify-between px-4 py-[7px] hairline-t">
          <span className="font-mono text-[10px] text-fg-3">
            {typed !== null ? 'enter to run · esc to dismiss' : 'type to command'}
          </span>
          <div className="flex items-center gap-3">
            {usage && usage.totalTokens > 0 && (
              <span
                className="font-mono text-[10px] tabular-nums text-fg-3"
                title={`~$${usage.estCostUsd.toFixed(4)} estimated · ${usage.totalTokens.toLocaleString()} tokens (${usage.inputAudio + usage.outputAudio} audio) — realtime pricing is approximate`}
              >
                ~${usage.estCostUsd < 0.01 ? usage.estCostUsd.toFixed(4) : usage.estCostUsd.toFixed(2)}
                <span className="text-fg-4"> · {formatTokens(usage.totalTokens)}</span>
              </span>
            )}
            <span className="font-mono text-[10px] text-fg-3">{hotkeyLabel ? `esc to end · ${hotkeyLabel}` : 'esc to end'}</span>
          </div>
        </div>
      </div>
    </div>
  )
}

function formatTokens(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1)}k tok` : `${n} tok`
}

function statusLine(state: VoiceEngineState): string {
  switch (state) {
    case 'minting':
      return 'minting session…'
    case 'connecting':
      return 'connecting…'
    case 'listening':
      return 'listening…'
    case 'error':
      return 'session failed'
    case 'closing':
      return 'closing…'
    default:
      return '…'
  }
}

/** Lane for the verb chip. Activity doesn't carry a lane, so derive it:
 *  mission verbs are their own lane; otherwise trust the freshest
 *  receipt for this verb (typed commands come back lane=floor); model
 *  calls default to brain. */
function laneOf(
  verb: string,
  receipt: Receipt | null,
): 'floor' | 'brain' | 'mission' {
  if (verb.startsWith('mission.')) return 'mission'
  if (receipt && receipt.verb === verb && receipt.lane !== 'undo') return receipt.lane
  return 'brain'
}

function chipClasses(
  status: 'running' | 'ok' | 'fail' | 'confirm',
  lane: 'floor' | 'brain' | 'mission',
): string {
  const base =
    'shrink-0 rounded-full px-2 py-[3px] font-mono text-[9.5px] uppercase tracking-[0.14em] ring-1'
  if (status === 'fail') return `${base} bg-danger/10 text-danger ring-danger/30`
  if (status === 'confirm')
    return `${base} animate-pulse-soft bg-warn/[0.12] text-warn ring-warn/35`
  const tone =
    lane === 'floor'
      ? 'bg-ok/10 text-ok ring-ok/30'
      : lane === 'mission'
        ? 'bg-warn/10 text-warn ring-warn/30'
        : 'bg-accent/[0.12] text-accent ring-accent/30'
  return `${base} ${tone}${status === 'running' ? ' animate-pulse-soft' : ''}`
}

function hhmm(ts: number): string {
  return new Date(ts).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })
}
