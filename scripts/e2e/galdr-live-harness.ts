// Browser half of scripts/e2e_galdr_live.py: drives the REAL VoiceEngine
// (GPT-Live transport) in headless Chrome. The "mic" plays a spoken
// request; SDP and tool calls go to the Python harness, which runs the
// real VoiceService. Everything observable is POSTed to /log.

import { VoiceEngine } from '../../src/renderer/voice/engine'

const log = (kind: string, data: unknown) =>
  void fetch('/log', { method: 'POST', body: JSON.stringify({ t: performance.now(), kind, data }) })

// The mic is a WebAudio stream playing /utterance.wav. A real capture
// device (even Chromium's fake one) makes macOS ask for microphone
// access, which a scripted run can't answer — getUserMedia just hangs.
// The engine still gets a genuine MediaStream and sends it over WebRTC.
navigator.mediaDevices.getUserMedia = async () => {
  const ctx = new AudioContext({ sampleRate: 48000 })
  const buf = await ctx.decodeAudioData(await (await fetch('/utterance.wav')).arrayBuffer())
  const src = ctx.createBufferSource()
  src.buffer = buf
  const dest = ctx.createMediaStreamDestination()
  src.connect(dest)
  src.start()
  log('mic', `playing ${buf.duration.toFixed(1)}s`)
  return dest.stream
}

// Tap the data channel (without touching the engine) so the run shows
// every protocol event except the high-rate transcript/audio deltas.
const QUIET = /(transcript|audio|arguments|output_text|content_part)\.(delta|added)|output_text\.delta/
const createDataChannel = RTCPeerConnection.prototype.createDataChannel
RTCPeerConnection.prototype.createDataChannel = function (label, init) {
  const dc = createDataChannel.call(this, label, init)
  dc.addEventListener('message', (e) => {
    try {
      const ev = JSON.parse(String(e.data))
      const inner = ev.type === 'response.event' ? `/${ev.event?.type}` : ''
      if (!QUIET.test(ev.type + inner)) log('dc', `${ev.type}${inner}`)
    } catch {
      /* ignore */
    }
  })
  return dc
}

const engine = new VoiceEngine()
engine.on('state', (s) => log('state', s))
engine.on('userTranscript', (text, final) => final && log('user', text))
engine.on('assistantTranscript', (text, done) => done && log('assistant', text))
engine.on('usage', (u) => log('usage', u))
engine.on('backendText', (text) => log('backend', text))
engine.on('error', (code, message) => log('error', { code, message }))
engine.on('closed', (reason) => log('closed', reason))
engine.on('toolCall', async (callId, name, argumentsJson) => {
  log('toolCall', { callId, name, argumentsJson })
  const res = await fetch('/tool', {
    method: 'POST',
    body: JSON.stringify({ callId, name, argumentsJson }),
  })
  const ev = await res.json()
  log('toolResult', { callId, ok: ev.ok, output: String(ev.output).slice(0, 300), image: ev.imageB64 ? `${ev.imageMime} ${Math.round(ev.imageB64.length / 1024)}KiB` : null })
  const image =
    ev.imageB64 && typeof ev.imageW === 'number'
      ? { b64: ev.imageB64, w: ev.imageW, h: ev.imageH, mime: ev.imageMime }
      : undefined
  engine.sendToolResult(callId, ev.output, image)
})

;(window as unknown as { stopVoice: () => Promise<void> }).stopVoice = () => engine.stop('harness')

engine
  .start({
    transport: 'live',
    model: 'gpt-live-1',
    exchangeSdp: async (sdp) => {
      const res = await fetch('/connect', { method: 'POST', body: JSON.stringify({ sdp }) })
      const ev = await res.json()
      if (!ev.ok) throw new Error(ev.error)
      log('connected', ev.liveSessionId)
      return ev.sdp
    },
  })
  .catch((err) => log('start_failed', String(err)))
