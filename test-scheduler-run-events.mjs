// The schedules dashboard follows a run started with run now to its end.
// The bridge answers run now as the run starts (its command loop takes one
// command at a time, and waiting for the run held every other command), so
// the run's outcome comes only in its events: system events named
// scheduler_run_claimed, then scheduler_run_<status>, with the job's id.
// The store took only events whose type began with scheduler_, so the run
// stayed "claimed" in the dashboard.
// Run from the repo root:
//   npx tsx test-scheduler-run-events.mjs

import assert from 'node:assert/strict'

const { bindSchedulerBridge, isSchedulerEvent, schedulerApi, useSchedulerStore } =
  await import('./src/renderer/state/scheduler-store.ts')

const JOB = 'sched_briefer'
const RUN = 'run_1'
const job = {
  id: JOB, name: 'Morning briefing', enabled: true, status: 'active',
  schedule: { kind: 'cron', expr: '0 6 * * *' }, fire_count: 4, prompt: 'brief me',
  execution: { kind: 'new' }, sinks: [],
}
// The run's file, as the bridge reads it back.
let onDisk = { run_id: RUN, job_id: JOB, job_name: job.name, started_at: 1, status: 'claimed', fire_number: 5 }

const answers = {
  'scheduler.run_job_now': () => ({ subtype: 'run_job_now', run: { ...onDisk } }),
  'scheduler.get_runs': (cmd) => ({ subtype: 'get_runs', jobId: cmd.jobId, runs: [{ ...onDisk }] }),
  'scheduler.recent_runs': () => ({ subtype: 'recent_runs', runs: [{ ...onDisk }] }),
  'scheduler.get_job': () => ({ subtype: 'get_job', job }),
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

// What App.tsx does with each bridge event.
function fromBridge(event) {
  if (isSchedulerEvent(event)) useSchedulerStore.getState().handleEvent(event)
}

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

await check('a run started with run now shows how it ended', async () => {
  const answer = await schedulerApi.runJobNow(JOB)
  assert.equal(answer.run.run_id, RUN)
  assert.equal(useSchedulerStore.getState().recentRuns[0].status, 'claimed')

  onDisk = { ...onDisk, status: 'succeeded', finished_at: 90 }
  fromBridge({
    type: 'system_event', sessionId: 'scheduler:global', subtype: 'scheduler_run_succeeded',
    message: "Scheduled job 'Morning briefing' succeeded", details: { jobId: JOB, runId: RUN },
  })
  await sleep(20)

  const { recentRuns, runsByJob } = useSchedulerStore.getState()
  assert.equal(recentRuns[0].status, 'succeeded', 'the run feed has the outcome')
  assert.equal(runsByJob[JOB]?.[0]?.status, 'succeeded', 'the job’s runs have the outcome')
})

await check('other system events are not the scheduler’s', async () => {
  assert.equal(isSchedulerEvent({ type: 'system_event', subtype: 'turn_cancelled', details: {} }), false)
  assert.equal(isSchedulerEvent({ type: 'scheduler_response', subtype: 'list_jobs' }), true)
})

process.exit(failures ? 1 : 0)
