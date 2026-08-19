// Quick check for the KaTeX markdown pass. Run from repo root:
//   npx tsx test-math-render.mjs
import { renderMarkdown } from './src/renderer/lib/markdown.ts'

const cases = [
  ['inline $',        'The policy $\\pi_\\theta(y \\mid x)$ factorizes.',              true],
  ['display $$',      '$$\\pi_\\theta(y \\mid x) = \\prod_{t=1}^{|y|} \\pi_\\theta(y_t \\mid x, y_{<t})$$', true],
  ['paren inline',    'Let \\(y_{<t}\\) be the prefix.',                               true],
  ['bracket display', '\\[ E = mc^2 \\]',                                              true],
  ['currency (NO)',   'It costs $5 and then $10 total.',                               false],
  ['streaming half',  'Loading $$\\pi_\\theta',                                        false],
  ['underscores',     'See $a_{i} \\cdot b^{j}$ here.',                                true],
]

let pass = 0
for (const [name, src, shouldRender] of cases) {
  const html = renderMarkdown(src)
  const rendered = html.includes('class="katex"')
  const ok = rendered === shouldRender
  pass += ok ? 1 : 0
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}  (rendered=${rendered}, expected=${shouldRender})`)
}
console.log(`\n${pass}/${cases.length} passed`)
process.exit(pass === cases.length ? 0 : 1)
