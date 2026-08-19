// Round-trip check for the copy-as-markdown path:
//   markdown --renderMarkdown--> HTML --rangeToMarkdown--> markdown
// Needs a DOM, so install jsdom first (it is not a project dependency):
//   npm i --no-save jsdom
// Then run from the repo root:
//   npx tsx test-copy-markdown.mjs

let JSDOM
try {
  ({ JSDOM } = await import('jsdom'))
} catch {
  console.error('jsdom not found — run:  npm i --no-save jsdom')
  process.exit(2)
}

const dom = new JSDOM('<!doctype html><body></body>')
globalThis.window = dom.window
globalThis.document = dom.window.document
globalThis.Node = dom.window.Node
globalThis.Element = dom.window.Element
globalThis.Range = dom.window.Range

const { renderMarkdown } = await import('./src/renderer/lib/markdown.ts')
const { rangeToMarkdown } = await import('./src/renderer/lib/copyMarkdown.ts')

function mount(md) {
  const host = document.createElement('div')
  host.className = 'md selectable'
  host.innerHTML = renderMarkdown(md)
  document.body.innerHTML = ''
  document.body.appendChild(host)
  return host
}

/** Full-content selection over the rendered body. */
function copyAll(md) {
  const host = mount(md)
  const range = document.createRange()
  range.selectNodeContents(host)
  return rangeToMarkdown(range)
}

/** Partial selection: from `startText` inside the Nth text node match to
 *  `endText`, both located by substring search over the rendered DOM. */
function copyBetween(md, startNeedle, endNeedle) {
  const host = mount(md)
  const walker = document.createTreeWalker(host, 4 /* SHOW_TEXT */)
  let startNode = null, startOff = 0, endNode = null, endOff = 0
  let n
  while ((n = walker.nextNode())) {
    if (!startNode) {
      const i = n.nodeValue.indexOf(startNeedle)
      if (i >= 0) { startNode = n; startOff = i }
    }
    if (startNode) {
      const j = n.nodeValue.indexOf(endNeedle)
      if (j >= 0 && (n !== startNode || j >= startOff)) { endNode = n; endOff = j + endNeedle.length; break }
    }
  }
  if (!startNode || !endNode) throw new Error(`needles not found: ${startNeedle} / ${endNeedle}`)
  const range = document.createRange()
  range.setStart(startNode, startOff)
  range.setEnd(endNode, endOff)
  return rangeToMarkdown(range)
}

let pass = 0, fail = 0
function check(name, actual, expected) {
  const ok = actual.trim() === expected.trim()
  if (ok) pass++
  else {
    fail++
    console.log(`\nFAIL  ${name}`)
    console.log('  expected:\n' + JSON.stringify(expected.trim()))
    console.log('  actual:\n' + JSON.stringify(actual.trim()))
  }
}

// ── whole-block round trips ───────────────────────────────────────────────
check('heading', copyAll('## Cloud tasks'), '## Cloud tasks')
check('paragraph + inline', copyAll('The **client trait** in `api.rs` is *nine* methods.'),
  'The **client trait** in `api.rs` is *nine* methods.')
check('link', copyAll('See [the docs](https://example.com/x) here.'),
  'See [the docs](https://example.com/x) here.')
check('bare url', copyAll('Visit https://example.com/x now.'), 'Visit https://example.com/x now.')
check('bullets', copyAll('- alpha\n- beta\n- gamma'), '- alpha\n- beta\n- gamma')
check('numbered', copyAll('1. alpha\n2. beta'), '1. alpha\n2. beta')
check('blockquote', copyAll('> quoted line'), '> quoted line')
check('hr', copyAll('text\n\n---\n\nmore'), 'text\n\n---\n\nmore')
check('strike', copyAll('~~gone~~ stays'), '~~gone~~ stays')

check('fenced code',
  copyAll('```python\ndef f(x):\n    return x\n```'),
  '```python\ndef f(x):\n    return x\n```')

check('code with blank lines',
  copyAll('```js\nconst a = 1\n\n\nconst b = 2\n```'),
  '```js\nconst a = 1\n\n\nconst b = 2\n```')

check('table',
  copyAll('| Method | Purpose |\n| --- | :---: |\n| `create_task` | Submit new task |\n| `list_tasks` | Paginated list |'),
  '| Method | Purpose |\n| --- | :---: |\n| `create_task` | Submit new task |\n| `list_tasks` | Paginated list |')

check('math inline', copyAll('Let $E = mc^2$ hold.'), 'Let $E = mc^2$ hold.')
check('math display', copyAll('$$\\pi_\\theta(y)$$'), '$$\n\\pi_\\theta(y)\n$$')

check('mixed doc',
  copyAll('# Title\n\nIntro **bold**.\n\n- one\n- two\n\n```sh\nls -la\n```\n\nEnd.'),
  '# Title\n\nIntro **bold**.\n\n- one\n- two\n\n```sh\nls -la\n```\n\nEnd.')

// ── partial selections (the hard case) ────────────────────────────────────
check('partial inside code block',
  copyBetween('```python\ndef f(x):\n    return x\n```', 'def f', 'return x'),
  '```python\ndef f(x):\n    return x\n```')

check('partial inside list item',
  copyBetween('- alpha one\n- beta two', 'alpha', 'alpha one'),
  '- alpha one')

check('partial spanning list items',
  copyBetween('- alpha one\n- beta two\n- gamma', 'alpha', 'beta two'),
  '- alpha one\n- beta two')

check('partial inside table cell',
  copyBetween('| A | B |\n| --- | --- |\n| one | two |', 'one', 'one'),
  'one')

check('partial spanning heading into paragraph',
  copyBetween('## Cloud tasks\n\nThis is the **Codex** infra.', 'Cloud', 'Codex'),
  '## Cloud tasks\n\nThis is the **Codex**')

check('partial mid-paragraph',
  copyBetween('Alpha beta gamma delta.', 'beta', 'gamma'),
  'beta gamma')

// ── decorations layered on top of renderMarkdown ──────────────────────────
const { highlightHtml } = await import('./src/renderer/lib/searchHighlight.ts')

function copyAllHtml(html) {
  const host = document.createElement('div')
  host.className = 'md selectable'
  host.innerHTML = html
  document.body.innerHTML = ''
  document.body.appendChild(host)
  const range = document.createRange()
  range.selectNodeContents(host)
  return rangeToMarkdown(range)
}

check('search highlight unwrapped',
  copyAllHtml(highlightHtml(renderMarkdown('The **client trait** is nine methods.'), 'trait')),
  'The **client trait** is nine methods.')

check('kanban chip unwrapped',
  copyAllHtml(renderMarkdown('Moved card_017 to done.').replace(
    /card_017/, '<span class="kanban-card-mention" data-card-id="card_017" title="done">card_017</span>')),
  'Moved card_017 to done.')

check('copy-skip chrome dropped',
  copyAllHtml('<p>Answer text.</p><button data-copy-skip="">copy</button>'),
  'Answer text.')

// ── inline edge cases ─────────────────────────────────────────────────────
// renderMarkdown's inline-code rule (/`+([^`]+)`+/) can't hold a backtick,
// so the DOM is already `<code>a </code> b``` — copy-out reproduces the DOM
// faithfully. Pre-existing renderer limitation, asserted so it stays visible.
check('code span holding a backtick (renderer-limited)',
  copyAll('Use ``a ` b`` here.'), 'Use `a ` b`` here.')

check('code span fence widens when content has backticks',
  copyAllHtml('<p>Use <code>a ` b</code> here.</p>'), 'Use ``a ` b`` here.')

check('link inside a list item',
  copyAll('- see [docs](https://x.dev/a)\n- and `code`'),
  '- see [docs](https://x.dev/a)\n- and `code`')

check('inline code inside a table cell',
  copyAll('| Method | Purpose |\n| --- | --- |\n| `apply_task` | runs `git apply --check` |'),
  '| Method | Purpose |\n| --- | --- |\n| `apply_task` | runs `git apply --check` |')

check('pipe escaped inside a cell',
  copyAll('| A | B |\n| --- | --- |\n| x \\| y | z |'),
  '| A | B |\n| --- | --- |\n| x \\| y | z |')

check('alignment preserved',
  copyAll('| L | C | R |\n| :--- | :---: | ---: |\n| a | b | c |'),
  '| L | C | R |\n| :--- | :---: | ---: |\n| a | b | c |')

check('math inside a table cell',
  copyAll('| Sym | Value |\n| --- | --- |\n| energy | $E = mc^2$ |'),
  '| Sym | Value |\n| --- | --- |\n| energy | $E = mc^2$ |')

check('partial across two separate rendered blocks',
  (() => {
    // Two sibling `.md` divs, as a message with two text parts renders.
    document.body.innerHTML =
      `<div class="stack"><div class="md selectable">${renderMarkdown('# One\n\nfirst para')}</div>` +
      `<div class="md selectable">${renderMarkdown('- item a\n- item b')}</div></div>`
    const host = document.querySelector('.stack')
    const range = document.createRange()
    range.selectNodeContents(host)
    return rangeToMarkdown(range)
  })(),
  '# One\n\nfirst para\n\n- item a\n- item b')

console.log(`\n${pass} passed, ${fail} failed`)
process.exit(fail === 0 ? 0 : 1)
