// Side-by-side table of two profile.mjs result files.
//   node compare.mjs out/a.json out/b.json
import fs from 'node:fs'
const [a, b] = process.argv.slice(2).map((f) => JSON.parse(fs.readFileSync(f, 'utf8')))
const rows = []
const add = (label, x, y, unit = '') => rows.push([label, x == null ? '–' : `${x}${unit}`, y == null ? '–' : `${y}${unit}`])
for (const k of Object.keys(b.scenarios)) {
  const x = a.scenarios[k] || {}, y = b.scenarios[k] || {}
  if (k === 'open') add('open: click → transcript', x.openMs, y.openMs, ' ms')
  add(`${k}: main thread busy`, x.busy?.task?.toFixed(2), y.busy?.task?.toFixed(2), ' s')
  add(`${k}: script`, x.busy?.script?.toFixed(2), y.busy?.script?.toFixed(2), ' s')
  if (['type', 'streamtype'].includes(k)) {
    add(`${k}: key → paint p50`, x.keyP50, y.keyP50, ' ms')
    add(`${k}: key → paint p95`, x.keyP95, y.keyP95, ' ms')
  }
  if (k === 'click') add('click: worst pointer → paint', x.ptrMax, y.ptrMax, ' ms')
  add(`${k}: worst frame`, x.frameMax, y.frameMax, ' ms')
  add(`${k}: long tasks`, x.longTaskMs, y.longTaskMs, ' ms')
}
if (a.subscriptions && b.subscriptions) {
  add('store subscriptions', a.subscriptions.total, b.subscriptions.total)
  add('React fibers', a.subscriptions.fibers, b.subscriptions.fibers)
}
if (a.final && b.final) {
  add('DOM nodes', a.final.nodes, b.final.nodes)
  add('JS heap', a.final.heapMB, b.final.heapMB, ' MB')
}
const w = [Math.max(...rows.map((r) => r[0].length)), 10, 10]
console.log(`${'metric'.padEnd(w[0])}  ${'before'.padStart(w[1])}  ${'after'.padStart(w[2])}`)
for (const r of rows) console.log(`${r[0].padEnd(w[0])}  ${r[1].padStart(w[1])}  ${r[2].padStart(w[2])}`)
