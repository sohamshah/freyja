// The schedules dashboard shows each run status in the color of the
// states it is grouped with. A run cut off by a crash or a restart
// (interrupted), one handed over too late to run (expired) and one that
// could not start (not_started) had no color: the run feed showed their
// word in the default white, and the run's drawer showed every status,
// failed ones too, in the pulsing accent of a run in flight, with
// interrupted, expired and not_started as their raw names. The feed and a
// schedule's "last run" still showed the raw names (TIMED_OUT,
// NOT_STARTED), and a timeline day of only cancelled, expired or not
// started runs looked like a day with no run.
// Run from the repo root:
//   npx tsx test-scheduler-run-status-look.mjs

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
window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} })
globalThis.ResizeObserver = class { observe() {} unobserve() {} disconnect() {} }
globalThis.IntersectionObserver = class { observe() {} unobserve() {} disconnect() {} }
window.HTMLElement.prototype.scrollIntoView = () => {}
window.HTMLCanvasElement.prototype.getContext = () => null
window.harness = {
  sendCommand: async () => {},
  sessionSave: async () => ({ ok: true }),
  sessionIndexSave: async () => ({ ok: true }),
  sessionLoad: async () => ({ ok: false }),
}
const { useSchedulerStore } = await import('./src/renderer/state/scheduler-store.ts')
const { ScheduledJobsDashboard } = await import('./src/renderer/components/ScheduledJobsDashboard.tsx')

// Each status: the color of its word and dot in the run feed, and the
// drawer's label, its color and whether its dot pulses.
const expected = {
  succeeded: { feed: 'ok', label: 'completed', drawer: 'text-ok', pulse: false },
  failed: { feed: 'danger', label: 'failed', drawer: 'text-danger', pulse: false },
  timed_out: { feed: 'danger', label: 'timed out', drawer: 'text-danger', pulse: false },
  budget_exceeded: { feed: 'danger', label: 'over budget', drawer: 'text-danger', pulse: false },
  // Counted with the failed runs by the "failed" filter and the timeline.
  interrupted: { feed: 'danger', label: 'interrupted', drawer: 'text-danger', pulse: false },
  partial_failure: { feed: 'warn', label: 'partial delivery failure', drawer: 'text-warn', pulse: false },
  running: { feed: 'accent', label: 'in flight', drawer: 'text-accent', pulse: true },
  // Runs that never ran grey, as a cancelled one.
  cancelled: { feed: 'fg-3', label: 'cancelled', drawer: 'text-fg-2', pulse: false },
  expired: { feed: 'fg-3', label: 'expired', drawer: 'text-fg-2', pulse: false },
  not_started: { feed: 'fg-3', label: 'not started', drawer: 'text-fg-2', pulse: false },
  queued: { feed: 'fg-3', label: 'queued', drawer: 'text-fg-2', pulse: false },
}

const now = Date.now() / 1000
const runOf = (status, startedAt, i) => ({
  run_id: `run_${status}`, job_id: 'sched_digest', job_name: 'digest', status,
  started_at: startedAt, finished_at: startedAt + 5, duration_seconds: 5,
  fire_number: i + 1, iteration: 0, output_text: `[${status}]`, delivery_reports: [],
})
useSchedulerStore.setState({
  jobs: [{
    id: 'sched_digest', name: 'digest', enabled: true, status: 'active',
    schedule: { kind: 'interval', seconds: 3600 }, fire_count: 1, prompt: 'digest',
    execution: { kind: 'new' }, sinks: [], next_fire_at: now + 1800,
  }],
  recentRuns: Object.keys(expected).map((status, i) => runOf(status, now - 60 * (i + 1), i)),
  runsByJob: {},
  selectedJobId: null,
  selectedRunId: null,
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

const host = document.createElement('div')
document.body.appendChild(host)
const root = createRoot(host)
flushSync(() => root.render(createElement(ScheduledJobsDashboard)))
await sleep(20)

function rowOf(status) {
  const row = [...host.querySelectorAll('button')]
    .find((b) => b.querySelector('span.italic')?.textContent === `[${status}]`)
  assert.ok(row, `a row for the ${status} run`)
  return row
}

for (const [status, look] of Object.entries(expected)) {
  await check(`the feed names the ${status} run "${look.label}", as its drawer does`, () => {
    assert.equal(rowOf(status).querySelector('span.uppercase').textContent, look.label)
  })

  await check(`the feed shows the ${status} run in ${look.feed}`, () => {
    const row = rowOf(status)
    const word = row.querySelector('span.uppercase')
    const dot = word.previousElementSibling
    assert.ok(word.classList.contains(`text-${look.feed}`), `its word is ${word.className}`)
    assert.ok(dot.classList.contains(`bg-${look.feed}`), `its dot is ${dot.className}`)
  })

  await check(`the drawer of the ${status} run says "${look.label}" in ${look.drawer}`, async () => {
    rowOf(status).dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
    await sleep(20)
    const label = host.querySelector('aside header > span')
    assert.ok(label, 'the drawer is open')
    assert.equal(label.textContent, look.label)
    assert.ok(label.classList.contains(look.drawer), `its label is ${label.className}`)
    const pulses = label.querySelector('span').classList.contains('animate-pulse-soft')
    assert.equal(pulses, look.pulse, `its dot ${pulses ? 'pulses' : 'is still'}`)
    useSchedulerStore.getState().selectRun(null)
    await sleep(20)
  })
}

// Yesterday held only runs that never ran or were cancelled.
const today = new Date()
today.setHours(0, 0, 0, 0)
const yesterday = today.getTime() / 1000 - 3600
const notRun = ['not_started', 'cancelled', 'expired'].map((status, i) => runOf(status, yesterday - 60 * i, i))
flushSync(() => useSchedulerStore.setState({ recentRuns: notRun }))
await sleep(20)

await check('a timeline day of only cancelled, expired or not started runs has a grey mark', () => {
  const day = host.querySelector('[title="1d ago — 3 run(s)"]')
  const empty = host.querySelector('[title="2d ago — 0 run(s)"]')
  assert.ok(day && empty, 'the timeline shows yesterday and the day before')
  const grey = (cell) => [...cell.classList].some((c) => c.startsWith('bg-fg-3'))
  assert.ok(grey(day), `yesterday is ${day.className}`)
  assert.ok(!grey(empty), `the day before is ${empty.className}`)
})

// The chips said "succeeded" and "running" over rows that say "completed"
// and "in flight", and so did the feed when a chip hid every run.
await check('the feed\'s filter chips name the statuses as its rows do', () => {
  const words = [...host.querySelectorAll('button')].map((b) => b.textContent)
  for (const word of ['all', 'completed', 'failed', 'in flight']) {
    assert.ok(words.includes(word), `a "${word}" chip`)
  }
  for (const word of ['succeeded', 'running']) {
    assert.ok(!words.includes(word), `no "${word}" chip`)
  }
})

await check('a chip that hides every run names its status as the rows do', async () => {
  const chip = [...host.querySelectorAll('button')].find((b) => b.textContent === 'in flight')
  chip.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
  await sleep(20)
  assert.ok(host.textContent.includes('no in flight runs — 3 others hidden by the filter'), host.textContent)
  const all = [...host.querySelectorAll('button')].find((b) => b.textContent === 'all')
  all.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
  await sleep(20)
})

await check('a schedule\'s last run is named as its drawer names it', async () => {
  flushSync(() => useSchedulerStore.getState().selectJob('sched_digest'))
  await sleep(20)
  const last = [...host.querySelectorAll('span')].find((s) => s.textContent.startsWith('last run '))
  assert.ok(last, 'the schedule shows its last run')
  assert.equal(last.querySelector('span').textContent, 'not started')
})

root.unmount()
host.remove()
process.exit(failures ? 1 : 0)
