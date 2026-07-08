// Scripted voice-session walkthrough for headless review (no API key,
// no mic, no Electron). Mirrors the inRendererDemo pattern: voice-store
// runs this instead of the real VoiceEngine when window.harness is
// missing or ?voicedemo=1, so the HUD/Sigil/receipts surface can be
// screenshotted and design-reviewed end to end. Every state the real
// engine can produce is walked: connect → listening (live level) →
// user transcript typing → thinking → acting → receipt → speaking →
// confirm-required → auto-confirm → receipt → idle. Loops forever
// (8 s pause) until stopped.

import type { Receipt } from '@shared/events'
import type { VoiceEngineState } from './engine'

export interface VoiceDemoHooks {
  setState(s: VoiceEngineState): void
  setUserLine(text: string): void
  setAssistantLine(text: string): void
  setLevel(level: number): void
  setActivity(
    activity: {
      verb: string
      status: 'running' | 'ok' | 'fail' | 'confirm'
      summary: string
    } | null,
  ): void
  addReceipt(receipt: Receipt): void
  // ── Session-projection callbacks (optional) ──────────────────────
  // The HUD hooks above are incremental (per-keystroke typing, ramping
  // levels) — useless as turn boundaries. These fire at the semantic
  // beats voice-store needs to mirror the walk into the session graph:
  // a finalized user utterance, a verb call + its result, and the
  // finalized assistant reply. All optional so the demo still runs when
  // no projection is wired.
  onUserFinal?(text: string): void
  onVerb?(callId: string, verb: string, args: Record<string, unknown>): void
  onVerbResult?(callId: string, ok: boolean, summary: string, verb: string): void
  onAssistantFinal?(text: string): void
}

export interface VoiceDemoHandle {
  stop(): void
}

export function startVoiceDemo(hooks: VoiceDemoHooks): VoiceDemoHandle {
  let cancelled = false
  const timers = new Set<number>()

  const wait = (ms: number): Promise<void> =>
    new Promise((resolve) => {
      const t = window.setTimeout(() => {
        timers.delete(t)
        resolve()
      }, ms)
      timers.add(t)
    })

  // Synthetic mic level: a slow breathing sine with per-tick jitter.
  // `talking` widens the amplitude so the sigil visibly ripples while
  // the scripted "user" is speaking.
  let talking = false
  let phase = 0
  const levelTimer = window.setInterval(() => {
    if (cancelled) return
    phase += 0.18
    const base = talking ? 0.45 : 0.08
    const swing = talking ? 0.35 : 0.05
    const level = Math.max(
      0,
      Math.min(1, base + swing * Math.abs(Math.sin(phase)) + (Math.random() - 0.5) * 0.06),
    )
    hooks.setLevel(level)
  }, 50)
  timers.add(levelTimer)

  const typeUserLine = async (text: string): Promise<void> => {
    talking = true
    for (let i = 1; i <= text.length; i++) {
      if (cancelled) return
      hooks.setUserLine(text.slice(0, i))
      await wait(40)
    }
    talking = false
  }

  const makeReceipt = (
    partial: Pick<Receipt, 'heard' | 'verb' | 'args' | 'summary' | 'undoable'> &
      Partial<Receipt>,
  ): Receipt => ({
    id: `demo-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    ts: Date.now(),
    lane: 'brain',
    ok: true,
    voiceSessionId: 'voice-demo',
    ...partial,
  })

  const run = async (): Promise<void> => {
    // Unique per-run call-id nonce so the forever-looping walk doesn't
    // reuse tool-call ids across iterations (which would collide in the
    // projected session's toolCalls map).
    const nonce = `demo-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`
    // connect
    hooks.setState('connecting')
    await wait(600)
    if (cancelled) return
    hooks.setState('listening')
    await wait(900)
    if (cancelled) return

    // ── beat 1: spotify.play, auto tier ─────────────────────────────
    await typeUserLine('play vienna by billy joel on spotify')
    if (cancelled) return
    hooks.onUserFinal?.('play vienna by billy joel on spotify')
    hooks.setState('thinking')
    await wait(650)
    if (cancelled) return
    hooks.setState('acting')
    hooks.setActivity({ verb: 'spotify.play', status: 'running', summary: '' })
    hooks.onVerb?.(`${nonce}-call1`, 'spotify.play', { query: 'vienna billy joel' })
    await wait(950)
    if (cancelled) return
    hooks.setActivity({
      verb: 'spotify.play',
      status: 'ok',
      summary: '▶ Vienna — Billy Joel',
    })
    hooks.onVerbResult?.(`${nonce}-call1`, true, '▶ Vienna — Billy Joel', 'spotify.play')
    hooks.addReceipt(
      makeReceipt({
        heard: 'play vienna by billy joel on spotify',
        verb: 'spotify.play',
        args: { query: 'vienna billy joel' },
        summary: '▶ Vienna — Billy Joel',
        undoable: false,
      }),
    )
    hooks.setState('speaking')
    hooks.setAssistantLine('Playing Vienna.')
    hooks.onAssistantFinal?.('Playing Vienna.')
    await wait(1400)
    if (cancelled) return
    hooks.setState('listening')
    await wait(1600)
    if (cancelled) return

    // ── beat 2: app.quit, confirm tier ───────────────────────────────
    hooks.setUserLine('')
    hooks.setAssistantLine('')
    hooks.setActivity(null)
    await typeUserLine('quit slack')
    if (cancelled) return
    hooks.onUserFinal?.('quit slack')
    hooks.setState('thinking')
    await wait(600)
    if (cancelled) return
    hooks.setState('acting')
    hooks.setActivity({ verb: 'app.quit', status: 'running', summary: '' })
    await wait(500)
    if (cancelled) return
    hooks.setActivity({ verb: 'app.quit', status: 'confirm', summary: 'Quit Slack' })
    hooks.setState('speaking')
    hooks.setAssistantLine('Quit Slack — confirm?')
    await wait(1000)
    if (cancelled) return
    hooks.setState('listening')
    // auto-confirm after 2.5 s so the confirm row is screenshotable but
    // the loop still advances unattended
    await wait(2500)
    if (cancelled) return
    hooks.setUserLine('go')
    hooks.setActivity({ verb: 'app.quit', status: 'running', summary: 'Quit Slack' })
    hooks.setState('acting')
    // Confirm granted — the verb actually runs now; project it as a call.
    hooks.onVerb?.(`${nonce}-call2`, 'app.quit', { name: 'Slack' })
    await wait(700)
    if (cancelled) return
    hooks.setActivity({ verb: 'app.quit', status: 'ok', summary: 'Quit Slack' })
    hooks.onVerbResult?.(`${nonce}-call2`, true, 'Quit Slack', 'app.quit')
    hooks.addReceipt(
      makeReceipt({
        heard: 'quit slack',
        verb: 'app.quit',
        args: { name: 'Slack' },
        summary: 'Quit Slack',
        undoable: true,
      }),
    )
    hooks.setState('speaking')
    hooks.setAssistantLine('Done.')
    hooks.onAssistantFinal?.('Done.')
    await wait(1000)
    if (cancelled) return
    hooks.setState('listening')

    // idle-close beat: linger, then reset lines and loop the walk
    await wait(8000)
    if (cancelled) return
    hooks.setUserLine('')
    hooks.setAssistantLine('')
    hooks.setActivity(null)
    hooks.setState('idle')
    await wait(600)
  }

  void (async () => {
    while (!cancelled) {
      try {
        await run()
      } catch (err) {
        // A demo script must never take the app down — log and retry.
        console.error('[voice-demo] script error', err)
        await wait(2000)
      }
    }
  })()

  return {
    stop() {
      cancelled = true
      for (const t of timers) window.clearTimeout(t)
      window.clearInterval(levelTimer)
      timers.clear()
      hooks.setLevel(0)
    },
  }
}
