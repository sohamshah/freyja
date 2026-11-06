// main's tails of the gateway's and the scheduler daemon's logs read a line
// the writer has not finished, or that a batch cuts, whole once it ends.
//
// A tail read up to the log's end and saved that offset for the app's next
// start, half of a line the gateway was still writing too (or one the 1 MB
// batch cap cut): after a restart, the rest of the line could not be parsed
// and its event was lost. And each batch was decoded on its own, so a
// character of more than one byte that a batch cut in two was corrupted.
// Run from the repo root:
//   npx tsx test-log-tail-partial-lines.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

// The tailers resolve ~/.freyja from HOME when they load (imported below).
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'freyja-log-tail-partial-test-'))
process.env.HOME = home
const logs = path.join(home, '.freyja', 'logs')
fs.mkdirSync(logs, { recursive: true })
const gatewayLog = path.join(logs, 'gateway.log')
const daemonLog = path.join(logs, 'daemon.log')
fs.writeFileSync(gatewayLog, '')
fs.writeFileSync(daemonLog, '')

const { GatewayLogTailer } = await import('./src/main/gatewayLogTail.ts')
const { SchedulerDaemonLogTailer } = await import('./src/main/schedulerDaemonLogTail.ts')

const SLACK = 'freyja:slack:T1:channel:C1:1.0'
const BATCH = 1024 * 1024
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const line = (event) => `${JSON.stringify(event)}\n`
const gatewayEvent = (message) => ({ type: 'system_event', sessionId: SLACK, subtype: 'turn_cancelled', message })
const daemonEvent = (message) => ({
  type: 'system_event', sessionId: 'scheduler:global', subtype: 'scheduler_run_succeeded',
  message, details: { jobId: 'sched_digest', runId: 'run_1' },
})
// A message whose "é" (two bytes) is cut by the batch that starts with its
// line: its first byte is the batch's last.
function cutMessage(event, tag) {
  const before = line(event('')).indexOf('""') + 1
  return `${tag} ${'a'.repeat(BATCH - 1 - before - tag.length - 1)}é done`
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

const forwarded = []
function open() {
  const tail = new GatewayLogTailer((e) => forwarded.push(e.message))
  tail.start()
  return tail
}

let tailer = open()
await sleep(50)
const half = line(gatewayEvent('second'))
fs.appendFileSync(gatewayLog, line(gatewayEvent('first')) + half.slice(0, 20))
await sleep(400)
tailer.stop() // The app closes while the gateway writes its line.
fs.appendFileSync(gatewayLog, half.slice(20))
tailer = open()
await sleep(400)
await check('a line the gateway was writing as the app closed is read whole at its next start', () => {
  assert.deepEqual(forwarded, ['first', 'second'])
})

const cut = cutMessage(gatewayEvent, 'cut')
fs.appendFileSync(gatewayLog, line(gatewayEvent(cut)))
await sleep(700)
await check('a character a batch cuts in two reaches the app whole', () => {
  assert.equal(forwarded.length, 3)
  assert.ok(forwarded[2].endsWith('é done'), JSON.stringify(forwarded[2].slice(-8)))
})

tailer.stop()
const long = cutMessage(gatewayEvent, 'long')
fs.appendFileSync(gatewayLog, line(gatewayEvent(long)))
tailer = open()
await sleep(100) // Its first batch, not its second.
tailer.stop() // The app closes.
tailer = open()
await sleep(700)
await check('a line the batch cap cut as the app closed is read whole at its next start', () => {
  assert.deepEqual(forwarded.slice(3), [long])
})

tailer.stop()
fs.appendFileSync(gatewayLog, line(gatewayEvent('third')))
tailer = open()
await sleep(400)
await check('the line after it is read once', () => {
  assert.deepEqual(forwarded.slice(4), ['third'])
})
tailer.stop()

const daemonForwarded = []
const daemonTail = new SchedulerDaemonLogTailer((e) => daemonForwarded.push(e.message))
daemonTail.start()
const daemonMessage = cutMessage(daemonEvent, 'daemon')
fs.appendFileSync(daemonLog, line(daemonEvent(daemonMessage)))
await sleep(2500)
daemonTail.stop()
await check('a character a batch cuts in two in the daemon’s log reaches the app whole', () => {
  assert.equal(daemonForwarded.length, 1)
  assert.ok(daemonForwarded[0].endsWith('é done'), JSON.stringify(daemonForwarded[0].slice(-8)))
})

fs.rmSync(home, { recursive: true, force: true })
if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
