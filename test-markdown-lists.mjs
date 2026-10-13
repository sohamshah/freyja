// Lists in the markdown renderer (src/renderer/lib/markdown.ts). Models nest
// lists by indentation, continue items on indented lines, and put blank
// lines between numbered items; each of those used to break the list.
// Run from the repo root:
//   npx tsx test-markdown-lists.mjs
import assert from 'node:assert/strict'
import { renderMarkdown } from './src/renderer/lib/markdown.ts'

const html = (src) => renderMarkdown(src).replace(/\n/g, '')
let n = 0
const check = (name, src, expected) => {
  assert.equal(html(src), expected, name)
  n += 1
  console.log(`ok  ${name}`)
}

check('tight bullet list', '- a\n- b', '<ul><li>a</li><li>b</li></ul>')

check(
  'nested list and continuation text return to the outer item',
  '- **Topic.** Waiting on:\n  - check one\n  - check two\n  About 30 minutes.\n- **Cost.** Cheap.',
  '<ul><li><strong>Topic.</strong> Waiting on:<ul><li>check one</li><li>check two</li></ul>' +
    '<p>About 30 minutes.</p></li><li><strong>Cost.</strong> Cheap.</li></ul>',
)

check(
  'blank lines between numbered items keep one list',
  '1. First\n\n2. Second\n\n3. Third',
  '<ol><li>First</li><li>Second</li><li>Third</li></ol>',
)

check('ordered list keeps its first number', '4. four\n5. five', '<ol start="4"><li>four</li><li>five</li></ol>')

check('parenthesis numbers', '1) one\n2) two', '<ol><li>one</li><li>two</li></ol>')

check(
  'numbered list with nested bullets',
  '1. Plan\n   - read\n   - write\n2. Ship',
  '<ol><li>Plan<ul><li>read</li><li>write</li></ul></li><li>Ship</li></ol>',
)

check(
  'three levels',
  '- a\n  - b\n    - c\n- d',
  '<ul><li>a<ul><li>b<ul><li>c</li></ul></li></ul></li><li>d</li></ul>',
)

check('wrapped item text joins the item', '- a long\nline', '<ul><li>a long line</li></ul>')

check(
  'indented paragraph after a blank line stays in the item',
  '- first\n\n  more about first\n- second',
  '<ul><li>first<p>more about first</p></li><li>second</li></ul>',
)

check(
  'unindented paragraph after a blank line ends the list',
  '- a\n- b\n\nAfter the list.',
  '<ul><li>a</li><li>b</li></ul><p>After the list.</p>',
)

check('heading right after a list ends it', '- a\n## Next', '<ul><li>a</li></ul><h2>Next</h2>')

check('switching bullet to number starts a new list', '- a\n1. b', '<ul><li>a</li></ul><ol><li>b</li></ol>')

check(
  'fenced code inside an item',
  '- run:\n  ```sh\n  npm test\n  ```\n- done',
  '<ul><li>run:<pre><code class="lang-sh">npm test</code></pre></li><li>done</li></ul>',
)

check(
  'paragraph then list then paragraph',
  'Intro:\n- a\n- b\n\nOutro.',
  '<p>Intro:</p><ul><li>a</li><li>b</li></ul><p>Outro.</p>',
)

check('bold at line start is not a list', '**Bold** start', '<p><strong>Bold</strong> start</p>')

check('a rule is not a list', '- - -', '<hr />')

console.log(`\nall passed (${n})`)
