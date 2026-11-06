// Store plumbing that keeps long sessions responsive: batched store
// notifications, the session-tree index, coalesced saves, and background
// events that must not rebuild the session list. Run from the repo root:
//   npx tsx test-store-plumbing.mjs
import assert from 'node:assert/strict'

const saves = []
const indexSaves = []
globalThis.window = globalThis.window ?? {}
window.harness = {
  sendCommand: async () => ({ ok: true }),
  sessionSave: async (payload) => { saves.push(payload.id); return { ok: true } },
  sessionIndexSave: async (rows) => { indexSaves.push(rows.length); return { ok: true } },
}

const store = await import('./src/renderer/state/store.ts')
const { useHarness, batchStoreUpdates, sessionTreeIndex, collectDescendantSessionIds,
  schedulePersistSession, schedulePersistIndex, flushScheduledPersists } = store
const H = () => useHarness.getState()
const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms))

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

console.log('\nbatched notifications')
await check('a batch notifies once, with the state from before it as prev', async () => {
  const calls = []
  const unsub = useHarness.subscribe((state, prev) => calls.push([state.inputDraft, prev.inputDraft]))
  useHarness.setState({ inputDraft: 'start' })
  calls.length = 0
  batchStoreUpdates(() => {
    useHarness.setState({ inputDraft: 'a' })
    assert.equal(H().inputDraft, 'a', 'getState is current inside the batch')
    useHarness.setState({ inputDraft: 'b' })
    useHarness.setState({ inputDraft: 'c' })
  })
  unsub()
  assert.deepEqual(calls, [['c', 'start']])
})
await check('nested batches notify once, at the outermost end', async () => {
  const calls = []
  const unsub = useHarness.subscribe(() => calls.push(1))
  batchStoreUpdates(() => {
    useHarness.setState({ inputDraft: 'x' })
    batchStoreUpdates(() => useHarness.setState({ inputDraft: 'y' }))
    assert.equal(calls.length, 0, 'no notification from the inner batch')
  })
  unsub()
  assert.equal(calls.length, 1)
})
await check('a batch that changes nothing notifies nobody', async () => {
  const calls = []
  const unsub = useHarness.subscribe(() => calls.push(1))
  batchStoreUpdates(() => {})
  unsub()
  assert.equal(calls.length, 0)
})
await check('outside a batch every set notifies', async () => {
  const calls = []
  const unsub = useHarness.subscribe(() => calls.push(1))
  useHarness.setState({ inputDraft: 'p' })
  useHarness.setState({ inputDraft: 'q' })
  unsub()
  assert.equal(calls.length, 2)
})
await check('a throw inside a batch still notifies what was applied', async () => {
  const calls = []
  const unsub = useHarness.subscribe((s) => calls.push(s.inputDraft))
  try {
    batchStoreUpdates(() => {
      useHarness.setState({ inputDraft: 'before-throw' })
      throw new Error('boom')
    })
  } catch {}
  unsub()
  assert.deepEqual(calls, ['before-throw'])
})

console.log('\nsession tree index')
// A tree deep and wide enough to catch ordering mistakes, plus a stray
// duplicate id and a cycle.
const tree = [
  row('root'), row('a', { parentSessionId: 'root' }), row('b', { parentSessionId: 'root' }),
  row('a1', { parentSessionId: 'a' }), row('b1', { parentSessionId: 'b' }), row('a2', { parentSessionId: 'a' }),
  row('a1x', { parentSessionId: 'a1' }), row('other'), row('a', { parentSessionId: 'other', title: 'dup' }),
  row('loop1', { parentSessionId: 'loop2' }), row('loop2', { parentSessionId: 'loop1' }),
]
function naiveDescendants(sessions, parentId) {
  const out = []
  const queue = [parentId]
  const seen = new Set([parentId])
  while (queue.length) {
    const cur = queue.shift()
    for (const s of sessions) {
      if (s.parentSessionId === cur && !seen.has(s.id)) { seen.add(s.id); out.push(s.id); queue.push(s.id) }
    }
  }
  return out
}
await check('descendants match the old full scan, in the same order', async () => {
  for (const id of ['root', 'a', 'b', 'other', 'loop1', 'missing']) {
    assert.deepEqual(collectDescendantSessionIds(tree, id), naiveDescendants(tree, id), `for ${id}`)
  }
})
await check('the index is built once per sessions array', async () => {
  assert.equal(sessionTreeIndex(tree), sessionTreeIndex(tree))
  assert.notEqual(sessionTreeIndex([...tree]), sessionTreeIndex(tree))
})
await check('byId keeps the first row for a duplicate id, like find() did', async () => {
  assert.equal(sessionTreeIndex(tree).byId.get('a').title, 'a')
})

console.log('\nbackground events and the session list')
await check("a background token that moves no shown number keeps the sessions array", async () => {
  useHarness.setState({ activeSessionId: 'root', sessions: [row('root'), row('kid', { parentSessionId: 'root' })], sessionArchive: {} })
  // The first event may sync the row to its slice (counts, cost); after
  // that, tokens that move nothing shown must leave the array alone.
  H().handleEvent({ type: 'thinking_delta', sessionId: 'kid', thinking: 'hm' })
  const before = H().sessions
  for (let i = 0; i < 5; i++) H().handleEvent({ type: 'thinking_delta', sessionId: 'kid', thinking: ' more' })
  assert.equal(H().sessions, before)
})
await check('an event for a session with no row keeps the sessions array', async () => {
  const before = H().sessions
  H().handleEvent({ type: 'thinking_delta', sessionId: 'not-registered-yet', thinking: 'hm' })
  assert.equal(H().sessions, before)
  assert.ok(H().sessionArchive['not-registered-yet'], 'the event still lands in its archive slice')
})
await check('usage that moves the cost updates the row', async () => {
  H().handleEvent({ type: 'usage', sessionId: 'kid', inputTokens: 10, outputTokens: 5, cost: 0.5, contextTokens: 10, contextWindow: 200000 })
  const kid = H().sessions.find((s) => s.id === 'kid')
  assert.ok(kid.totalCost > 0, `cost ${kid.totalCost}`)
})

console.log('\nkanban cards')
const { deriveSnapshotFromTask } = await import('./src/renderer/lib/kanbanSnapshot.ts')
const kanbanEvent = (subtype, details) =>
  H().handleEvent({ type: 'system_event', sessionId: 'root', subtype, message: subtype, details })
await check("an agent's kanban list keeps the history the app has for a card the judge passed", async () => {
  useHarness.setState({ activeSessionId: 'root', kanbanCards: {} })
  // The bridge's shapes: a verdict sends task.to_dict(), a list sends
  // to_dict(include_history=False), which has no events or comments.
  const judged = {
    id: 'card_002', title: 'Write the parser', status: 'done', summary: 'Meets the spec.',
    events: [
      { kind: 'updated', actor: 'code card_002 (sub_w)', message: 'Updated card_002', timestamp: 1, details: { status: 'review' } },
      { kind: 'updated', actor: 'kanban-judge', message: 'Updated card_002', timestamp: 2, details: { status: 'done', summary: true } },
    ],
    comments: [{ author: 'code card_002 (sub_w)', body: 'Parser written.', timestamp: 1 }],
  }
  kanbanEvent('kanban_update', { action: 'update', task: judged })
  const { events, comments, ...listed } = judged
  kanbanEvent('kanban_list', { action: 'list', tasks: [{ ...listed, title: 'Write the parser (v2)' }], count: 1 })
  const card = H().kanbanCards.card_002
  assert.equal(card.title, 'Write the parser (v2)', 'the list still updates the card')
  assert.deepEqual(card.events, events)
  assert.deepEqual(card.comments, comments)
  assert.equal(deriveSnapshotFromTask(card).verifierActor, 'kanban-judge')
})
await check('a card sent with its history replaces the history the app had', async () => {
  const fresh = [{ kind: 'updated', actor: 'kanban-judge', message: 'Updated card_002', timestamp: 3, details: { status: 'running' } }]
  kanbanEvent('kanban_update', { action: 'update', task: { id: 'card_002', status: 'running', events: fresh, comments: [] } })
  assert.deepEqual(H().kanbanCards.card_002.events, fresh)
  assert.deepEqual(H().kanbanCards.card_002.comments, [])
})
await check('a fork at a message shows the cards as they stood there, as the bridge cut its journal', async () => {
  // card_001 was made before the fork point and passed after it; card_002
  // was made after it. The bridge sends the board it replayed from the
  // journal rows stamped before the cut.
  const before = { id: 'card_001', title: 'Plan', status: 'running', createdAt: 1_000, updatedAt: 1_500, events: [], comments: [] }
  const passed = { ...before, status: 'done', updatedAt: 3_000 }
  const later = { id: 'card_002', title: 'Ship', status: 'ready', createdAt: 2_500, updatedAt: 2_500, events: [], comments: [] }
  useHarness.setState({
    activeSessionId: 'src',
    sessions: [row('src')],
    sessionArchive: {},
    messages: [
      { id: 'u1', role: 'user', parts: [{ type: 'text', text: 'plan it' }], createdAt: 900 },
      { id: 'u2', role: 'user', parts: [{ type: 'text', text: 'ship it' }], createdAt: 2_000 },
    ],
    systemEvents: [
      { id: 'e1', subtype: 'kanban_create', message: '', at: 1_000, details: { task: before } },
      { id: 'e2', subtype: 'kanban_create', message: '', at: 2_500, details: { task: later } },
      { id: 'e3', subtype: 'kanban_update', message: '', at: 3_000, details: { task: passed } },
    ],
    kanbanCards: { card_001: passed, card_002: later },
  })
  H().handleEvent({
    type: 'session_branched', originalSessionId: 'src', newSessionId: 'src-b', newName: 'fork',
    messageOrdinal: 1, messageCreatedAt: 2_000, idRemap: { src: 'src-b' }, childMappings: [],
    kanbanCards: [before],
  })
  const fork = H().sessionArchive['src-b']
  assert.deepEqual(Object.keys(fork.kanbanCards), ['card_001'])
  assert.equal(fork.kanbanCards.card_001.status, 'running')
  assert.deepEqual(H().kanbanCards, { card_001: passed, card_002: later }, 'the source keeps its cards')
})

console.log('\ncoalesced saves')
await check('repeated schedules for one session become one save', async () => {
  useHarness.setState({ activeSessionId: 'root', sessions: [row('root'), row('s2')], messages: [{ id: 'm', role: 'user', parts: [{ type: 'text', text: 'hi' }], createdAt: 1 }] })
  saves.length = 0
  indexSaves.length = 0
  for (let i = 0; i < 5; i++) schedulePersistSession('root')
  schedulePersistSession('s2')
  for (let i = 0; i < 3; i++) schedulePersistIndex()
  assert.equal(saves.length, 0, 'nothing written synchronously')
  await tick(1100)
  assert.deepEqual(saves.sort(), ['root'], 's2 has no slice to save, root saves once')
  assert.equal(indexSaves.length, 1)
})
await check('flushScheduledPersists writes pending saves at once', async () => {
  saves.length = 0
  schedulePersistSession('root')
  flushScheduledPersists()
  await tick()
  assert.deepEqual(saves, ['root'])
  await tick(1100)
  assert.deepEqual(saves, ['root'], 'the flushed timer does not fire again')
})
await check('an empty or missing id schedules nothing', async () => {
  saves.length = 0
  schedulePersistSession(undefined)
  schedulePersistSession('')
  await tick(1100)
  assert.equal(saves.length, 0)
})

if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
process.exit(0)
