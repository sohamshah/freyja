// Sub-agent records vs. the bridge. The renderer persists its copy of each
// session's sub-agent records; after the bridge restarts they still say
// `running` for children that died with it. Drives the real `handleEvent`
// and `switchSession` with what the bridge sends (`session_switched`
// details, `subagents_snapshot`) and checks which records get settled and
// saved. Run from the repo root:
//   npx tsx test-subagent-reconcile.mjs
import assert from 'node:assert/strict'
import { flushScheduledPersists, useHarness } from './src/renderer/state/store.ts'

const sent = []
const saves = []
const indexSaves = []
let diskSlices = {}
globalThis.window = globalThis.window ?? {}
window.harness = {
  sendCommand: async (cmd) => { sent.push(cmd) },
  sessionSave: async (payload) => { saves.push(payload); return { ok: true } },
  sessionIndexSave: async (rows) => { indexSaves.push(rows); return { ok: true } },
  sessionLoad: async (id) =>
    diskSlices[id] ? { ok: true, session: { slice: structuredClone(diskSlices[id]) } } : { ok: false },
}

const INTERRUPTED = 'Interrupted: the app restarted while this was running'
const tick = () => new Promise((r) => setTimeout(r, 0))
// Saves are coalesced (schedulePersistSession): write whatever is pending
// now instead of waiting out the delay.
const settleSaves = () => {
  flushScheduledPersists()
  return tick()
}

const rec = (id, state, extra = {}) => ({
  id, label: id, mode: 'background', state, task: 't', startedAt: 1,
  elapsedMs: 5, tokensIn: 0, tokensOut: 0, toolsCalled: 0, ...extra,
})
const row = (id, extra = {}) => ({
  id, title: id, workspace: '~/', model: 'claude-sonnet-4-6', reasoningLevel: 'medium',
  coordinationStrategy: 'bus', createdAt: 1, updatedAt: 1, messageCount: 0,
  totalInputTokens: 0, totalOutputTokens: 0, cacheReadTokens: 0, ...extra,
})
const slice = (subagents) => ({
  messages: [{ id: 'u1', role: 'user', parts: [{ type: 'text', text: 'go' }], createdAt: 1 }],
  currentStreamingMessageId: null, currentTurnId: null, thinking: '', isStreaming: false,
  toolCalls: {}, toolCallOrder: [], fileChanges: [],
  subagents: Object.fromEntries(subagents.map((r) => [r.id, r])),
  subagentOrder: subagents.map((r) => r.id),
  usage: {
    currentContextTokens: 0, totalInputTokens: 0, totalOutputTokens: 0, totalCacheReadTokens: 0,
    totalCacheWriteTokens: 0, totalCost: 0, lastTurnInputTokens: 0, lastTurnOutputTokens: 0,
    contextWindow: 200000,
  },
  systemEvents: [], kanbanCards: {}, busMessages: [], inboxEvents: [], artifacts: [], widgets: {},
  autoDispatchEnabled: false, model: 'claude-sonnet-4-6', reasoningLevel: 'medium',
  coordinationStrategy: 'bus', runtime: 'native',
})

/** A parent `p` (active) whose children a..d are in these states, plus an
 *  archived parent `q` with one running child and a Slack session whose
 *  child runs in the gateway daemon. */
function seed() {
  sent.length = 0
  saves.length = 0
  indexSaves.length = 0
  diskSlices = {}
  useHarness.setState({
    ...slice([
      rec('a', 'running'),
      rec('b', 'running'),
      rec('c', 'done', { result: 'report' }),
      rec('d', 'running'),
    ]),
    activeSessionId: 'p',
    sessionArchive: {
      q: slice([rec('q1', 'running')]),
      'freyja:slack:D1': slice([rec('s1', 'running')]),
    },
    sessions: [
      row('p', { childSessionIds: ['a', 'b', 'c', 'd'] }),
      row('a', { parentSessionId: 'p' }),
      row('b', { parentSessionId: 'p' }),
      row('c', { parentSessionId: 'p', completed: true, success: true }),
      // Its own completion reached the row; the parent's copy missed it.
      row('d', { parentSessionId: 'p', completed: true, success: true }),
      row('q', { childSessionIds: ['q1'] }),
      row('q1', { parentSessionId: 'q' }),
      row('freyja:slack:D1', { agentType: 'gateway-slack', childSessionIds: ['s1'] }),
      row('s1', { parentSessionId: 'freyja:slack:D1' }),
    ],
  })
}

const H = () => useHarness.getState()
const ev = (e) => H().handleEvent(e)
const rowOf = (id) => H().sessions.find((s) => s.id === id)

let failures = 0
async function check(name, fn) {
  try {
    // Don't let a save one case scheduled land in the next case's counts.
    await settleSaves()
    seed()
    await fn()
    console.log(`ok   ${name}`)
  } catch (err) {
    failures++
    console.log(`FAIL ${name}\n     ${err.message}`)
  }
}

await check('a snapshot settles only the records the bridge is not running, and saves them', async () => {
  ev({ type: 'subagents_snapshot', sessionId: 'p', runningIds: ['b'] })
  const s = H().subagents
  assert.equal(s.a.state, 'failed')
  assert.equal(s.a.result, INTERRUPTED)
  assert.equal(s.b.state, 'running')
  assert.deepEqual(s.c, rec('c', 'done', { result: 'report' }))
  // A child that already reported done keeps that outcome.
  assert.equal(s.d.state, 'done')
  assert.equal(s.d.result, undefined)
  assert.equal(rowOf('a').completed, true)
  assert.equal(rowOf('a').success, false)
  assert.ok(!rowOf('b').completed)
  assert.equal(rowOf('d').success, true)
  await settleSaves()
  const saved = saves.find((p) => p.id === 'p')
  assert.ok(saved, 'parent slice saved')
  assert.equal(saved.slice.subagents.a.state, 'failed')
  assert.equal(indexSaves.length, 1)
  assert.equal(indexSaves[0].find((r) => r.id === 'a').completed, true)
  // Nothing else to settle: no second write.
  ev({ type: 'subagents_snapshot', sessionId: 'p', runningIds: ['b'] })
  await settleSaves()
  assert.equal(saves.length, 1)
  assert.equal(indexSaves.length, 1)
})

await check('session_switched carries the same answer and is still logged', async () => {
  ev({
    type: 'system_event', sessionId: 'p', subtype: 'session_switched', message: 'Switched',
    details: { model: 'claude-sonnet-4-6', runningSubagentIds: [] },
  })
  assert.equal(H().subagents.a.state, 'failed')
  assert.equal(H().subagents.b.state, 'failed')
  assert.equal(H().systemEvents.at(-1).subtype, 'session_switched')
  // An older bridge that sends no list settles nothing.
  seed()
  ev({ type: 'system_event', sessionId: 'p', subtype: 'session_switched', message: 'Switched', details: {} })
  assert.equal(H().subagents.a.state, 'running')
})

await check('an archived session is settled in the archive', async () => {
  ev({ type: 'subagents_snapshot', sessionId: 'q', runningIds: [] })
  assert.equal(H().sessionArchive.q.subagents.q1.state, 'failed')
  assert.equal(H().subagents.a.state, 'running')
  assert.equal(rowOf('q1').completed, true)
  await settleSaves()
  assert.deepEqual(saves.map((p) => p.id), ['q'])
})

await check("the local bridge's answer never settles a gateway session's children", async () => {
  ev({ type: 'subagents_snapshot', sessionId: 'freyja:slack:D1', runningIds: [] })
  assert.equal(H().sessionArchive['freyja:slack:D1'].subagents.s1.state, 'running')
  assert.equal(saves.length, 0)
})

await check('a live bridge ready asks about every session still showing running agents', async () => {
  ev({ type: 'ready', sessionId: 'desktop-new', mode: 'live', capabilities: {} })
  await tick()
  const asked = sent.filter((c) => c.type === 'list_subagents').map((c) => c.sessionId).sort()
  // p was parked in the archive under its own id; the Slack session is skipped.
  assert.deepEqual(asked, ['p', 'q'])
  assert.equal(H().activeSessionId, 'desktop-new')
  sent.length = 0
  ev({ type: 'ready', sessionId: 'demo-1', mode: 'demo', capabilities: {} })
  await tick()
  assert.equal(sent.filter((c) => c.type === 'list_subagents').length, 0)
})

await check('loading from disk settles nothing until the bridge answers', async () => {
  diskSlices.q = slice([rec('q1', 'running'), rec('q2', 'running')])
  useHarness.setState({ sessionArchive: {} })
  await H().switchSession('q')
  assert.equal(H().activeSessionId, 'q')
  assert.equal(H().subagents.q1.state, 'running')
  const types = sent.map((c) => c.type)
  assert.deepEqual(types.slice(-2), ['switch_session', 'list_subagents'])
  assert.equal(sent.at(-1).sessionId, 'q')
  // After a renderer reload the bridge still runs its children: kept.
  ev({ type: 'subagents_snapshot', sessionId: 'q', runningIds: ['q1', 'q2'] })
  assert.equal(H().subagents.q1.state, 'running')
  assert.equal(H().subagents.q2.state, 'running')
  ev({ type: 'subagents_snapshot', sessionId: 'q', runningIds: ['q2'] })
  assert.equal(H().subagents.q1.state, 'failed')
  assert.equal(H().subagents.q2.state, 'running')
})

if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
