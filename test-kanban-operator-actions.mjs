// The operator's buttons on a kanban card (KanbanBridgeView.tsx's card
// focus view). Force dispatch said "Force-dispatched card_002" as soon as
// it was pressed, even when the bridge refused the card; the reason only
// reached the activity feed. Run from the repo root:
//   npx tsx test-kanban-operator-actions.mjs

import assert from 'node:assert/strict'
import { createElement } from 'react'
import { flushSync } from 'react-dom'
import { createRoot } from 'react-dom/client'

// Needs a DOM, so install jsdom first (it is not a project dependency):
//   npm i --no-save jsdom
let JSDOM
try {
  ({ JSDOM } = await import('jsdom'))
} catch {
  console.error('jsdom not found — run:  npm i --no-save jsdom')
  process.exit(2)
}
const dom = new JSDOM('<!doctype html><body></body>', { pretendToBeVisual: true })
globalThis.window = dom.window
globalThis.document = dom.window.document
globalThis.requestAnimationFrame = dom.window.requestAnimationFrame.bind(dom.window)
globalThis.cancelAnimationFrame = dom.window.cancelAnimationFrame.bind(dom.window)
globalThis.IS_REACT_ACT_ENVIRONMENT = false
// The board's done-column backdrop asks for WebGL; jsdom has none.
dom.window.HTMLCanvasElement.prototype.getContext = () => null
const sent = []
window.harness = {
  sendCommand: async (cmd) => { sent.push(cmd) },
  sessionSave: async () => ({ ok: true }),
  sessionIndexSave: async () => ({ ok: true }),
  sessionLoad: async () => ({ ok: false }),
}
const { useHarness } = await import('./src/renderer/state/store.ts')
const { KanbanBridgeView } = await import('./src/renderer/components/views/KanbanBridgeView.tsx')
const H = () => useHarness.getState()

let failures = 0
async function check(name, fn) {
  try {
    await fn()
    console.log(`ok   ${name}`)
  } catch (err) {
    failures++
    console.log(`FAIL ${name}\n     ${err.message.split('\n').join('\n     ')}`)
  }
}

const SESSION = 'session-kanban'
const tile = (id, status, parents = [], metadata = {}) => ({ id, title: `title ${id}`, status, parents, metadata, events: [], comments: [] })
const settle = () => new Promise((resolve) => setTimeout(resolve, 0))

/** Mount the board, open `cardId`'s focus view, and return its buttons. */
async function openCard(cards, cardId) {
  const host = document.createElement('div')
  document.body.replaceChildren(host)
  const root = createRoot(host)
  flushSync(() => root.render(createElement(KanbanBridgeView, {
    sessionId: SESSION, objective: 'o', cards, agents: [], telemetryEvents: [], contextPct: 0, cost: 0,
    autoDispatchEnabled: false, onAttach() {}, onOpenDispatcherBrief() {},
  })))
  const article = [...host.querySelectorAll('article')].find((a) => a.textContent.includes(`title ${cardId}`))
  assert.ok(article, `no tile for ${cardId}`)
  flushSync(() => article.click())
  const button = (label) => [...host.querySelectorAll('footer button')].find((b) => b.textContent === label)
  return { root, button }
}

/** What the bridge sends back for the session. */
const answer = (subtype, message, details) =>
  H().handleEvent({ type: 'system_event', sessionId: SESSION, subtype, message, details: { chatVisible: false, ...details } })

await check('a refused force dispatch toasts the reason, not that the card went out', async () => {
  useHarness.setState({ activeSessionId: SESSION, toast: null })
  const { root, button } = await openCard([tile('card_001', 'running'), tile('card_002', 'ready', ['card_001'])], 'card_002')
  button('Force dispatch').click()
  await settle()
  assert.deepEqual(sent.at(-1), { type: 'kanban_operator_force_dispatch', sessionId: SESSION, cardId: 'card_002' })
  assert.equal(H().toast, null)
  answer('kanban_operator_force_dispatch_failed', 'Could not dispatch card_002: it waits on card_001',
    { cardId: 'card_002', reason: 'it waits on card_001' })
  await settle()
  assert.equal(H().toast?.message, 'Could not dispatch card_002: it waits on card_001')
  assert.equal(H().toast?.tone, 'warn')
  root.unmount()
})

await check('a force dispatch the bridge carried out toasts that the card went out', async () => {
  useHarness.setState({ activeSessionId: SESSION, toast: null })
  const { root, button } = await openCard([tile('card_002', 'ready')], 'card_002')
  button('Force dispatch').click()
  await settle()
  assert.equal(H().toast, null)
  answer('kanban_dispatched', 'Auto-dispatched general on card_002',
    { cardId: 'card_002', agentType: 'general', lane: 'worker', source: 'operator_force' })
  await settle()
  assert.equal(H().toast?.message, 'Force-dispatched card_002')
  // The press is answered: the tick's later dispatch of the card, or a
  // second answer, toasts nothing.
  useHarness.setState({ toast: null })
  answer('kanban_dispatched', 'Auto-dispatched general on card_002',
    { cardId: 'card_002', agentType: 'general', lane: 'worker', source: 'idle' })
  answer('kanban_dispatched', 'Auto-dispatched general on card_002',
    { cardId: 'card_002', agentType: 'general', lane: 'worker', source: 'operator_force' })
  await settle()
  assert.equal(H().toast, null)
  root.unmount()
})

if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
