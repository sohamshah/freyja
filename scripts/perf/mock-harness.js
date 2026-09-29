// Stand-in for the Electron preload's window.harness. Serves real session
// files from ~/.freyja/sessions through the harness server and records what
// the renderer tries to persist, so a profiling run sees the same data and
// the same save traffic as the app.
;(() => {
  const listeners = new Set()
  const stats = {
    sessionSave: [], // { id, cloneMs, at }
    sessionIndexSave: [], // { rows, cloneMs, at }
    commands: [],
  }
  window.__harnessStats = stats
  window.__emit = (events) => {
    for (const ev of Array.isArray(events) ? events : [events]) {
      for (const l of listeners) {
        try { l(ev) } catch (err) { console.error('[mock] listener error', err) }
      }
    }
  }
  // Electron's ipcRenderer.invoke serializes the payload synchronously on
  // the renderer main thread (V8 ValueSerializer). structuredClone runs the
  // same serializer plus a deserialize, so it is an upper bound on the cost.
  const emulateIpc = (payload) => {
    const t0 = performance.now()
    try { structuredClone(payload) } catch { /* ignore */ }
    return performance.now() - t0
  }
  const base = {
    onEvent(listener) {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    async sendCommand(cmd) {
      stats.commands.push(cmd?.type)
      return { ok: true }
    },
    async getMode() { return 'live' },
    getZoomFactor() { return 1 },
    async sessionList() {
      const r = await fetch('/__sessions/_index.json')
      const j = await r.json()
      return { ok: true, sessions: j.sessions }
    },
    async sessionLoad(id) {
      const t0 = performance.now()
      const r = await fetch(`/__sessions/${id}.json`)
      if (!r.ok) return { ok: false, error: 'not found' }
      const text = await r.text()
      const t1 = performance.now()
      const session = JSON.parse(text)
      const t2 = performance.now()
      ;(stats.loads = stats.loads || []).push({ id, fetchMs: Math.round(t1 - t0), parseMs: Math.round(t2 - t1), mb: +(text.length / 1e6).toFixed(1), at: Math.round(t0) })
      return { ok: true, session }
    },
    async sessionSave(payload) {
      const cloneMs = emulateIpc(payload)
      stats.sessionSave.push({ id: payload?.id, cloneMs, at: performance.now() })
      return { ok: true, bytes: 0, durationMs: 0 }
    },
    async sessionIndexSave(rows) {
      const cloneMs = emulateIpc(rows)
      stats.sessionIndexSave.push({ rows: rows?.length, cloneMs, at: performance.now() })
      return { ok: true, bytes: 0, durationMs: 0 }
    },
    async settingsGet() { return { ok: false } },
    async getAppInfo() { return { version: 'harness' } },
  }
  window.harness = new Proxy(base, {
    get(target, prop) {
      if (prop in target) return target[prop]
      if (typeof prop === 'symbol') return undefined
      return async () => ({ ok: false, error: 'harness: not mocked', rows: [], sessions: [] })
    },
  })
})()
