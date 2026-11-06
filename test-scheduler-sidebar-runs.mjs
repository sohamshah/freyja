// The sidebar's scheduled runs show each run's status as the schedules
// dashboard does. It gave every run that was not failed or going on the
// green "ok" dot: a cancelled run, one cut off by a crash or a restart
// (interrupted), and one whose delivery partly failed looked like runs
// that succeeded. Each row also named its run's status by its raw name
// (not_started, timed_out), where the dashboard says "not started".
// Run from the repo root:
//   npx tsx test-scheduler-sidebar-runs.mjs

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
// The backdrop, the sticky headers and the active row measure or scroll
// themselves; jsdom lays nothing out.
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
const { Sidebar } = await import('./src/renderer/components/Sidebar.tsx')

// The dot the dashboard's run feed gives each status (RunRow).
const expected = {
  succeeded: 'bg-ok',
  failed: 'bg-danger',
  timed_out: 'bg-danger',
  partial_failure: 'bg-warn',
  running: 'bg-accent',
  cancelled: 'bg-fg-3',
  interrupted: 'bg-danger',
  claimed: 'bg-fg-3',
  expired: 'bg-fg-3',
  not_started: 'bg-fg-3',
}
useSchedulerStore.setState({
  recentRuns: Object.keys(expected).map((status, i) => ({
    run_id: `run_${i}`, job_id: 'sched_digest', job_name: `digest ${status}`, status,
    started_at: Date.now() / 1000 - 60, duration_seconds: 1, fire_number: i + 1, iteration: 0,
    output_text: '', delivery_reports: [],
  })),
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
flushSync(() => root.render(createElement(Sidebar)))
const head = [...host.querySelectorAll('button')].find((b) => b.textContent.includes('scheduled runs'))
head.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
await sleep(20)

function rowOf(status) {
  const row = [...host.querySelectorAll('button')].find((b) => b.title.startsWith(`digest ${status} · `))
  assert.ok(row, `a row for the ${status} run`)
  return row
}

function dotOf(status) {
  const dot = rowOf(status).querySelector('.fold-dot')
  return [...dot.classList].find((c) => /^bg-/.test(c))
}

for (const [status, dot] of Object.entries(expected)) {
  await check(`the ${status} run has the dashboard's ${dot} dot`, () => {
    assert.equal(dotOf(status), dot)
  })
}

// The words the dashboard's run feed and drawer use.
const labels = { timed_out: 'timed out', not_started: 'not started', succeeded: 'completed', claimed: 'starting' }
for (const [status, label] of Object.entries(labels)) {
  await check(`the ${status} run is named "${label}", as on the dashboard`, () => {
    const row = rowOf(status)
    assert.ok(row.title.endsWith(` · ${label}`), `its title is ${row.title}`)
    const line = row.querySelector('.font-mono.text-fg-3:not(.shrink-0)')
    assert.ok(line.textContent.startsWith(`${label} · `), `its line is ${line.textContent}`)
  })
}

root.unmount()
process.exit(failures ? 1 : 0)
