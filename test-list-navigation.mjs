// Keyboard navigation + scroll-reveal math for the ⌘K palette. Drives the pure
// helpers the way the component does, modelling a scroll viewport of fixed
// height over uniformly sized rows. Run from the repo root:
//   npx tsx test-list-navigation.mjs
import { stepSelection, scrollToReveal } from './src/renderer/lib/listNavigation.ts'

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

/** A stand-in for the scroll container: `count` rows of `rowHeight` inside a
 *  viewport of `viewportHeight`, with a group label of `labelHeight` above the
 *  first row of every `groupSize` rows. Mirrors the palette's DOM layout. */
function list({ count, rowHeight = 44, viewportHeight = 360, groupSize = Infinity, labelHeight = 22 }) {
  const layout = []
  let y = 0
  for (let i = 0; i < count; i++) {
    const firstInGroup = i % groupSize === 0
    const labelTop = firstInGroup ? y : null
    if (firstInGroup) y += labelHeight
    layout.push({ top: y, height: rowHeight, labelTop })
    y += rowHeight
  }
  return {
    selected: 0,
    scrollTop: 0,
    count,
    viewportHeight,
    contentHeight: y,
    layout,
    press(key, mods = {}) {
      const next = stepSelection({ key, ...mods }, { selected: this.selected, count: this.count, pageStep: 8 })
      if (next != null) this.selected = next
      this.settle()
      return this
    },
    /** Run the reveal pass the layout effect runs after every selection change. */
    settle() {
      const row = this.layout[this.selected]
      if (!row) return this
      const to = scrollToReveal({
        scrollTop: this.scrollTop,
        viewportHeight: this.viewportHeight,
        rowTop: row.top,
        rowHeight: row.height,
        anchorTop: row.labelTop != null ? Math.min(row.labelTop, row.top) : undefined,
        pad: 8,
        contentHeight: this.contentHeight,
      })
      if (to != null) this.scrollTop = to
      return this
    },
    /** True when the selected row is wholly inside the viewport. */
    get visible() {
      const row = this.layout[this.selected]
      return row.top >= this.scrollTop && row.top + row.height <= this.scrollTop + this.viewportHeight
    },
    scrollTo(v) {
      this.scrollTop = v
      return this
    },
  }
}

console.log('\nstepSelection — which keys navigate')
{
  const opts = { selected: 5, count: 100, pageStep: 8 }
  check('ArrowDown steps forward', stepSelection({ key: 'ArrowDown' }, opts), 6)
  check('ArrowUp steps back', stepSelection({ key: 'ArrowUp' }, opts), 4)
  check('PageDown jumps a page', stepSelection({ key: 'PageDown' }, opts), 13)
  check('PageUp jumps back a page', stepSelection({ key: 'PageUp' }, opts), 0)
  check('cmd-ArrowDown goes last', stepSelection({ key: 'ArrowDown', metaKey: true }, opts), 99)
  check('cmd-ArrowUp goes first', stepSelection({ key: 'ArrowUp', metaKey: true }, opts), 0)
  check('ctrl-n steps forward', stepSelection({ key: 'n', ctrlKey: true }, opts), 6)
  check('ctrl-p steps back', stepSelection({ key: 'p', ctrlKey: true }, opts), 4)
  // Home/End belong to the caret in a search field, so they must fall through.
  check('Home falls through', stepSelection({ key: 'Home' }, opts), null)
  check('End falls through', stepSelection({ key: 'End' }, opts), null)
  check('Enter falls through', stepSelection({ key: 'Enter' }, opts), null)
  check('Escape falls through', stepSelection({ key: 'Escape' }, opts), null)
  check('a plain letter falls through', stepSelection({ key: 'a' }, opts), null)
  check('bare n falls through', stepSelection({ key: 'n' }, opts), null)
  check('alt-ArrowDown falls through to word motion', stepSelection({ key: 'ArrowDown', altKey: true }, opts), null)
}

console.log('\nstepSelection — ends and empty lists')
{
  check(
    'down from last wraps to first',
    stepSelection({ key: 'ArrowDown' }, { selected: 9, count: 10 }),
    0,
  )
  check(
    'up from first wraps to last',
    stepSelection({ key: 'ArrowUp' }, { selected: 0, count: 10 }),
    9,
  )
  check(
    'wrap can be turned off',
    stepSelection({ key: 'ArrowDown' }, { selected: 9, count: 10, wrap: false }),
    9,
  )
  check(
    'PageDown clamps at the end, never wraps',
    stepSelection({ key: 'PageDown' }, { selected: 96, count: 100, pageStep: 8 }),
    99,
  )
  check(
    'PageUp clamps at the start, never wraps',
    stepSelection({ key: 'PageUp' }, { selected: 3, count: 100, pageStep: 8 }),
    0,
  )
  check('empty list navigates nowhere', stepSelection({ key: 'ArrowDown' }, { selected: 0, count: 0 }), null)
  check('single item stays put', stepSelection({ key: 'ArrowDown' }, { selected: 0, count: 1 }), 0)
}

console.log('\nscrollToReveal — the reported bug')
{
  // The bug: 360px viewport shows ~6 of 44px rows. Arrowing to row 7 used to
  // leave scrollTop at 0, so the highlight vanished below the fold.
  const l = list({ count: 1383 })
  for (let i = 0; i < 6; i++) l.press('ArrowDown')
  check('row 6 still needs no scroll', l.scrollTop, 0)
  check('row 6 is visible', l.visible, true)

  l.press('ArrowDown')
  check('row 7 scrolled the list', l.scrollTop > 0, true)
  check('row 7 is visible', l.visible, true)

  // Walk deep into the list; the highlight must never leave the viewport.
  let everHidden = false
  for (let i = 7; i < 400; i++) {
    l.press('ArrowDown')
    if (!l.visible) everHidden = true
  }
  check('highlight stayed visible for 400 rows', everHidden, false)
  check('selection reached row 400', l.selected, 400)
}

console.log('\nscrollToReveal — minimal movement and no-ops')
{
  const l = list({ count: 100 })
  check('already-visible row does not scroll', scrollToReveal({
    scrollTop: 0, viewportHeight: 360, rowTop: 100, rowHeight: 44, pad: 8,
  }), null)

  // Stepping down by one should move by exactly the overhang, not recentre.
  check('reveals by the smallest delta', scrollToReveal({
    scrollTop: 0, viewportHeight: 360, rowTop: 352, rowHeight: 44, pad: 8, contentHeight: 4400,
  }), 352 + 44 + 8 - 360)

  check('reveals upward with pad', scrollToReveal({
    scrollTop: 200, viewportHeight: 360, rowTop: 150, rowHeight: 44, pad: 8, contentHeight: 4400,
  }), 142)

  check('never scrolls above the top', scrollToReveal({
    scrollTop: 4, viewportHeight: 360, rowTop: 0, rowHeight: 44, pad: 8, contentHeight: 4400,
  }), 0)

  check('never scrolls past the bottom', scrollToReveal({
    scrollTop: 0, viewportHeight: 360, rowTop: 4356, rowHeight: 44, pad: 8, contentHeight: 4400,
  }), 4040)

  // A row taller than the viewport: pin its top rather than chase its bottom.
  check('oversized row pins to its top', scrollToReveal({
    scrollTop: 0, viewportHeight: 100, rowTop: 500, rowHeight: 400, pad: 8, contentHeight: 4400,
  }), 808)

  l.scrollTo(0)
}

console.log('\nscrollToReveal — group labels')
{
  // Arrowing into the first row of a group must reveal that group's label too.
  const l = list({ count: 60, groupSize: 10, labelHeight: 22 })
  const firstOfSecondGroup = l.layout[10]
  check('row 10 carries a label above it', firstOfSecondGroup.labelTop != null, true)

  const to = scrollToReveal({
    scrollTop: firstOfSecondGroup.top - 4,
    viewportHeight: 360,
    rowTop: firstOfSecondGroup.top,
    rowHeight: 44,
    anchorTop: firstOfSecondGroup.labelTop,
    pad: 8,
    contentHeight: l.contentHeight,
  })
  check('label is pulled into view, not clipped', to, firstOfSecondGroup.labelTop - 8)

  // Walking the whole grouped list keeps every selection visible.
  let everHidden = false
  for (let i = 0; i < 59; i++) {
    l.press('ArrowDown')
    if (!l.visible) everHidden = true
  }
  check('grouped walk kept the highlight visible', everHidden, false)
}

console.log('\nscrollToReveal — wrap-around jumps')
{
  const l = list({ count: 200 })
  l.press('ArrowUp') // wraps to the last row
  check('wrap up selects the last row', l.selected, 199)
  check('wrap up scrolled to the bottom', l.visible, true)
  check('wrap up hit the scroll floor', l.scrollTop, l.contentHeight - 360)

  l.press('ArrowDown') // wraps back to the first
  check('wrap down selects the first row', l.selected, 0)
  check('wrap down scrolled back to the top', l.scrollTop, 0)
}

console.log('\nscrollToReveal — a user scroll is not fought')
{
  // The palette only writes scrollTop when the row is actually out of view, so
  // a mouse-wheel scroll that leaves the selection visible is left alone.
  const l = list({ count: 200 })
  l.selected = 4
  l.scrollTo(30).settle()
  check('mouse scroll preserved while row stays visible', l.scrollTop, 30)
}

console.log(failures === 0 ? '\nAll list-navigation checks passed.\n' : `\n${failures} check(s) failed.\n`)
process.exit(failures === 0 ? 0 : 1)
