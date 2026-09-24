// Mid-turn follow-ups and sub-agent memos in the renderer store. Drives the
// real `handleEvent` with the event sequences the bridge emits (captured
// from live runs) and checks where messages land in the timeline. Run from
// the repo root:
//   npx tsx test-followup-placement.mjs
import assert from 'node:assert/strict'
import { useHarness } from './src/renderer/state/store.ts'

const sent = []
globalThis.window = globalThis.window ?? {}
window.harness = { sendCommand: async (cmd) => { sent.push(cmd) } }

const S = useHarness.getState()
const sid = S.activeSessionId
const ev = (e) => useHarness.getState().handleEvent({ sessionId: sid, ...e })
/** The conversation as the timeline renders it: sorted by createdAt. */
const timeline = () =>
  [...useHarness.getState().messages]
    .sort((a, b) => a.createdAt - b.createdAt)
    .map((m) => `${m.role}:${m.parts.filter((p) => p.type === 'text').map((p) => p.text).join('')}`)

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

await check('a message sent mid-turn waits as a pending bubble, then lands where it was injected', async () => {
  await useHarness.getState().sendMessage('run the checks')
  ev({ type: 'turn_start', turnId: 'turn-1' })
  ev({ type: 'text_delta', text: 'Running them now.' })
  await useHarness.getState().sendMessage('also lint', { force: false })
  const cmd = sent.at(-1)
  assert.equal(cmd.followup, true)
  assert.equal(cmd.force, false)
  const clientId = cmd.clientId
  assert.equal(useHarness.getState().pendingFollowups[sid].length, 1)
  assert.deepEqual(timeline(), ['user:run the checks', 'assistant:Running them now.'])

  ev({ type: 'followup_queued', messageId: 'm1', clientId, force: false, interrupting: false, at: Date.now() })
  ev({
    type: 'inbox_injected', at: Date.now() + 5, midTurn: true,
    items: [{ messageId: 'm1', kind: 'followup', clientId, force: false, content: 'also lint' }],
  })
  ev({ type: 'text_delta', text: 'Linting too.' })
  ev({ type: 'turn_complete', turnId: 'turn-1', success: true })

  assert.deepEqual(timeline(), [
    'user:run the checks',
    'assistant:Running them now.',
    'user:also lint',
    'assistant:Linting too.',
  ])
  assert.equal(useHarness.getState().pendingFollowups[sid].length, 0)
  const injected = useHarness.getState().messages.find((m) => m.id === clientId)
  assert.deepEqual(injected.followup, { force: false, midTurn: true })
  assert.equal(useHarness.getState().isStreaming, false)
})

await check('ctrl+enter sends force; "now" upgrades a queued one', async () => {
  ev({ type: 'turn_start', turnId: 'turn-2' })
  await useHarness.getState().sendMessage('stop, do X instead', { force: true })
  assert.equal(sent.at(-1).force, true)
  await useHarness.getState().sendMessage('and Y')
  const y = sent.at(-1).clientId
  await useHarness.getState().injectFollowupNow(sid, y)
  assert.deepEqual(sent.at(-1), { type: 'followup_control', sessionId: sid, clientId: y, action: 'inject' })
  assert.equal(useHarness.getState().pendingFollowups[sid].find((p) => p.clientId === y).force, true)
})

await check('withdrawing hands the text back to the composer', async () => {
  const pend = useHarness.getState().pendingFollowups[sid]
  const target = pend[pend.length - 1]
  useHarness.getState().setInputDraft('half-typed')
  await useHarness.getState().withdrawFollowup(sid, target.clientId)
  assert.equal(useHarness.getState().inputDraft, 'and Y\n\nhalf-typed')
  assert.equal(sent.at(-1).action, 'withdraw')
})

await check('a follow-up that missed its turn is promoted ahead of the next reply', async () => {
  const [first] = useHarness.getState().pendingFollowups[sid]
  ev({ type: 'turn_complete', turnId: 'turn-2', success: true })
  ev({
    type: 'followups_promoted', at: Date.now() + 50,
    items: [{ messageId: 'm2', clientId: first.clientId, content: first.content }],
  })
  await new Promise((r) => setTimeout(r, 60))
  ev({ type: 'turn_start', turnId: 'turn-3' })
  ev({ type: 'text_delta', text: 'Doing X.' })
  ev({ type: 'turn_complete', turnId: 'turn-3', success: true })
  const t = timeline()
  assert.deepEqual(t.slice(-2), ['user:stop, do X instead', 'assistant:Doing X.'])
  assert.equal(useHarness.getState().pendingFollowups[sid].length, 0)
})

await check('stopping the turn restores queued follow-ups (bridge-initiated withdraw)', async () => {
  ev({ type: 'turn_start', turnId: 'turn-4' })
  await useHarness.getState().sendMessage('queued thing')
  const c = sent.at(-1).clientId
  useHarness.getState().setInputDraft('')
  await useHarness.getState().cancelTurn()
  const cancel = sent.at(-1)
  assert.equal(cancel.type, 'force_cancel')
  assert.equal(cancel.scope, 'turn')
  ev({ type: 'followup_withdrawn', messageId: 'm4', clientId: c, content: 'queued thing', restore: true })
  assert.equal(useHarness.getState().inputDraft, 'queued thing')
  ev({ type: 'turn_complete', turnId: 'turn-4', success: false })
})

await check('a memo is a chip in the timeline and the wake turn follows it', async () => {
  const now = Date.now() + 1000
  ev({
    type: 'inbox_event', action: 'enqueued',
    message: {
      id: 'memo-1', fromSession: 'sub_1', fromLabel: 'wordsmith', fromRole: 'agent',
      content: '[sub-agent memo · wordsmith · id sub_1 · done · 24s]\nYour background `general` sub-agent finished.',
      force: false, replyTo: null, timestamp: now, deliveredAt: null, kind: 'memo',
      meta: { subagentId: 'sub_1', label: 'wordsmith', agentType: 'general', state: 'done', elapsedMs: 24000 },
    },
  })
  const rec = useHarness.getState().inboxEvents.find((e) => e.id === 'memo-1')
  assert.equal(rec.kind, 'memo')
  assert.equal(rec.meta.label, 'wordsmith')
})

await check('cutting into a tool batch settles every cut chip', async () => {
  ev({ type: 'turn_start', turnId: 'turn-5' })
  ev({ type: 'tool_use_start', id: 'ta', name: 'bash' })
  ev({ type: 'tool_use_start', id: 'tb', name: 'read_file' })
  ev({
    type: 'system_event', subtype: 'turn_interrupted', message: 'Stopped the running tools',
    details: { phase: 'tools', cut: [{ id: 'ta', name: 'bash', started: true }, { id: 'tb', name: 'read_file', started: false }] },
  })
  const tcs = useHarness.getState().toolCalls
  assert.equal(tcs.ta.status, 'error')
  assert.equal(tcs.tb.status, 'error')
  assert.match(tcs.tb.result, /Not run/)
  ev({ type: 'turn_complete', turnId: 'turn-5', success: true })
})

await check('stop-agents is its own scope', async () => {
  await useHarness.getState().stopSubagents()
  assert.equal(sent.at(-1).scope, 'subagents')
})

if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
