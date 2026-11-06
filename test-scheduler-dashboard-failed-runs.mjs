// The schedules dashboard counts a run cut off by a crash or a restart
// (interrupted) among the runs that did not succeed: the "failed" filter of
// the run feed shows it, and the 14-day timeline colors its day as one with
// a failed run. Both left it out: a job whose run was cut off every night
// showed an empty "failed" feed and green days.
// Run from the repo root:
//   npx tsx test-scheduler-dashboard-failed-runs.mjs

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

const now = Date.now() / 1000
const run = (n, status) => ({
  run_id: `run_${n}`, job_id: 'sched_digest', job_name: 'digest', status,
  started_at: now - 60 * n, finished_at: now - 60 * n + 5, duration_seconds: 5,
  fire_number: n, iteration: 0, output_text: '', delivery_reports: [],
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

// Each status and the words the feed names it by.
for (const [status, label] of [['interrupted', 'interrupted'], ['budget_exceeded', 'over budget']]) {
  useSchedulerStore.setState({
    jobs: [{
      id: 'sched_digest', name: 'digest', enabled: true, status: 'active',
      schedule: { kind: 'interval', seconds: 3600 }, fire_count: 2, prompt: 'digest',
      execution: { kind: 'new' }, sinks: [], next_fire_at: now + 1800,
    }],
    recentRuns: [run(1, 'succeeded'), run(2, status)],
    runsByJob: {},
    selectedJobId: null,
  })
  const host = document.createElement('div')
  document.body.appendChild(host)
  const root = createRoot(host)
  flushSync(() => root.render(createElement(ScheduledJobsDashboard)))
  await sleep(20)

  await check(`the "failed" filter shows a run that ended ${status}`, async () => {
    const chip = [...host.querySelectorAll('button')].find((b) => b.textContent === 'failed')
    chip.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
    await sleep(20)
    const rows = [...host.querySelectorAll('button')].filter((b) => b.textContent.includes('digest'))
      .map((b) => b.querySelector('span.uppercase')?.textContent).filter(Boolean)
    assert.deepEqual(rows, [label])
  })

  await check(`the timeline colors a day with a run that ended ${status} as failed`, () => {
    const today = host.querySelector('[title^="today"]')
    assert.ok(today, 'the timeline has today')
    assert.ok(today.classList.contains('bg-danger/70'), `today is ${today.className}`)
  })

  root.unmount()
  host.remove()
}

process.exit(failures ? 1 : 0)
