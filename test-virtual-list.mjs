// Fixed-row windowing for the artifact browser's lists. The app had no
// virtualization at all before this — long lists were handled with
// `content-visibility` on conversation rows and hard slice() caps everywhere
// else, neither of which works for a scrollable list of thousands of
// interactive rows. Run from the repo root:
//   npx tsx test-virtual-list.mjs
import { windowRange } from './src/renderer/lib/useVirtualList.ts'

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

const ROW = 56
const VIEW = 560 // exactly 10 rows

console.log('\nwindowRange — the visible slice')
{
  const top = windowRange({ count: 2000, rowHeight: ROW, scrollTop: 0, viewportHeight: VIEW, overscan: 0 })
  check('starts at the first row', top.start, 0)
  check('renders a viewport worth', top.end, 10)
  check('total height spans every row', top.totalHeight, 2000 * ROW)
  check('no offset at the top', top.offsetY, 0)

  const mid = windowRange({ count: 2000, rowHeight: ROW, scrollTop: 100 * ROW, viewportHeight: VIEW, overscan: 0 })
  check('window follows the scroll', [mid.start, mid.end], [100, 110])
  check('offset lines the slab up with the scroll', mid.offsetY, 100 * ROW)
}

console.log('\nwindowRange — overscan')
{
  const mid = windowRange({ count: 2000, rowHeight: ROW, scrollTop: 100 * ROW, viewportHeight: VIEW, overscan: 8 })
  check('renders above the fold', mid.start, 92)
  check('renders below the fold', mid.end, 118)
  check('offset accounts for the overscan', mid.offsetY, 92 * ROW)

  const top = windowRange({ count: 2000, rowHeight: ROW, scrollTop: 0, viewportHeight: VIEW, overscan: 8 })
  check('overscan clamps at the start', top.start, 0)

  const bottom = windowRange({
    count: 2000, rowHeight: ROW, scrollTop: 2000 * ROW - VIEW, viewportHeight: VIEW, overscan: 8,
  })
  check('overscan clamps at the end', bottom.end, 2000)
}

console.log('\nwindowRange — partial rows')
{
  // Scrolled half a row: both the partly-hidden top row and the partly-visible
  // bottom row have to be rendered or the list shows gaps at the edges.
  const half = windowRange({
    count: 100, rowHeight: ROW, scrollTop: ROW / 2, viewportHeight: VIEW, overscan: 0,
  })
  check('keeps the half-scrolled top row', half.start, 0)
  check('includes the half-visible bottom row', half.end, 11)
}

console.log('\nwindowRange — degenerate inputs')
{
  check('empty list renders nothing', windowRange({
    count: 0, rowHeight: ROW, scrollTop: 0, viewportHeight: VIEW,
  }), { start: 0, end: 0, totalHeight: 0, offsetY: 0 })

  check('zero row height renders nothing', windowRange({
    count: 10, rowHeight: 0, scrollTop: 0, viewportHeight: VIEW,
  }), { start: 0, end: 0, totalHeight: 0, offsetY: 0 })

  // Before the ResizeObserver has measured the container, viewportHeight is 0.
  // Rendering nothing there means a blank list for a frame.
  const unmeasured = windowRange({ count: 500, rowHeight: ROW, scrollTop: 0, viewportHeight: 0, overscan: 0 })
  check('unmeasured viewport still renders a screenful', unmeasured.end > 0, true)

  check('negative scroll clamps to the top', windowRange({
    count: 500, rowHeight: ROW, scrollTop: -400, viewportHeight: VIEW, overscan: 0,
  }).start, 0)

  const overscrolled = windowRange({
    count: 20, rowHeight: ROW, scrollTop: 999_999, viewportHeight: VIEW, overscan: 0,
  })
  check('scroll past the end clamps to the last page', overscrolled.end, 20)
  check('and does not run off the front', overscrolled.start, 10)

  const shorterThanViewport = windowRange({
    count: 3, rowHeight: ROW, scrollTop: 0, viewportHeight: VIEW, overscan: 0,
  })
  check('a list shorter than the viewport renders whole', shorterThanViewport.end, 3)
}

console.log('\nwindowRange — the whole scroll, at scale')
{
  // Walk a 100k-row list the length of its scroll and assert the invariants
  // that make windowing correct: every visible row is inside the window, and
  // the window never runs off either end.
  const count = 100_000
  let bad = 0
  for (let scrollTop = 0; scrollTop <= count * ROW - VIEW; scrollTop += ROW * 137) {
    const w = windowRange({ count, rowHeight: ROW, scrollTop, viewportHeight: VIEW, overscan: 6 })
    const firstVisible = Math.floor(scrollTop / ROW)
    const lastVisible = Math.min(count - 1, Math.floor((scrollTop + VIEW - 1) / ROW))
    if (w.start > firstVisible) bad++
    if (w.end <= lastVisible) bad++
    if (w.start < 0 || w.end > count) bad++
    if (w.offsetY !== w.start * ROW) bad++
  }
  check('every visible row stayed inside the window', bad, 0)

  const w = windowRange({ count, rowHeight: ROW, scrollTop: 50_000 * ROW, viewportHeight: VIEW, overscan: 6 })
  check('a 100k-row list still renders ~22 rows', w.end - w.start, 22)
}

console.log(failures === 0 ? '\nAll virtual-list checks passed.\n' : `\n${failures} check(s) failed.\n`)
process.exit(failures === 0 ? 0 : 1)
