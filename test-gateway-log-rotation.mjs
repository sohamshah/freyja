// main's tail of the gateway's log follows a rotation of it without losing
// or repeating an event, one while the app runs or one while it is closed.
//
// The gateway rotates gateway.log in place (bridge/gateway/pid.py,
// rotate_log_if_large): it copies the whole log to gateway.log.1, each byte
// at the offset it had, replaces the .1 before with that copy (the old one
// gzipped first), then empties the log. The tail saw only a log shorter than
// where it was, and read on from its start: the events written since its
// last read, now at the end of .1, were lost.
//
// The tail saves its offset for the app's next start, but not which .1 it
// had read: one made while the app was closed was taken as read, so the
// events written before it were lost and the log was read on from the old
// offset, in the middle of its first lines. Rotated twice while the app was
// closed, the offset was in a log gzipped since, and the tail read the newest
// .1 on from it: from the middle of a line, or past events it never read.
// Run from the repo root:
//   npx tsx test-gateway-log-rotation.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

// The tailer resolves ~/.freyja from HOME when it loads (imported below).
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'freyja-gateway-rotation-test-'))
process.env.HOME = home
const logs = path.join(home, '.freyja', 'logs')
fs.mkdirSync(logs, { recursive: true })
const log = path.join(logs, 'gateway.log')
const segment = `${log}.1`

const { GatewayLogTailer } = await import('./src/main/gatewayLogTail.ts')

const SLACK = 'freyja:slack:T1:channel:C1:1.0'
const event = (n) => ({ type: 'system_event', sessionId: SLACK, subtype: 'turn_cancelled', message: `event ${n}` })
// The gateway's own log lines fill the log between its events.
const noise = `2026-11-06 04:00:00 [INFO] freyja.gateway: ${'x'.repeat(200)}`
const write = (...ns) => fs.appendFileSync(log, ns.map((n) => `${noise}\n${JSON.stringify(event(n))}\n`).join(''))
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

// As rotate_log_if_large does it, in its order. ``empty`` false stops it
// after .1 is replaced, before the log is emptied.
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

write(0) // Before the app first started: not replayed.
const forwarded = []
// The app starts: the tail resumes from the offset it saved.
function open() {
  const tail = new GatewayLogTailer((e) => forwarded.push(e.message))
  tail.start()
  return tail
}
let tailer = open()
const events = (...ns) => ns.map((n) => `event ${n}`)

write(1, 2)
await sleep(400)
await check('the tail reads the gateway’s events as they come', () => {
  assert.deepEqual(forwarded, events(1, 2))
})

write(3, 4) // Not read yet when the gateway rotates its log.
rotate()
write(5)
await sleep(400)
await check('events written just before a rotation are read from .1, then the new log', () => {
  assert.deepEqual(forwarded, events(1, 2, 3, 4, 5))
})

write(6)
rotate({ empty: false }) // The tail looks while the log is being rotated.
await sleep(400)
fs.truncateSync(log, 0)
write(7)
await sleep(400)
await check('a tail that looks in the middle of a rotation reads each event once', () => {
  assert.deepEqual(forwarded, events(1, 2, 3, 4, 5, 6, 7))
})

write(8)
await sleep(400)
write(9)
rotate()
await sleep(400)
await check('a second rotation, with the first .1 gzipped, loses nothing', () => {
  assert.deepEqual(forwarded, events(1, 2, 3, 4, 5, 6, 7, 8, 9))
})

fs.truncateSync(log, 0) // By hand, with no rotation.
write(10)
await sleep(400)
await check('a log emptied by hand is read again from its start', () => {
  assert.deepEqual(forwarded, events(1, 2, 3, 4, 5, 6, 7, 8, 9, 10))
})

tailer.stop() // The app closes.
write(11, 12)
rotate()
write(13)
tailer = open()
await sleep(400)
await check('events written before a rotation while the app was closed are read from .1, then the new log', () => {
  assert.deepEqual(forwarded, events(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13))
})

tailer.stop()
write(14, 15, 16)
rotate()
write(17, 18) // The new log grows past where the tail was.
tailer = open()
await sleep(400)
await check('a new log longer than where the tail was is read from its start', () => {
  assert.deepEqual(forwarded, events(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18))
})

write(19)
rotate()
await sleep(400) // Read from .1 while the app runs.
tailer.stop()
tailer = open()
write(20)
await sleep(400)
await check('a rotation read before the app closed is not read again', () => {
  assert.deepEqual(forwarded, events(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20))
})

tailer.stop()
write(21)
rotate({ empty: false }) // The app opens while the log is being rotated.
tailer = open()
await sleep(400)
fs.truncateSync(log, 0)
write(22)
await sleep(400)
await check('an app that opens in the middle of a rotation reads each event once', () => {
  assert.deepEqual(forwarded, events(1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22))
})

tailer.stop()
write(23, 24) // Not read yet when the gateway rotates its log twice.
rotate()
write(25, 26, 27)
rotate() // 23 and 24 are gzipped now.
write(28)
const warned = []
const warn = console.warn
console.warn = (...args) => warned.push(args.join(' '))
tailer = open()
await sleep(400)
console.warn = warn
await check('after two rotations while the app was closed, the newest .1 is read from its start, and the skip is logged', () => {
  assert.deepEqual(forwarded.slice(22), events(25, 26, 27, 28))
  assert.equal(warned.length, 1)
  assert.match(warned[0], /skipped/)
})

tailer.stop()
fs.rmSync(home, { recursive: true, force: true })
if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
