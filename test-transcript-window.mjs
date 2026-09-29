// Tail windowing for the conversation transcript: which entries mount on
// open, and how far each scroll-up reveal reaches. Run from the repo root:
//   npx tsx test-transcript-window.mjs
import {
  TRANSCRIPT_PART_BUDGET,
  entryWeight,
  revealEarlier,
  tailStart,
} from './src/renderer/lib/transcriptWindow.ts'

let failures = 0
function check(name, actual, expected) {
  const a = JSON.stringify(actual)
  const e = JSON.stringify(expected)
  if (a === e) {
    console.log(`  ok   ${name}`)
  } else {
    failures++
    console.log(`  FAIL ${name}\n         expected ${e}\n         actual   ${a}`)
  }
}

console.log('\nentryWeight')
check('a part-less entry still costs its frame', entryWeight(0), 1)
check('parts add to the frame', entryWeight(12), 13)
check('negative counts clamp', entryWeight(-3), 1)

console.log('\ntailStart — what mounts on open')
check('an empty transcript starts at 0', tailStart([]), 0)
check('everything fits under the budget', tailStart([2, 3, 4], 100), 0)
check('stops once the tail reaches the budget', tailStart([50, 50, 50, 50], 100), 2)
check('a single oversized entry still mounts alone', tailStart([10, 10, 500], 100), 2)
check('the budget boundary is inclusive', tailStart([1, 60, 40], 100), 1)
{
  // Shaped like the long research session: a few huge turns, many small.
  const weights = [2, 111, 6, 12, 9, 3, 142, 2, 17, 2, 5, 2, 134, 2, 57, 25, 2, 104, 5, 3, 2, 77]
  const start = tailStart(weights)
  const mounted = weights.slice(start).reduce((a, b) => a + b, 0)
  check('mounts at least the budget', mounted >= TRANSCRIPT_PART_BUDGET, true)
  check('drops the entry before the start', mounted - weights[start] < TRANSCRIPT_PART_BUDGET, true)
}

console.log('\nrevealEarlier — scrolling up')
check('reveals one budget above the start', revealEarlier([50, 50, 50, 50, 50], 4, 100), 2)
check('stops at the top', revealEarlier([10, 10, 10], 2, 100), 0)
check('already at the top stays there', revealEarlier([10, 10], 0, 100), 0)
check('a start past the end clamps to the length', revealEarlier([50, 50, 50], 9, 100), 1)
check('always moves at least one entry', revealEarlier([500, 500], 1, 100), 0)

if (failures > 0) {
  console.log(`\n${failures} failure(s)`)
  process.exit(1)
}
console.log('\nall passed')
