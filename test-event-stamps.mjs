// Times the renderer store gives events. Main stamps each event it forwards
// with `receivedAt`; the store applies events at its next animation frame,
// which a hidden window can hold back for minutes, so a reducer that stamps
// an event uses `receivedAt`, not the time it runs. Drives the real
// `handleEvent`. Run from the repo root:
//   npx tsx test-event-stamps.mjs
import assert from 'node:assert/strict'
import { useHarness } from './src/renderer/state/store.ts'

globalThis.window = globalThis.window ?? {}
window.harness = { sendCommand: async () => ({ ok: true }) }

const sid = useHarness.getState().activeSessionId
const ev = (e) => useHarness.getState().handleEvent({ sessionId: sid, ...e })
// Main received each event five minutes before the frame that applies it.
const received = Date.now() - 5 * 60_000
const last = () => useHarness.getState().messages.at(-1)

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

await check('a system-only message is placed when main received it', async () => {
  ev({ type: 'system_event', subtype: 'compaction_skipped', message: 'skipped', details: { chatVisible: true }, receivedAt: received })
  assert.equal(last().createdAt, received)
  assert.equal(useHarness.getState().systemEvents.at(-1).at, received)
})

await check('a message appended without createdAt is placed when main received it', async () => {
  ev({ type: 'message_appended', role: 'assistant', content: 'synthesis', receivedAt: received + 1 })
  assert.equal(last().createdAt, received + 1)
})

await check('a turn the bridge did not stamp starts when main received it, and ends then too', async () => {
  ev({ type: 'turn_start', turnId: 'turn-9', receivedAt: received + 2 })
  ev({ type: 'thinking_delta', thinking: 'hm', receivedAt: received + 3 })
  ev({ type: 'tool_use_start', id: 'call_9', name: 'bash', receivedAt: received + 4 })
  ev({ type: 'turn_complete', turnId: 'turn-9', success: true, receivedAt: received + 5 })
  const reply = last()
  assert.equal(reply.createdAt, received + 2)
  assert.deepEqual(
    [reply.parts[0].startedAt, useHarness.getState().toolCalls.call_9.startedAt, reply.completedAt],
    [received + 3, received + 4, received + 5],
  )
})

await check('the bridge\'s own stamp still wins', async () => {
  ev({ type: 'message_appended', role: 'assistant', content: 'stamped', createdAt: received - 10, receivedAt: received })
  assert.equal(last().createdAt, received - 10)
})

if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
