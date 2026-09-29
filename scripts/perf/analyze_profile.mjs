// Aggregate a .cpuprofile: self and inclusive time by function. With
// --map <bundle.js.map>, positions are mapped back to original sources.
//   node analyze_profile.mjs <file.cpuprofile> [N] [--map file.map]
import fs from 'node:fs'
import { SourceMapConsumer } from 'source-map-js'
const argv = process.argv.slice(2)
const file = argv[0]
const N = Number(argv[1] && !argv[1].startsWith('--') ? argv[1] : 30)
const mapIdx = argv.indexOf('--map')
const smc = mapIdx >= 0 ? new SourceMapConsumer(JSON.parse(fs.readFileSync(argv[mapIdx + 1], 'utf8'))) : null
const p = JSON.parse(fs.readFileSync(file, 'utf8'))
const byId = new Map(p.nodes.map((n) => [n.id, n]))
const parent = new Map()
for (const n of p.nodes) for (const c of n.children || []) parent.set(c, n.id)
const dt = new Map()
for (let i = 0; i < p.samples.length; i++) dt.set(p.samples[i], (dt.get(p.samples[i]) || 0) + (p.timeDeltas[i] || 0))
const keyCache = new Map()
const key = (n) => {
  if (keyCache.has(n.id)) return keyCache.get(n.id)
  const cf = n.callFrame
  let k
  const f = (cf.url || '').split('/').pop()
  if (smc && /index-.*\.js$/.test(f) && cf.lineNumber >= 0) {
    // Map the function's start position to its original name + file:line.
    const o = smc.originalPositionFor({ line: cf.lineNumber + 1, column: cf.columnNumber })
    const src = (o.source || '?').replace(/^.*\/(src|node_modules)\//, '$1/')
    k = `${cf.functionName || o.name || '(anon)'} ${src}:${o.line}`
  } else {
    k = `${cf.functionName || '(anon)'} ${f}:${cf.lineNumber + 1}`
  }
  keyCache.set(n.id, k)
  return k
}
const self = new Map(), incl = new Map()
let total = 0
for (const [id, us] of dt) {
  total += us
  const n = byId.get(id)
  self.set(key(n), (self.get(key(n)) || 0) + us)
  const seen = new Set()
  let cur = id
  while (cur != null) { const k = key(byId.get(cur)); if (!seen.has(k)) { seen.add(k); incl.set(k, (incl.get(k) || 0) + us) } cur = parent.get(cur) }
}
const fmt = (m) => [...m.entries()].sort((a, b) => b[1] - a[1]).slice(0, N).map(([k, v]) => `${(v / 1000).toFixed(1).padStart(8)}ms ${((v / total) * 100).toFixed(1).padStart(5)}%  ${k}`).join('\n')
console.log(`total ${(total / 1000).toFixed(0)}ms\n--- self ---\n${fmt(self)}\n--- inclusive ---\n${fmt(incl)}`)
