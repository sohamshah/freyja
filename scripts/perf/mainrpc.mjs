// Evaluate JS inside the live Freyja main process through its Node inspector.
//   node mainrpc.mjs <file.js | -e "expr">   (the code may use `electron` and `await`)
import fs from 'node:fs'
const argv = process.argv.slice(2)
const code = argv[0] === '-e' ? argv[1] : fs.readFileSync(argv[0], 'utf8')
const list = await (await fetch('http://127.0.0.1:9229/json/list')).json()
const ws = new WebSocket(list[0].webSocketDebuggerUrl)
let id = 0
const pending = new Map()
ws.onmessage = (m) => {
  const msg = JSON.parse(m.data)
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id) }
}
await new Promise((r) => (ws.onopen = r))
const send = (method, params = {}) => new Promise((r) => { const i = ++id; pending.set(i, r); ws.send(JSON.stringify({ id: i, method, params })) })
const outDir = JSON.stringify(new URL('./out', import.meta.url).pathname)
const wrapped = `(async () => { globalThis.__fpOut = ${outDir}; const electron = process.mainModule.require('electron'); const require = process.mainModule.require.bind(process.mainModule); ${code}\n })()`
const res = await send('Runtime.evaluate', { expression: wrapped, awaitPromise: true, returnByValue: true, timeout: 600000 })
if (res.result?.exceptionDetails) console.error('EXC', JSON.stringify(res.result.exceptionDetails).slice(0, 2000))
const v = res.result?.result?.value
console.log(typeof v === 'string' ? v : JSON.stringify(v, null, 1))
ws.close()
