// Runs INSIDE the live Freyja main process (via mainrpc.mjs). Records a
// Chrome trace of the window's renderer for __fpTraceMs (default 4 s) and
// returns main-thread time by trace event name, plus frame counts. Detaches
// when done.
const TRACE_MS = Number(globalThis.__fpTraceMs || 4000)
const wc = electron.webContents.getAllWebContents().find((w) => w.getType() === 'window')
const dbg = wc.debugger
if (!dbg.isAttached()) dbg.attach('1.3')
const send = (m, p) => dbg.sendCommand(m, p || {})
const events = []
let done
const finished = new Promise((r) => (done = r))
const onMessage = (_e, method, params) => {
  if (method === 'Tracing.dataCollected') for (const e of params.value) events.push(e)
  if (method === 'Tracing.tracingComplete') done()
}
dbg.on('message', onMessage)
await send('Tracing.start', {
  transferMode: 'ReportEvents',
  traceConfig: {
    includedCategories: ['devtools.timeline', 'disabled-by-default-devtools.timeline', 'disabled-by-default-devtools.timeline.frame', 'v8'],
    excludedCategories: ['*'],
  },
})
await new Promise((r) => setTimeout(r, TRACE_MS))
await send('Tracing.end')
await finished
dbg.removeListener('message', onMessage)
dbg.detach()

// The window's renderer main thread: the CrRendererMain with the most events.
const counts = new Map()
for (const e of events) counts.set(`${e.pid}:${e.tid}`, (counts.get(`${e.pid}:${e.tid}`) || 0) + 1)
const main = events
  .filter((e) => e.name === 'thread_name' && e.args?.name === 'CrRendererMain')
  .sort((a, b) => (counts.get(`${b.pid}:${b.tid}`) || 0) - (counts.get(`${a.pid}:${a.tid}`) || 0))[0]
const agg = {}
const n = {}
for (const e of events) {
  if (!main || e.pid !== main.pid || e.tid !== main.tid || e.ph !== 'X' || !e.dur) continue
  agg[e.name] = (agg[e.name] || 0) + e.dur / 1000
  n[e.name] = (n[e.name] || 0) + 1
}
const seconds = TRACE_MS / 1000
return {
  seconds,
  // Nested events count against their parents too: read names, not a total.
  mainThreadMsByEvent: Object.entries(agg)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 16)
    .map(([k, v]) => `${Math.round(v)}ms x${n[k]} ${k}`),
}
