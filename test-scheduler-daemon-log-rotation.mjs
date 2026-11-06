// main's tail of the scheduler daemon's log follows a rotation of it
// without losing or repeating a run's or a job's event.
//
// The daemon rotates daemon.log in place (bridge/scheduler/daemon.py,
// rotate_daemon_log_if_large): it copies the whole log to daemon.log.1, each
// byte at the offset it had, replaces the .1 before with that copy (the old
// one gzipped first), then empties the log. The tail saw only a log shorter
// than where it was, and read on from its start: the events written since its
// last read, now at the end of .1, were lost.
// Run from the repo root:
//   npx tsx test-scheduler-daemon-log-rotation.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

// The tailer resolves ~/.freyja from HOME when it loads (imported below).
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'freyja-daemon-rotation-test-'))
process.env.HOME = home
const logs = path.join(home, '.freyja', 'logs')
fs.mkdirSync(logs, { recursive: true })
const log = path.join(logs, 'daemon.log')
const segment = `${log}.1`

const { SchedulerDaemonLogTailer } = await import('./src/main/schedulerDaemonLogTail.ts')

const runEvent = (n) => ({
  type: 'system_event', sessionId: 'scheduler:global', subtype: 'scheduler_run_succeeded',
  message: `run ${n}`, details: { jobId: 'sched_digest', runId: `run_${n}` },
})
// The daemon's session streams fill the log between its events.
const noise = JSON.stringify({ type: 'text_delta', sessionId: 'scheduler:sched_digest.ephemeral', delta: 'x'.repeat(200) })
const write = (...ns) => fs.appendFileSync(log, ns.map((n) => `${noise}\n${JSON.stringify(runEvent(n))}\n`).join(''))
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// As rotate_daemon_log_if_large does it, in its order. ``empty`` false stops
// it after .1 is replaced, before the log is emptied.
let rotations = 0
function rotate({ empty = true } = {}) {
  if (fs.existsSync(segment)) fs.renameSync(segment, `${log}.${++rotations}.gz`) // gzipped there
  fs.copyFileSync(log, `${segment}.tmp`)
  fs.renameSync(`${segment}.tmp`, segment)
  if (empty) fs.truncateSync(log, 0)
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

write(0) // Before the app started: not replayed.
const forwarded = []
const tailer = new SchedulerDaemonLogTailer((event) => forwarded.push(event.details.runId))
tailer.start()
const runs = (...ns) => ns.map((n) => `run_${n}`)

write(1, 2)
await sleep(1200)
await check('the tail reads the daemon’s events as they come', () => {
  assert.deepEqual(forwarded, runs(1, 2))
})

write(3, 4) // Not read yet when the daemon rotates its log.
rotate()
write(5)
await sleep(1200)
await check('events written just before a rotation are read from .1, then the new log', () => {
  assert.deepEqual(forwarded, runs(1, 2, 3, 4, 5))
})

write(6)
rotate({ empty: false }) // The tail looks while the log is being rotated.
await sleep(1200)
fs.truncateSync(log, 0)
write(7)
await sleep(1200)
await check('a tail that looks in the middle of a rotation reads each event once', () => {
  assert.deepEqual(forwarded, runs(1, 2, 3, 4, 5, 6, 7))
})

write(8)
await sleep(1200)
write(9)
rotate()
await sleep(1200)
await check('a second rotation, with the first .1 gzipped, loses nothing', () => {
  assert.deepEqual(forwarded, runs(1, 2, 3, 4, 5, 6, 7, 8, 9))
})

fs.truncateSync(log, 0) // By hand, with no rotation.
write(10)
await sleep(1200)
await check('a log emptied by hand is read again from its start', () => {
  assert.deepEqual(forwarded, runs(1, 2, 3, 4, 5, 6, 7, 8, 9, 10))
})

tailer.stop()
fs.rmSync(home, { recursive: true, force: true })
if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
