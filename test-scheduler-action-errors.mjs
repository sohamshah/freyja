// The schedules dashboard says why the bridge refused an action. Its card
// and detail buttons awaited the bridge's answer and dropped it, so a
// refused resume (a job that ran all its max fires) did nothing visible.
// The Morning room's resume of the stopped briefer dropped it too, and so
// did the card's delete. The detail view's delete went back to the list
// even when refused, so its reason was set in a view no longer shown. The
// Morning room's run now dropped a refusal: a refused generate said
// "Briefer running" and polled for ten minutes, and a refused job intent
// said "Job started" and counted as dispatched.
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
const RUN_REFUSED = 'job sched_digest is running; try again when its run ends'
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
  'scheduler.run_job_now': () => ({ subtype: 'run_job_now', error: RUN_REFUSED }),
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

// The Morning room with today's briefing, whose decision and today line
// run the job.
const runIt = { kind: 'fire_job', job_id: job.id }
const briefing = {
  version: 1, date: new Date().toLocaleDateString('en-CA'), generated_at_iso: '2026-11-06T06:00:00-08:00',
  decisions: [{ verb: 'Run', project: 'digest', body: 'the digest', actions: [{ label: 'run digest', kind: 'primary', intent: runIt }] }],
  today: [{ project: 'digest', what: 'the digest', intent: runIt }],
}
function mountMorningRoom() {
  window.harness.getBriefing = async () => ({
    dates: [briefing.date], date: briefing.date, json: briefing, markdown: null,
    brieferJobId: job.id, brieferStopped: null,
  })
  // Its headline reads the reduced-motion setting, which jsdom has not.
  window.matchMedia = () => ({ matches: true })
  useHarness.setState({ toast: null })
  return mount(MorningRoom)
}

await check('a refused rebrief in the Morning room says why and does not poll', async () => {
  const intervals = []
  const realSetInterval = globalThis.setInterval
  globalThis.setInterval = (fn, ms, ...rest) => {
    intervals.push(ms)
    return realSetInterval(fn, ms, ...rest)
  }
  try {
    const { host, unmount } = mountMorningRoom()
    await sleep(20)
    clickButton(host, '↻ rebrief')
    await sleep(20)
    const toast = useHarness.getState().toast
    assert.ok(toast?.message.includes(RUN_REFUSED), `the bridge’s reason is shown (toast: ${toast?.message})`)
    assert.ok(!toast.message.includes('Briefer running'), 'it does not say the briefer runs')
    assert.ok(!intervals.includes(5000), 'no poll for a new briefing')
    assert.ok(host.textContent.includes('↻ rebrief'), 'the button is not left generating')
    unmount()
  } finally {
    globalThis.setInterval = realSetInterval
  }
})

await check('a refused job intent in the Morning room says why and is not counted', async () => {
  const { host, unmount } = mountMorningRoom()
  await sleep(20)
  clickButton(host, 'run digest')
  await sleep(20)
  let toast = useHarness.getState().toast
  assert.ok(toast?.message.includes(RUN_REFUSED), `the bridge’s reason is shown (toast: ${toast?.message})`)
  assert.ok(!toast.message.includes('Job started'), 'it does not say the job started')
  assert.ok(host.textContent.includes('run digest') && !host.textContent.includes('✓ run digest'), 'its button is live again')

  clickButton(host, 'run all 1 in parallel')
  await sleep(20)
  toast = useHarness.getState().toast
  assert.ok(toast?.message.includes(RUN_REFUSED), `the bridge’s reason is shown (toast: ${toast?.message})`)
  assert.ok(!toast.message.includes('1 run dispatched'), 'the refused run is not counted')
  const todayRun = [...host.querySelectorAll('button.mroom-run')]
  assert.ok(todayRun.length === 1 && todayRun[0].textContent === 'run' && !todayRun[0].disabled, 'its today line is live again')
  unmount()
})

console.log(failures ? `\n${failures} failed` : '\nall passed')
process.exit(failures ? 1 : 0)
