// The schedules dashboard's job list follows jobs made, changed, paused,
// resumed, stopped or removed in another process: the gateway (a schedule
// tool call in a Slack thread) or the scheduler daemon (a job's own run, or
// the stop of a job that keeps failing). Each sends a system event named
// scheduler_job_<what>, with the job's id. The gateway's reached the app
// through gatewayLogTail.ts and the daemon's not at all, and the store took
// neither: its scheduler_job_* branches matched an event type the bridge
// never sends. The list stayed as it was when last read.
// Run from the repo root:
//   npx tsx test-scheduler-job-events.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

// The tailers resolve ~/.freyja from HOME when they load (imported below).
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'freyja-job-events-test-'))
process.env.HOME = home
const logs = path.join(home, '.freyja', 'logs')
fs.mkdirSync(logs, { recursive: true })
fs.mkdirSync(path.join(home, '.freyja', 'control'), { recursive: true })
fs.writeFileSync(path.join(home, '.freyja', 'control', 'desktop-tail.offset'), '0')
const daemonLog = path.join(logs, 'daemon.log')
const gatewayLog = path.join(logs, 'gateway.log')

const { GatewayLogTailer } = await import('./src/main/gatewayLogTail.ts')
const { SchedulerDaemonLogTailer } = await import('./src/main/schedulerDaemonLogTail.ts')
const { bindSchedulerBridge, isSchedulerEvent, useSchedulerStore } =
  await import('./src/renderer/state/scheduler-store.ts')

const job = (id, fields = {}) => ({
  id, name: id, enabled: true, status: 'active', schedule: { kind: 'interval', seconds: 3600 },
  fire_count: 0, prompt: id, execution: { kind: 'new' }, sinks: [], ...fields,
})
const jobEvent = (subtype, jobId) => ({
  type: 'system_event', sessionId: 'scheduler:global', subtype,
  message: `Scheduled job '${jobId}' ${subtype}`, details: { jobId },
})
const line = (l) => JSON.stringify(l) + '\n'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// The jobs on disk, as the app's bridge reads them back.
let onDisk = [job('sched_digest'), job('sched_old')]
bindSchedulerBridge({
  send(cmd) {
    const answer = {
      'scheduler.list_jobs': () => ({ subtype: 'list_jobs', jobs: onDisk.map((j) => ({ ...j })) }),
      'scheduler.get_job': () => ({ subtype: 'get_job', job: onDisk.find((j) => j.id === cmd.jobId) }),
      'scheduler.get_runs': () => ({ subtype: 'get_runs', jobId: cmd.jobId, runs: [] }),
      'scheduler.recent_runs': () => ({ subtype: 'recent_runs', runs: [] }),
    }[cmd.type]?.()
    if (answer) {
      setTimeout(() => useSchedulerStore.getState().handleEvent({
        type: 'scheduler_response', requestId: cmd.id, ...answer,
      }), 0)
    }
  },
})
// What App.tsx does with each event main forwards.
const toStore = (events) => {
  for (const event of events) {
    if (isSchedulerEvent(event)) useSchedulerStore.getState().handleEvent(event)
  }
}
const listed = () => useSchedulerStore.getState().jobs.map((j) => `${j.id}:${j.status}`)

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

const fromDaemon = []
const daemonTail = new SchedulerDaemonLogTailer((event) => fromDaemon.push(event))
fs.writeFileSync(daemonLog, '')
daemonTail.start()
fs.appendFileSync(daemonLog, [
  jobEvent('scheduler_job_created', 'sched_new'),
  jobEvent('scheduler_job_stopped_failing', 'sched_digest'),
  { type: 'text_delta', sessionId: 'scheduler:sched_new.ephemeral', delta: 'scheduler_job_created' },
].map(line).join(''))
await sleep(1200)
daemonTail.stop()

await check('a job event the daemon writes reaches the app', () => {
  assert.deepEqual(fromDaemon, [
    jobEvent('scheduler_job_created', 'sched_new'),
    jobEvent('scheduler_job_stopped_failing', 'sched_digest'),
  ])
})

await check('the job list shows a job the daemon made and one it stopped', async () => {
  useSchedulerStore.setState({ jobs: onDisk.map((j) => ({ ...j })) })
  onDisk = [job('sched_digest', { enabled: false, status: 'disabled_error' }), job('sched_old'), job('sched_new')]
  toStore(fromDaemon)
  await sleep(20)
  assert.deepEqual(listed(), ['sched_digest:disabled_error', 'sched_old:active', 'sched_new:active'])
})

await check('the job list follows a job paused, then removed, in the gateway', async () => {
  fs.writeFileSync(gatewayLog, [
    jobEvent('scheduler_job_paused', 'sched_new'),
    jobEvent('scheduler_job_removed', 'sched_old'),
  ].map(line).join(''))
  const fromGateway = []
  const gatewayTail = new GatewayLogTailer((event) => fromGateway.push(event))
  gatewayTail.start()
  await sleep(100)
  gatewayTail.stop()
  assert.equal(fromGateway.length, 2)

  onDisk = [job('sched_digest', { enabled: false, status: 'disabled_error' }), job('sched_new', { status: 'paused' })]
  toStore(fromGateway)
  await sleep(20)
  assert.deepEqual(listed(), ['sched_digest:disabled_error', 'sched_new:paused'])
})

fs.rmSync(home, { recursive: true, force: true })
if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
