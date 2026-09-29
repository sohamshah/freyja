// Runs INSIDE the live Freyja main process (via mainrpc.mjs). Drives a
// disposable fork of a long session so typing and streaming can be
// measured without a model turn and without touching the real session.
//
//   globalThis.__fpPhase = 'fork'     __fpSource = '<session id>'  → forks it
//       (parent only: no sub-agents, no project copy) and waits until the
//       app has switched to the fork. Returns { forkId }.
//   globalThis.__fpPhase = 'measure'  __fpFork, __fpReplay (path or ''),
//       __fpTypeMs, __fpLabel → types into the composer (letters and spaces
//       only — never Enter, so nothing is sent) while replaying recorded
//       bridge events into the fork, and reports input latency and renderer
//       busy time. Recorded child-session events go to fake ids, so no real
//       session's state changes.
//   globalThis.__fpPhase = 'restore'  __fpSource → clears what was typed,
//       restores the draft, switches back to the source session, detaches.
const fs = require('fs')
const phase = globalThis.__fpPhase
const wc = electron.webContents.getAllWebContents().find((w) => w.getType() === 'window')
const dbg = wc.debugger
if (!dbg.isAttached()) dbg.attach('1.3')
const send = (m, p) => dbg.sendCommand(m, p || {})
const ev = async (expr) => {
  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true })
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails).slice(0, 600))
  return r.result.value
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const pushEvent = (e) => wc.send('harness:bridge-event', e)
const isActive = (id) =>
  ev(`(() => { const b = document.querySelector('[data-session-id="${id}"] > button'); return !!b && b.className.includes('ring-hairline') })()`)

if (phase === 'fork') {
  const source = globalThis.__fpSource
  let forkId = null
  const origSend = wc.send
  wc.send = function (channel, ...a) {
    const e = a[0]
    if (channel === 'harness:bridge-event' && e?.type === 'session_branched' && e.originalSessionId === source) forkId = e.newSessionId
    return origSend.call(this, channel, ...a)
  }
  try {
    const handler = electron.ipcMain._invokeHandlers.get('harness:send-command')
    await handler({ sender: wc }, {
      type: 'branch_session',
      sessionId: source,
      messageOrdinal: -1,
      newName: 'perf-test fork (safe to delete)',
      childSessionIds: [],
      cloneProject: false,
    })
    for (let i = 0; i < 120 && !forkId; i++) await sleep(500)
  } finally {
    delete wc.send
  }
  if (!forkId) return { error: 'no session_branched event within 60 s' }
  for (let i = 0; i < 120 && !(await isActive(forkId)); i++) await sleep(500)
  for (let i = 0; i < 60; i++) {
    if (await ev(`document.querySelectorAll('[data-tool-call-id]').length > 5`)) break
    await sleep(500)
  }
  globalThis.__fpForkId = forkId
  return { forkId, active: await isActive(forkId) }
}

if (phase === 'measure') {
  const forkId = globalThis.__fpFork
  if (!(await isActive(forkId))) return { error: `fork ${forkId} is not the active session` }
  const typeMs = Number(globalThis.__fpTypeMs || 20000)
  const replayPath = globalThis.__fpReplay || ''

  // Recorded events → the fork (parent) and fake child ids.
  let rows = []
  if (replayPath) {
    const src = JSON.parse(fs.readFileSync(replayPath, 'utf8'))
    // The recorded parent is whoever opens the replayed turn.
    const parent = src.find(([, e]) => e.type === 'turn_start')?.[1].sessionId
    const kids = new Map()
    rows = src.map(([at, e]) => {
      const out = { ...e }
      if (out.sessionId === parent) out.sessionId = forkId
      else if (out.sessionId) {
        if (!kids.has(out.sessionId)) kids.set(out.sessionId, `perf-child-${kids.size + 1}`)
        out.sessionId = kids.get(out.sessionId)
      }
      if (out.turnId) out.turnId = 'turn-perf-' + (globalThis.__fpLabel || 'x')
      return [at, out]
    })
  }

  // Probes: Event Timing for keys, long animation frames for forced layout.
  await ev(`(() => {
    const p = window.__fpDrive = { keys: [], loaf: [] }
    p.o1 = new PerformanceObserver((l) => { for (const e of l.getEntries()) if (/^key|input/.test(e.name)) p.keys.push({ name: e.name, d: e.duration, proc: e.processingEnd - e.processingStart }) })
    p.o1.observe({ type: 'event', durationThreshold: 16 })
    p.o2 = new PerformanceObserver((l) => { for (const e of l.getEntries()) p.loaf.push({ d: e.duration, fl: (e.scripts || []).reduce((a, s) => a + (s.forcedStyleAndLayoutDuration || 0), 0) }) })
    p.o2.observe({ type: 'long-animation-frame' })
    const ta = [...document.querySelectorAll('textarea')].filter((t) => t.getAttribute('aria-hidden') !== 'true').pop()
    window.__fpSavedDraft = window.__fpSavedDraft ?? ta.value
    ta.focus()
    return true
  })()`)
  await send('Performance.enable')
  const metrics = async () => Object.fromEntries((await send('Performance.getMetrics')).metrics.map((x) => [x.name, x.value]))
  const m0 = await metrics()
  const t0 = Date.now()

  // Replay pump.
  let sent = 0
  const replayDone = (async () => {
    let i = 0
    while (i < rows.length && Date.now() - t0 < typeMs + 5000) {
      const now = Date.now() - t0
      while (i < rows.length && rows[i][0] <= now) { pushEvent(rows[i++][1]); sent++ }
      await sleep(4)
    }
  })()

  // Typing: letters and spaces at ~14 keys/s. Never Enter.
  const text = 'the quick brown fox jumps over the lazy dog while the swarm keeps streaming '
  let typed = 0
  while (Date.now() - t0 < typeMs) {
    if (globalThis.__fpNoType) { await sleep(100); continue }
    const ch = text[typed++ % text.length]
    const code = ch === ' ' ? 'Space' : `Key${ch.toUpperCase()}`
    const vk = ch === ' ' ? 32 : ch.toUpperCase().charCodeAt(0)
    await send('Input.dispatchKeyEvent', { type: 'keyDown', key: ch, code, text: ch, unmodifiedText: ch, windowsVirtualKeyCode: vk })
    await send('Input.dispatchKeyEvent', { type: 'keyUp', key: ch, code, windowsVirtualKeyCode: vk })
    await sleep(70)
  }
  await replayDone
  // Always close the turn the replay opened, even if it was cut short, so
  // the fork doesn't sit "streaming" forever.
  const turnId = 'turn-perf-' + (globalThis.__fpLabel || 'x')
  if (rows.some(([, e]) => e.type === 'turn_start' && e.sessionId === forkId)) {
    pushEvent({ type: 'message_stop', sessionId: forkId, stopReason: 'end_turn' })
    pushEvent({ type: 'turn_complete', sessionId: forkId, turnId, success: true })
  }
  await sleep(500)
  const m1 = await metrics()
  const probe = await ev(`(() => { const p = window.__fpDrive; p.o1.disconnect(); p.o2.disconnect(); delete window.__fpDrive; return { keys: p.keys, loaf: p.loaf } })()`)
  await send('Performance.disable')
  const secs = (Date.now() - t0) / 1000
  const kd = probe.keys.filter((k) => k.name === 'keydown').map((k) => k.d).sort((a, b) => a - b)
  const pct = (a, q) => (a.length ? a[Math.min(a.length - 1, Math.floor(q * a.length))] : null)
  return {
    label: globalThis.__fpLabel,
    seconds: +secs.toFixed(1),
    keysTyped: typed,
    eventsReplayed: sent,
    keydownOver16ms: kd.length,
    keydownP50: pct(kd, 0.5),
    keydownP90: pct(kd, 0.9),
    keydownMax: kd.length ? kd[kd.length - 1] : null,
    handlerMsAvg: probe.keys.length ? +(probe.keys.reduce((a, k) => a + k.proc, 0) / probe.keys.length).toFixed(1) : 0,
    longFrames: probe.loaf.length,
    forcedLayoutMs: Math.round(probe.loaf.reduce((a, f) => a + f.fl, 0)),
    busyPct: Math.round(((m1.TaskDuration - m0.TaskDuration) / secs) * 100),
    scriptSec: +(m1.ScriptDuration - m0.ScriptDuration).toFixed(2),
    layoutSec: +(m1.LayoutDuration - m0.LayoutDuration).toFixed(2),
    layouts: m1.LayoutCount - m0.LayoutCount,
    msPerLayout: m1.LayoutCount - m0.LayoutCount ? +(((m1.LayoutDuration - m0.LayoutDuration) * 1000) / (m1.LayoutCount - m0.LayoutCount)).toFixed(1) : 0,
  }
}

if (phase === 'restore') {
  const source = globalThis.__fpSource
  await ev(`(() => { const ta = [...document.querySelectorAll('textarea')].filter((t) => t.getAttribute('aria-hidden') !== 'true').pop(); ta.focus(); return true })()`)
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'a', code: 'KeyA', modifiers: 4, windowsVirtualKeyCode: 65, commands: ['selectAll'] })
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'a', code: 'KeyA', modifiers: 4, windowsVirtualKeyCode: 65 })
  await send('Input.dispatchKeyEvent', { type: 'keyDown', key: 'Backspace', code: 'Backspace', windowsVirtualKeyCode: 8 })
  await send('Input.dispatchKeyEvent', { type: 'keyUp', key: 'Backspace', code: 'Backspace', windowsVirtualKeyCode: 8 })
  const saved = await ev(`(() => { const s = window.__fpSavedDraft || ''; delete window.__fpSavedDraft; return s })()`)
  if (saved) await send('Input.insertText', { text: saved })
  await sleep(300)
  await ev(`(() => { const b = document.querySelector('[data-session-id="${source}"] > button'); if (b) b.click(); return !!b })()`)
  for (let i = 0; i < 40 && !(await isActive(source)); i++) await sleep(250)
  const draftNow = await ev(`[...document.querySelectorAll('textarea')].filter((t) => t.getAttribute('aria-hidden') !== 'true').pop().value`)
  const back = await isActive(source)
  dbg.detach()
  return { backOnSource: back, draftRestored: draftNow === saved, draftLength: draftNow.length }
}

return { error: `unknown phase ${phase}` }
