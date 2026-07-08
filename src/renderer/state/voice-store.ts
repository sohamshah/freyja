import { create } from 'zustand'
import type { BridgeCommand, BridgeEvent, Receipt, VoiceConfig } from '../../shared/events'
import {
  VoiceEngine,
  VoiceEngineUnavailableError,
  type VoiceEngineState,
} from '../voice/engine'
import { startVoiceDemo, type VoiceDemoHandle } from '../voice/demoDriver'

// Voice-store — renderer state for the Galdr voice exchange
// (docs/GALDR-BUILD.md §7.2). Scheduler-store pattern: a separate
// zustand store, bound to the bridge via bindVoiceBridge({send}),
// fed by handleEvent(ev) from App.tsx's flushBridgeEvents mirror.
// Owns the singleton VoiceEngine — components never touch it.

export interface VoiceActivity {
  verb: string
  status: 'running' | 'ok' | 'fail' | 'confirm'
  summary: string
}

interface VoiceStore {
  engineState: VoiceEngineState
  active: boolean
  voiceSessionId: string | null
  userLine: string
  assistantLine: string
  activity: VoiceActivity | null
  receipts: Receipt[]
  config: VoiceConfig | null
  micLevel: number
  error: string | null
  hudOpen: boolean

  toggleVoice(): void
  endVoice(reason: string): void
  typedCommand(text: string): void
  undo(receiptId: string): void
  confirmPending(approve: boolean): void
  setConfigPatch(patch: {
    enabled?: boolean
    model?: string
    voice?: string
    vadMode?: 'semantic_vad' | 'server_vad'
    idleTimeoutSec?: number
  }): void
  hydrate(): void
  handleEvent(event: BridgeEvent): void
}

// ── Module-level session machinery (not reactive state) ─────────────

let _bridge: { send: (cmd: BridgeCommand) => void } | null = null
let _engine: VoiceEngine | null = null
let _demo: VoiceDemoHandle | null = null
let _idleTimer: number | null = null
let _sessionStartedAt = 0
/** Exchange generation — bumped whenever an exchange starts or ends so
 *  stale async continuations (the rejection handler of an engine.start
 *  whose exchange was already toggled away) can detect they've been
 *  superseded and stand down instead of mutating the new exchange. */
let _sessionGen = 0
/** Model-originated function-call ids awaiting a voice_tool_result.
 *  Typed-command / undo results reuse the voice_tool_result event but
 *  have no function_call in the realtime conversation to answer, so
 *  only ids in this set get relayed back via sendToolResult. */
const _pendingCallIds = new Set<string>()

/** Mic level above which the operator counts as "still talking" for the
 *  idle timer. Sits above the breathing-room noise floor (~0.08 after
 *  the engine's gain) but well below conversational speech. */
const LEVEL_ACTIVITY_THRESHOLD = 0.12
const DEFAULT_IDLE_TIMEOUT_SEC = 25

export function bindVoiceBridge(bridge: { send: (cmd: BridgeCommand) => void }): void {
  _bridge = bridge
}

function _send(cmd: BridgeCommand): void {
  if (_bridge === null) return
  try {
    _bridge.send(cmd)
  } catch (err) {
    console.error('[voice-store] send failed', err)
  }
}

function clearIdleTimer(): void {
  if (_idleTimer !== null) {
    window.clearTimeout(_idleTimer)
    _idleTimer = null
  }
}

/** Re-arm the auto-close countdown. Called on any sign of life:
 *  transcripts (either side), tool activity, or mic level above the
 *  talking threshold. Demo sessions never idle out — the scripted walk
 *  loops forever for design review. */
function resetIdleTimer(): void {
  const s = useVoiceStore.getState()
  if (!s.active || _demo !== null) return
  clearIdleTimer()
  const timeoutSec = s.config?.idleTimeoutSec ?? DEFAULT_IDLE_TIMEOUT_SEC
  _idleTimer = window.setTimeout(() => {
    _idleTimer = null
    useVoiceStore.getState().endVoice('idle')
  }, Math.max(5, timeoutSec) * 1000)
}

/** Prepend-with-dedupe. voice_tool_result may carry the same receipt
 *  that voice_receipt also broadcasts, and undo re-emits an existing
 *  receipt with undone=true — upserting by id handles all three. */
function upsertReceipt(receipt: Receipt): void {
  useVoiceStore.setState((s) => {
    const idx = s.receipts.findIndex((r) => r.id === receipt.id)
    if (idx >= 0) {
      const next = [...s.receipts]
      next[idx] = receipt
      return { receipts: next }
    }
    return { receipts: [receipt, ...s.receipts].slice(0, 50) }
  })
}

/** Close a FAILED exchange out (engine start rejected, transport died
 *  mid-session): stop the engine and end the bridge session like
 *  endVoice, but PARK the HUD on the error row — hudOpen stays true with
 *  engineState 'error' so VoiceHUD renders the message and the
 *  "⌥space to retry" hint, mirroring the mint-failure path in
 *  voice_error. Esc / ⌥Space dismisses via endVoice's inactive branch. */
function failVoice(message?: string): void {
  clearIdleTimer()
  _sessionGen++
  const s = useVoiceStore.getState()
  const voiceSessionId = s.voiceSessionId
  const seconds =
    _sessionStartedAt > 0
      ? Math.max(0, Math.round((Date.now() - _sessionStartedAt) / 1000))
      : 0
  _sessionStartedAt = 0
  _pendingCallIds.clear()
  // `active` flips BEFORE engine.stop so its closed event (checked
  // against `active`) can't re-enter endVoice and unmount the HUD.
  useVoiceStore.setState({
    active: false,
    voiceSessionId: null,
    engineState: 'error',
    micLevel: 0,
    activity: null,
    error: message ?? s.error ?? 'voice session failed',
  })
  if (_engine !== null) {
    void _engine.stop('error').catch(() => {})
  }
  if (voiceSessionId && voiceSessionId !== 'voice-demo') {
    _send({ type: 'voice_session_end', voiceSessionId, reason: 'error', stats: { seconds } })
  }
}

/** Human message for engine.start() rejections. getUserMedia failures
 *  arrive as DOMExceptions whose .name carries the story; everything
 *  else (SDP exchange, WebRTC internals) already has a useful message. */
function describeEngineStartError(err: unknown): string {
  const name = err instanceof Error ? err.name : ''
  if (name === 'NotAllowedError' || name === 'PermissionDeniedError') {
    return 'mic access denied — System Settings › Privacy & Security › Microphone'
  }
  if (name === 'NotFoundError' || name === 'DevicesNotFoundError') {
    return 'no microphone found'
  }
  return err instanceof Error ? err.message : String(err)
}

/** Pull the human `summary` out of a tool-result `output` payload (a
 *  JSON string addressed to the model). */
function outputSummary(output: string): string | null {
  try {
    const parsed: unknown = JSON.parse(output)
    if (typeof parsed === 'object' && parsed !== null) {
      const summary = (parsed as { summary?: unknown }).summary
      if (typeof summary === 'string' && summary) return summary
    }
  } catch {
    /* not JSON — nothing to surface */
  }
  return null
}

function startDemo(): void {
  if (_demo !== null) return
  _sessionStartedAt = Date.now()
  useVoiceStore.setState({ voiceSessionId: 'voice-demo' })
  _demo = startVoiceDemo({
    setState: (s) => useVoiceStore.setState({ engineState: s }),
    setUserLine: (text) => useVoiceStore.setState({ userLine: text }),
    setAssistantLine: (text) => useVoiceStore.setState({ assistantLine: text }),
    setLevel: (level) => useVoiceStore.setState({ micLevel: level }),
    setActivity: (activity) => useVoiceStore.setState({ activity }),
    addReceipt: (receipt) => upsertReceipt(receipt),
  })
}

/** Lazily construct the singleton engine and wire its events into the
 *  store exactly once. The engine survives across sessions (start/stop
 *  are its lifecycle); listeners are process-lifetime. */
function ensureEngine(): VoiceEngine {
  if (_engine !== null) return _engine
  const engine = new VoiceEngine()

  engine.on('state', (s) => {
    const st = useVoiceStore.getState()
    // Once failVoice has parked the HUD on the error row, the engine's
    // own closing→idle teardown must not clobber it — the error stays
    // visible until the operator dismisses (Esc) or retries (⌥Space).
    if (!st.active && st.engineState === 'error' && (s === 'closing' || s === 'idle')) {
      return
    }
    useVoiceStore.setState({ engineState: s })
    // 'error' is only set on fatal mid-session transport failures (ICE
    // gave up) — close the session out, but keep the HUD open showing
    // what happened: a silent flash-and-vanish tells the operator
    // nothing (start()-rejection failures route through failVoice via
    // the .catch in voice_session_ready instead).
    if (s === 'error' && st.active) {
      failVoice()
    }
  })

  engine.on('userTranscript', (text, final) => {
    useVoiceStore.setState({ userLine: text })
    resetIdleTimer()
    // Forward every transcript (partials included) — the bridge journals
    // finals and panic-scans partials for the floor stop-words.
    const { voiceSessionId } = useVoiceStore.getState()
    if (voiceSessionId) {
      _send({ type: 'voice_transcript', voiceSessionId, role: 'user', text, final })
    }
  })

  engine.on('assistantTranscript', (text, done) => {
    useVoiceStore.setState({ assistantLine: text })
    resetIdleTimer()
    const { voiceSessionId } = useVoiceStore.getState()
    if (voiceSessionId) {
      _send({ type: 'voice_transcript', voiceSessionId, role: 'assistant', text, final: done })
    }
  })

  engine.on('toolCall', (callId, name, argumentsJson) => {
    resetIdleTimer()
    _pendingCallIds.add(callId)
    // The single `act` tool wraps every verb — surface the inner verb on
    // the HUD chip when the arguments parse, the tool name otherwise.
    let verb = name
    try {
      const parsed: unknown = JSON.parse(argumentsJson)
      if (
        typeof parsed === 'object' &&
        parsed !== null &&
        typeof (parsed as { verb?: unknown }).verb === 'string'
      ) {
        verb = (parsed as { verb: string }).verb
      }
    } catch {
      /* malformed args — the bridge will refuse; keep the tool name */
    }
    useVoiceStore.setState({ activity: { verb, status: 'running', summary: '' } })
    const s = useVoiceStore.getState()
    if (s.voiceSessionId) {
      _send({
        type: 'voice_tool_call',
        voiceSessionId: s.voiceSessionId,
        callId,
        name,
        argumentsJson,
        heard: s.userLine || undefined,
      })
    }
  })

  engine.on('level', (rms) => {
    useVoiceStore.setState({ micLevel: rms })
    if (rms > LEVEL_ACTIVITY_THRESHOLD) resetIdleTimer()
  })

  engine.on('closed', (reason) => {
    // Engine closed underneath us (endVoice flips `active` before it
    // calls engine.stop, so this only fires standalone on engine-side
    // teardown paths) — mirror it into session bookkeeping.
    if (useVoiceStore.getState().active) {
      useVoiceStore.getState().endVoice(reason)
    }
  })

  engine.on('error', (code, message) => {
    useVoiceStore.setState({ error: `${code}: ${message}` })
  })

  _engine = engine
  return engine
}

export const useVoiceStore = create<VoiceStore>((set, get) => ({
  engineState: 'idle',
  active: false,
  voiceSessionId: null,
  userLine: '',
  assistantLine: '',
  activity: null,
  receipts: [],
  config: null,
  micLevel: 0,
  error: null,
  hudOpen: false,

  toggleVoice() {
    const s = get()
    if (s.active) {
      s.endVoice('toggle')
      return
    }
    // Alt+Space is registered unconditionally in main (contract §7.4 —
    // "the renderer decides whether voice is enabled"), so THIS is where
    // the Settings toggle is enforced: never open a live-mic exchange
    // while voice is switched off or unmintable. The bridge refuses too
    // (voice_disabled / no_api_key), but the operator gets immediate,
    // visible feedback here instead of a silent hot mic.
    if (s.config && (!s.config.enabled || !s.config.hasApiKey)) {
      set({
        active: false,
        hudOpen: true,
        engineState: 'error',
        error: s.config.enabled
          ? 'voice needs an OpenAI API key — set OPENAI_API_KEY for the bridge'
          : 'voice is disabled — enable it in settings',
        userLine: '',
        assistantLine: '',
        activity: null,
        micLevel: 0,
      })
      return
    }
    _sessionGen++
    set({
      active: true,
      hudOpen: true,
      error: null,
      userLine: '',
      assistantLine: '',
      activity: null,
      micLevel: 0,
    })
    // Headless review path: no Electron preload, or ?voicedemo=1 forces
    // the scripted walk even inside the app (screenshot rigs).
    const inDemo =
      typeof window !== 'undefined' &&
      (!(window as unknown as { harness?: unknown }).harness ||
        new URLSearchParams(window.location.search).get('voicedemo') === '1')
    if (inDemo) {
      startDemo()
      return
    }
    if (_bridge === null) {
      // window.harness exists but nothing bound the voice bridge — a
      // wiring bug, not an operator condition. Fail loudly and reset.
      set({ active: false, hudOpen: false, engineState: 'error', error: 'voice bridge not bound' })
      return
    }
    set({ engineState: 'minting' })
    _sessionStartedAt = Date.now()
    _send({ type: 'voice_session_start' })
  },

  endVoice(reason) {
    clearIdleTimer()
    const s = get()
    if (!s.active) {
      // e.g. HUD left open to show a mint/engine error — dismiss it and
      // clear the parked error state so the sigil returns to idle.
      if (s.hudOpen || s.engineState === 'error') {
        set({ hudOpen: false, engineState: 'idle', error: null })
      }
      return
    }
    _sessionGen++
    if (_demo !== null) {
      _demo.stop()
      _demo = null
    }
    const wasDemo = s.voiceSessionId === 'voice-demo'
    const voiceSessionId = s.voiceSessionId
    const seconds =
      _sessionStartedAt > 0
        ? Math.max(0, Math.round((Date.now() - _sessionStartedAt) / 1000))
        : 0
    _sessionStartedAt = 0
    _pendingCallIds.clear()
    // Flip `active` BEFORE stopping the engine — its closed event checks
    // `active` to decide whether to re-enter endVoice.
    set({
      active: false,
      hudOpen: false,
      voiceSessionId: null,
      engineState: 'idle',
      micLevel: 0,
      activity: null,
      userLine: '',
      assistantLine: '',
    })
    if (_engine !== null) {
      void _engine.stop(reason).catch(() => {})
    }
    if (voiceSessionId && !wasDemo) {
      _send({ type: 'voice_session_end', voiceSessionId, reason, stats: { seconds } })
    }
  },

  typedCommand(text) {
    const trimmed = text.trim()
    if (!trimmed) return
    resetIdleTimer()
    // Floor lane: the bridge parses it deterministically and answers
    // with a voice_tool_result (callId `typed-<ts>`) — no model involved.
    _send({ type: 'voice_typed_command', text: trimmed })
  },

  undo(receiptId) {
    resetIdleTimer()
    _send({ type: 'voice_undo', receiptId })
  },

  confirmPending(approve) {
    // Conversation-only confirm: the button injects "go"/"cancel" as
    // typed speech so the CONVERSATION carries the decision — the model
    // holds the confirm_token (from the CONFIRM REQUIRED output string)
    // and re-calls act with it on assent. Tokens always flow through
    // the model, never around it (contract §4/§6), so there is no
    // renderer-side token to relay.
    resetIdleTimer()
    const s = get()
    if (_engine !== null && s.active && _demo === null) {
      _engine.sendText(approve ? 'go' : 'cancel')
    }
    if (s.activity?.status === 'confirm') {
      set({ activity: approve ? { ...s.activity, status: 'running' } : null })
    }
  },

  setConfigPatch(patch) {
    // The bridge owns ~/.freyja/voice/config.json; it echoes the merged
    // config back as a voice_config event, which is what updates state.
    _send({ type: 'voice_set_config', patch })
  },

  hydrate() {
    _send({ type: 'voice_get_config' })
    _send({ type: 'voice_receipts_list', limit: 12 })
  },

  handleEvent(event) {
    switch (event.type) {
      case 'voice_session_ready': {
        const s = get()
        if (!s.active || s.voiceSessionId !== null) {
          // Operator toggled off while the mint was in flight, OR a
          // second ready raced an exchange that already has its session
          // (⌥Space mashing double-mints). Close the surplus bridge
          // session — accepting it would start a second engine and leave
          // one of the two mic tracks live with no owner.
          _send({
            type: 'voice_session_end',
            voiceSessionId: event.voiceSessionId,
            reason: 'aborted',
            stats: { seconds: 0 },
          })
          return
        }
        set({ voiceSessionId: event.voiceSessionId })
        const engine = ensureEngine()
        resetIdleTimer()
        const gen = _sessionGen
        void engine
          .start({
            clientSecret: event.clientSecret,
            model: event.model,
            webrtcUrl: event.webrtcUrl,
          })
          .catch((err: unknown) => {
            // The exchange this start belonged to is already over
            // (toggled off / failed) — the rejection is stale news.
            if (gen !== _sessionGen) return
            if (err instanceof VoiceEngineUnavailableError) {
              // No WebRTC surface (tests, plain browser): close the real
              // session and fall back to the scripted demo walk.
              const sid = useVoiceStore.getState().voiceSessionId
              if (sid && sid !== 'voice-demo') {
                _send({
                  type: 'voice_session_end',
                  voiceSessionId: sid,
                  reason: 'webrtc_unavailable',
                  stats: { seconds: 0 },
                })
              }
              useVoiceStore.setState({ voiceSessionId: null })
              startDemo()
              return
            }
            // Mic denied / SDP exchange failed: keep the HUD open on the
            // error row (never a silent flash-and-vanish) — same
            // treatment as a mint failure.
            failVoice(describeEngineStartError(err))
          })
        return
      }

      case 'voice_tool_result': {
        resetIdleTimer()
        // Relay to the model FIRST, and even on needsConfirm — the output
        // string is how the model learns it must ask the user to confirm
        // aloud. Only model-originated call ids have a function_call in
        // the conversation to answer (see _pendingCallIds).
        if (_pendingCallIds.has(event.callId)) {
          _pendingCallIds.delete(event.callId)
          if (_engine !== null) _engine.sendToolResult(event.callId, event.output)
        }
        // Typed floor commands the grammar refused come back receipt-less
        // with the explanation ("typed commands are floor-only: …") in
        // `output` — addressed to a model that never saw the call. Chip
        // them as `floor` with that summary instead of repainting the
        // PREVIOUS verb's chip as the failure.
        let verb = event.receipt?.verb ?? get().activity?.verb ?? 'act'
        let summary = event.receipt?.summary ?? event.say ?? ''
        if (!event.receipt && event.callId.startsWith('typed-')) {
          verb = 'floor'
          summary = outputSummary(event.output) ?? summary
        }
        if (event.needsConfirm) {
          set({ activity: { verb, status: 'confirm', summary: event.needsConfirm.summary } })
        } else {
          set({ activity: { verb, status: event.ok ? 'ok' : 'fail', summary } })
        }
        if (event.receipt) upsertReceipt(event.receipt)
        return
      }

      case 'voice_receipt': {
        upsertReceipt(event.receipt)
        resetIdleTimer()
        return
      }

      case 'voice_receipts': {
        set({ receipts: event.receipts.slice(0, 50) })
        return
      }

      case 'voice_config': {
        set({ config: event.config })
        return
      }

      case 'voice_error': {
        set({ error: `${event.code}: ${event.message}` })
        const s = get()
        // Mint failure: the session never came up, so there's nothing to
        // close bridge-side. Keep the HUD open showing the error; Esc /
        // toggle dismisses it via endVoice's inactive branch.
        if (s.active && !s.voiceSessionId) {
          clearIdleTimer()
          _sessionStartedAt = 0
          set({ active: false, engineState: 'error' })
        }
        return
      }

      case 'voice_panic': {
        // Floor caught a stop-word in the live transcript: kill the
        // in-flight response and audio immediately, then end the session.
        if (_engine !== null) _engine.cancelResponse()
        get().endVoice('panic')
        return
      }

      case 'voice_session_closed': {
        const s = get()
        // Bridge-side close of a session we still think is live (bridge
        // restart, server-side timeout). Clear the id first so endVoice
        // doesn't echo a redundant voice_session_end back.
        if (s.active && s.voiceSessionId === event.voiceSessionId) {
          set({ voiceSessionId: null })
          get().endVoice(event.reason || 'closed')
        }
        return
      }

      case 'voice_timer_fired': {
        // Surface the fire on the HUD chip; the bridge also posts a
        // macOS notification, so this is glanceable state, not the alarm.
        set({
          activity: {
            verb: 'timer',
            status: 'ok',
            summary: event.label ? `⏱ ${event.label}` : '⏱ timer done',
          },
        })
        return
      }

      default:
        return
    }
  },
}))
