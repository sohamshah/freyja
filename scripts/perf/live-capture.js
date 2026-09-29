// Runs INSIDE the live Freyja main process (via mainrpc.mjs). Attaches the
// window's debugger, records a renderer CPU profile + input/frame timing and
// the main process's own event-loop delay for CAPTURE_MS, and writes the
// results to scripts/perf/out. Read-only: it observes, doesn't drive, and
// removes its probes and detaches when done. Set __fpWaitForEvents (events/s)
// to arm it ahead of a turn: it waits for streaming before recording.
const fs = require('fs')
const path = require('path')
const { monitorEventLoopDelay, performance: mperf } = require('perf_hooks')
const OUT = globalThis.__fpOut || require('os').tmpdir()
const CAPTURE_MS = Number(globalThis.__fpCaptureMs || 25000)
const tag = globalThis.__fpTag || `live-${Date.now()}`
const wc = electron.webContents.getAllWebContents().find((w) => w.getType() === 'window')
const dbg = wc.debugger
if (!dbg.isAttached()) dbg.attach('1.3')
const send = (m, p) => dbg.sendCommand(m, p || {})
const reval = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 800))
  return r.result.value
}

await reval(`(() => {
  const perf = window.__fpPerf = { events: [], loaf: [], frames: [], longtasks: [] }
  new PerformanceObserver((l) => { for (const e of l.getEntries()) perf.events.push({ name: e.name, start: e.startTime, duration: e.duration, delay: e.processingStart - e.startTime, proc: e.processingEnd - e.processingStart }) }).observe({ type: 'event', durationThreshold: 16 })
  new PerformanceObserver((l) => { for (const e of l.getEntries()) perf.loaf.push({ start: e.startTime, duration: e.duration, blocking: e.blockingDuration, styleLayout: e.styleAndLayoutStart ? e.startTime + e.duration - e.styleAndLayoutStart : 0, scripts: (e.scripts || []).map((s) => ({ invoker: s.invoker, fn: s.sourceFunctionName, url: (s.sourceURL||'').split('/').pop(), pos: s.sourceCharPosition, dur: s.duration, fl: s.forcedStyleAndLayoutDuration })) }) }).observe({ type: 'long-animation-frame' })
  new PerformanceObserver((l) => { for (const e of l.getEntries()) perf.longtasks.push({ start: e.startTime, duration: e.duration }) }).observe({ type: 'longtask' })
  let last = 0
  const tick = (t) => { if (last) perf.frames.push(t - last); last = t; if (perf.frames.length > 20000) perf.frames.shift(); requestAnimationFrame(tick) }
  requestAnimationFrame(tick)
})()`)

const eld = monitorEventLoopDelay({ resolution: 5 })
eld.enable()
// Also catch individual main-thread stalls with timestamps.
const stalls = []
let lastTick = mperf.now()
const probe = setInterval(() => { const now = mperf.now(); const gap = now - lastTick - 20; if (gap > 40) stalls.push({ at: Math.round(now), gapMs: Math.round(gap) }); lastTick = now }, 20)

// Count the bridge events the window receives, so busy time can be read
// per event. Waits (up to __fpWaitMs) for __fpWaitForEvents events/s before
// starting, so a capture can be armed ahead of a turn.
let events = 0
const byType = {}
const origSend = wc.send
wc.send = function (channel, ...args) {
  if (channel === 'harness:bridge-event') {
    events += 1
    const t = args[0]?.type || '?'
    byType[t] = (byType[t] || 0) + 1
  }
  return origSend.call(this, channel, ...args)
}
const restoreSend = () => { delete wc.send }
const minRate = Number(globalThis.__fpWaitForEvents || 0)
if (minRate > 0) {
  const deadline = Date.now() + Number(globalThis.__fpWaitMs || 600000)
  let armed = false
  while (Date.now() < deadline) {
    const before = events
    await new Promise((r) => setTimeout(r, 2000))
    if ((events - before) / 2 >= minRate) { armed = true; break }
  }
  if (!armed) {
    restoreSend()
    dbg.detach()
    return { tag, waited: true, armed: false, note: 'no streaming seen before the deadline' }
  }
}
events = 0
for (const k of Object.keys(byType)) delete byType[k]

await send('Performance.enable')
await send('Profiler.enable')
await send('Profiler.setSamplingInterval', { interval: 500 })
await send('Profiler.start')
const m0 = Object.fromEntries((await send('Performance.getMetrics')).metrics.map((x) => [x.name, x.value]))
await new Promise((r) => setTimeout(r, CAPTURE_MS))
const m1 = Object.fromEntries((await send('Performance.getMetrics')).metrics.map((x) => [x.name, x.value]))
const { profile } = await send('Profiler.stop')
const capturedEvents = events
restoreSend()
clearInterval(probe)
eld.disable()
fs.mkdirSync(OUT, { recursive: true })
fs.writeFileSync(path.join(OUT, `${tag}.cpuprofile`), JSON.stringify(profile))
const perf = await reval(`JSON.parse(JSON.stringify({ events: window.__fpPerf.events, loaf: window.__fpPerf.loaf, frames: window.__fpPerf.frames.slice(-3000), longtasks: window.__fpPerf.longtasks, nodes: document.getElementsByTagName('*').length }))`)
fs.writeFileSync(path.join(OUT, `${tag}.perf.json`), JSON.stringify(perf))
// Leave the app as we found it: end the rAF sampler (its next push throws
// before it re-arms), make the observer sinks no-ops, detach.
await reval(`(() => {
  const p = window.__fpPerf
  const sink = { push() {}, shift() {}, length: 0 }
  p.events = sink; p.loaf = sink; p.longtasks = sink
  p.frames = { push() { throw new Error('perf probe stopped') }, shift() {}, length: 0 }
  delete window.__fpPerf
})()`)
await send('Profiler.disable')
await send('Performance.disable')
dbg.detach()
const d = (k) => +(m1[k] - m0[k]).toFixed(3)
return {
  tag,
  events: capturedEvents,
  eventsPerSec: +(capturedEvents / (CAPTURE_MS / 1000)).toFixed(1),
  msScriptPerEvent: capturedEvents ? +((d('ScriptDuration') * 1000) / capturedEvents).toFixed(2) : null,
  eventTypes: Object.entries(byType).sort((a, b) => b[1] - a[1]).slice(0, 6),
  rendererSecondsBusy: { task: d('TaskDuration'), script: d('ScriptDuration'), layout: d('LayoutDuration'), style: d('RecalcStyleDuration') },
  layouts: d('LayoutCount'), recalcs: d('RecalcStyleCount'),
  heapMB: Math.round(m1.JSHeapUsedSize / 1e6), nodes: perf.nodes,
  mainLoopDelay: { p50: +(eld.percentile(50) / 1e6).toFixed(1), p99: +(eld.percentile(99) / 1e6).toFixed(1), max: +(eld.max / 1e6).toFixed(1) },
  mainStalls: stalls.slice(-20),
  keyEvents: perf.events.filter((e) => e.name.startsWith('key') || e.name === 'input' || e.name === 'beforeinput').length,
  loafCount: perf.loaf.length, longtasks: perf.longtasks.length,
}
