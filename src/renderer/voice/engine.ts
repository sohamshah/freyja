// VoiceEngine — WebRTC leg of the Galdr voice agent (docs/GALDR-BUILD.md §7.1).
//
// Owns the browser half of a voice exchange: mic capture, the
// RTCPeerConnection to the OpenAI Realtime API, the `oai-events` data
// channel, remote audio playback, and the engine-side state machine.
// Audio NEVER crosses the bridge IPC — only tool calls and transcripts
// do, and those are forwarded by voice-store, not by this class.
//
// This file must stay import-light: shared event types only, no React,
// no components. voice-store owns the singleton instance.

export type VoiceEngineState =
  | 'idle'
  | 'minting'
  | 'connecting'
  | 'listening'
  | 'thinking'
  | 'acting'
  | 'speaking'
  | 'error'
  | 'closing'

export type EngineEvents = {
  state: (s: VoiceEngineState) => void
  userTranscript: (text: string, final: boolean) => void
  assistantTranscript: (text: string, done: boolean) => void
  /** Store forwards to bridge as voice_tool_call. */
  toolCall: (callId: string, name: string, argumentsJson: string) => void
  /** Mic level 0..1, ~30 Hz, for the waveform/sigil. */
  level: (rms: number) => void
  closed: (reason: string) => void
  error: (code: string, message: string) => void
}

/** Thrown by start() when the environment has no WebRTC/mic surface
 *  (jsdom tests, plain-browser design review). voice-store maps this
 *  to demo mode instead of surfacing an error. */
export class VoiceEngineUnavailableError extends Error {
  readonly code = 'webrtc_unavailable'
  constructor() {
    super('WebRTC / mediaDevices unavailable in this environment')
    this.name = 'VoiceEngineUnavailableError'
  }
}

/** How long we keep the remote audio element muted after response.cancel
 *  — long enough to swallow the buffered tail of the cancelled response
 *  without eating the start of the next one. */
const CANCEL_MUTE_MS = 300

/** Level-meter emission cadence (~30 Hz). rAF fires at display rate; we
 *  throttle so the store isn't hammered at 120 Hz on ProMotion panels. */
const LEVEL_INTERVAL_MS = 33

export class VoiceEngine {
  private _state: VoiceEngineState = 'idle'
  private listeners: { [K in keyof EngineEvents]: Set<EngineEvents[K]> } = {
    state: new Set(),
    userTranscript: new Set(),
    assistantTranscript: new Set(),
    toolCall: new Set(),
    level: new Set(),
    closed: new Set(),
    error: new Set(),
  }

  private pc: RTCPeerConnection | null = null
  private dc: RTCDataChannel | null = null
  private micStream: MediaStream | null = null
  private audioEl: HTMLAudioElement | null = null
  private audioCtx: AudioContext | null = null
  private levelRaf: number | null = null
  private cancelMuteTimer: number | null = null
  /** Start generation — bumped by every start() and stop() so an
   *  in-flight start can detect it was superseded mid-await (stop()
   *  during the mic-permission prompt, a re-entrant start) and release
   *  what it acquired instead of resurrecting a dead session with a hot
   *  mic nobody owns. */
  private startGen = 0

  // ── Per-session protocol bookkeeping ──────────────────────────────
  /** function_call argument deltas accumulated per call_id until the
   *  output_item.done arrives with the authoritative JSON. */
  private fnArgsBuf = new Map<string, string>()
  /** Tool calls we've surfaced but not yet answered via sendToolResult.
   *  While non-empty, response.done keeps us in 'acting' instead of
   *  flipping back to 'listening'. */
  private pendingToolCalls = new Set<string>()
  /** User speech transcript accumulated per conversation item id. */
  private userTranscriptBuf = new Map<string, string>()
  /** Assistant audio transcript for the CURRENT response (reset on
   *  response.created — responses are serial on a realtime session). */
  private assistantTranscriptBuf = ''
  private sawAudioDeltaThisResponse = false
  /** Barge-in: speech_started while the assistant is audible mutes the
   *  remote element; cleared on the next response.created. */
  private muteUntilNextResponse = false

  get state(): VoiceEngineState {
    return this._state
  }

  on<K extends keyof EngineEvents>(k: K, fn: EngineEvents[K]): () => void {
    this.listeners[k].add(fn)
    return () => {
      this.listeners[k].delete(fn)
    }
  }

  private emit<K extends keyof EngineEvents>(
    k: K,
    ...args: Parameters<EngineEvents[K]>
  ): void {
    for (const fn of this.listeners[k]) {
      try {
        ;(fn as (...a: Parameters<EngineEvents[K]>) => void)(...args)
      } catch (err) {
        console.error(`[voice-engine] ${k} listener error`, err)
      }
    }
  }

  private setState(s: VoiceEngineState): void {
    if (this._state === s) return
    this._state = s
    this.emit('state', s)
  }

  // ── Lifecycle ─────────────────────────────────────────────────────

  async start(ready: {
    clientSecret: string
    model: string
    webrtcUrl: string
  }): Promise<void> {
    if (
      typeof window === 'undefined' ||
      typeof RTCPeerConnection === 'undefined' ||
      typeof navigator === 'undefined' ||
      !navigator.mediaDevices?.getUserMedia
    ) {
      throw new VoiceEngineUnavailableError()
    }
    const gen = ++this.startGen
    // Defensive re-entrancy: a stray second start tears down the first
    // connection rather than leaking a live mic track. Plain teardown —
    // NOT stop() — so no 'closed' event fires and the store can't
    // mistake this internal restart for the whole exchange ending.
    if (this.pc || this.micStream) {
      await this.teardown()
      if (gen !== this.startGen) return
    }

    this.setState('connecting')
    try {
      const mic = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true,
        },
      })
      if (gen !== this.startGen) {
        // stop() ran while the permission prompt was up — the exchange
        // is over; don't let the just-granted mic go hot.
        for (const track of mic.getTracks()) {
          try {
            track.stop()
          } catch {
            /* best effort */
          }
        }
        return
      }
      this.micStream = mic

      const pc = new RTCPeerConnection()
      this.pc = pc

      // Hidden element for the remote audio track. Created per session
      // and removed in stop() so a closed exchange can't keep playing.
      const audio = document.createElement('audio')
      audio.autoplay = true
      audio.style.display = 'none'
      document.body.appendChild(audio)
      this.audioEl = audio

      pc.ontrack = (e) => {
        if (!this.audioEl) return
        this.audioEl.srcObject = e.streams[0] ?? new MediaStream([e.track])
      }
      pc.onconnectionstatechange = () => {
        // 'disconnected' can self-heal (ICE flaps); only 'failed' is
        // terminal. The store reacts to the error state by ending the
        // session cleanly.
        if (this.pc !== pc) return
        if (pc.connectionState === 'failed') {
          this.emit('error', 'webrtc_failed', 'WebRTC connection failed')
          this.setState('error')
        }
      }

      for (const track of mic.getAudioTracks()) pc.addTrack(track, mic)

      const dc = pc.createDataChannel('oai-events')
      this.dc = dc
      dc.onmessage = (e) => {
        let parsed: unknown
        try {
          parsed = JSON.parse(String(e.data))
        } catch {
          return // non-JSON frames are ignored silently, per contract
        }
        try {
          this.handleServerEvent(parsed)
        } catch (err) {
          console.error('[voice-engine] server event handler error', err)
        }
      }

      const offer = await pc.createOffer()
      // Past this point everything acquired is on this.* — a superseding
      // start()/stop() bumped the generation AND ran teardown, so stale
      // continuations just step aside; nothing of theirs is still live.
      if (gen !== this.startGen) return
      await pc.setLocalDescription(offer)
      if (gen !== this.startGen) return

      const res = await fetch(ready.webrtcUrl, {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${ready.clientSecret}`,
          'Content-Type': 'application/sdp',
        },
        body: offer.sdp ?? '',
      })
      if (gen !== this.startGen) return
      if (!res.ok) {
        const detail = await res.text().catch(() => '')
        throw new Error(
          `SDP exchange failed: HTTP ${res.status} ${detail.slice(0, 200)}`,
        )
      }
      const answerSdp = await res.text()
      if (gen !== this.startGen) return
      await pc.setRemoteDescription({ type: 'answer', sdp: answerSdp })
      if (gen !== this.startGen) return

      this.startLevelMeter(mic)
      // connecting → listening happens on session.created over the data
      // channel (§7.1) — the DC being open is implied by receiving it.
    } catch (err) {
      // Release everything acquired so far (esp. the live mic track) and
      // rethrow — the store owns user-facing failure state and messaging
      // (it keeps the HUD open on the error row), so land in 'idle', not
      // 'error': everything is genuinely released. Superseded starts were
      // already torn down by whoever bumped the generation — don't
      // destroy the successor's resources.
      if (gen === this.startGen) {
        await this.teardown()
        if (gen === this.startGen) this.setState('idle')
      }
      throw err
    }
  }

  async stop(reason: string): Promise<void> {
    // Invalidate any in-flight start() FIRST — even when there's nothing
    // to tear down yet (start may be parked at getUserMedia, holding
    // nothing but about to acquire the mic).
    this.startGen++
    if (this._state === 'idle' && !this.pc && !this.micStream) return
    this.setState('closing')
    await this.teardown()
    this.setState('idle')
    this.emit('closed', reason)
  }

  private async teardown(): Promise<void> {
    if (this.levelRaf !== null) {
      cancelAnimationFrame(this.levelRaf)
      this.levelRaf = null
    }
    if (this.cancelMuteTimer !== null) {
      window.clearTimeout(this.cancelMuteTimer)
      this.cancelMuteTimer = null
    }
    if (this.audioCtx) {
      try {
        await this.audioCtx.close()
      } catch {
        /* already closed */
      }
      this.audioCtx = null
    }
    if (this.micStream) {
      for (const track of this.micStream.getTracks()) {
        try {
          track.stop()
        } catch {
          /* best effort */
        }
      }
      this.micStream = null
    }
    if (this.dc) {
      try {
        this.dc.close()
      } catch {
        /* best effort */
      }
      this.dc = null
    }
    if (this.pc) {
      try {
        this.pc.close()
      } catch {
        /* best effort */
      }
      this.pc = null
    }
    if (this.audioEl) {
      try {
        this.audioEl.pause()
        this.audioEl.srcObject = null
        this.audioEl.remove()
      } catch {
        /* best effort */
      }
      this.audioEl = null
    }
    this.fnArgsBuf.clear()
    this.pendingToolCalls.clear()
    this.userTranscriptBuf.clear()
    this.assistantTranscriptBuf = ''
    this.sawAudioDeltaThisResponse = false
    this.muteUntilNextResponse = false
  }

  // ── Outbound (renderer → model) ───────────────────────────────────

  sendToolResult(callId: string, outputJson: string): void {
    this.pendingToolCalls.delete(callId)
    this.sendEvent({
      type: 'conversation.item.create',
      item: {
        type: 'function_call_output',
        call_id: callId,
        output: outputJson,
      },
    })
    // One response may carry SEVERAL act calls (parallel tool calls are
    // on by default). Responses are serial on a realtime session — a
    // response.create while results are still owed gets rejected and the
    // in-flight response never voices the later outcomes. Batch: only
    // the LAST owed result kicks the follow-up response, which then sees
    // every function_call_output at once.
    if (this.pendingToolCalls.size === 0) {
      this.sendEvent({ type: 'response.create' })
    }
  }

  sendText(text: string): void {
    this.sendEvent({
      type: 'conversation.item.create',
      item: {
        type: 'message',
        role: 'user',
        content: [{ type: 'input_text', text }],
      },
    })
    this.sendEvent({ type: 'response.create' })
  }

  cancelResponse(): void {
    this.sendEvent({ type: 'response.cancel' })
    // The remote track keeps flowing for a beat after the cancel (audio
    // already buffered client-side); mute briefly so the user hears the
    // model actually stop. response.created also unmutes, whichever
    // comes first.
    if (this.audioEl) {
      this.audioEl.muted = true
      if (this.cancelMuteTimer !== null) window.clearTimeout(this.cancelMuteTimer)
      this.cancelMuteTimer = window.setTimeout(() => {
        this.cancelMuteTimer = null
        if (this.audioEl && !this.muteUntilNextResponse) this.audioEl.muted = false
      }, CANCEL_MUTE_MS)
    }
  }

  private sendEvent(event: Record<string, unknown>): void {
    if (!this.dc || this.dc.readyState !== 'open') {
      this.emit(
        'error',
        'data_channel_closed',
        `cannot send ${String(event.type)} — data channel not open`,
      )
      return
    }
    try {
      this.dc.send(JSON.stringify(event))
    } catch (err) {
      this.emit('error', 'send_failed', err instanceof Error ? err.message : String(err))
    }
  }

  // ── Inbound (model → renderer), data-channel JSON ─────────────────

  private handleServerEvent(raw: unknown): void {
    if (typeof raw !== 'object' || raw === null) return
    const ev = raw as Record<string, unknown>
    const type = typeof ev.type === 'string' ? ev.type : ''

    switch (type) {
      case 'session.created': {
        // DC open + session.created ⇒ the exchange is live (§7.1).
        if (this._state === 'connecting') this.setState('listening')
        return
      }

      case 'input_audio_buffer.speech_started': {
        // Barge-in: the server (semantic/server VAD) handles truncating
        // the assistant's response; locally we mute the already-buffered
        // audio tail until the next response starts so the user isn't
        // talked over. Muting (vs pausing) keeps the WebRTC track's clock
        // running so nothing desyncs.
        if (this._state === 'speaking' && this.audioEl) {
          this.audioEl.muted = true
          this.muteUntilNextResponse = true
        }
        // Keep 'acting' while a tool result is still owed — the verb chip
        // should not vanish mid-execution just because the user spoke.
        if (this.pendingToolCalls.size === 0) this.setState('listening')
        return
      }

      case 'response.created': {
        this.assistantTranscriptBuf = ''
        this.sawAudioDeltaThisResponse = false
        if (this.muteUntilNextResponse || this.cancelMuteTimer !== null) {
          this.muteUntilNextResponse = false
          if (this.cancelMuteTimer !== null) {
            window.clearTimeout(this.cancelMuteTimer)
            this.cancelMuteTimer = null
          }
          if (this.audioEl) this.audioEl.muted = false
        }
        this.setState('thinking')
        return
      }

      case 'response.function_call_arguments.delta': {
        const callId = typeof ev.call_id === 'string' ? ev.call_id : ''
        const delta = typeof ev.delta === 'string' ? ev.delta : ''
        if (!callId) return
        this.fnArgsBuf.set(callId, (this.fnArgsBuf.get(callId) ?? '') + delta)
        if (this._state !== 'acting') this.setState('acting')
        return
      }

      case 'response.function_call_arguments.done': {
        // Authoritative full JSON — replaces whatever we accumulated.
        const callId = typeof ev.call_id === 'string' ? ev.call_id : ''
        if (callId && typeof ev.arguments === 'string') {
          this.fnArgsBuf.set(callId, ev.arguments)
        }
        return
      }

      case 'response.output_item.done': {
        const item = ev.item as Record<string, unknown> | undefined
        if (!item || item.type !== 'function_call') return
        const callId = typeof item.call_id === 'string' ? item.call_id : ''
        const name = typeof item.name === 'string' ? item.name : ''
        if (!callId || !name) return
        const argumentsJson =
          typeof item.arguments === 'string' && item.arguments.length > 0
            ? item.arguments
            : this.fnArgsBuf.get(callId) ?? '{}'
        this.fnArgsBuf.delete(callId)
        this.pendingToolCalls.add(callId)
        if (this._state !== 'acting') this.setState('acting')
        this.emit('toolCall', callId, name, argumentsJson)
        return
      }

      case 'conversation.item.input_audio_transcription.delta': {
        const itemId = typeof ev.item_id === 'string' ? ev.item_id : ''
        const delta = typeof ev.delta === 'string' ? ev.delta : ''
        if (!itemId) return
        const acc = (this.userTranscriptBuf.get(itemId) ?? '') + delta
        this.userTranscriptBuf.set(itemId, acc)
        // Emit the ACCUMULATED text (not the delta) so consumers can
        // simply replace their line rather than concatenate.
        this.emit('userTranscript', acc, false)
        return
      }

      case 'conversation.item.input_audio_transcription.completed': {
        const itemId = typeof ev.item_id === 'string' ? ev.item_id : ''
        const text =
          typeof ev.transcript === 'string'
            ? ev.transcript
            : this.userTranscriptBuf.get(itemId) ?? ''
        this.userTranscriptBuf.delete(itemId)
        if (text) this.emit('userTranscript', text, true)
        return
      }

      case 'response.output_audio_transcript.delta': {
        const delta = typeof ev.delta === 'string' ? ev.delta : ''
        this.assistantTranscriptBuf += delta
        if (!this.sawAudioDeltaThisResponse) {
          this.sawAudioDeltaThisResponse = true
          this.setState('speaking')
        }
        this.emit('assistantTranscript', this.assistantTranscriptBuf, false)
        return
      }

      case 'response.output_audio_transcript.done': {
        const text =
          typeof ev.transcript === 'string'
            ? ev.transcript
            : this.assistantTranscriptBuf
        if (text) this.emit('assistantTranscript', text, true)
        return
      }

      case 'response.done': {
        // Back to listening unless a tool result is still owed — in that
        // case sendToolResult's response.create re-enters the cycle.
        if (this.pendingToolCalls.size === 0) this.setState('listening')
        return
      }

      case 'error': {
        // Server-side error event. Deliberately NOT a state change:
        // most of these are benign protocol nags (e.g. response.cancel
        // with nothing active); transport death is caught by
        // onconnectionstatechange instead.
        const err = ev.error as Record<string, unknown> | undefined
        const code = typeof err?.code === 'string' ? err.code : 'server_error'
        const message =
          typeof err?.message === 'string' ? err.message : JSON.stringify(ev).slice(0, 300)
        this.emit('error', code, message)
        return
      }

      default:
        // Unknown event types are ignored silently (contract §7.1) —
        // the GA protocol adds events without notice.
        return
    }
  }

  // ── Mic level meter ───────────────────────────────────────────────

  private startLevelMeter(stream: MediaStream): void {
    // Progressive enhancement — no AudioContext (old jsdom, weird
    // embeds) just means no waveform, not a dead session.
    const Ctx: typeof AudioContext | undefined =
      window.AudioContext ??
      (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext
    if (!Ctx) return
    let ctx: AudioContext
    try {
      ctx = new Ctx()
      const src = ctx.createMediaStreamSource(stream)
      const analyser = ctx.createAnalyser()
      analyser.fftSize = 2048
      src.connect(analyser)
      this.audioCtx = ctx
      const data = new Float32Array(analyser.fftSize)
      let lastEmit = 0
      const loop = () => {
        this.levelRaf = requestAnimationFrame(loop)
        const now = performance.now()
        if (now - lastEmit < LEVEL_INTERVAL_MS) return
        lastEmit = now
        analyser.getFloatTimeDomainData(data)
        let sum = 0
        for (let i = 0; i < data.length; i++) sum += data[i] * data[i]
        const rms = Math.sqrt(sum / data.length)
        // Conversational speech RMS peaks around 0.25 after AGC — scale
        // so a normal voice fills most of the 0..1 range for the sigil.
        this.emit('level', Math.min(1, rms * 4))
      }
      this.levelRaf = requestAnimationFrame(loop)
    } catch (err) {
      console.warn('[voice-engine] level meter unavailable:', err)
    }
  }
}
