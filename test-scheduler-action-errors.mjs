// The schedules dashboard says why the bridge refused an action. Its card
// and detail buttons awaited the bridge's answer and dropped it, so a
// refused resume (a job that ran all its max fires) did nothing visible.
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

function mount() {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const root = createRoot(host)
  flushSync(() => root.render(createElement(ScheduledJobsDashboard)))
  return { host, unmount: () => { root.unmount(); host.remove() } }
}

function clickResume(host) {
  const button = [...host.querySelectorAll('button')].find((b) => b.textContent.trim() === 'resume')
  assert.ok(button, 'a resume button')
  button.dispatchEvent(new window.MouseEvent('click', { bubbles: true }))
}

await check('a refused resume on a schedule card says why', async () => {
  useSchedulerStore.setState({ jobs: [job], selectedJobId: null })
  const { host, unmount } = mount()
  await sleep(20)
  clickResume(host)
  await sleep(20)
  assert.ok(host.textContent.includes(REFUSED), 'the bridge’s reason is shown')
  unmount()
})

await check('a refused resume in a schedule’s detail view says why', async () => {
  useSchedulerStore.setState({ jobs: [job], selectedJobId: job.id })
  const { host, unmount } = mount()
  await sleep(20)
  clickResume(host)
  await sleep(20)
  assert.ok(host.textContent.includes(REFUSED), 'the bridge’s reason is shown')
  unmount()
})

console.log(failures ? `\n${failures} failed` : '\nall passed')
process.exit(failures ? 1 : 0)
