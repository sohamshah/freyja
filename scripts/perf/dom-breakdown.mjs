// Open a session in the harness and report DOM nodes per region.
//   node dom-breakdown.mjs <dist> "<session title>"
import { chromium } from 'playwright-core'
import http from 'node:http'; import fs from 'node:fs'; import path from 'node:path'; import os from 'node:os'
const DIST = path.resolve(process.argv[2]); const TITLE = process.argv[3]; if (!TITLE) throw new Error('usage: node dom-breakdown.mjs <dist> "<session title>"'); const HERE = path.dirname(new URL(import.meta.url).pathname)
const SESS = path.join(os.homedir(), '.freyja/sessions')
const server = http.createServer((req, res) => { const u = decodeURIComponent(req.url.split('?')[0]); const f = u.startsWith('/__sessions/') ? path.join(SESS, u.slice(12)) : path.join(DIST, u === '/' ? 'index.html' : u); fs.readFile(f, (e, d) => { if (e) { res.writeHead(404); res.end(); return } res.writeHead(200, { 'content-type': f.endsWith('.js') ? 'text/javascript' : f.endsWith('.css') ? 'text/css' : f.endsWith('.html') ? 'text/html' : 'application/octet-stream' }); res.end(d) }) })
await new Promise((r) => server.listen(0, '127.0.0.1', r))
const browser = await chromium.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true })
const ctx = await browser.newContext({ viewport: { width: 1512, height: 900 } })
await ctx.addInitScript({ path: path.join(HERE, 'mock-harness.js') })
const page = await ctx.newPage()
await page.goto(`http://127.0.0.1:${server.address().port}/`)
const row = page.getByText(TITLE, { exact: true }).first()
await row.waitFor({ timeout: 60000 }); await page.waitForTimeout(2500); await row.click()
await page.waitForFunction(() => document.querySelectorAll('[data-tool-call-id]').length > 20, null, { timeout: 60000 })
await page.waitForTimeout(2000)
console.log(JSON.stringify(await page.evaluate(() => {
  const count = (el) => el ? el.getElementsByTagName('*').length : 0
  const out = { total: document.getElementsByTagName('*').length }
  out.aside = [...document.querySelectorAll('aside')].map((a) => count(a))
  out.svgNodes = document.querySelectorAll('svg *').length
  const scroller = [...document.querySelectorAll('.overflow-y-auto')].find((e) => e.querySelector('[data-tool-call-id]'))
  out.conversation = count(scroller)
  out.toolChips = document.querySelectorAll('[data-tool-call-id]').length
  // biggest direct subtrees under body
  const big = []
  const walk = (el, depth) => { for (const c of el.children) { const n = count(c); if (n > 1500 && depth < 6) { big.push([depth, c.tagName + '.' + String(c.className).slice(0, 60), n]); walk(c, depth + 1) } } }
  walk(document.body, 0)
  out.big = big
  const asides = [...document.querySelectorAll('aside')]
  out.sections = asides.map((a) => {
    const sc = a.querySelector('.overflow-y-auto')
    return sc ? [...sc.children].map((c) => [count(c), (c.textContent || '').trim().slice(0, 50)]) : []
  })
  return out
}), null, 1))
await browser.close(); server.close()
