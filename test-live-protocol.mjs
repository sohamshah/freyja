// LiveProtocol — the GPT-Live half of the voice engine
// (src/renderer/voice/live-protocol.ts, docs/GALDR-LIVE.md). Drives the
// real class with a fake data channel and a manual clock: the backend
// tool loop, screenshot outputs, transcript turns, usage, close. Run from
// the repo root:
//   npx tsx test-live-protocol.mjs
import assert from 'node:assert/strict'

import {
  ASSISTANT_TURN_GAP_MS,
  LiveProtocol,
  USER_TURN_GAP_MS,
} from './src/renderer/voice/live-protocol.ts'

let failures = 0
async function check(name, fn) {
  try {
    await fn()
    console.log(`ok   ${name}`)
  } catch (err) {
    failures++
    console.log(`FAIL ${name}\n     ${err.message}`)
  }
}

function rig() {
  let now = 0
  let nextId = 1
  const timers = new Map()
  const sent = []
  const log = []
  const usage = []
  const errors = []
  const closed = []
  let state = 'connecting'
  const p = new LiveProtocol({
    send: (e) => sent.push(e),
    getState: () => state,
    setState: (s) => {
      state = s
      log.push(`state:${s}`)
    },
    userTranscript: (t, f) => log.push(`user${f ? '!' : ''}:${t}`),
    assistantTranscript: (t, f) => log.push(`asst${f ? '!' : ''}:${t}`),
    toolCall: (id, name, args) => log.push(`call:${id}:${name}:${args}`),
    backendText: (t) => log.push(`backend:${t}`),
    usage: (u) => usage.push(u),
    error: (code) => errors.push(code),
    serverClosed: (r) => closed.push(r),
    setTimeout: (fn, ms) => {
      const id = nextId++
      timers.set(id, { at: now + ms, fn })
      return id
    },
    clearTimeout: (id) => {
      timers.delete(id)
    },
    maxMessageBytes: () => 262_144,
  })
  const advance = (ms) => {
    const until = now + ms
    for (;;) {
      const due = [...timers.entries()]
        .filter(([, t]) => t.at <= until)
        .sort((a, b) => a[1].at - b[1].at)[0]
      if (!due) break
      now = due[1].at
      timers.delete(due[0])
      due[1].fn()
    }
    now = until
  }
  const finals = () => log.filter((l) => l.includes('!:'))
  return { p, sent, log, usage, errors, closed, advance, finals, state: () => state }
}

const started = { type: 'session.started', session: { id: 'live_1' } }
const fnCall = (callId, name, args = '{}') => ({
  type: 'response.event',
  delegation_id: 'item_1',
  event: {
    type: 'response.output_item.done',
    item: { type: 'function_call', call_id: callId, name, arguments: args },
  },
})
const backend = (type, id, usage) => ({
  type: 'response.event',
  delegation_id: 'item_1',
  event: { type, response: { id, usage } },
})

await check('session.started moves connecting → listening and unlocks sending', () => {
  const r = rig()
  r.p.announce('too early')
  assert.deepEqual(r.errors, ['live_not_started'])
  assert.equal(r.sent.length, 0)
  r.p.handle(started)
  assert.equal(r.state(), 'listening')
  r.p.announce('Mission update — build: green')
  assert.equal(r.sent[0].type, 'session.commentary.append')
  assert.equal(r.sent[0].delegation_id, null)
  assert.equal(r.sent[0].content, 'Mission update — build: green')
  assert.match(String(r.sent[0].event_id), /^galdr_\d+$/)
})

await check('backend function call → toolCall; result → item.create then ONE response.create', () => {
  const r = rig()
  r.p.handle(started)
  r.p.handle({ type: 'session.delegation.created', delegation: { id: 'item_1', target: 'responses' } })
  assert.equal(r.state(), 'thinking')
  r.p.handle(backend('response.created', 'resp_1'))
  r.p.handle(fnCall('call_a', 'computer_see'))
  r.p.handle(fnCall('call_b', 'spotify_now_playing'))
  assert.equal(r.state(), 'acting')
  assert.ok(r.log.includes('call:call_a:computer_see:{}'))
  r.p.handle(backend('response.completed', 'resp_1'))
  assert.equal(r.state(), 'acting', 'still owed results')

  r.p.sendToolResult('call_a', '{"ok":true}', { b64: 'AAAA', w: 1280, h: 800 })
  assert.deepEqual(
    r.sent.map((e) => e.type),
    ['response.item.create'],
    'no response.create while call_b is owed',
  )
  const item = r.sent[0].item
  assert.equal(item.type, 'function_call_output')
  assert.equal(item.call_id, 'call_a')
  assert.deepEqual(item.output, [
    { type: 'input_text', text: '{"ok":true}' },
    { type: 'input_text', text: 'Current screen, 1280x800px, (0,0) top-left.' },
    { type: 'input_image', image_url: 'data:image/png;base64,AAAA' },
  ])

  r.p.sendToolResult('call_b', '{"ok":true,"summary":"Vienna"}')
  assert.deepEqual(
    r.sent.map((e) => e.type),
    ['response.item.create', 'response.item.create', 'response.create'],
  )
  assert.equal((r.sent[1].item).output, '{"ok":true,"summary":"Vienna"}')
})

await check('backend usage is text tokens net of cache; live seconds are snapshots', () => {
  const r = rig()
  r.p.handle(started)
  r.p.handle(
    backend('response.completed', 'resp_1', {
      input_tokens: 1203,
      input_tokens_details: { cached_tokens: 1000 },
      output_tokens: 30,
      total_tokens: 1233,
    }),
  )
  r.p.handle({ type: 'session.usage.updated', usage: { seconds: 60 } })
  assert.equal(r.usage[0].inputText, 203)
  assert.equal(r.usage[0].inputCached, 1000)
  assert.equal(r.usage[0].outputText, 30)
  assert.equal(r.usage[1].liveSeconds, 60)
  assert.equal(r.usage[1].inputText, 0)
})

await check('transcript fragments become turns on silence; delegation closes the user turn', () => {
  const r = rig()
  r.p.handle(started)
  r.p.handle({ type: 'session.input_transcript.delta', delta: ' What is' })
  r.p.handle({ type: 'session.input_transcript.delta', delta: ' on my screen' })
  assert.deepEqual(r.finals(), [])
  r.p.handle({ type: 'session.delegation.created', delegation: { id: 'item_1' } })
  assert.deepEqual(r.finals(), ['user!:What is on my screen'])

  r.p.handle({ type: 'session.output_transcript.delta', delta: 'Looking.' })
  assert.equal(r.state(), 'speaking')
  r.advance(ASSISTANT_TURN_GAP_MS - 1)
  assert.equal(r.finals().length, 1)
  r.advance(1)
  assert.deepEqual(r.finals(), ['user!:What is on my screen', 'asst!:Looking.'])
  assert.equal(r.state(), 'listening')

  r.p.handle({ type: 'session.input_transcript.delta', delta: 'thanks' })
  r.advance(USER_TURN_GAP_MS)
  assert.equal(r.finals().at(-1), 'user!:thanks')
})

await check('a backchannel mid-utterance flushes AFTER the operator, not before', () => {
  const r = rig()
  r.p.handle(started)
  r.p.handle({ type: 'session.input_transcript.delta', delta: 'Open my calendar and' })
  r.p.handle({ type: 'session.output_transcript.delta', delta: 'Mm.' })
  // the operator keeps talking past the assistant's gap
  r.advance(ASSISTANT_TURN_GAP_MS - 500)
  r.p.handle({ type: 'session.input_transcript.delta', delta: ' tell me about tomorrow' })
  r.advance(600)
  assert.deepEqual(r.finals(), [], 'assistant line held while the operator talks')
  r.advance(USER_TURN_GAP_MS)
  assert.deepEqual(r.finals(), [
    'user!:Open my calendar and tell me about tomorrow',
    'asst!:Mm.',
  ])
})

await check('voice talking while a tool is owed keeps the acting state', () => {
  const r = rig()
  r.p.handle(started)
  r.p.handle(fnCall('call_a', 'computer_see'))
  r.p.handle({ type: 'session.output_transcript.delta', delta: 'On it.' })
  assert.equal(r.state(), 'acting')
})

await check('typed text goes to the backend as a user message', () => {
  const r = rig()
  r.p.handle(started)
  r.p.sendText('go')
  assert.deepEqual(
    r.sent.map((e) => e.type),
    ['response.item.create', 'response.create'],
  )
  assert.deepEqual(r.sent[0].item, {
    type: 'message',
    role: 'user',
    content: [{ type: 'input_text', text: 'go' }],
  })
})

await check('close() sends session.close and resolves on session.closed without serverClosed', async () => {
  const r = rig()
  r.p.handle(started)
  const done = r.p.close(10_000)
  assert.equal(r.sent.at(-1)?.type, 'session.close')
  r.p.handle({ type: 'session.closed', reason: 'close_requested', usage: { seconds: 42 } })
  await done
  assert.deepEqual(r.closed, [])
  assert.equal(r.usage.at(-1)?.liveSeconds, 42)
})

await check('close() resolves on timeout when session.closed never comes', async () => {
  const r = rig()
  r.p.handle(started)
  const done = r.p.close(600)
  r.advance(600)
  await done
})

await check('a server-side close (expired) is reported', () => {
  const r = rig()
  r.p.handle(started)
  r.p.handle({ type: 'session.closed', reason: 'expired' })
  assert.deepEqual(r.closed, ['expired'])
})

await check('error events surface code', () => {
  const r = rig()
  r.p.handle({ type: 'error', error: { code: 'invalid_audio', message: 'odd bytes' } })
  assert.deepEqual(r.errors, ['invalid_audio'])
})

await check('backend message text is surfaced', () => {
  const r = rig()
  r.p.handle(started)
  r.p.handle({
    type: 'response.event',
    event: {
      type: 'response.output_item.done',
      item: {
        type: 'message',
        role: 'assistant',
        content: [{ type: 'output_text', text: 'Three unread; one from Priya.' }],
      },
    },
  })
  assert.ok(r.log.includes('backend:Three unread; one from Priya.'))
})

await check('an image too big for the data channel is dropped, the result is not', () => {
  const r = rig()
  r.p.handle(started)
  r.p.handle(fnCall('call_a', 'computer_see'))
  r.p.sendToolResult('call_a', '{"ok":true}', { b64: 'A'.repeat(300_000), w: 1280, h: 800 })
  assert.deepEqual(r.errors, ['image_too_large'])
  assert.deepEqual(r.sent.map((e) => e.type), ['response.item.create', 'response.create'])
  const out = (r.sent[0].item).output
  assert.equal(typeof out, 'string')
  assert.match(String(out), /^\{"ok":true\}\n\(screenshot omitted/)
})

await check('a JPEG-refit image keeps its mime in the data URL', () => {
  const r = rig()
  r.p.handle(started)
  r.p.handle(fnCall('call_a', 'computer_see'))
  r.p.sendToolResult('call_a', '{}', { b64: 'QUJD', w: 10, h: 10, mime: 'image/jpeg' })
  const out = (r.sent[0].item).output
  assert.equal(out[2].image_url, 'data:image/jpeg;base64,QUJD')
})

if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
process.exit(0)
