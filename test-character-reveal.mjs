// The typewriter reveal for streamed text keeps up with a fast stream,
// shows text that is already there when it mounts, and does not dump a
// burst of text after a pause. Renders the real hook. Run from the repo root:
//   npx tsx test-character-reveal.mjs

import assert from 'node:assert/strict'
import { createElement } from 'react'
import { flushSync } from 'react-dom'
import { createRoot } from 'react-dom/client'

// Needs a DOM, so install jsdom first (it is not a project dependency):
//   npm i --no-save jsdom
let JSDOM
try {
  ({ JSDOM } = await import('jsdom'))
} catch {
  console.error('jsdom not found — run:  npm i --no-save jsdom')
  process.exit(2)
}
const dom = new JSDOM('<!doctype html><body></body>', { pretendToBeVisual: true })
globalThis.window = dom.window
globalThis.document = dom.window.document
globalThis.requestAnimationFrame = dom.window.requestAnimationFrame.bind(dom.window)
globalThis.cancelAnimationFrame = dom.window.cancelAnimationFrame.bind(dom.window)
globalThis.IS_REACT_ACT_ENVIRONMENT = false
const { useCharacterReveal } = await import('./src/renderer/lib/characterReveal.ts')

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
let failures = 0
async function check(name, fn) {
  try {
    await fn()
    console.log(`ok   ${name}`)
  } catch (err) {
    failures++
    console.log(`FAIL ${name}\n     ${err.message.split('\n').join('\n     ')}`)
  }
}

function Probe({ text, active }) {
  return createElement('span', null, useCharacterReveal(text, active))
}
function mount(text, active) {
  const host = document.createElement('div')
  document.body.appendChild(host)
  const root = createRoot(host)
  const render = (t, a) => flushSync(() => root.render(createElement(Probe, { text: t, active: a })))
  render(text, active)
  return { shown: () => host.textContent.length, render, unmount: () => { root.unmount(); host.remove() } }
}
const words = (n) => 'word '.repeat(Math.ceil(n / 5)).slice(0, n)

await check('text already there on mount shows at once, even mid-stream', () => {
  const m = mount(words(3000), true)
  assert.equal(m.shown(), 3000)
  m.unmount()
})

await check('a 400 chars/s stream is never more than ~1 s behind', async () => {
  const m = mount('', true)
  let sent = 0
  let worst = 0
  const t0 = Date.now()
  while (Date.now() - t0 < 2000) {
    sent = Math.floor(((Date.now() - t0) / 1000) * 400)
    m.render(words(sent), true)
    await sleep(50)
    worst = Math.max(worst, sent - m.shown())
  }
  // The old fixed 72 chars/s pace would trail by ~650 chars here.
  assert.ok(worst < 400, `worst lag ${worst} chars`)
  m.unmount()
})

await check('after a pause, new text is revealed over frames, not dumped', async () => {
  const m = mount('', true)
  m.render(words(100), true)
  await sleep(1500) // caught up, then idle
  assert.equal(m.shown(), 100)
  await sleep(400)
  m.render(words(160), true)
  await sleep(20) // about one frame
  const firstFrame = m.shown() - 100
  assert.ok(firstFrame < 30, `first frame revealed ${firstFrame} of 60 new chars`)
  await sleep(1500)
  assert.equal(m.shown(), 160)
  m.unmount()
})

await check('going inactive shows everything at once', async () => {
  const m = mount('', true)
  m.render(words(500), true)
  await sleep(50)
  assert.ok(m.shown() < 500)
  m.render(words(500), false)
  await sleep(20)
  assert.equal(m.shown(), 500)
  m.unmount()
})

console.log(failures ? `\n${failures} FAILED` : '\nall passed')
process.exit(failures ? 1 : 0)
