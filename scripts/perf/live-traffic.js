// Runs INSIDE the live Freyja main process (via mainrpc.mjs). For
// __fpTrafficMs, counts every bridge event forwarded to the window (by type
// and by session) and times the session save / index save IPC handlers.
// Every patch is undone before returning.
const WINDOW_MS = Number(globalThis.__fpTrafficMs || 30000)
const wc = electron.webContents.getAllWebContents().find((w) => w.getType() === 'window')
const { ipcMain } = electron
const byType = {}
const bySession = {}
let events = 0
let bytes = 0
const origSend = wc.send
wc.send = function (channel, ...args) {
  if (channel === 'harness:bridge-event') {
    const ev = args[0] || {}
    events += 1
    byType[ev.type] = (byType[ev.type] || 0) + 1
    const sid = ev.sessionId || ev.record?.id || ev.id || '?'
    bySession[sid] = (bySession[sid] || 0) + 1
    if (events % 25 === 0) bytes += JSON.stringify(ev).length * 25
  }
  return origSend.call(this, channel, ...args)
}
const saves = []
const handlers = ipcMain._invokeHandlers
const wrapped = []
for (const ch of ['session:save', 'session:index-save']) {
  const orig = handlers?.get(ch)
  if (!orig) continue
  const wrap = async (e, payload) => {
    const t0 = performance.now()
    const r = await orig(e, payload)
    saves.push({ ch, id: payload?.id || (Array.isArray(payload) ? `${payload.length} rows` : '?'), ms: Math.round(performance.now() - t0), kb: Math.round((r?.bytes || 0) / 1024) })
    return r
  }
  handlers.set(ch, wrap)
  wrapped.push([ch, orig])
}
await new Promise((r) => setTimeout(r, WINDOW_MS))
wc.send = origSend
delete wc.send // fall back to the prototype method again
for (const [ch, orig] of wrapped) handlers.set(ch, orig)
const top = (o, n) => Object.entries(o).sort((a, b) => b[1] - a[1]).slice(0, n)
return {
  seconds: WINDOW_MS / 1000,
  events, perSec: +(events / (WINDOW_MS / 1000)).toFixed(1), approxMB: +(bytes / 1e6).toFixed(2),
  sessions: Object.keys(bySession).length,
  byType: top(byType, 14), bySession: top(bySession, 10), saves,
}
