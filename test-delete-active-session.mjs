// Delete on the session you are viewing. The sidebar used to hide Delete for
// it and the store refused it ("Can't delete the active session"), so you had
// to open another session first. Now the view moves off it, then it goes.
// Run from the repo root:
//   npx tsx test-delete-active-session.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const home = fs.mkdtempSync(path.join(os.tmpdir(), 'freyja-delete-active-test-'))
process.env.HOME = home
const { useHarness } = await import('./src/renderer/state/store.ts')

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

const row = (id, updatedAt, extra = {}) => ({
  id, title: id, workspace: '~/', model: 'claude-sonnet-4-6', reasoningLevel: 'medium',
  coordinationStrategy: 'bus', createdAt: 1, updatedAt, messageCount: 1,
  totalInputTokens: 0, totalOutputTokens: 0, cacheReadTokens: 0, ...extra,
})
const slice = (text) => ({
  messages: [{ id: `u-${text}`, role: 'user', parts: [{ type: 'text', text }], createdAt: 1 }],
  currentStreamingMessageId: null, currentTurnId: null, thinking: '', isStreaming: false,
  toolCalls: {}, toolCallOrder: [], fileChanges: [], subagents: {}, subagentOrder: [],
  usage: {
    currentContextTokens: 0, totalInputTokens: 0, totalOutputTokens: 0, totalCacheReadTokens: 0,
    totalCacheWriteTokens: 0, totalCost: 0, lastTurnInputTokens: 0, lastTurnOutputTokens: 0,
    contextWindow: 200000,
  },
  systemEvents: [], kanbanCards: {}, busMessages: [], inboxEvents: [], artifacts: [], widgets: {},
  autoDispatchEnabled: false, model: 'claude-sonnet-4-6', reasoningLevel: 'medium',
  coordinationStrategy: 'bus', runtime: 'native',
})

/** `active` is on screen; every other session in `rows` sits in the archive. */
function seed(active, rows, mru = []) {
  const calls = []
  globalThis.window = globalThis.window ?? {}
  window.harness = {
    sessionDelete: async (id) => { calls.push(['delete', id]); return { ok: true } },
    sendCommand: async (cmd) => { calls.push([cmd.type, cmd.sessionId]); return { ok: true } },
    sessionSave: async () => ({ ok: true }),
    sessionIndexSave: async () => ({ ok: true }),
  }
  useHarness.setState({
    ...slice(active),
    toast: null,
    activeSessionId: active,
    sessionMRU: mru,
    sessionPanes: [{ id: 'pane-main', sessionId: active, createdAt: 1 }],
    activePaneId: 'pane-main',
    sessionArchive: Object.fromEntries(rows.filter((r) => r.id !== active).map((r) => [r.id, slice(r.id)])),
    sessions: rows,
  })
  return calls
}

const H = () => useHarness.getState()
const ids = () => H().sessions.map((s) => s.id).sort()

await check('deleting the session on screen moves to the last one viewed, then deletes it', async () => {
  const calls = seed('a', [row('a', 3), row('b', 1), row('c', 2)], ['b'])
  await H().deleteSession('a')
  assert.equal(H().activeSessionId, 'b')
  assert.equal(H().messages[0].parts[0].text, 'b')
  assert.deepEqual(ids(), ['b', 'c'])
  assert.equal(H().sessionArchive.a, undefined)
  assert.deepEqual(H().sessionPanes.map((p) => p.sessionId), ['b'])
  assert.ok(calls.some(([t, id]) => t === 'delete_session' && id === 'a'))
  assert.equal(H().toast?.message, 'Session deleted')
})

await check('deleting the parent of the sub-agent on screen takes the whole tree and lands on the newest session left', async () => {
  const calls = seed('p1', [
    row('p', 5, { childSessionIds: ['p1'] }),
    row('p1', 6, { parentSessionId: 'p' }),
    row('old', 1),
    row('newer', 4),
  ])
  await H().deleteSession('p')
  assert.equal(H().activeSessionId, 'newer')
  assert.deepEqual(ids(), ['newer', 'old'])
  assert.deepEqual(calls.filter(([t]) => t === 'delete').map(([, id]) => id), ['p', 'p1'])
})

await check('deleting the sub-agent on screen goes back to its parent', async () => {
  seed('p1', [row('p', 5, { childSessionIds: ['p1'] }), row('p1', 6, { parentSessionId: 'p' }), row('x', 9)], ['x'])
  await H().deleteSession('p1')
  assert.equal(H().activeSessionId, 'p')
  assert.deepEqual(ids(), ['p', 'x'])
})

await check('deleting the only session opens a fresh one', async () => {
  const calls = seed('solo', [row('solo', 1)])
  await H().deleteSession('solo')
  assert.notEqual(H().activeSessionId, 'solo')
  assert.deepEqual(ids(), [H().activeSessionId])
  assert.equal(H().messages.length, 0)
  assert.ok(calls.some(([t, id]) => t === 'delete_session' && id === 'solo'))
})

fs.rmSync(home, { recursive: true, force: true })
if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
process.exit(0)
