// Composer auto-grow, contained side panels, and portaled overlays.
//   node check-composer.mjs --dist <built renderer> --title "<session title>"
import http from 'node:http'; import fs from 'node:fs'; import path from 'node:path'; import os from 'node:os'
import { chromium } from 'playwright-core'
const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, arr) => { if (a.startsWith('--')) acc.push([a.slice(2), arr[i + 1]]); return acc }, []))
const HERE = path.dirname(new URL(import.meta.url).pathname); const SESS = path.join(os.homedir(), '.freyja/sessions')
const server = http.createServer((req, res) => { const u = decodeURIComponent(req.url.split('?')[0]); const f = u.startsWith('/__sessions/') ? path.join(SESS, u.slice(12)) : path.join(path.resolve(args.dist), u === '/' ? 'index.html' : u); fs.readFile(f, (e, d) => { if (e) { res.writeHead(404); res.end(); return } res.writeHead(200, { 'content-type': f.endsWith('.js') ? 'text/javascript' : f.endsWith('.css') ? 'text/css' : 'text/html' }); res.end(d) }) })
await new Promise((r) => server.listen(0, '127.0.0.1', r))
const browser = await chromium.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true })
const ctx = await browser.newContext({ viewport: { width: 1512, height: 900 } }); await ctx.addInitScript({ path: path.join(HERE, 'mock-harness.js') })
const page = await ctx.newPage(); const errors = []; page.on('pageerror', (e) => errors.push(String(e)))
let failures = 0; const check = (n, ok, d = '') => { console.log(`${ok ? '  ok  ' : '  FAIL'} ${n}${d ? `  (${d})` : ''}`); if (!ok) failures++ }
await page.goto(`http://127.0.0.1:${server.address().port}/`)
await page.getByText(args.title, { exact: true }).first().waitFor({ timeout: 60000 }); await page.waitForTimeout(12000); await page.mouse.move(700, 400)
await page.evaluate((t) => [...document.querySelectorAll('[data-session-id] button')].find((b) => b.textContent.includes(t)).click(), args.title)
await page.waitForFunction(() => document.querySelectorAll('[data-tool-call-id]').length > 5, null, { timeout: 60000 }); await page.waitForTimeout(1000)

// Composer: after each change, the height must equal what the old
// collapse-and-measure method gives (the ground truth), within 1px.
const ta = page.locator('textarea:not([aria-hidden="true"])').last()
await ta.click()
const truth = () => page.evaluate(() => {
  const el = [...document.querySelectorAll('textarea')].filter((t) => t.getAttribute('aria-hidden') !== 'true').pop()
  const shown = el.getBoundingClientRect().height
  const prev = el.style.height; el.style.height = '0px'; const sh = el.scrollHeight; el.style.height = prev
  return { shown: Math.round(shown), expected: Math.min(260, sh), overflow: el.style.overflowY }
})
const steps = [['empty', ''], ['one line', 'hello there'], ['wrapped', 'word '.repeat(60)], ['three lines', 'a\nb\nc'], ['past the cap', 'line\n'.repeat(30)], ['back to one line', 'short']]
for (const [label, text] of steps) {
  await ta.fill(text); await page.waitForTimeout(80)
  const t = await truth()
  check(`composer height: ${label}`, Math.abs(t.shown - t.expected) <= 1, `${t.shown}px vs ${t.expected}px, overflow ${t.overflow}`)
}
check('composer scrolls only past the cap', (await ta.evaluate((el) => el.style.overflowY)) === 'hidden')
check('one hidden measuring copy', (await page.evaluate(() => document.querySelectorAll('textarea[aria-hidden="true"]').length)) === 1)
await ta.fill('')

// Side panels are contained and keep their size.
const panels = await page.evaluate(() => [...document.querySelectorAll('aside')].slice(0, 2).map((a) => ({ contain: getComputedStyle(a).contain, h: Math.round(a.getBoundingClientRect().height), top: Math.round(a.getBoundingClientRect().top) })))
check('both side panels contained', panels.every((p) => p.contain === 'strict' || (p.contain.includes('size') && p.contain.includes('layout'))), JSON.stringify(panels))
check('side panels fill the window height', panels.every((p) => p.h > 700), panels.map((p) => p.h).join(', '))
// Sidebar still scrolls and a row menu opens inside it.
const list = page.locator('aside .overflow-y-auto').first()
const before = await list.evaluate((el) => el.scrollTop); await list.hover(); await page.mouse.wheel(0, 600); await page.waitForTimeout(300)
check('sidebar list scrolls', (await list.evaluate((el) => el.scrollTop)) > before)
const row = page.locator('aside [data-session-id]').nth(3); await row.click({ button: 'right' }); await page.waitForTimeout(200)
check('row context menu opens', await page.getByText('Open split').first().isVisible())
await page.keyboard.press('Escape'); await page.mouse.click(700, 300)

// The log modal (rendered by the activity panel) must cover the window.
const expand = page.locator('aside').nth(1).getByRole('button', { name: 'expand' })
await expand.scrollIntoViewIfNeeded(); await expand.click(); await page.waitForTimeout(300)
const modal = await page.evaluate(() => { const m = [...document.querySelectorAll('.fixed.inset-0')].pop(); if (!m) return null; const r = m.getBoundingClientRect(); return { w: Math.round(r.width), h: Math.round(r.height), parentIsBody: m.parentElement === document.body } })
check('log modal covers the window', !!modal && modal.w === 1512 && modal.h === 900, JSON.stringify(modal))
check('log modal is portaled to <body>', !!modal?.parentIsBody)
check('no page errors', errors.length === 0, errors.slice(0, 2).join(' | '))
await browser.close(); server.close()
if (failures) { console.log(`\n${failures} failure(s)`); process.exit(1) }
console.log('\nall checks passed')
