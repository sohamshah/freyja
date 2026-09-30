// LiveProtocol — the GPT-Live half of VoiceEngine (docs/GALDR-LIVE.md).
//
// GPT-Live is full duplex: a voice layer that listens and talks at once,
// plus a delegated Responses backend that reasons and calls our per-verb
// tools. This class is the pure event logic for that protocol — the
// engine owns WebRTC/mic/audio and hands us the `oai-events` data
// channel's JSON. No DOM, no RTC: unit-testable with a fake `send`.
//
// What it translates, both ways:
//   response.event{function_call}   → toolCall (store relays to bridge)
//   sendToolResult(output, image?)  → response.item.create + response.create
//   session.*_transcript.delta      → user/assistant lines, split into
//                                     turns by silence (deltas are audio-
//                                     cadence fragments, never turn-final)
//   session.usage.updated / backend response.completed → usage

import type { UsageDelta, VoiceEngineState } from './engine'

/** Silence after the operator's last transcript fragment that closes
 *  their turn. A delegation closes it sooner (the voice layer decided
 *  the operator asked for something). */
export const USER_TURN_GAP_MS = 1000
/** Same for the assistant. Longer: the voice pauses between clauses
 *  (over 1.4 s mid-sentence in e2e, splitting one answer in two), and a
 *  backchannel ("mm") shouldn't become a turn of its own. */
export const ASSISTANT_TURN_GAP_MS = 2000
/** How long stop() waits for session.closed after session.close before
 *  tearing the transport down anyway. */
export const CLOSE_WAIT_MS = 600

export type LiveUsage = UsageDelta & {
  /** Cumulative billed voice seconds (a running total, NOT an increment —
   *  session.usage.updated snapshots must never be summed). */
  liveSeconds?: number
}

export type LiveHooks = {
  send: (event: Record<string, unknown>) => void
  getState: () => VoiceEngineState
  setState: (s: VoiceEngineState) => void
  userTranscript: (text: string, final: boolean) => void
  assistantTranscript: (text: string, done: boolean) => void
  toolCall: (callId: string, name: string, argumentsJson: string) => void
  /** The backend's reply text for one response — what the voice layer
   *  was handed to relay (it paraphrases; this is the source). */
  backendText: (text: string) => void
  usage: (u: LiveUsage) => void
  error: (code: string, message: string) => void
  /** The server ended the session on its own (expired, content, hangup) —
   *  NOT fired for a close we asked for. */
  serverClosed: (reason: string) => void
  setTimeout: (fn: () => void, ms: number) => number
  clearTimeout: (id: number) => void
  /** Largest data-channel message the transport will take, in bytes. */
  maxMessageBytes: () => number
}

export type ToolImage = { b64: string; w: number; h: number; mime?: string }

export class LiveProtocol {
  private started = false
  private closing = false
  private closedWaiters: Array<() => void> = []
  private eventSeq = 0

  /** Backend function calls surfaced but not yet answered. While any are
   *  owed we stay 'acting'; the LAST answer sends response.create. */
  private pending = new Set<string>()
  /** Backend responses in flight (response.created without a terminal
   *  event) — keeps the HUD on 'thinking' between tool rounds. */
  private backendActive = new Set<string>()

  private userBuf = ''
  private assistantBuf = ''
  private userTimer: number | null = null
  private assistantTimer: number | null = null
  /** The assistant went quiet while the operator was mid-sentence (a
   *  backchannel): its line is flushed right after the operator's. */
  private assistantFlushOwed = false

  constructor(private readonly h: LiveHooks) {}

  get isStarted(): boolean {
    return this.started
  }

  // ── inbound ────────────────────────────────────────────────────────

  handle(raw: unknown): void {
    if (typeof raw !== 'object' || raw === null) return
    const ev = raw as Record<string, unknown>
    switch (ev.type) {
      case 'session.started':
        this.started = true
        if (this.h.getState() === 'connecting') this.h.setState('listening')
        return

      case 'session.input_transcript.delta':
        this.onUserDelta(str(ev.delta))
        return

      case 'session.output_transcript.delta':
        this.onAssistantDelta(str(ev.delta))
        return

      case 'session.delegation.created':
        // The voice layer heard a request worth delegating — that's the
        // end of the operator's turn, whatever the transcript cadence.
        this.flushUser()
        if (this.pending.size === 0) this.h.setState('thinking')
        return

      case 'response.event':
        this.onBackendEvent(ev.event)
        return

      case 'session.usage.updated': {
        const usage = ev.usage as Record<string, unknown> | undefined
        const seconds = num(usage?.seconds)
        this.h.usage({ ...ZERO_USAGE, liveSeconds: seconds })
        return
      }

      case 'session.closed': {
        const usage = ev.usage as Record<string, unknown> | undefined
        if (usage && typeof usage.seconds === 'number') {
          this.h.usage({ ...ZERO_USAGE, liveSeconds: num(usage.seconds) })
        }
        this.flushUser()
        this.flushAssistant(true)
        const waiters = this.closedWaiters
        this.closedWaiters = []
        for (const w of waiters) w()
        if (!this.closing) this.h.serverClosed(str(ev.reason) || 'closed')
        return
      }

      case 'error': {
        const err = ev.error as Record<string, unknown> | undefined
        this.h.error(
          str(err?.code) || 'server_error',
          str(err?.message) || JSON.stringify(ev).slice(0, 300),
        )
        return
      }

      default:
        // *.appended acks, mute acks, unknown additions — ignored.
        return
    }
  }

  private onBackendEvent(raw: unknown): void {
    if (typeof raw !== 'object' || raw === null) return
    const inner = raw as Record<string, unknown>
    const response = inner.response as Record<string, unknown> | undefined
    switch (inner.type) {
      case 'response.created': {
        const id = str(response?.id)
        if (id) this.backendActive.add(id)
        if (this.pending.size === 0 && this.h.getState() !== 'speaking') {
          this.h.setState('thinking')
        }
        return
      }

      case 'response.output_item.done': {
        const item = inner.item as Record<string, unknown> | undefined
        if (item?.type === 'message' && Array.isArray(item.content)) {
          const text = (item.content as Array<Record<string, unknown>>)
            .map((part) => (part.type === 'output_text' ? str(part.text) : ''))
            .join('')
            .trim()
          if (text) this.h.backendText(text)
          return
        }
        if (!item || item.type !== 'function_call') return
        const callId = str(item.call_id)
        const name = str(item.name)
        if (!callId || !name) return
        this.pending.add(callId)
        this.h.setState('acting')
        this.h.toolCall(callId, name, str(item.arguments) || '{}')
        return
      }

      case 'response.completed':
      case 'response.failed':
      case 'response.incomplete':
      case 'response.cancelled': {
        const id = str(response?.id)
        if (id) this.backendActive.delete(id)
        const usage = response?.usage as Record<string, unknown> | undefined
        if (usage) {
          const cached = num(
            (usage.input_tokens_details as Record<string, unknown> | undefined)?.cached_tokens,
          )
          this.h.usage({
            ...ZERO_USAGE,
            inputText: Math.max(0, num(usage.input_tokens) - cached),
            inputCached: cached,
            outputText: num(usage.output_tokens),
            totalTokens: num(usage.total_tokens),
          })
        }
        if (inner.type === 'response.failed') {
          const err = response?.error as Record<string, unknown> | undefined
          this.h.error(str(err?.code) || 'backend_failed', str(err?.message) || 'backend response failed')
        }
        this.settle()
        return
      }

      default:
        return
    }
  }

  // ── transcripts → turns ────────────────────────────────────────────

  private onUserDelta(delta: string): void {
    if (!delta) return
    this.userBuf += delta
    this.h.userTranscript(this.userBuf.trim(), false)
    this.rearm('user')
  }

  private onAssistantDelta(delta: string): void {
    if (!delta) return
    this.assistantBuf += delta
    // The voice talks while the backend works ("on it") — the verb chip
    // outranks that, so only claim 'speaking' when nothing is owed.
    if (this.pending.size === 0) this.h.setState('speaking')
    this.h.assistantTranscript(this.assistantBuf.trim(), false)
    this.rearm('assistant')
  }

  private rearm(side: 'user' | 'assistant'): void {
    if (side === 'user') {
      if (this.userTimer !== null) this.h.clearTimeout(this.userTimer)
      this.userTimer = this.h.setTimeout(() => {
        this.userTimer = null
        this.flushUser()
      }, USER_TURN_GAP_MS)
    } else {
      if (this.assistantTimer !== null) this.h.clearTimeout(this.assistantTimer)
      this.assistantTimer = this.h.setTimeout(() => {
        this.assistantTimer = null
        this.flushAssistant(false)
      }, ASSISTANT_TURN_GAP_MS)
    }
  }

  private flushUser(): void {
    if (this.userTimer !== null) {
      this.h.clearTimeout(this.userTimer)
      this.userTimer = null
    }
    const text = this.userBuf.trim()
    this.userBuf = ''
    if (text) this.h.userTranscript(text, true)
    if (this.assistantFlushOwed) {
      this.assistantFlushOwed = false
      this.flushAssistant(true)
    }
  }

  private flushAssistant(force: boolean): void {
    if (!force && this.userBuf.trim()) {
      // Operator still mid-sentence: keep the order user → assistant.
      this.assistantFlushOwed = true
      return
    }
    if (this.assistantTimer !== null) {
      this.h.clearTimeout(this.assistantTimer)
      this.assistantTimer = null
    }
    const text = this.assistantBuf.trim()
    this.assistantBuf = ''
    if (text) this.h.assistantTranscript(text, true)
    this.settle()
  }

  /** Back to 'listening' once nothing is owed, nothing's running on the
   *  backend, and the voice has gone quiet. */
  private settle(): void {
    if (this.pending.size > 0 || this.backendActive.size > 0) return
    if (this.assistantBuf.trim()) return
    const s = this.h.getState()
    if (s === 'thinking' || s === 'acting' || s === 'speaking') this.h.setState('listening')
  }

  // ── outbound ───────────────────────────────────────────────────────

  sendToolResult(callId: string, output: string, image?: ToolImage): void {
    this.pending.delete(callId)
    // A screenshot rides INSIDE the function_call_output: the backend is
    // the one that looks (the voice layer takes no images), and Responses
    // accepts input_text/input_image parts as a tool output.
    const itemFor = (withImage: boolean) => ({
      type: 'response.item.create',
      item: {
        type: 'function_call_output',
        call_id: callId,
        output:
          withImage && image
            ? [
                { type: 'input_text', text: output },
                {
                  type: 'input_text',
                  text: `Current screen, ${image.w}x${image.h}px, (0,0) top-left.`,
                },
                {
                  type: 'input_image',
                  image_url: `data:${image.mime ?? 'image/png'};base64,${image.b64}`,
                },
              ]
            : withImage === false && image
              ? `${output}\n(screenshot omitted: too large to send; call again or describe what you need)`
              : output,
      },
    })
    let event: Record<string, unknown> = itemFor(true)
    // An output that can't be sent strands the backend ("submit the
    // pending function call outputs") — drop the picture, never the result.
    if (image && JSON.stringify(event).length + 64 > this.h.maxMessageBytes()) {
      this.h.error('image_too_large', `screenshot for ${callId} exceeds the data-channel limit`)
      event = itemFor(false)
    }
    this.send(event)
    // Every owed output first, then ONE response.create — the backend
    // resumes seeing all of them at once.
    if (this.pending.size === 0) this.send({ type: 'response.create' })
  }

  /** Typed input (the HUD confirm buttons) goes straight to the backend,
   *  which holds the confirm token and re-calls the tool. */
  sendText(text: string): void {
    this.send({
      type: 'response.item.create',
      item: { type: 'message', role: 'user', content: [{ type: 'input_text', text }] },
    })
    if (this.pending.size === 0) this.send({ type: 'response.create' })
  }

  /** Something for the voice to say aloud in its own words — mission
   *  reports landing mid-exchange. */
  announce(text: string): void {
    this.send({ type: 'session.commentary.append', delegation_id: null, content: clip(text) })
  }

  /** Redirect the voice right now (panic). Doesn't cancel backend work. */
  interrupt(text: string): void {
    this.send({ type: 'session.instructions.append', delegation_id: null, content: clip(text) })
  }

  /** Graceful close: session.close, then wait (bounded) for session.closed
   *  so the final usage lands and billing stops before the transport
   *  goes. Resolves either way. */
  close(waitMs = CLOSE_WAIT_MS): Promise<void> {
    this.closing = true
    if (!this.started) return Promise.resolve()
    return new Promise((resolve) => {
      let done = false
      const finish = () => {
        if (done) return
        done = true
        resolve()
      }
      this.closedWaiters.push(finish)
      this.h.setTimeout(finish, waitMs)
      this.send({ type: 'session.close' })
    })
  }

  dispose(): void {
    if (this.userTimer !== null) this.h.clearTimeout(this.userTimer)
    if (this.assistantTimer !== null) this.h.clearTimeout(this.assistantTimer)
    this.userTimer = null
    this.assistantTimer = null
    this.pending.clear()
    this.backendActive.clear()
    this.closing = true
    const waiters = this.closedWaiters
    this.closedWaiters = []
    for (const w of waiters) w()
  }

  private send(event: Record<string, unknown>): void {
    if (!this.started) {
      this.h.error('live_not_started', `cannot send ${String(event.type)} before session.started`)
      return
    }
    this.h.send({ event_id: `galdr_${++this.eventSeq}`, ...event })
  }
}

const ZERO_USAGE: UsageDelta = {
  inputText: 0,
  inputAudio: 0,
  inputCached: 0,
  outputText: 0,
  outputAudio: 0,
  totalTokens: 0,
}

function str(v: unknown): string {
  return typeof v === 'string' ? v : ''
}

function num(v: unknown): number {
  return typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : 0
}

/** Appends take ≤500 tokens; ~4 chars/token, with margin. */
function clip(text: string): string {
  return text.length > 1600 ? `${text.slice(0, 1597)}...` : text
}
