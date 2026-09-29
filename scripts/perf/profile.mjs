// Profile the Freyja renderer against a real long session. See README.md.
//   node profile.mjs --dist <built renderer dir> --title "<session title>"
//                    [--scenarios open,type,scroll,click,stream,streamtype]
//                    [--out results.json] [--cpuprofile prefix] [--trace 1]
//                    [--replay out/replay.json] [--speed 1]
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
const DIST = path.resolve(args.dist)
const HERE = path.dirname(new URL(import.meta.url).pathname)
const SESS = path.join(os.homedir(), '.freyja/sessions')
if (!args.dist || !args.title) throw new Error('usage: node profile.mjs --dist <built renderer> --title "<session title>" [...]')
const SESSION_TITLE = args.title
const SCENARIOS = (args.scenarios || 'open,type,scroll,click,stream,streamtype').split(',')
const REPLAY = args.replay ? path.resolve(args.replay) : path.join(HERE, 'out/replay.json')
const SPEED = Number(args.speed || 1)

const MIME = { '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.json': 'application/json', '.woff2': 'font/woff2', '.svg': 'image/svg+xml', '.png': 'image/png', '.mp4': 'video/mp4' }
const server = http.createServer((req, res) => {
  const url = decodeURIComponent(req.url.split('?')[0])
  let file
  if (url.startsWith('/__sessions/')) file = path.join(SESS, url.slice('/__sessions/'.length))
  else if (url.startsWith('/__replay')) file = REPLAY
  else file = path.join(DIST, url === '/' ? 'index.html' : url)
  fs.readFile(file, (err, data) => {
    if (err) { res.writeHead(404); res.end(); return }
    res.writeHead(200, { 'content-type': MIME[path.extname(file)] || 'application/octet-stream' })
    res.end(data)
  })
})
await new Promise((r) => server.listen(0, '127.0.0.1', r))
const PORT = server.address().port

const browser = await chromium.launch({
  executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  headless: args.headful ? false : true,
  args: ['--enable-precise-memory-info', '--js-flags=--expose-gc'],
})
// Default matches a 14" MacBook; --viewport 1752x1170 --dpr 1.67 matches a larger window.
const [VW, VH] = String(args.viewport || '1512x900').split('x').map(Number)
const context = await browser.newContext({ viewport: { width: VW, height: VH }, deviceScaleFactor: Number(args.dpr || 2) })
await context.addInitScript({ path: path.join(HERE, 'mock-harness.js') })
// Instrumentation: Event Timing (input latency), long animation frames
// (script attribution), long tasks, and a rAF frame-gap recorder.
await context.addInitScript(() => {
  const perf = (window.__perf = { events: [], loaf: [], longtasks: [], frames: [], recording: false })
  new PerformanceObserver((list) => {
    for (const e of list.getEntries()) {
      if (!perf.recording) continue
      perf.events.push({ name: e.name, start: e.startTime, duration: e.duration, inputDelay: e.processingStart - e.startTime, processing: e.processingEnd - e.processingStart })
    }
  }).observe({ type: 'event', buffered: false, durationThreshold: 16 })
  try {
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        if (!perf.recording) continue
        perf.loaf.push({
          start: e.startTime, duration: e.duration, blocking: e.blockingDuration,
          render: e.renderStart ? e.startTime + e.duration - e.renderStart : 0,
          styleLayout: e.styleAndLayoutStart ? e.startTime + e.duration - e.styleAndLayoutStart : 0,
          scripts: (e.scripts || []).map((s) => ({ invoker: s.invoker, fn: s.sourceFunctionName, dur: s.duration, fl: s.forcedStyleAndLayoutDuration })),
        })
      }
    }).observe({ type: 'long-animation-frame', buffered: false })
  } catch {}
  new PerformanceObserver((list) => {
    for (const e of list.getEntries()) if (perf.recording) perf.longtasks.push({ start: e.startTime, duration: e.duration })
  }).observe({ type: 'longtask', buffered: false })
  let last = 0
  const tick = (t) => {
    if (perf.recording && last) perf.frames.push(t - last)
    last = t
    requestAnimationFrame(tick)
  }
  requestAnimationFrame(tick)
  window.__startRec = () => { perf.events = []; perf.loaf = []; perf.longtasks = []; perf.frames = []; perf.recording = true }
  window.__stopRec = () => { perf.recording = false; return JSON.parse(JSON.stringify(perf)) }
})

const page = await context.newPage()
const consoleErrors = []
page.on('console', (m) => { if (m.type() === 'error') consoleErrors.push(m.text().slice(0, 300)) })
page.on('pageerror', (e) => consoleErrors.push('pageerror: ' + String(e).slice(0, 300)))
const cdp = await context.newCDPSession(page)
await cdp.send('Performance.enable')

function pct(arr, p) {
  if (!arr.length) return 0
  const s = [...arr].sort((a, b) => a - b)
  return s[Math.min(s.length - 1, Math.floor((p / 100) * s.length))]
}
function summarize(rec) {
  const keyEvents = rec.events.filter((e) => ['keydown', 'keypress', 'keyup', 'input', 'beforeinput'].includes(e.name))
  const ptrEvents = rec.events.filter((e) => ['pointerdown', 'pointerup', 'click', 'mousedown', 'mouseup'].includes(e.name))
  const frames = rec.frames
  return {
    frames: frames.length,
    frameP50: +pct(frames, 50).toFixed(1),
    frameP95: +pct(frames, 95).toFixed(1),
    frameMax: +Math.max(0, ...frames).toFixed(1),
    jank50: frames.filter((f) => f > 50).length,
    jank100: frames.filter((f) => f > 100).length,
    longTasks: rec.longtasks.length,
    longTaskMs: Math.round(rec.longtasks.reduce((a, b) => a + b.duration, 0)),
    longTaskMax: Math.round(Math.max(0, ...rec.longtasks.map((t) => t.duration))),
    keyEventsOver16: keyEvents.length,
    keyP50: pct(keyEvents.map((e) => e.duration), 50),
    keyP95: pct(keyEvents.map((e) => e.duration), 95),
    keyMax: Math.max(0, ...keyEvents.map((e) => e.duration)),
    ptrMax: Math.max(0, ...ptrEvents.map((e) => e.duration)),
    keyInputDelayAvg: +(keyEvents.reduce((a, e) => a + e.inputDelay, 0) / (keyEvents.length || 1)).toFixed(1),
    keyProcessingAvg: +(keyEvents.reduce((a, e) => a + e.processing, 0) / (keyEvents.length || 1)).toFixed(1),
    keyProcessingMax: +Math.max(0, ...keyEvents.map((e) => e.processing)).toFixed(1),
    forcedLayoutInInputMs: Math.round(rec.loaf.reduce((a, f) => a + f.scripts.filter((x) => /oninput|onkeydown|onkeypress/.test(x.invoker || '')).reduce((b, x) => b + (x.fl || 0), 0), 0)),
  }
}
function topScripts(rec, n = 12) {
  const agg = new Map()
  for (const f of rec.loaf) for (const s of f.scripts) {
    const k = `${s.invoker} :: ${s.fn || '?'}`
    const v = agg.get(k) || { dur: 0, n: 0, fl: 0 }
    v.dur += s.dur; v.n += 1; v.fl += s.fl || 0
    agg.set(k, v)
  }
  return [...agg.entries()].sort((a, b) => b[1].dur - a[1].dur).slice(0, n).map(([k, v]) => `${Math.round(v.dur)}ms x${v.n} (forced layout ${Math.round(v.fl)}ms)  ${k}`)
}
async function metrics() {
  const m = await cdp.send('Performance.getMetrics')
  const g = Object.fromEntries(m.metrics.map((x) => [x.name, x.value]))
  const dom = await page.evaluate(() => ({ nodes: document.getElementsByTagName('*').length, imgs: document.images.length }))
  return { heapMB: Math.round(g.JSHeapUsedSize / 1e6), nodes: dom.nodes, imgs: dom.imgs, listeners: g.JSEventListeners, layoutCount: g.LayoutCount, recalcStyleCount: g.RecalcStyleCount }
}
async function rawMetrics() {
  const m = await cdp.send('Performance.getMetrics')
  return Object.fromEntries(m.metrics.map((x) => [x.name, x.value]))
}
let profN = 0
async function withProfile(name, fn) {
  const m0 = await rawMetrics()
  const r = await withProfileInner(name, fn)
  const m1 = await rawMetrics()
  const d = (k) => +(m1[k] - m0[k]).toFixed(3)
  busy[name] = { script: d('ScriptDuration'), layout: d('LayoutDuration'), style: d('RecalcStyleDuration'), task: d('TaskDuration'), layouts: d('LayoutCount'), recalcs: d('RecalcStyleCount') }
  return r
}
const busy = {}
async function traced(name, fn) {
  if (!args.trace) return fn()
  const events = []
  const onData = (d) => { for (const e of d.value) events.push(e) }
  cdp.on('Tracing.dataCollected', onData)
  await cdp.send('Tracing.start', { transferMode: 'ReportEvents', traceConfig: { includedCategories: ['devtools.timeline', 'disabled-by-default-devtools.timeline', 'v8', 'blink.user_timing'], excludedCategories: ['*'] } })
  const r = await fn()
  const done = new Promise((res) => cdp.once('Tracing.tracingComplete', res))
  await cdp.send('Tracing.end')
  await done
  cdp.off('Tracing.dataCollected', onData)
  // Renderer main thread: sum complete-event durations per name (nested
  // events double count against their parents; read names, not totals).
  // The page's renderer main thread: the CrRendererMain with the most events.
  const counts = new Map()
  const mains = events.filter((e) => e.name === 'thread_name' && e.args?.name === 'CrRendererMain')
  for (const e of events) { const k = e.pid + ':' + e.tid; counts.set(k, (counts.get(k) || 0) + 1) }
  const main = mains.sort((a, b) => (counts.get(b.pid + ':' + b.tid) || 0) - (counts.get(a.pid + ':' + a.tid) || 0))[0]
  const agg = {}
  for (const e of events) {
    if (!main || e.pid !== main.pid || e.tid !== main.tid || e.ph !== 'X' || !e.dur) continue
    agg[e.name] = (agg[e.name] || 0) + e.dur / 1000
  }
  const top = Object.entries(agg).sort((a, b) => b[1] - a[1]).slice(0, 18).map(([k, v]) => `${Math.round(v)}ms ${k}`)
  if (args.dumpTrace) {
    // For each HitTest on the main thread, name the enclosing events.
    const mine = events.filter((e) => main && e.pid === main.pid && e.tid === main.tid && e.ph === 'X' && e.dur).sort((a, b) => a.ts - b.ts)
    const parents = {}
    for (const h of mine.filter((e) => e.name === 'HitTest')) {
      const encl = mine.filter((e) => e !== h && e.ts <= h.ts && e.ts + e.dur >= h.ts + h.dur && e.name !== 'RunTask').map((e) => e.name)
      const k = encl.slice(-3).join(' > ')
      parents[k] = (parents[k] || 0) + h.dur / 1000
    }
    console.log('hittest parents', JSON.stringify(Object.entries(parents).sort((a, b) => b[1] - a[1]).slice(0, 8)))
    console.log('hittest sample args', JSON.stringify(mine.filter((e) => e.name === 'HitTest').slice(0, 3).map((e) => e.args)))
  }
  traces[name] = top
  console.log('trace', name, top.join(' | '))
  return r
}
const traces = {}
async function withProfileInner(name, fn) {
  if (args.trace) return traced(name, fn)
  if (!args.cpuprofile) return fn()
  await cdp.send('Profiler.enable')
  await cdp.send('Profiler.setSamplingInterval', { interval: 200 })
  await cdp.send('Profiler.start')
  const r = await fn()
  const { profile } = await cdp.send('Profiler.stop')
  const f = `${args.cpuprofile}-${++profN}-${name}.cpuprofile`
  fs.writeFileSync(f, JSON.stringify(profile))
  return r
}

const results = { dist: DIST, scenarios: {} }
await page.goto(`http://127.0.0.1:${PORT}/`)
// Splash + hydrate. Wait for the sidebar row to exist.
const row = page.getByText(SESSION_TITLE, { exact: true }).first()
await row.waitFor({ timeout: 60000 })
await page.waitForTimeout(3000)
results.baseline = await metrics()

// ── open: click the session in the sidebar, wait until the transcript mounts
if (SCENARIOS.includes('open')) {
  await page.evaluate(() => window.__startRec())
  const t0 = Date.now()
  await withProfile('open', async () => {
    await page.evaluate((title) => {
      window.__openClickAt = performance.now()
      const el = [...document.querySelectorAll('[data-session-id] button')].find((b) => b.textContent.includes(title))
      el.click()
    }, SESSION_TITLE)
    await page.waitForFunction(() => document.querySelectorAll('[data-tool-call-id]').length > 50, null, { timeout: 60000, polling: 50 })
    await page.evaluate(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r))))
  })
  const openMs = Date.now() - t0
  const openTiming = await page.evaluate(() => ({ clickAt: Math.round(window.__openClickAt), loads: window.__harnessStats.loads }))
  console.log('open timing', JSON.stringify(openTiming))
  await page.waitForTimeout(1500)
  const rec = await page.evaluate(() => window.__stopRec())
  results.scenarios.open = { openMs, ...summarize(rec), after: await metrics(), top: topScripts(rec) }
  console.log('open', JSON.stringify(results.scenarios.open, null, 1))
} else {
  await row.click()
  await page.waitForFunction(() => document.querySelectorAll('[data-tool-call-id]').length > 50, null, { timeout: 60000 })
  await page.waitForTimeout(1500)
}

const composer = page.locator('textarea:not([aria-hidden="true"])').last()
const TEXT = 'the quick brown fox jumps over the lazy dog while the swarm keeps streaming '

async function typeScenario(name) {
  await composer.click()
  await page.evaluate(() => window.__startRec())
  await withProfile(name, async () => {
    await page.keyboard.type(TEXT, { delay: 35 })
  })
  await page.waitForTimeout(300)
  const rec = await page.evaluate(() => window.__stopRec())
  // clear the draft
  await page.keyboard.press('Meta+A'); await page.keyboard.press('Backspace')
  return { ...summarize(rec), top: topScripts(rec) }
}

if (SCENARIOS.includes('type')) {
  results.scenarios.type = await typeScenario('type')
  console.log('type', JSON.stringify(results.scenarios.type, null, 1))
}

if (SCENARIOS.includes('scroll')) {
  const box = await page.locator('.overflow-y-auto').filter({ has: page.locator('[data-tool-call-id]') }).first().boundingBox()
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.evaluate(() => window.__startRec())
  await withProfile('scroll', async () => {
    for (let i = 0; i < 60; i++) { await page.mouse.wheel(0, -400); await page.waitForTimeout(16) }
    for (let i = 0; i < 60; i++) { await page.mouse.wheel(0, 400); await page.waitForTimeout(16) }
  })
  await page.waitForTimeout(300)
  const rec = await page.evaluate(() => window.__stopRec())
  results.scenarios.scroll = { ...summarize(rec), top: topScripts(rec) }
  console.log('scroll', JSON.stringify(results.scenarios.scroll, null, 1))
}

if (SCENARIOS.includes('click')) {
  // Expand then collapse a handful of tool chips near the bottom.
  const chips = page.locator('[data-tool-call-id] button').filter({ hasNotText: /copy/i })
  const n = await chips.count()
  await page.evaluate(() => window.__startRec())
  await withProfile('click', async () => {
    for (let i = Math.max(0, n - 6); i < n; i++) {
      const c = chips.nth(i)
      if (!(await c.isVisible())) continue
      await c.click({ timeout: 2000 }).catch(() => {})
      await page.waitForTimeout(150)
      await c.click({ timeout: 2000 }).catch(() => {})
      await page.waitForTimeout(150)
    }
  })
  const rec = await page.evaluate(() => window.__stopRec())
  results.scenarios.click = { chips: n, ...summarize(rec), top: topScripts(rec) }
  console.log('click', JSON.stringify(results.scenarios.click, null, 1))
}

async function replay(durationCapMs) {
  // Timed replay of real bridge events through the preload listener.
  return page.evaluate(async ({ speed, cap }) => {
    const r = await fetch('/__replay')
    const rows = await r.json()
    const t0 = performance.now()
    let i = 0
    await new Promise((resolve) => {
      const pump = () => {
        const now = (performance.now() - t0) * speed
        const batch = []
        while (i < rows.length && rows[i][0] <= now) batch.push(rows[i++][1])
        if (batch.length) for (const ev of batch) window.__emit(ev)
        if (i >= rows.length || performance.now() - t0 > cap) resolve()
        else setTimeout(pump, 4)
      }
      pump()
    })
    return { emitted: i, total: rows.length, ms: Math.round(performance.now() - t0) }
  }, { speed: SPEED, cap: durationCapMs })
}

if (SCENARIOS.includes('stream')) {
  await page.evaluate(() => { window.__harnessStats.sessionSave.length = 0; window.__harnessStats.sessionIndexSave.length = 0 })
  await page.evaluate(() => window.__startRec())
  const rep = await withProfile('stream', () => replay(40000))
  await page.waitForTimeout(1000)
  const rec = await page.evaluate(() => window.__stopRec())
  const saves = await page.evaluate(() => window.__harnessStats)
  results.scenarios.stream = {
    ...rep, ...summarize(rec), after: await metrics(),
    saves: saves.sessionSave.length, saveCloneMs: Math.round(saves.sessionSave.reduce((a, b) => a + b.cloneMs, 0)),
    indexSaves: saves.sessionIndexSave.length, indexCloneMs: Math.round(saves.sessionIndexSave.reduce((a, b) => a + b.cloneMs, 0)),
    top: topScripts(rec),
  }
  console.log('stream', JSON.stringify(results.scenarios.stream, null, 1))
}

if (SCENARIOS.includes('streamtype')) {
  // Type while the swarm streams — the case the operator actually feels.
  await composer.click()
  await page.evaluate(() => window.__startRec())
  const typing = (async () => { await page.waitForTimeout(500); await page.keyboard.type(TEXT + TEXT, { delay: 35 }) })()
  const rep = await withProfile('streamtype', async () => { const r = await replay(12000); await typing; return r })
  await page.waitForTimeout(300)
  const rec = await page.evaluate(() => window.__stopRec())
  await page.keyboard.press('Meta+A'); await page.keyboard.press('Backspace')
  results.scenarios.streamtype = { ...rep, ...summarize(rec), top: topScripts(rec) }
  console.log('streamtype', JSON.stringify(results.scenarios.streamtype, null, 1))
}

for (const [k, v] of Object.entries(busy)) if (results.scenarios[k]) results.scenarios[k].busy = v
console.log('busy', JSON.stringify(busy))
results.subscriptions = await page.evaluate(() => {
  const root = document.getElementById('root')
  const key = Object.keys(root).find((k) => k.startsWith('__reactContainer'))
  let total = 0, fibers = 0
  const stack = [root[key]]
  while (stack.length) {
    const f = stack.pop(); if (!f) continue; fibers++
    if (typeof f.type === 'function') { let h = f.memoizedState; while (h) { if (h.queue && typeof h.queue === 'object' && 'getSnapshot' in h.queue) total++; h = h.next } }
    if (f.sibling) stack.push(f.sibling); if (f.child) stack.push(f.child)
  }
  return { total, fibers }
})
console.log('subscriptions', results.subscriptions)
results.final = await metrics()
results.consoleErrors = consoleErrors.slice(0, 20)
if (args.out) fs.writeFileSync(args.out, JSON.stringify(results, null, 1))
console.log('final', results.final, 'errors', consoleErrors.length)
if (consoleErrors.length) console.log(consoleErrors.slice(0, 5))
await browser.close()
server.close()
