// Session saves: what they cost the main thread, and that they write the
// right bytes. Saves run in the Electron main process, whose thread also
// routes the window's input, so a stall here is a stall in typing.
// Run from the repo root:
//   npx tsx test-persistence-writer.mjs [path/to/a/large/session.json]
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { monitorEventLoopDelay } from 'node:perf_hooks'

// A large session to save: the one given, or a synthetic ~25 MB one.
const given = process.argv[2]
const big = given
  ? JSON.parse(fs.readFileSync(given, 'utf8'))
  : (() => {
      const image = 'iVBORw0KGgo'.padEnd(900_000, 'A')
      const toolCalls = {}
      for (let i = 0; i < 700; i++) {
        toolCalls[`toolu_${i}`] = {
          id: `toolu_${i}`, name: i % 25 === 0 ? 'view_image' : 'bash', status: 'done', startedAt: i,
          arguments: { command: 'echo '.repeat(20) }, result: 'x'.repeat(300),
          ...(i % 25 === 0 ? { resultImages: [{ pngBase64: image, mimeType: 'image/png', width: 1600, height: 1000, takenAt: i }] } : {}),
        }
      }
      return {
        version: 1, id: 'session-big', title: 'big', model: 'm', workspace: '~/', createdAt: 1, updatedAt: 2,
        messageCount: 1, totalInputTokens: 0, totalOutputTokens: 0, cacheReadTokens: 0,
        slice: { messages: [{ id: 'm1', role: 'user', parts: [{ type: 'text', text: 'hi' }], createdAt: 1 }], toolCalls },
      }
    })()
big.id = 'session-big'

// Isolate: the module resolves ~/.freyja from HOME when it loads.
const home = fs.mkdtempSync(path.join(os.tmpdir(), 'freyja-persist-test-'))
process.env.HOME = home
const persistence = await import('./src/main/persistence.ts')
const sessionsDir = path.join(home, '.freyja', 'sessions')

let failures = 0
function check(name, ok, detail = '') {
  console.log(`${ok ? '  ok  ' : '  FAIL'} ${name}${detail ? `  (${detail})` : ''}`)
  if (!ok) failures++
}

async function measure(label, fn) {
  const eld = monitorEventLoopDelay({ resolution: 1 })
  eld.enable()
  let worst = 0
  let last = performance.now()
  const probe = setInterval(() => {
    const now = performance.now()
    worst = Math.max(worst, now - last - 2)
    last = now
  }, 2)
  const t0 = performance.now()
  await fn()
  const total = performance.now() - t0
  clearInterval(probe)
  eld.disable()
  console.log(`  ${label}: done in ${Math.round(total)} ms, longest main-thread stall ${Math.round(Math.max(worst, eld.max / 1e6))} ms`)
  return Math.max(worst, eld.max / 1e6)
}

try {
  console.log(`\nsaving a ${(JSON.stringify(big).length / 1e6).toFixed(1)} MB session`)
  // Seed an index with many rows, as a long-lived install has.
  const rows = Array.from({ length: 1500 }, (_, i) => ({
    version: 1, id: `session-${i}`, title: `s${i}`, model: 'm', workspace: '~/', createdAt: i, updatedAt: i,
    messageCount: 1, totalInputTokens: 0, totalOutputTokens: 0, cacheReadTokens: 0, task: 'x'.repeat(5000),
  }))
  await persistence.saveSessionIndex(rows)

  await measure('first save', () => persistence.saveSession(big))
  const stall = await measure('second save', () => persistence.saveSession({ ...big, title: 'big 2' }))
  console.log('\nresults')
  check('main thread never stalls 40 ms or more on a save', stall < 40, `${Math.round(stall)} ms`)

  const onDisk = JSON.parse(fs.readFileSync(path.join(sessionsDir, 'session-big.json'), 'utf8'))
  check('the file holds the last save', onDisk.title === 'big 2')
  check('the file round-trips exactly', JSON.stringify(onDisk) === JSON.stringify({ ...big, title: 'big 2' }))
  const index = JSON.parse(fs.readFileSync(path.join(sessionsDir, '_index.json'), 'utf8'))
  const row = index.sessions.find((r) => r.id === 'session-big')
  check('the index row is upserted', !!row && row.title === 'big 2')
  check('the index keeps every other row', index.sessions.length === 1501, `${index.sessions.length} rows`)
  check('no temp files left behind', fs.readdirSync(sessionsDir).every((f) => !f.endsWith('.tmp')))

  // Rapid saves of one session land in order: the last one wins.
  await Promise.all([1, 2, 3, 4, 5].map((n) => persistence.saveSession({ ...big, title: `burst ${n}` })))
  const afterBurst = JSON.parse(fs.readFileSync(path.join(sessionsDir, 'session-big.json'), 'utf8'))
  check('a burst of saves ends on the last one', afterBurst.title === 'burst 5', afterBurst.title)
  const indexAfter = JSON.parse(fs.readFileSync(path.join(sessionsDir, '_index.json'), 'utf8'))
  check('the index ends on the last one too', indexAfter.sessions.find((r) => r.id === 'session-big')?.title === 'burst 5')

  // The index read on the next save must see writes made by others.
  const other = JSON.parse(fs.readFileSync(path.join(sessionsDir, '_index.json'), 'utf8'))
  other.sessions.push({ ...rows[0], id: 'session-written-elsewhere' })
  await new Promise((r) => setTimeout(r, 20))
  fs.writeFileSync(path.join(sessionsDir, '_index.json'), JSON.stringify(other))
  await persistence.saveSession({ ...big, title: 'after external write' })
  const merged = JSON.parse(fs.readFileSync(path.join(sessionsDir, '_index.json'), 'utf8'))
  check('an index written by another process is not lost', merged.sessions.some((r) => r.id === 'session-written-elsewhere'))

  if (typeof persistence.flushPersistenceWrites === 'function') {
    const pending = persistence.saveSession({ ...big, title: 'flushed' })
    await persistence.flushPersistenceWrites(5000)
    const flushed = JSON.parse(fs.readFileSync(path.join(sessionsDir, 'session-big.json'), 'utf8'))
    check('flushPersistenceWrites waits for in-flight saves', flushed.title === 'flushed')
    await pending
  }
} finally {
  fs.rmSync(home, { recursive: true, force: true })
}

if (failures) {
  console.log(`\n${failures} failure(s)`)
  process.exit(1)
}
console.log('\nall passed')
process.exit(0)
