// Runs INSIDE the live Freyja main process. Walks the renderer's React fiber
// tree (read-only) and counts useSyncExternalStore subscriptions — i.e.
// zustand selectors re-run on every store update — per component type.
// Returns the top types with a source location for each.
const wc = electron.webContents.getAllWebContents().find((w) => w.getType() === 'window')
const dbg = wc.debugger
if (!dbg.isAttached()) dbg.attach('1.3')
const send = (m, p) => dbg.sendCommand(m, p || {})
const r = await send('Runtime.evaluate', {
  returnByValue: true,
  expression: `(() => {
    const root = document.getElementById('root')
    const key = Object.keys(root).find((k) => k.startsWith('__reactContainer'))
    let fiber = root[key]
    const counts = new Map()
    window.__fpTypes = []
    let total = 0, fibers = 0
    const stack = [fiber]
    while (stack.length) {
      const f = stack.pop()
      if (!f) continue
      fibers++
      if (typeof f.type === 'function') {
        let h = f.memoizedState, n = 0
        while (h) { if (h.queue && typeof h.queue === 'object' && 'getSnapshot' in h.queue) n++; h = h.next }
        if (n) {
          total += n
          let c = counts.get(f.type)
          if (!c) { c = { idx: window.__fpTypes.push(f.type) - 1, name: f.type.name, inst: 0, subs: 0 }; counts.set(f.type, c) }
          c.inst++; c.subs += n
        }
      }
      if (f.sibling) stack.push(f.sibling)
      if (f.child) stack.push(f.child)
    }
    return { total, fibers, types: [...counts.values()].sort((a, b) => b.subs - a.subs).slice(0, 25) }
  })()`,
})
const out = r.result.value
for (const t of out.types) {
  const h = await send('Runtime.evaluate', { expression: `window.__fpTypes[${t.idx}]` })
  const props = await send('Runtime.getProperties', { objectId: h.result.objectId, ownProperties: false })
  const loc = (props.internalProperties || []).find((p) => p.name === '[[FunctionLocation]]')
  if (loc) t.loc = [loc.value.value.lineNumber, loc.value.value.columnNumber]
}
await send('Runtime.evaluate', { expression: 'delete window.__fpTypes' })
dbg.detach()
return out
