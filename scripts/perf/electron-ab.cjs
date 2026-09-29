// Electron main script: load a built renderer (with the mock bridge) in THIS
// Electron, open a session, and measure what the harness's Chrome can't —
// layout per keystroke, the per-frame commit wait, and key-to-paint latency
// on the app's own Chromium. Prints one JSON line and quits.
//
//   <electron binary> electron-ab.cjs <dist dir> "<session title>"
const { app, BrowserWindow } = require('electron')
const http = require('http')
const fs = require('fs')
const path = require('path')
const os = require('os')

const [DIST, TITLE] = process.argv.slice(-2)
const SESS = path.join(os.homedir(), '.freyja/sessions')
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

app.whenReady().then(async () => {
  const server = http.createServer((req, res) => {
    const u = decodeURIComponent(req.url.split('?')[0])
    const f = u.startsWith('/__sessions/') ? path.join(SESS, u.slice(12)) : path.join(path.resolve(DIST), u === '/' ? 'index.html' : u)
    fs.readFile(f, (e, d) => {
      if (e) { res.writeHead(404); res.end(); return }
      const ext = path.extname(f)
      res.writeHead(200, { 'content-type': { '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.json': 'application/json' }[ext] || 'application/octet-stream' })
      res.end(d)
    })
  })
  await new Promise((r) => server.listen(0, '127.0.0.1', r))
  const preload = path.join(os.tmpdir(), `fp-preload-${process.pid}.cjs`)
  fs.writeFileSync(preload, `eval(${JSON.stringify(fs.readFileSync(path.join(__dirname, 'mock-harness.js'), 'utf8'))})`)
  // FP_WINDOW=app mirrors the app's own window: transparent + vibrancy.
  const appWindow = process.env.FP_WINDOW === 'app'
    ? { titleBarStyle: 'hiddenInset', backgroundColor: '#00000000', transparent: true, vibrancy: process.env.FP_VIBRANCY || 'fullscreen-ui', visualEffectState: 'active', roundedCorners: true, hasShadow: true }
    : {}
  const win = new BrowserWindow({ width: 1752, height: 1170, show: true, ...appWindow, webPreferences: { preload, contextIsolation: false, sandbox: false } })
  const wc = win.webContents
  const js = (code) => wc.executeJavaScript(code, true)
  const out = { electron: process.versions.electron, window: process.env.FP_WINDOW === 'app' ? 'transparent + vibrancy' : 'plain' }
  try {
    await win.loadURL(`http://127.0.0.1:${server.address().port}/`)
    for (let i = 0; i < 120; i++) {
      if (await js(`!![...document.querySelectorAll('[data-session-id] button')].find((b) => b.textContent.includes(${JSON.stringify(TITLE)}))`)) break
      await sleep(500)
    }
    await sleep(13000) // boot splash
    wc.sendInputEvent({ type: 'mouseMove', x: 900, y: 500 })
    await js(`[...document.querySelectorAll('[data-session-id] button')].find((b) => b.textContent.includes(${JSON.stringify(TITLE)})).click()`)
    for (let i = 0; i < 60; i++) {
      if (await js(`document.querySelectorAll('[data-tool-call-id]').length > 5`)) break
      await sleep(500)
    }
    await sleep(3000)

    // 1 — forced layout per composer change / per streamed token.
    Object.assign(out, await js(`(() => {
      const ta = [...document.querySelectorAll('textarea')].filter((t) => t.getAttribute('aria-hidden') !== 'true').pop()
      const conv = [...document.querySelectorAll('.overflow-y-auto')].find((e) => e.querySelector('[data-tool-call-id]'))
      const saved = ta.value
      let flip = 0
      const key = () => { void document.body.offsetHeight; const t0 = performance.now(); ta.value = saved + (flip++ % 2 ? 'x' : 'y'); void document.body.offsetHeight; return performance.now() - t0 }
      const vis = [...conv.querySelectorAll('.md')].filter((m) => { const r = m.getBoundingClientRect(); return r.bottom > 0 && r.top < innerHeight && r.height > 0 }).pop()
      const token = () => { const n = document.createTextNode(''); vis.appendChild(n); void document.body.offsetHeight; const t0 = performance.now(); n.data = ' token'; void document.body.offsetHeight; const t = performance.now() - t0; n.remove(); return t }
      const med = (f) => { const a = Array.from({ length: 9 }, f).sort((x, y) => x - y); return +a[4].toFixed(1) }
      try { return { layoutPerKeystrokeMs: med(key), layoutPerTokenMs: vis ? med(token) : null, domNodes: document.getElementsByTagName('*').length } }
      finally { ta.value = saved; void document.body.offsetHeight }
    })()`))

    // FP_BLUR=1: leave the window visible but give focus to another one,
    // the state the app is in while the operator works elsewhere.
    if (process.env.FP_BLUR === '1') {
      const other = new BrowserWindow({ width: 300, height: 200, x: 20, y: 20, show: true })
      other.focus()
      await sleep(1500)
      out.focused = win.isFocused()
    }

    // 2 — cost of one repaint per frame: a 4px pulse for 4 s, traced.
    const dbg = wc.debugger
    dbg.attach('1.3')
    const events = []
    let done
    const fin = new Promise((r) => (done = r))
    dbg.on('message', (_e, m, p) => { if (m === 'Tracing.dataCollected') events.push(...p.value); if (m === 'Tracing.tracingComplete') done() })
    await dbg.sendCommand('Performance.enable')
    const metrics = async () => Object.fromEntries((await dbg.sendCommand('Performance.getMetrics')).metrics.map((x) => [x.name, x.value]))
    await js(`(() => { const s = document.createElement('style'); s.id = 'fp-ab'; s.textContent = '@keyframes fp-bg { 0%,100% { background-color: rgba(255,0,0,0.01) } 50% { background-color: rgba(255,0,0,0.02) } } #fp-dot { position: fixed; top: 0; left: 0; width: 4px; height: 4px; animation: fp-bg 2.4s infinite; pointer-events: none }'; document.head.appendChild(s); const d = document.createElement('div'); d.id = 'fp-dot'; document.body.appendChild(d); return 1 })()`)
    await sleep(500)
    await dbg.sendCommand('Tracing.start', { transferMode: 'ReportEvents', traceConfig: { includedCategories: ['devtools.timeline', 'disabled-by-default-devtools.timeline'], excludedCategories: ['*'] } })
    const m0 = await metrics()
    await sleep(4000)
    const m1 = await metrics()
    await dbg.sendCommand('Tracing.end')
    await fin
    await js(`document.getElementById('fp-dot')?.remove(); document.getElementById('fp-ab')?.remove(); 1`)
    const tn = {}
    for (const e of events) if (e.name === 'thread_name') tn[e.pid + ':' + e.tid] = e.args.name
    const commits = events.filter((e) => e.name === 'Commit' && e.ph === 'X' && tn[e.pid + ':' + e.tid] === 'CrRendererMain').map((e) => e.dur / 1000)
    out.idleRepaint = {
      busyPct: Math.round(((m1.TaskDuration - m0.TaskDuration) / 4) * 100),
      frames: commits.length,
      commitMsAvg: commits.length ? +(commits.reduce((a, b) => a + b, 0) / commits.length).toFixed(2) : 0,
    }

    // 3 — key-to-paint while typing (letters/spaces, ~14 keys/s, 8 s).
    await js(`(() => { window.__fpKeys = []; const o = new PerformanceObserver((l) => { for (const e of l.getEntries()) if (e.name === 'keydown') window.__fpKeys.push(e.duration) }); o.observe({ type: 'event', durationThreshold: 16 }); window.__fpObs = o; const ta = [...document.querySelectorAll('textarea')].filter((t) => t.getAttribute('aria-hidden') !== 'true').pop(); window.__fpSaved = ta.value; ta.focus(); return 1 })()`)
    const text = 'the quick brown fox jumps over the lazy dog '
    const t0 = Date.now()
    let n = 0
    while (Date.now() - t0 < 8000) {
      const ch = text[n++ % text.length]
      const keyCode = ch === ' ' ? 'Space' : ch
      wc.sendInputEvent({ type: 'keyDown', keyCode })
      wc.sendInputEvent({ type: 'char', keyCode: ch })
      wc.sendInputEvent({ type: 'keyUp', keyCode })
      await sleep(70)
    }
    await sleep(500)
    const keys = await js(`(() => { window.__fpObs.disconnect(); return window.__fpKeys.sort((a, b) => a - b) })()`)
    out.typing = {
      keys: n,
      keydownOver16ms: keys.length,
      p50: keys.length ? keys[Math.floor(keys.length / 2)] : 0,
      p90: keys.length ? keys[Math.floor(keys.length * 0.9)] : 0,
      max: keys.length ? keys[keys.length - 1] : 0,
    }
    // 4 — FP_REPLAY=<replay.json>: type while recorded bridge events stream
    //     into the open session (parent → it, children → fake ids).
    if (process.env.FP_REPLAY) {
      const src = JSON.parse(fs.readFileSync(process.env.FP_REPLAY, 'utf8'))
      const parent = src.find(([, e]) => e.type === 'turn_start')?.[1].sessionId
      const openId = await js(`document.querySelector('[data-session-id] > button.ring-hairline, [data-session-id] > button[class*="ring-hairline"]')?.parentElement?.dataset.sessionId`)
      const kids = new Map()
      const rows = src.map(([at, e]) => {
        const o = { ...e }
        if (o.sessionId === parent) o.sessionId = openId
        else if (o.sessionId) { if (!kids.has(o.sessionId)) kids.set(o.sessionId, `ab-child-${kids.size + 1}`); o.sessionId = kids.get(o.sessionId) }
        return [at, o]
      })
      await js(`window.__fpRows = ${JSON.stringify(rows)}; 1`)
      await js(`(() => { window.__fpKeys = []; const o = new PerformanceObserver((l) => { for (const e of l.getEntries()) if (e.name === 'keydown') window.__fpKeys.push(e.duration) }); o.observe({ type: 'event', durationThreshold: 16 }); window.__fpObs = o; return 1 })()`)
      const s0 = await metrics()
      await js(`(() => { const rows = window.__fpRows; const t0 = performance.now(); let i = 0; const pump = () => { const now = performance.now() - t0; const batch = []; while (i < rows.length && rows[i][0] <= now) batch.push(rows[i++][1]); if (batch.length) window.__emit(batch); if (i < rows.length && now < 25000) setTimeout(pump, 4); else window.__fpReplayDone = i }; pump(); return 1 })()`)
      const r0 = Date.now()
      let k = 0
      while (Date.now() - r0 < 20000) {
        const ch = text[k++ % text.length]
        const keyCode = ch === ' ' ? 'Space' : ch
        wc.sendInputEvent({ type: 'keyDown', keyCode })
        wc.sendInputEvent({ type: 'char', keyCode: ch })
        wc.sendInputEvent({ type: 'keyUp', keyCode })
        await sleep(70)
      }
      await sleep(1000)
      const s1 = await metrics()
      const rk = await js(`(() => { window.__fpObs.disconnect(); return window.__fpKeys.sort((a, b) => a - b) })()`)
      const secs = (Date.now() - r0) / 1000
      out.streamingTyping = {
        replayed: await js('window.__fpReplayDone || 0'),
        p50: rk.length ? rk[Math.floor(rk.length / 2)] : 0,
        p90: rk.length ? rk[Math.floor(rk.length * 0.9)] : 0,
        max: rk.length ? rk[rk.length - 1] : 0,
        busyPct: Math.round(((s1.TaskDuration - s0.TaskDuration) / secs) * 100),
        layoutSec: +(s1.LayoutDuration - s0.LayoutDuration).toFixed(2),
        scriptSec: +(s1.ScriptDuration - s0.ScriptDuration).toFixed(2),
      }
    }
    dbg.detach()
  } catch (err) {
    out.error = String(err).slice(0, 500)
  }
  process.stdout.write(JSON.stringify(out) + '\n')
  fs.rmSync(preload, { force: true })
  server.close()
  app.exit(0)
})
