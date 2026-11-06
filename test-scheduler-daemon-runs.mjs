// The schedules dashboard and the sidebar follow a run the scheduler daemon
// starts. The daemon often owns the tick; its runs' events (system events
// named scheduler_run_claimed, then scheduler_run_<status>) go to its
// stdout, ~/.freyja/logs/daemon.log, which nothing read. The app's own
// bridge sends its events on its stdout, and the gateway's reach the app
// through gatewayLogTail.ts, so the daemon's runs alone stayed as they were
// when last read: a finished run "running", a new one missing.
// Run from the repo root:
//   npx tsx test-scheduler-daemon-runs.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

// The tailers resolve ~/.freyja from HOME when they load (imported below).
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'freyja-daemon-runs-test-'))
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

const JOB = 'sched_briefer'
const RUN = 'run_1'
const runEvent = (subtype) => ({
  type: 'system_event', sessionId: 'scheduler:global', subtype,
  message: `Scheduled job 'Morning briefing' ${subtype}`, details: { jobId: JOB, runId: RUN },
})
const line = (l) => (typeof l === 'string' ? l : JSON.stringify(l)) + '\n'
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

await check('a run the gateway starts already reaches the app through its log', async () => {
  fs.writeFileSync(gatewayLog, line(runEvent('scheduler_run_succeeded')))
  const forwarded = []
  const tailer = new GatewayLogTailer((event) => forwarded.push(event))
  tailer.start()
  await sleep(100)
  tailer.stop()
  assert.deepEqual(forwarded.map((e) => e.subtype), ['scheduler_run_succeeded'])
})

// The daemon's log: what it wrote before the app started, then its run.
fs.writeFileSync(daemonLog, line(runEvent('scheduler_run_failed')))
const forwarded = []
const tailer = new SchedulerDaemonLogTailer((event) => forwarded.push(event))
tailer.start()
fs.appendFileSync(daemonLog, [
  '2026-11-06 06:00:00,120 [INFO] freyja.scheduler.service: job sched_briefer claimed',
  { type: 'log', level: 'info', message: 'freyja headless daemon starting (pid=60228)' },
  runEvent('scheduler_run_claimed'),
  { type: 'text_delta', sessionId: `scheduler:${JOB}.ephemeral`, delta: 'Good morning' },
  { type: 'system_event', sessionId: `scheduler:${JOB}.ephemeral`, subtype: 'turn_cancelled', message: 'x' },
].map(line).join(''))
// The last line arrives in two writes.
const last = line(runEvent('scheduler_run_succeeded'))
fs.appendFileSync(daemonLog, last.slice(0, 40))
await sleep(1200)
fs.appendFileSync(daemonLog, last.slice(40))
await sleep(1200)
tailer.stop()

await check('a run the daemon starts reaches the app: its claim, then how it ended', () => {
  assert.deepEqual(forwarded, [runEvent('scheduler_run_claimed'), runEvent('scheduler_run_succeeded')])
})

await check('what the daemon wrote before the app started is not replayed', () => {
  assert.ok(!forwarded.some((e) => e.subtype === 'scheduler_run_failed'))
})

await check('the daemon’s sessions and log lines stay its own', () => {
  assert.ok(forwarded.every((e) => e.sessionId === 'scheduler:global'))
})

await check('the dashboard shows how the daemon’s run ended', async () => {
  // The run's file, as the app's bridge reads it back.
  let onDisk = { run_id: RUN, job_id: JOB, job_name: 'Morning briefing', started_at: 1, status: 'running' }
  bindSchedulerBridge({
    send(cmd) {
      const answer = {
        'scheduler.get_runs': () => ({ subtype: 'get_runs', jobId: cmd.jobId, runs: [{ ...onDisk }] }),
        'scheduler.recent_runs': () => ({ subtype: 'recent_runs', runs: [{ ...onDisk }] }),
        'scheduler.get_job': () => ({ subtype: 'get_job' }),
      }[cmd.type]?.()
      if (answer) {
        setTimeout(() => useSchedulerStore.getState().handleEvent({
          type: 'scheduler_response', requestId: cmd.id, ...answer,
        }), 0)
      }
    },
  })
  useSchedulerStore.setState({ recentRuns: [{ ...onDisk }] })
  onDisk = { ...onDisk, status: 'succeeded', finished_at: 90 }
  // What App.tsx does with each event main forwards.
  for (const event of forwarded) {
    if (isSchedulerEvent(event)) useSchedulerStore.getState().handleEvent(event)
  }
  await sleep(20)
  assert.equal(useSchedulerStore.getState().recentRuns[0].status, 'succeeded')
  assert.equal(useSchedulerStore.getState().runsByJob[JOB]?.[0]?.status, 'succeeded')
})

fs.rmSync(home, { recursive: true, force: true })
if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
