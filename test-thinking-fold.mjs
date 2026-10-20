// Thinking blocks stay open while the model writes them and fold once the
// next part starts; a task row in the activity panel opens its card in the
// dashboard's tasks tab. Drives the real store and renders the real
// components. Run from the repo root:
//   npx tsx test-thinking-fold.mjs

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
const scrolledTo = []
dom.window.HTMLElement.prototype.scrollIntoView = function () { scrolledTo.push(this.dataset.taskRow) }
globalThis.DOMParser = dom.window.DOMParser
globalThis.NodeFilter = dom.window.NodeFilter
globalThis.CSS = { escape: (s) => String(s).replace(/["\\]/g, '\\$&') }
globalThis.IS_REACT_ACT_ENVIRONMENT = false
window.harness = {
  sendCommand: async () => {},
  sessionSave: async () => ({ ok: true }),
  sessionIndexSave: async () => ({ ok: true }),
  sessionLoad: async () => ({ ok: false }),
}
const { useHarness } = await import('./src/renderer/state/store.ts')
const { ThinkingStream } = await import('./src/renderer/components/ThinkingStream.tsx')
const { TasksListRailView } = await import('./src/renderer/components/views/TasksListRailView.tsx')

const h = () => useHarness.getState()
const emit = (ev) => h().handleEvent(ev)
const S = 'session-local'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

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

function mount() {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const root = createRoot(host)
  return {
    host,
    render: (el) => flushSync(() => root.render(el)),
    unmount: () => { root.unmount(); host.remove() },
  }
}
const thinking = (props) => createElement(ThinkingStream, props)

// ── Store ──────────────────────────────────────────────────────────────

emit({ type: 'ready', sessionId: S, mode: 'live', capabilities: {} })

await check('a thinking part records when its first and latest deltas came', async () => {
  emit({ type: 'turn_start', turnId: 't1', sessionId: S })
  emit({ type: 'thinking_delta', thinking: '**Planning**\n\nfirst', sessionId: S })
  const first = h().messages.at(-1).parts.at(-1)
  assert.equal(first.type, 'thinking')
  assert.equal(typeof first.startedAt, 'number')
  assert.equal(first.endedAt, first.startedAt)
  await sleep(15)
  emit({ type: 'thinking_delta', thinking: ' and more', sessionId: S })
  const second = h().messages.at(-1).parts.at(-1)
  assert.equal(second.text, '**Planning**\n\nfirst and more')
  assert.equal(second.startedAt, first.startedAt)
  assert.ok(second.endedAt > second.startedAt)
})

await check('openDashboardTask opens the tasks tab with that task focused', () => {
  h().openDashboardTask('task_007')
  assert.equal(h().missionDashboardOpen, true)
  assert.equal(h().missionDashboardTab, 'tasks')
  assert.equal(h().missionDashboardFocusTaskId, 'task_007')
  h().clearDashboardTaskFocus()
  assert.equal(h().missionDashboardFocusTaskId, null)
  h().toggleMissionDashboard(false)
})

// ── ThinkingStream ─────────────────────────────────────────────────────

const TEXT = '**Reading the reducer**\n\nThe delta appends to the last part.\n\n**Writing the fix**\n\nAdd the timing fields.'

await check('live: open, with the current step in the header', async () => {
  const m = mount()
  m.render(thinking({ text: TEXT, live: true, startedAt: Date.now() - 4200 }))
  await sleep(900) // the reveal catches up within ~0.6 s
  const el = m.host.querySelector('.thought')
  assert.equal(el.dataset.live, 'true')
  assert.equal(el.dataset.open, 'true')
  assert.ok(m.host.querySelector('.thought-window'), 'live window mounted')
  assert.equal(m.host.querySelector('.thought-topic').textContent, 'Writing the fix')
  assert.match(m.host.querySelector('.thought-time').textContent, /^4s$/)
  m.unmount()
})

await check('folds when the block stops being the live tail', () => {
  const m = mount()
  const t0 = Date.now() - 12_000
  m.render(thinking({ text: TEXT, live: true, startedAt: t0, endedAt: t0 + 9000 }))
  m.render(thinking({ text: TEXT, live: false, startedAt: t0, endedAt: t0 + 9000 }))
  const el = m.host.querySelector('.thought')
  assert.equal(el.dataset.live, 'false')
  assert.equal(el.dataset.open, 'false')
  const head = m.host.querySelector('.thought-head').textContent.replace(/\s+/g, ' ')
  assert.match(head, /thought ?for 9s/)
  assert.match(head, /Reading the reducer → Writing the fix/)
  m.unmount()
})

await check('a fold during the stream is dropped once it ends, and a click opens a done block', () => {
  const m = mount()
  m.render(thinking({ text: TEXT, live: true }))
  m.host.querySelector('.thought-head').dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
  flushSync(() => {})
  assert.equal(m.host.querySelector('.thought').dataset.open, 'false', 'click folds a live block')
  m.render(thinking({ text: TEXT, live: false }))
  assert.equal(m.host.querySelector('.thought').dataset.open, 'false')
  m.host.querySelector('.thought-head').dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
  flushSync(() => {})
  const el = m.host.querySelector('.thought')
  assert.equal(el.dataset.open, 'true')
  assert.equal(m.host.querySelector('.thought-topic'), null, 'no summary over the open text')
  const titles = [...m.host.querySelectorAll('.thought-body h4')].map((n) => n.textContent)
  assert.deepEqual(titles, ['Reading the reducer', 'Writing the fix'])
  m.unmount()
})

await check('a saved block starts folded, with no time when it carries none', () => {
  const m = mount()
  m.render(thinking({ text: 'Plain thought without titles. Second sentence.', live: false }))
  assert.equal(m.host.querySelector('.thought').dataset.open, 'false')
  assert.equal(m.host.querySelector('.thought-time'), null)
  assert.equal(m.host.querySelector('.thought-topic').textContent, 'Plain thought without titles.')
  m.unmount()
})

await check('a search hit inside a folded block opens it and marks the hit', () => {
  const m = mount()
  m.render(thinking({ text: TEXT, live: false, searchQuery: 'timing' }))
  assert.equal(m.host.querySelector('.thought').dataset.open, 'true')
  assert.equal(m.host.querySelectorAll('.thought-body mark.search-hit').length, 1)
  m.render(thinking({ text: TEXT, live: false, searchQuery: 'nowhere' }))
  assert.equal(m.host.querySelector('.thought').dataset.open, 'false')
  m.unmount()
})

// ── Tasks tab focus ────────────────────────────────────────────────────

await check('the tasks view opens the focused card, unfolding older done tasks', async () => {
  const tasks = Array.from({ length: 8 }, (_, i) => ({
    id: `task_${i}`,
    title: `Task number ${i}`,
    status: 'done',
    completedAt: 1_000_000 + i,
    events: [],
    agents: [],
  }))
  let handled = 0
  const m = mount()
  m.render(
    createElement(TasksListRailView, {
      title: 'Mission',
      tasks,
      agents: [],
      contextPct: 0,
      cost: 0,
      onAttach: () => {},
      focusTaskId: 'task_0',
      onFocusTaskHandled: () => { handled++ },
    }),
  )
  await sleep(50)
  assert.equal(handled, 1)
  // task_0 finished first, so it sits past the five newest done rows.
  assert.ok(m.host.querySelector('[data-task-row="task_0"]'), 'older done row is shown')
  assert.match(m.host.querySelector('[data-task-row="task_0"]').className, /ring-accent/)
  assert.ok(document.body.textContent.includes('Task number 0'))
  assert.deepEqual(scrolledTo, ['task_0'])
  m.unmount()
})

console.log(failures ? `\n${failures} FAILED` : '\nall passed')
process.exit(failures ? 1 : 0)
