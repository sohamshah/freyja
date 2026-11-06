// The app shows what the gateway reports. It tails the gateway's event log
// and passes on the events that name a session; an error that named none
// (the gateway failing at something that is no one session's) was dropped,
// so the app never heard of it. Run from the repo root:
//   npx tsx test-gateway-log-tail.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

// The tailer resolves ~/.freyja from HOME when it loads (imported below).
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'freyja-gateway-tail-test-'))
process.env.HOME = home
const { GatewayLogTailer } = await import('./src/main/gatewayLogTail.ts')
const logFile = path.join(home, '.freyja', 'logs', 'gateway.log')
fs.mkdirSync(path.dirname(logFile), { recursive: true })
fs.mkdirSync(path.join(home, '.freyja', 'control'), { recursive: true })
fs.writeFileSync(path.join(home, '.freyja', 'control', 'desktop-tail.offset'), '0')

const SLACK = 'freyja:slack:T1:channel:C1:1.0'
const lines = [
  '2026-11-06 04:00:00 [INFO] freyja.gateway: slack adapter connected',
  { type: 'log', level: 'info', message: 'bridge state ready' },
  { type: 'error', message: 'ANTHROPIC_API_KEY is not set', recoverable: true },
  { type: 'system_event', sessionId: SLACK, subtype: 'turn_cancelled', message: 'Cancelled 1' },
]
fs.writeFileSync(logFile, lines.map((l) => (typeof l === 'string' ? l : JSON.stringify(l))).join('\n') + '\n')

const forwarded = []
const tailer = new GatewayLogTailer((event) => forwarded.push(event))
tailer.start()
await new Promise((resolve) => setTimeout(resolve, 100))
tailer.stop()

let failures = 0
function check(name, fn) {
  try {
    fn()
    console.log(`ok   ${name}`)
  } catch (err) {
    failures++
    console.log(`FAIL ${name}\n     ${err.message}`)
  }
}

check('an error that names no session reaches the app, as the bridge\'s own do', () => {
  const errors = forwarded.filter((e) => e.type === 'error')
  assert.deepEqual(errors, [{ type: 'error', message: 'ANTHROPIC_API_KEY is not set', recoverable: true }])
})

check('events for a session still reach it', () => {
  assert.deepEqual(forwarded.filter((e) => e.sessionId).map((e) => e.subtype), ['turn_cancelled'])
})

check('other events that name no session stay out', () => {
  assert.equal(forwarded.filter((e) => e.type === 'log').length, 0)
  assert.equal(forwarded.length, 2)
})

fs.rmSync(home, { recursive: true, force: true })
if (failures) {
  console.log(`\n${failures} failed`)
  process.exit(1)
}
console.log('\nall passed')
