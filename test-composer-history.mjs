// Composer history recall. Drives `stepHistory` through key sequences the
// way the composer does, tracking composer text, caret and cursor between
// presses. Run from the repo root:
//   npx tsx test-composer-history.mjs
import { stepHistory } from './src/renderer/lib/composerHistory.ts'

/** A stand-in for the textarea + the component's history state. Arrow keys
 *  that `stepHistory` declines fall through to the browser default, which
 *  we model the way Chromium behaves in a textarea: ↑ moves up one line, or
 *  to offset 0 when already on the first line; ↓ mirrors it. */
function composer(text = '', caret = text.length) {
  return {
    text,
    caret,
    cursor: null,
    stash: '',
    press(key) {
      const step = stepHistory({
        key,
        caret: this.caret,
        selectionEnd: this.caret,
        value: this.text,
        entries: this.entries,
        cursor: this.cursor,
        stashedDraft: this.stash,
      })
      if (step) {
        if (step.stash) this.stash = this.text
        this.cursor = step.cursor
        this.text = step.text
        this.caret = step.caretAt === 'start' ? 0 : step.text.length
        return this
      }
      // Browser default caret movement.
      const before = this.text.slice(0, this.caret)
      const nl = before.lastIndexOf('\n')
      if (key === 'ArrowUp') {
        if (nl === -1) this.caret = 0
        else {
          const col = this.caret - nl - 1
          const prevStart = before.lastIndexOf('\n', nl - 1) + 1
          this.caret = Math.min(prevStart + col, nl)
        }
      } else {
        const nextNl = this.text.indexOf('\n', this.caret)
        if (nextNl === -1) this.caret = this.text.length
        else {
          const col = this.caret - (nl + 1)
          const lineEnd = this.text.indexOf('\n', nextNl + 1)
          const end = lineEnd === -1 ? this.text.length : lineEnd
          this.caret = Math.min(nextNl + 1 + col, end)
        }
      }
      return this
    },
    /** Simulate the operator typing, which exits history in onChange. */
    type(s) {
      this.text = this.text.slice(0, this.caret) + s + this.text.slice(this.caret)
      this.caret += s.length
      this.cursor = null
      return this
    },
  }
}

let pass = 0, fail = 0
function check(name, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected)
  if (ok) pass++
  else {
    fail++
    console.log(`\nFAIL  ${name}`)
    console.log('  expected: ' + JSON.stringify(expected))
    console.log('  actual:   ' + JSON.stringify(actual))
  }
}
const state = (c) => ({ text: c.text, caret: c.caret, index: c.cursor?.index ?? null })

// newest first
const HIST = ['third message', 'second message', 'first message']

// ── empty composer: one press recalls ─────────────────────────────────────
{
  const c = composer(''); c.entries = HIST
  c.press('ArrowUp')
  check('empty composer recalls newest', state(c), { text: 'third message', caret: 0, index: 0 })
  c.press('ArrowUp')
  check('second ↑ walks back', state(c), { text: 'second message', caret: 0, index: 1 })
  c.press('ArrowUp')
  check('third ↑ walks back', state(c), { text: 'first message', caret: 0, index: 2 })
  c.press('ArrowUp')
  check('↑ holds at the oldest', state(c), { text: 'first message', caret: 0, index: 2 })
}

// ── single-line draft: must reach the start first ─────────────────────────
{
  const c = composer('my draft'); c.entries = HIST
  c.press('ArrowUp')
  check('↑ from the end moves the caret, not history', state(c), { text: 'my draft', caret: 0, index: null })
  c.press('ArrowUp')
  check('↑ at offset 0 recalls', state(c), { text: 'third message', caret: 0, index: 0 })
}

// ── multiline draft: walk every line before leaving ───────────────────────
{
  const draft = 'line one\nline two\nline three'
  const c = composer(draft); c.entries = HIST
  c.press('ArrowUp')
  check('multiline ↑ #1 stays in the text', state(c), { text: draft, caret: 17, index: null })
  c.press('ArrowUp')
  check('multiline ↑ #2 stays in the text', state(c), { text: draft, caret: 8, index: null })
  c.press('ArrowUp')
  check('multiline ↑ #3 lands at the start', state(c), { text: draft, caret: 0, index: null })
  c.press('ArrowUp')
  check('multiline ↑ #4 finally recalls', state(c), { text: 'third message', caret: 0, index: 0 })
}

// ── the draft comes back ──────────────────────────────────────────────────
{
  const c = composer('half-written thought'); c.entries = HIST
  c.press('ArrowUp').press('ArrowUp')          // to start, then recall
  c.press('ArrowUp').press('ArrowUp')          // back to the oldest
  check('walked to the oldest', state(c), { text: 'first message', caret: 0, index: 2 })
  c.press('ArrowDown')
  check('↓ from offset 0 crosses the text first', state(c), { text: 'first message', caret: 13, index: 2 })
  c.press('ArrowDown')
  check('↓ at the end steps forward', state(c), { text: 'second message', caret: 14, index: 1 })
  c.press('ArrowDown')
  check('↓ steps forward again', state(c), { text: 'third message', caret: 13, index: 0 })
  c.press('ArrowDown')
  check('↓ past the newest restores the draft', state(c), { text: 'half-written thought', caret: 20, index: null })
  c.press('ArrowDown')
  check('↓ again does nothing', state(c), { text: 'half-written thought', caret: 20, index: null })
}

// ── a multiline recalled message is navigable, not skipped ────────────────
{
  const c = composer(''); c.entries = ['recalled line A\nrecalled line B']
  c.press('ArrowUp')
  check('recalled multiline lands at the start', state(c), { text: 'recalled line A\nrecalled line B', caret: 0, index: 0 })
  c.press('ArrowDown')
  check('↓ moves within the recalled text', state(c), { text: 'recalled line A\nrecalled line B', caret: 16, index: 0 })
  c.press('ArrowDown')
  check('↓ reaches the end', state(c), { text: 'recalled line A\nrecalled line B', caret: 31, index: 0 })
  c.press('ArrowDown')
  check('↓ at the end exits to the draft', state(c), { text: '', caret: 0, index: null })
}

// ── editing a recalled message keeps the edit and re-stashes it ───────────
{
  const c = composer('original draft'); c.entries = HIST
  c.press('ArrowUp').press('ArrowUp')
  check('recalled', state(c), { text: 'third message', caret: 0, index: 0 })
  c.type('EDITED ')
  check('typing exits history', state(c), { text: 'EDITED third message', caret: 7, index: null })
  c.caret = 0
  c.press('ArrowUp')
  check('↑ re-enters from the newest', state(c), { text: 'third message', caret: 0, index: 0 })
  c.press('ArrowDown').press('ArrowDown')
  check('↓ returns the edited text, not the original draft', state(c),
    { text: 'EDITED third message', caret: 20, index: null })
}

// ── no history at all ─────────────────────────────────────────────────────
{
  const c = composer('lone draft', 0); c.entries = []
  c.press('ArrowUp')
  check('↑ with no history is inert', state(c), { text: 'lone draft', caret: 0, index: null })
  c.press('ArrowDown')
  check('↓ while composing is inert', state(c), { text: 'lone draft', caret: 10, index: null })
}

// ── a ranged selection never recalls ──────────────────────────────────────
{
  const step = stepHistory({
    key: 'ArrowUp', caret: 0, selectionEnd: 5, value: 'hello',
    entries: HIST, cursor: null, stashedDraft: '',
  })
  check('shift-selection is left to the browser', step, null)
}

// ── history shrinking under an active cursor ──────────────────────────────
{
  const step = stepHistory({
    key: 'ArrowDown', caret: 4, selectionEnd: 4, value: 'gone',
    entries: [], cursor: { index: 2, total: 3 }, stashedDraft: 'my draft',
  })
  check('emptied history hands the draft back', step,
    { text: 'my draft', caretAt: 'end', cursor: null, stash: false })
}

console.log(`\n${pass} passed, ${fail} failed`)
process.exit(fail === 0 ? 0 : 1)
