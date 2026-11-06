// Checks for aggregateRows in src/renderer/components/MetricsDashboard.tsx,
// the metrics dashboard's reading of ~/.freyja/telemetry/compaction.jsonl.
// Run from the repo root:
//   npx tsx test-metrics-aggregate.mjs

import assert from 'node:assert/strict'

const { aggregateRows } = await import('./src/renderer/components/MetricsDashboard.tsx')

let failures = 0
function check(name, fn) {
  try {
    fn()
    console.log(`ok   ${name}`)
  } catch (err) {
    failures++
    console.log(`FAIL ${name}\n     ${err.message.split('\n').join('\n     ')}`)
  }
}

let ts = 1_000
const event = (session_id, subtype, trigger, tokens_before = 0, tokens_after = 0) => ({
  type: 'compaction_event', ts: ts++, session_id, subtype, trigger, tokens_before, tokens_after,
})
const pressure = (session_id, pressure_pct) => ({
  type: 'pressure_signal', ts: ts++, session_id, band: 'strong', pressure_pct,
})

// What the bridge writes: a summary writes a start row, then a complete,
// skipped or failed row. Start rows carry no tokens_after.
const rows = [
  pressure('s1', 72),
  event('s1', 'compaction_start', 'pre_request', 700_000),
  event('s1', 'compaction_complete', 'pre_request', 700_000, 90_000),
  pressure('s1', 45),
  event('s1', 'compaction_start', 'agent_summarize_context', 450_000),
  event('s1', 'compaction_complete', 'agent_summarize_context', 450_000, 50_000),
  event('s1', 'compaction_start', 'manual', 200_000),
  event('s1', 'compaction_complete', 'manual', 200_000, 20_000),
  event('s2', 'compaction_start', 'restore', 680_000),
  event('s2', 'compaction_complete', 'restore', 680_000, 30_000),
  event('s2', 'compaction_start', 'overflow_cascade', 300_000),
  event('s2', 'compaction_skipped', 'overflow_cascade', 300_000),
  event('s2', 'compaction_start', 'restore', 600_000),
  event('s2', 'compaction_failed', 'restore', 600_000, 600_000),
  event('s2', 'context_pruning', 'pre_request', 100_000, 80_000),
  event('s2', 'thrash_skip', 'thrash_detector', 90_000, 90_000),
]
const agg = aggregateRows(rows, 'all')

check('each compaction counts once, by its outcome row; starts, skips and failures are attempts', () => {
  assert.equal(agg.summaryCount, 4)
  assert.equal(agg.pruneCount, 1)
  assert.equal(agg.thrashCount, 1)
  assert.equal(agg.compactions, 5)
  const perSession = Object.fromEntries(agg.perSession.map((s) => [s.sessionId, s.compactions]))
  assert.deepEqual(perSession, { s1: 3, s2: 2 })
})

check('tokens saved and the savings trend read outcome rows only', () => {
  // 610k + 400k + 180k + 650k + 20k
  assert.equal(agg.tokensSaved, 1_860_000)
  assert.equal(agg.avgSavedPerCompaction, 372_000)
  assert.equal(agg.savingsSeries.length, 5)
  assert.ok(agg.savingsSeries.every((p) => p.pct < 100))
})

check('the trigger mix adds up to the compactions it heads', () => {
  const total = Object.values(agg.triggerCounts).reduce((a, b) => a + b, 0)
  assert.equal(total, agg.compactions)
  assert.deepEqual(agg.triggerCounts, {
    pre_request: 2, agent_summarize_context: 1, manual: 1, restore: 1,
  })
})

check('cooperation covers every summary: restore is the runtime, manual counts on its own', () => {
  const cs = agg.cooperativeStats
  assert.equal(cs.agentDrivenCount, 1)
  assert.equal(cs.runtimeFallbackCount, 2)
  assert.equal(cs.manualCount, 1)
  assert.equal(cs.meanPressurePctAgent, 45)
})

check('a has-compaction filter keeps a session whose only rows are attempts out', () => {
  const only = aggregateRows([
    event('s3', 'compaction_start', 'overflow_cascade', 300_000),
    event('s3', 'compaction_skipped', 'overflow_cascade', 300_000),
  ], 'has_compaction')
  assert.equal(only.compactions, 0)
  assert.deepEqual(only.perSession.map((s) => s.sessionId), [])
})

check('an overflow recovery that only dropped images counts as a prune, not a summary', () => {
  // Before 2026-11-06 it wrote a compaction_complete row; now a media_pruning row.
  const elisions = aggregateRows([
    pressure('s4', 75),
    { ...event('s4', 'compaction_complete', 'overflow_cascade_image_elision'), mechanism: 'summary' },
    { ...event('s4', 'media_pruning', 'overflow_cascade_image_elision'), mechanism: 'cumulative_image_prune' },
    event('s4', 'compaction_start', 'overflow_cascade', 300_000),
    event('s4', 'compaction_complete', 'overflow_cascade', 300_000, 40_000),
  ], 'all')
  assert.equal(elisions.summaryCount, 1)
  assert.equal(elisions.pruneCount, 2)
  assert.equal(elisions.compactions, 3)
  assert.deepEqual(elisions.triggerCounts, { overflow_cascade_image_elision: 2, overflow_cascade: 1 })
  assert.equal(elisions.cooperativeStats.runtimeFallbackCount, 1)
  const [s4] = elisions.perSession
  assert.deepEqual(
    s4.compactionRows.map((c) => c.subtype),
    ['media_pruning', 'media_pruning', 'compaction_start', 'compaction_complete'],
  )
})

if (failures > 0) {
  console.log(`\n${failures} check(s) failed`)
  process.exit(1)
}
console.log('\nall checks passed')
