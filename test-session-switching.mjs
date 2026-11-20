// Session switching with split panes. Drives the real store with a fake
// bridge whose disk loads finish when the test says so. Run from the repo root:
//   npx tsx test-session-switching.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const home = fs.mkdtempSync(path.join(os.tmpdir(), 'freyja-switch-test-'))
process.env.HOME = home
process.env.FREYJA_HOME = path.join(home, '.freyja')
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

const row = (id, extra = {}) => ({
  id, title: id, workspace: '~/', model: 'claude-sonnet-4-6', reasoningLevel: 'medium',
  coordinationStrategy: 'bus', createdAt: 1, updatedAt: 1, messageCount: 1,
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

/** Disk loads wait here until `finishLoad(id)`. */
let loads = new Map()
const finishLoad = async (id) => {
  loads.get(id)({ ok: true, session: { slice: slice(id) } })
  await new Promise((r) => setTimeout(r, 0))
}

/** `active` is on screen in `pane-main`; `archived` sit in memory; every
 *  other row is only on disk. `slack` is a Slack session, which always
 *  reloads from disk. */
function seed({ active, archived = [], panes, extraRows = [] }) {
  const switched = []
  loads = new Map()
  globalThis.window = globalThis.window ?? {}
  window.harness = {
    sendCommand: async (cmd) => {
      if (cmd.type === 'switch_session') switched.push(cmd.sessionId)
      return { ok: true }
    },
    sessionLoad: (id) => new Promise((resolve) => loads.set(id, resolve)),
    sessionSave: async () => ({ ok: true }),
    sessionIndexSave: async () => ({ ok: true }),
  }
  const ids = [active, ...archived, ...extraRows]
  useHarness.setState({
    ...slice(active),
    toast: null,
    inputDraft: '',
    pendingAttachments: [],
    activeSessionId: active,
    sessionMRU: [],
    sessionPanes: (panes ?? [active]).map((sessionId, i) => ({
      id: i === 0 ? 'pane-main' : `pane-${i}`, sessionId, createdAt: 1,
    })),
    activePaneId: 'pane-main',
    sessionArchive: Object.fromEntries(archived.map((id) => [id, slice(id)])),
    sessions: ids.map((id) => row(id, id === 'slack' ? { agentType: 'gateway-slack' } : {})),
  })
  return switched
}

const H = () => useHarness.getState()
const shown = () => H().sessionPanes.map((p) => p.sessionId)

await check('the last click wins when an earlier switch is still loading from disk', async () => {
  const switched = seed({ active: 'a', archived: ['b', 'slack'], extraRows: ['cold'] })
  const first = H().switchSession('cold')
  await H().switchSession('b')
  await finishLoad('cold')
  await first
  assert.equal(H().activeSessionId, 'b')
  assert.equal(H().messages[0].parts[0].text, 'b')
  assert.deepEqual(shown(), ['b'])
  assert.deepEqual(switched, ['b'])

  // A Slack session reloads even when cached; a click back on the session
  // on screen cancels that too.
  const slow = H().switchSession('slack')
  await H().switchSession('b')
  await finishLoad('slack')
  await slow
  assert.equal(H().activeSessionId, 'b')
  assert.deepEqual(switched, ['b'])
})

await check('closing a pane whose session is still loading keeps the closed session away', async () => {
  const switched = seed({ active: 'a', archived: ['slack'], panes: ['a', 'slack'] })
  // What a click inside the Slack pane does: focus the pane, switch to it.
  H().setActiveSessionPane('pane-1')
  const pending = H().switchSession('slack')
  await H().closeSessionPane('pane-1')
  await finishLoad('slack')
  await pending
  assert.equal(H().activeSessionId, 'a')
  assert.deepEqual(shown(), ['a'])
  assert.equal(H().activePaneId, 'pane-main')
  assert.deepEqual(switched, [])
})

await check('a session already open in a pane is focused there, not shown twice', async () => {
  const switched = seed({ active: 'a', archived: ['b', 'c'], panes: ['a', 'b'] })
  await H().switchSession('b')
  assert.deepEqual(shown(), ['a', 'b'])
  assert.equal(H().activePaneId, 'pane-1')
  assert.equal(H().activeSessionId, 'b')
  // A session in no pane still goes into the active one.
  await H().switchSession('c')
  assert.deepEqual(shown(), ['a', 'c'])
  assert.deepEqual(switched, ['b', 'c'])

  // "Split" on a session that has a pane opens no second pane, and says so.
  await H().openSessionPane('a', 'split')
  assert.deepEqual(shown(), ['a', 'c'])
  assert.equal(H().activePaneId, 'pane-main')
  assert.equal(H().toast?.message, 'Already open in a pane')
})

await check('a pasted screenshot stays with its caption across switches', async () => {
  seed({ active: 'a', archived: ['b'] })
  const shot = { id: 'att-1', type: 'image', mimeType: 'image/png', dataBase64: 'AA==', previewUrl: 'data:image/png;base64,AA==' }
  useHarness.setState({ inputDraft: 'what is this?', pendingAttachments: [shot] })
  await H().switchSession('b')
  await H().switchSession('a')
  assert.equal(H().inputDraft, 'what is this?')
  assert.deepEqual(H().pendingAttachments, [shot])
  await H().newSession()
  assert.equal(H().inputDraft, 'what is this?')
  assert.deepEqual(H().pendingAttachments, [shot])
})

fs.rmSync(home, { recursive: true, force: true })
if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
process.exit(0)
