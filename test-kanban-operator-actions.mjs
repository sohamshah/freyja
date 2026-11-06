// The operator's buttons on a kanban card (KanbanBridgeView.tsx's card
// focus view). Force dispatch, Cancel, Unblock and Reassign said they were
// done as soon as they were pressed, even when the bridge refused; the
// reason only reached the activity feed. A triage card a cancelled or
// failed parent left behind had no Unblock button, though the bridge
// unblocks it, so only the agent could release it. Run from the repo root:
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

/** Mount the board, open `cardId`'s focus view, and return its buttons
 *  and its status pip. */
async function openCard(cards, cardId, props = {}) {
  const host = document.createElement('div')
  document.body.replaceChildren(host)
  const root = createRoot(host)
  flushSync(() => root.render(createElement(KanbanBridgeView, {
    sessionId: SESSION, objective: 'o', cards, agents: [], telemetryEvents: [], contextPct: 0, cost: 0,
    autoDispatchEnabled: false, onAttach() {}, onOpenDispatcherBrief() {}, ...props,
  })))
  const article = [...host.querySelectorAll('article')].find((a) => a.textContent.includes(`title ${cardId}`))
  assert.ok(article, `no tile for ${cardId}`)
  flushSync(() => article.click())
  const button = (label) => [...host.querySelectorAll('footer button')].find((b) => b.textContent === label)
  const pip = host.querySelector('section span[title]')
  return { root, button, pip }
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

// The bridge's `unblock` releases a triage card when a parent never
// finishes (cancelled, failed, or missing from the board) and no other
// parent can still finish; the mission root counts as done.
await check('a triage card the bridge would unblock has an Unblock button that sends the unblock', async () => {
  const cards = [
    tile('card_001', 'running', [], { role: 'mission_root' }),
    tile('card_002', 'cancelled'),
    tile('card_003', 'running'),
    tile('card_004', 'failed'),
    tile('card_005', 'done'),
    tile('card_006', 'triage', ['card_001', 'card_002', 'card_005']),
    tile('card_007', 'triage', ['card_004']),
    tile('card_008', 'triage', ['card_009']),
    tile('card_010', 'triage', ['card_002', 'card_003']),
    tile('card_011', 'triage', ['card_003']),
  ]
  const unblockable = async (id) => {
    const { root, button } = await openCard(cards, id)
    const unblock = button('Unblock')
    if (unblock) {
      unblock.click()
      await settle()
      assert.deepEqual(sent.at(-1), { type: 'kanban_operator_unblock', sessionId: SESSION, cardId: id })
    }
    root.unmount()
    return !!unblock
  }
  assert.equal(await unblockable('card_006'), true)
  assert.equal(await unblockable('card_007'), true)
  assert.equal(await unblockable('card_008'), true)
  // Another parent can still finish, or none failed: the bridge refuses.
  assert.equal(await unblockable('card_010'), false)
  assert.equal(await unblockable('card_011'), false)
})

// The tick sends a crashed or timed-out card back to a worker, and the
// bridge's force dispatch now does too; the card view offered only
// Unblock. A failed card goes nowhere.
await check('a crashed or timed-out card has Force dispatch, and a failed card does not', async () => {
  for (const status of ['crashed', 'timed_out']) {
    const { root, button } = await openCard([tile('card_002', status)], 'card_002')
    assert.ok(button('Force dispatch'), `no Force dispatch on a ${status} card`)
    button('Force dispatch').click()
    await settle()
    assert.deepEqual(sent.at(-1), { type: 'kanban_operator_force_dispatch', sessionId: SESSION, cardId: 'card_002' })
    root.unmount()
  }
  const { root, button } = await openCard([tile('card_002', 'failed')], 'card_002')
  assert.equal(button('Force dispatch'), undefined)
  root.unmount()
})

// No worker takes a failed or cancelled card again, so a new assignee
// changes nothing; Reassign… was offered on them all the same.
await check('a failed or cancelled card has no Reassign…, and a crashed card has', async () => {
  const shown = async (status) => {
    const { root, button } = await openCard([tile('card_002', status)], 'card_002')
    const reassign = !!button('Reassign…')
    root.unmount()
    return reassign
  }
  assert.equal(await shown('failed'), false)
  assert.equal(await shown('cancelled'), false)
  assert.equal(await shown('crashed'), true)
})

// Only the tick's review lane judges a card, and no tick runs with
// autopilot off; the card view offered nothing for a card in review.
await check('a card in review has Judge now, which toasts when the judge starts', async () => {
  useHarness.setState({ activeSessionId: SESSION, toast: null })
  const { root, button } = await openCard([tile('card_002', 'review')], 'card_002')
  button('Judge now').click()
  await settle()
  assert.deepEqual(sent.at(-1), { type: 'kanban_operator_force_dispatch', sessionId: SESSION, cardId: 'card_002' })
  assert.equal(H().toast, null)
  answer('kanban_review_started', 'Judge reviewing card_002 (iter 1/5)', { cardId: 'card_002', reviewIteration: 1, sticky: false })
  await settle()
  assert.equal(H().toast?.message, 'Judging card_002')
  assert.equal(H().toast?.tone, 'info')
  root.unmount()
})

// The bridge sends kanban_review_started only once the judge runs, so a
// judge that does not start (no model for judge-deep, the sub-agent cap)
// answers the press with kanban_judge_failed alone.
await check('a Judge now whose judge does not start toasts why', async () => {
  useHarness.setState({ activeSessionId: SESSION, toast: null })
  const { root, button } = await openCard([tile('card_002', 'review')], 'card_002')
  button('Judge now').click()
  await settle()
  const message = 'Judge crashed for card_002: Too many active sub-agents (8/8)'
  answer('kanban_judge_failed', message, { cardId: 'card_002', error: 'Too many active sub-agents (8/8)' })
  await settle()
  assert.equal(H().toast?.message, message)
  assert.equal(H().toast?.tone, 'warn')
  // A judge that started and then crashed answered with the start.
  useHarness.setState({ toast: null })
  button('Judge now').click()
  await settle()
  answer('kanban_review_started', 'Judge reviewing card_002 (iter 1/3)', { cardId: 'card_002', reviewIteration: 1, sticky: false })
  await settle()
  assert.equal(H().toast?.message, 'Judging card_002')
  useHarness.setState({ toast: null })
  answer('kanban_judge_failed', message, { cardId: 'card_002' })
  await settle()
  assert.equal(H().toast, null)
  root.unmount()
})

// With autopilot on, the tick judges every card in review and a press
// while its judge runs is refused. With autopilot off, Judge now stays: it
// is the only way to start a judge again if the one the app saw start
// never sends its verdict (a bridge restart), and the bridge refuses a
// press while one really runs.
await check('Judge now is hidden while a judge is on the card with autopilot on', async () => {
  const started = [{ subtype: 'kanban_review_started', message: '', at: 1, details: { cardId: 'card_002' }, sessionId: SESSION, sessionTitle: 's' }]
  const shown = async (props) => {
    const { root, button } = await openCard([tile('card_002', 'review')], 'card_002', props)
    const judgeNow = !!button('Judge now')
    root.unmount()
    return judgeNow
  }
  assert.equal(await shown({ autoDispatchEnabled: true, telemetryEvents: started }), false)
  assert.equal(await shown({ autoDispatchEnabled: true }), true)
  assert.equal(await shown({ autoDispatchEnabled: false, telemetryEvents: started }), true)
})

// The tiles said so; the card's own view showed the plain status.
await check("a card held on a parent that can still finish says so in the card's own view", async () => {
  const { root, pip } = await openCard([tile('card_001', 'running'), tile('card_002', 'ready', ['card_001'])], 'card_002')
  assert.equal(pip.textContent, 'waiting')
  assert.equal(pip.getAttribute('title'), 'Waits on card_001')
  root.unmount()
})

// Cancel, Unblock and Reassign said they were done as soon as they were
// pressed, and the bridge's refusals reached only the activity feed.
const pressed = [
  {
    label: 'Unblock', status: 'blocked', command: { type: 'kanban_operator_unblock' },
    done: ['kanban_operator_unblocked', 'Operator unblocked card_002', {}], toast: 'Unblocked card_002',
    refused: ['kanban_operator_unblock_failed', "Could not unblock card_002: card is 'review'; nothing to unblock"],
  },
  {
    label: 'Cancel', status: 'running', command: { type: 'kanban_operator_cancel' },
    done: ['kanban_operator_cancelled', 'Operator cancelled card_002', {}], toast: 'Cancelled card_002',
    refused: ['kanban_operator_cancel_failed', 'Could not cancel card_002: it is not on the board'],
  },
  {
    label: 'Reassign…', status: 'ready', command: { type: 'kanban_operator_reassign', assignee: 'code' },
    done: ['kanban_operator_reassigned', 'Operator reassigned card_002 → code', { assignee: 'code' }],
    toast: 'Reassigned card_002 → code',
    refused: ['kanban_operator_reassign_failed', 'Could not reassign card_002: it is not on the board'],
  },
]
window.confirm = () => true
window.prompt = () => 'code'
for (const action of pressed) {
  await check(`${action.label} toasts the bridge's refusal, not that it was done`, async () => {
    useHarness.setState({ activeSessionId: SESSION, toast: null })
    const { root, button } = await openCard([tile('card_002', action.status)], 'card_002')
    button(action.label).click()
    await settle()
    assert.deepEqual(sent.at(-1), { ...action.command, sessionId: SESSION, cardId: 'card_002' })
    assert.equal(H().toast, null)
    const [subtype, message] = action.refused
    answer(subtype, message, { cardId: 'card_002', reason: 'r' })
    await settle()
    assert.equal(H().toast?.message, message)
    assert.equal(H().toast?.tone, 'warn')
    root.unmount()
  })

  await check(`${action.label} toasts that it was done when the bridge did it`, async () => {
    useHarness.setState({ activeSessionId: SESSION, toast: null })
    const { root, button } = await openCard([tile('card_002', action.status)], 'card_002')
    button(action.label).click()
    await settle()
    assert.equal(H().toast, null)
    const [subtype, message, details] = action.done
    answer(subtype, message, { cardId: 'card_002', ...details })
    await settle()
    assert.equal(H().toast?.message, action.toast)
    assert.equal(H().toast?.tone, 'info')
    // Answered once: a second answer toasts nothing.
    useHarness.setState({ toast: null })
    answer(subtype, message, { cardId: 'card_002', ...details })
    await settle()
    assert.equal(H().toast, null)
    root.unmount()
  })
}

await check('a press the Slack gateway could not take toasts why', async () => {
  useHarness.setState({ activeSessionId: SESSION, toast: null })
  const { root, button } = await openCard([tile('card_002', 'blocked')], 'card_002')
  button('Unblock').click()
  await settle()
  const message = "Not done: this session's kanban board is in the Slack gateway, which is not running."
  answer('control_command_skipped', message, { command: 'kanban_operator_unblock', requestId: undefined })
  await settle()
  assert.equal(H().toast?.message, message)
  assert.equal(H().toast?.tone, 'warn')
  // The press is answered.
  useHarness.setState({ toast: null })
  answer('kanban_operator_unblocked', 'Operator unblocked card_002', { cardId: 'card_002' })
  await settle()
  assert.equal(H().toast, null)
  root.unmount()
})

if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
