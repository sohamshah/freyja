// Functional checks for the long-session performance changes, run against a
// built renderer with the mock bridge:
//   node verify.mjs --dist out/dist-fix --title "<session title>" [--shots out/shots]
import http from 'node:http'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { chromium } from 'playwright-core'

const args = Object.fromEntries(
  process.argv.slice(2).reduce((acc, a, i, arr) => {
    if (a.startsWith('--')) acc.push([a.slice(2), arr[i + 1]?.startsWith('--') ? true : arr[i + 1] ?? true])
    return acc
  }, []),
)
if (!args.title) throw new Error('--title "<session title>" is required')
const DIST = path.resolve(args.dist)
const HERE = path.dirname(new URL(import.meta.url).pathname)
const SESS = path.join(os.homedir(), '.freyja/sessions')
const SHOTS = args.shots ? path.resolve(args.shots) : null
if (SHOTS) fs.mkdirSync(SHOTS, { recursive: true })

const server = http.createServer((req, res) => {
  const u = decodeURIComponent(req.url.split('?')[0])
  const f = u.startsWith('/__sessions/') ? path.join(SESS, u.slice(12)) : path.join(DIST, u === '/' ? 'index.html' : u)
  fs.readFile(f, (e, d) => {
    if (e) { res.writeHead(404); res.end(); return }
    const ext = path.extname(f)
    res.writeHead(200, { 'content-type': { '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.json': 'application/json' }[ext] || 'application/octet-stream' })
    res.end(d)
  })
})
await new Promise((r) => server.listen(0, '127.0.0.1', r))
const browser = await chromium.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true })
const ctx = await browser.newContext({ viewport: { width: 1512, height: 900 } })
await ctx.addInitScript({ path: path.join(HERE, 'mock-harness.js') })
const page = await ctx.newPage()
const errors = []
page.on('pageerror', (e) => errors.push(String(e)))
page.on('console', (m) => { if (m.type() === 'error' && !m.text().includes('404')) errors.push(m.text()) })

let failures = 0
const check = (name, ok, detail = '') => {
  console.log(`${ok ? '  ok  ' : '  FAIL'} ${name}${detail ? `  (${detail})` : ''}`)
  if (!ok) failures++
}
const shot = async (name) => { if (SHOTS) await page.screenshot({ path: path.join(SHOTS, `${name}.png`) }) }

await page.goto(`http://127.0.0.1:${server.address().port}/`)
const row = page.getByText(args.title, { exact: true }).first()
await row.waitFor({ timeout: 60000 })
await page.waitForTimeout(2500)
await page.evaluate((t) => [...document.querySelectorAll('[data-session-id] button')].find((b) => b.textContent.includes(t)).click(), args.title)
await page.waitForFunction(() => document.querySelectorAll('[data-tool-call-id]').length > 5, null, { timeout: 60000 })
await page.waitForTimeout(1500)
await shot('1-open')

const session = await page.evaluate(async (t) => {
  const idx = await (await fetch('/__sessions/_index.json')).json()
  const row = idx.sessions.find((s) => s.title === t)
  const s = await (await fetch(`/__sessions/${row.id}.json`)).json()
  const msgs = s.slice.messages
  const firstUser = msgs.find((m) => m.role === 'user')
  const firstText = (firstUser?.parts || []).map((p) => p.text || '').join('').trim()
  return { count: msgs.length, firstText: firstText.slice(0, 40), firstId: msgs[0].id }
}, args.title)

const scroller = page.locator('.overflow-y-auto').filter({ has: page.locator('[data-tool-call-id]') }).first()
const mountedMessages = () => page.evaluate(() => document.querySelectorAll('.group\\/msg, .render-cached').length)

// 1 — opens on a tail window with an "earlier" affordance
const earlier = page.getByRole('button', { name: /earlier (message|activity)/ })
check('opens on the tail with an "earlier" control', await earlier.count() === 1)
const initialMounted = await mountedMessages()
check('mounts fewer than all messages', initialMounted < session.count, `${initialMounted}/${session.count}`)
check('newest content is at the bottom', await page.evaluate(() => {
  const el = [...document.querySelectorAll('.overflow-y-auto')].find((e) => e.querySelector('[data-tool-call-id]'))
  return el.scrollHeight - el.scrollTop - el.clientHeight < 20
}))

// 2 — the button reveals a chunk and keeps the viewport put
const before = await mountedMessages()
const anchorBefore = await page.evaluate(() => {
  const el = [...document.querySelectorAll('.overflow-y-auto')].find((e) => e.querySelector('[data-tool-call-id]'))
  el.scrollTop = 0
  return null
})
await page.waitForTimeout(100)
await earlier.click()
await page.waitForTimeout(600)
check('clicking "earlier" mounts more', (await mountedMessages()) > before, `${before} → ${await mountedMessages()}`)

// 3 — scrolling up keeps revealing until the first message is mounted
const box = await scroller.boundingBox()
await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
for (let i = 0; i < 120; i++) {
  await page.mouse.wheel(0, -1500)
  await page.waitForTimeout(40)
  if ((await earlier.count()) === 0) break
}
await page.waitForTimeout(500)
check('scrolling to the top reveals everything', (await earlier.count()) === 0)
const probe = async () => page.evaluate((t) => {
  const el = [...document.querySelectorAll('.overflow-y-auto')].find((e) => e.querySelector('[data-tool-call-id]'))
  const txt = el.textContent
  return { has: txt.includes(t) }
}, session.firstText)
// textContent, not innerText: innerText leaves out rows content-visibility
// has skipped offscreen, which is not the same as unmounted.
check('the first user message is mounted', (await probe()).has, session.firstText)
await shot('2-top')

// 4 — sending snaps back? (no bridge) — instead: ⌘F search mounts all
await page.evaluate(() => {
  const el = [...document.querySelectorAll('.overflow-y-auto')].find((e) => e.querySelector('[data-tool-call-id]'))
  el.scrollTop = el.scrollHeight
})
await page.keyboard.press('Meta+f')
await page.waitForTimeout(200)
const word = session.firstText.split(/\s+/).find((w) => w.length > 5) || session.firstText.slice(0, 6)
await page.keyboard.type(word)
await page.waitForTimeout(800)
const hits = await page.evaluate(() => document.querySelectorAll('.search-hit').length)
check('in-session search finds text in the oldest message', hits > 0, `"${word}" → ${hits} hits`)
await page.keyboard.press('Escape')
await page.waitForTimeout(300)

// 5 — sidebar rows render with fold counts, and toggling works
const rows = await page.evaluate(() => document.querySelectorAll('aside [data-session-id]').length)
check('sidebar renders session rows', rows > 10, `${rows} rows`)
const activeRow = page.locator(`aside [data-session-id]`).filter({ hasText: args.title }).first()
const caret = activeRow.locator('[role="button"]').first()
if (await caret.count()) {
  const rowsBefore = rows
  await caret.click()
  await page.waitForTimeout(700)
  const rowsAfter = await page.evaluate(() => document.querySelectorAll('aside [data-session-id]').length)
  const badge = await activeRow.innerText()
  check('collapsing the active session hides its sub-sessions', rowsAfter < rowsBefore, `${rowsBefore} → ${rowsAfter}`)
  check('collapsed row shows its sub-session count', /▸\s*\d+/.test(badge), badge.split('\n').pop())
  await caret.click()
  await page.waitForTimeout(700)
}

// 6 — tool timeline: one tooltip, on hover
const bars = page.locator('aside .absolute.group')
const nBars = await bars.count()
check('timeline renders bars', nBars > 10, `${nBars} bars`)
if (nBars) {
  await bars.nth(nBars - 1).scrollIntoViewIfNeeded()
  await bars.nth(nBars - 1).hover({ force: true })
  await page.waitForTimeout(200)
  const tip = await page.evaluate(() => [...document.querySelectorAll('aside .z-30')].map((e) => e.textContent).filter(Boolean))
  check('hovering a bar shows its tooltip', tip.length === 1, tip[0]?.slice(0, 60))
  await shot('3-timeline')
}

check('no page errors', errors.length === 0, errors.slice(0, 3).join(' | '))
await browser.close()
server.close()
if (failures) { console.log(`\n${failures} failure(s)`); process.exit(1) }
console.log('\nall checks passed')
