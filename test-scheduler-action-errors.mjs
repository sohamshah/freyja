// The schedules dashboard says why the bridge refused an action. Its card
// and detail buttons awaited the bridge's answer and dropped it, so a
// refused resume (a job that ran all its max fires) did nothing visible.
// The Morning room's resume of the stopped briefer dropped it too, and so
// did the card's delete. The detail view's delete went back to the list
// even when refused, so its reason was set in a view no longer shown.
// Run from the repo root:
//   npx tsx test-scheduler-action-errors.mjs

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
window.harness = {
  sendCommand: async () => {},
  sessionSave: async () => ({ ok: true }),
  sessionIndexSave: async () => ({ ok: true }),
  sessionLoad: async () => ({ ok: false }),
}
const { bindSchedulerBridge, useSchedulerStore } = await import('./src/renderer/state/scheduler-store.ts')
const { ScheduledJobsDashboard } = await import('./src/renderer/components/ScheduledJobsDashboard.tsx')
const { MorningRoom } = await import('./src/renderer/components/MorningRoom.tsx')
const { useHarness } = await import('./src/renderer/state/store.ts')

const REFUSED = 'it ran all 1 of its max fires; raise or clear max fires first'
const job = {
  id: 'sched_digest', name: 'digest', enabled: false, status: 'disabled_max_fires',
  schedule: { kind: 'interval', seconds: 3600 }, fire_count: 1, max_fires: 1,
  prompt: 'digest', execution: { kind: 'new' }, sinks: [],
}

// The bridge: answers each command as the Python bridge does.
const answers = {
  'scheduler.list_jobs': () => ({ subtype: 'list_jobs', jobs: [job] }),
  'scheduler.get_runs': (cmd) => ({ subtype: 'get_runs', jobId: cmd.jobId, runs: [] }),
  'scheduler.recent_runs': () => ({ subtype: 'recent_runs', runs: [] }),
  'scheduler.metrics': () => ({ subtype: 'metrics' }),
  'scheduler.daemon_status': () => ({ subtype: 'daemon_status' }),
  'scheduler.resume_job': () => ({ subtype: 'resume_job', error: REFUSED }),
  'scheduler.remove_job': (cmd) => ({ subtype: 'remove_job', error: `job ${cmd.jobId} is locked by a peer; retry shortly` }),
}
bindSchedulerBridge({
  send(cmd) {
    const answer = answers[cmd.type]?.(cmd)
    if (!answer) return
    setTimeout(() => useSchedulerStore.getState().handleEvent({
      type: 'scheduler_response', requestId: cmd.id, ...answer,
    }), 0)
  },
})

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
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

function mount(component = ScheduledJobsDashboard) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const root = createRoot(host)
  flushSync(() => root.render(createElement(component)))
  return { host, unmount: () => { root.unmount(); host.remove() } }
}

function clickButton(host, label = 'resume') {
  const button = [...host.querySelectorAll('button')].find((b) => b.textContent.trim() === label)
  assert.ok(button, `a ${label} button`)
  button.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
}

await check('a refused resume on a schedule card says why', async () => {
  useSchedulerStore.setState({ jobs: [job], selectedJobId: null })
  const { host, unmount } = mount()
  await sleep(20)
  clickButton(host)
  await sleep(20)
  assert.ok(host.textContent.includes(REFUSED), 'the bridge’s reason is shown')
  unmount()
})

await check('a refused resume in a schedule’s detail view says why', async () => {
  useSchedulerStore.setState({ jobs: [job], selectedJobId: job.id })
  const { host, unmount } = mount()
  await sleep(20)
  clickButton(host)
  await sleep(20)
  assert.ok(host.textContent.includes(REFUSED), 'the bridge’s reason is shown')
  unmount()
})

await check('a refused delete on a schedule card says why', async () => {
  useSchedulerStore.setState({ jobs: [job], selectedJobId: null })
  window.confirm = () => true
  const { host, unmount } = mount()
  await sleep(20)
  clickButton(host, 'delete')
  await sleep(20)
  assert.ok(host.textContent.includes('is locked by a peer; retry shortly'), 'the bridge’s reason is shown')
  unmount()
})

await check('a refused delete in a schedule’s detail view says why and stays on it', async () => {
  useSchedulerStore.setState({ jobs: [job], selectedJobId: job.id })
  window.confirm = () => true
  const { host, unmount } = mount()
  await sleep(20)
  clickButton(host, 'delete')
  await sleep(20)
  assert.equal(useSchedulerStore.getState().selectedJobId, job.id, 'still on the detail view')
  assert.ok(host.textContent.includes('is locked by a peer; retry shortly'), 'the bridge’s reason is shown')
  unmount()
})

await check('a refused resume of the stopped briefer says why', async () => {
  window.harness.getBriefing = async () => ({
    dates: [], date: null, json: null, markdown: null,
    brieferJobId: job.id, brieferStopped: 'the model call failed',
  })
  useHarness.setState({ toast: null })
  // Its backdrop asks for WebGL, which jsdom has not.
  window.HTMLCanvasElement.prototype.getContext = () => null
  const { host, unmount } = mount(MorningRoom)
  await sleep(20)
  clickButton(host, '▸ resume')
  await sleep(20)
  assert.ok(useHarness.getState().toast?.message.includes(REFUSED), 'the bridge’s reason is shown')
  unmount()
})

console.log(failures ? `\n${failures} failed` : '\nall passed')
process.exit(failures ? 1 : 0)
