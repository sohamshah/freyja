// UI behaviour checks on the built renderer, with the mock bridge and
// synthetic sessions (nothing reads or writes ~/.freyja):
//   npx vite build --outDir "$PWD/scripts/perf/out/dist" --minify false --emptyOutDir
//   node scripts/perf/check-ui.mjs [--dist scripts/perf/out/dist]
// Covers: delete the session on screen, the delete confirm, Esc on the row
// menu, a rename with a space, Esc with ⌘K over the dashboard, the
// permission prompt above the dashboard, and a message that starts with a
// path. Screenshots land in out/ui-shots.
import http from 'node:http'
import fs from 'node:fs'
import path from 'node:path'
import { chromium } from 'playwright-core'

const HERE = path.dirname(new URL(import.meta.url).pathname)
const distArg = process.argv.indexOf('--dist')
const DIST = path.resolve(distArg > 0 ? process.argv[distArg + 1] : path.join(HERE, 'out/dist'))
const SESS = path.join(HERE, 'out/ui-sessions')
const SHOTS = path.join(HERE, 'out/ui-shots')
const MOCK = path.join(HERE, 'mock-harness.js')
fs.rmSync(SESS, { recursive: true, force: true })
fs.mkdirSync(SESS, { recursive: true })
fs.mkdirSync(SHOTS, { recursive: true })

const now = Date.now()
const slice = (text) => ({
  messages: [
    { id: `u-${text}`, role: 'user', parts: [{ type: 'text', text: `question about ${text}` }], createdAt: now - 60000 },
    { id: `a-${text}`, role: 'assistant', parts: [{ type: 'text', text: `answer about ${text}` }], createdAt: now - 50000 },
  ],
  currentStreamingMessageId: null, currentTurnId: null, thinking: '', isStreaming: false,
  toolCalls: {}, toolCallOrder: [], fileChanges: [], subagents: {}, subagentOrder: [],
  usage: {
    currentContextTokens: 0, totalInputTokens: 0, totalOutputTokens: 0, totalCacheReadTokens: 0,
    totalCacheWriteTokens: 0, totalCost: 0, lastTurnInputTokens: 0, lastTurnOutputTokens: 0, contextWindow: 200000,
  },
  systemEvents: [], kanbanCards: {}, busMessages: [], inboxEvents: [], artifacts: [], widgets: {},
  autoDispatchEnabled: false, model: 'claude-sonnet-5-5', reasoningLevel: 'medium', coordinationStrategy: 'bus', runtime: 'native',
})
const titles = ['Alpha research', 'Beta deploy', 'Gamma notes', 'Delta review']
const metas = titles.map((title, i) => ({
  version: 1, id: `session-${title.split(' ')[0].toLowerCase()}`, title, model: 'claude-sonnet-5-5',
  reasoningLevel: 'medium', coordinationStrategy: 'bus', workspace: '~/', createdAt: now - 1e6,
  updatedAt: now - i * 60000, messageCount: 2, totalInputTokens: 0, totalOutputTokens: 0, cacheReadTokens: 0, totalCost: 0,
}))
fs.writeFileSync(path.join(SESS, '_index.json'), JSON.stringify({ sessions: metas }))
for (const m of metas) fs.writeFileSync(path.join(SESS, `${m.id}.json`), JSON.stringify({ ...m, slice: slice(m.title) }))

const server = http.createServer((req, res) => {
  const u = decodeURIComponent(req.url.split('?')[0])
  const f = u.startsWith('/__sessions/') ? path.join(SESS, u.slice(12)) : path.join(DIST, u === '/' ? 'index.html' : u)
  fs.readFile(f, (e, d) => {
    if (e) { res.writeHead(404); res.end(); return }
    const ext = path.extname(f)
    res.writeHead(200, { 'content-type': { '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html', '.json': 'application/json' }[ext] || 'application/octet-stream' })
    res.end(d)
  })
})
await new Promise((r) => server.listen(0, '127.0.0.1', r))
const browser = await chromium.launch({ executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome', headless: true })
const ctx = await browser.newContext({ viewport: { width: 1400, height: 860 } })
await ctx.addInitScript({ path: MOCK })
const page = await ctx.newPage()
const errors = []
page.on('pageerror', (e) => errors.push(String(e)))

let failures = 0
const check = (name, ok, detail = '') => {
  console.log(`${ok ? '  ok  ' : '  FAIL'} ${name}${detail ? `  (${detail})` : ''}`)
  if (!ok) failures++
}
const shot = (name) => page.screenshot({ path: path.join(SHOTS, `${name}.png`) })
const row = (title) => page.locator('[data-session-id]').filter({ hasText: title }).first()
const activeTitle = () => page.evaluate(() => {
  return document.querySelector('[data-session-id] button.ring-hairline')?.textContent ?? ''
})
const commands = () => page.evaluate(() => window.__harnessStats.commands.slice())

await page.goto(`http://127.0.0.1:${server.address().port}/`)
await row('Alpha research').waitFor({ timeout: 60000 })
await page.waitForTimeout(2500)

// View Beta, then Alpha: deleting Alpha should land back on Beta.
await row('Beta deploy').locator('button').first().click()
await page.waitForTimeout(600)
await row('Alpha research').locator('button').first().click()
await page.waitForTimeout(600)
check('Alpha is on screen', (await activeTitle()).includes('Alpha research'))

await row('Alpha research').hover()
await row('Alpha research').locator('[title="More options"]').click()
await page.waitForTimeout(200)
await shot('1-menu-on-active')
const del = page.getByRole('button', { name: 'Delete', exact: true })
check('Delete shows in the menu of the session on screen', await del.isVisible())
// Escape closes the menu.
await page.keyboard.press('Escape')
await page.waitForTimeout(200)
check('Escape closes the menu', !(await del.isVisible()))

await row('Alpha research').hover()
await row('Alpha research').locator('[title="More options"]').click()
await page.waitForTimeout(200)
await page.getByRole('button', { name: 'Delete', exact: true }).click()
await page.waitForTimeout(200)
const confirm = page.getByRole('button', { name: 'Confirm delete' })
check('first click asks to confirm', await confirm.isVisible())
check('nothing deleted yet', (await row('Alpha research').count()) === 1)
await shot('2-confirm')
await confirm.click()
await page.waitForTimeout(1200)
await shot('3-after-delete')
check('Alpha row is gone', (await page.locator('[data-session-id]').filter({ hasText: 'Alpha research' }).count()) === 0)
check('view moved to Beta (the previous session)', (await activeTitle()).includes('Beta deploy'), await activeTitle())
const cmds = await commands()
check('bridge was told to delete', cmds.includes('delete_session'), cmds.slice(-4).join(','))
const shown = await page.evaluate(() => document.body.innerText.includes('answer about Beta deploy'))
check('Beta transcript is on screen', shown)

// Rename a session that is not on screen, with a space in the name.
await row('Gamma notes').click({ button: 'right' })
await page.waitForTimeout(200)
await page.getByRole('button', { name: 'Rename', exact: true }).click()
await page.waitForTimeout(200)
await page.keyboard.press('Meta+A')
await page.keyboard.type('Gamma two words')
await page.keyboard.press('Enter')
await page.waitForTimeout(600)
check('rename kept the space', (await page.locator('[data-session-id]').filter({ hasText: 'Gamma two words' }).count()) === 1)
check('typing a space did not switch session', (await activeTitle()).includes('Beta deploy'), await activeTitle())
await shot('4-after-rename')


// ⌘K over the dashboard: Esc closes only ⌘K.
await page.keyboard.press('Meta+o')
await page.waitForTimeout(800)
const dashOpen = () => page.evaluate(() => document.body.innerText.toLowerCase().includes('mission dashboard'))
check('dashboard opens', await dashOpen())
await page.keyboard.press('Meta+k')
await page.waitForTimeout(500)
const paletteOpen = () => page.evaluate(() => !!document.querySelector('.z-\\[60\\]'))
check('⌘K opens over it', await paletteOpen())
await page.keyboard.press('Escape')
await page.waitForTimeout(400)
check('Esc closes ⌘K', !(await paletteOpen()))
check('…and the dashboard stays', await dashOpen())
await shot('5-dashboard-after-esc')

// A permission request while the dashboard is open shows on top of it.
await page.evaluate(() => window.__emit({ type: 'permission_request', requestId: 'perm-1', sessionId: 'session-beta', level: 'high', prompt: 'rm -rf build/', reason: 'cleanup' }))
await page.waitForTimeout(600)
const promptOnTop = await page.evaluate(() => {
  const label = [...document.querySelectorAll('span')].find((el) => el.textContent === 'permission required')
  if (!label) return 'no prompt'
  const r = label.getBoundingClientRect()
  const hit = document.elementFromPoint(r.left + 4, r.top + r.height / 2)
  return hit && label.closest('.fixed').contains(hit) ? 'top' : `covered by ${hit?.className}`
})
check('permission prompt is on top of the dashboard', promptOnTop === 'top', promptOnTop)
await shot('6-permission-over-dashboard')
await page.waitForTimeout(500) // past the 400 ms guard
await page.keyboard.press('Escape')
await page.waitForTimeout(400)
const afterDeny = await commands()
check('Esc denies the request', afterDeny.includes('permission_response'), afterDeny.slice(-3).join(','))
check('…and the dashboard stays', await dashOpen())
await page.keyboard.press('Escape')
await page.waitForTimeout(400)
check('a second Esc closes the dashboard', !(await dashOpen()))

// A message that starts with a path is sent, not erased.
const box = page.locator('textarea[placeholder*="mention files"]')
await box.click()
await box.fill('/Users/me/app/log.txt shows a crash, why?')
await page.keyboard.press('Enter')
await page.waitForTimeout(600)
const sent = await commands()
check('a path-led message is sent', sent.includes('send_message'), sent.slice(-3).join(','))
const toastText = await page.evaluate(() => document.body.innerText)
check('no unknown-command toast', !/unknown command/i.test(toastText))
await shot('7-path-message')

// A mistyped command is held once with a suggestion; Enter again sends it.
const sendsBefore = (await commands()).filter((c) => c === 'send_message').length
await page.waitForTimeout(3500) // let the earlier toast clear
await box.click()
await box.fill('/compcat')
await page.keyboard.press('Enter')
await page.waitForTimeout(400)
const heldText = await page.evaluate(() => document.body.innerText)
check('a mistyped command is held, with a suggestion', /did you mean \/compact/i.test(heldText))
check('…and stays in the composer', (await box.inputValue()) === '/compcat', await box.inputValue())
check('…and is not sent', (await commands()).filter((c) => c === 'send_message').length === sendsBefore)
await page.keyboard.press('Enter')
await page.waitForTimeout(400)
check('Enter again sends it', (await commands()).filter((c) => c === 'send_message').length === sendsBefore + 1)
await shot('8-mistyped-command')

check('no page errors', errors.length === 0, errors.slice(0, 3).join(' | '))
await browser.close()
server.close()
console.log(failures ? `\n${failures} failed` : '\nall passed')
process.exit(failures ? 1 : 0)
