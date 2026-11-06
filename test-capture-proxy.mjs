// The capture proxy: which display it captures, and that a display_id it
// does not know is an error rather than some other screen. Run from the
// repo root:
//   npx tsx test-capture-proxy.mjs
//
// Nothing is captured. `electron` is swapped, through require.cache, for a
// stub whose `screen` lists made-up displays and whose `desktopCapturer`
// hands back fake thumbnails named after their display.
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)

function fakeImage(name) {
  return {
    isEmpty: () => false,
    getSize: () => ({ width: 200, height: 100 }),
    resize: () => fakeImage(name),
    toJPEG: () => Buffer.from(`jpeg:${name}`),
    toPNG: () => Buffer.from(`png:${name}`),
  }
}

let displays = []
let sourceIds = null // the displays desktopCapturer has a source for; null = all
const getSourcesCalls = []
const stub = {
  screen: {
    getAllDisplays: () => displays,
    getPrimaryDisplay: () => displays[0],
    getDisplayNearestPoint: () => displays[0],
  },
  desktopCapturer: {
    getSources: async (opts) => {
      getSourcesCalls.push(opts)
      if (opts.types[0] === 'window') {
        return [{ id: 'window:7:0', display_id: '', thumbnail: fakeImage('window-7') }]
      }
      return displays
        .filter((d) => sourceIds === null || sourceIds.includes(d.id))
        .map((d) => ({ id: `screen:${d.id}:0`, display_id: String(d.id), thumbnail: fakeImage(`display-${d.id}`) }))
    },
  },
}
const electronPath = require.resolve('electron')
require.cache[electronPath] = { id: electronPath, filename: electronPath, loaded: true, exports: stub }

const { startCaptureProxy } = await import('./src/main/captureProxy.ts')

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

const proxy = await startCaptureProxy()
async function get(query, token = proxy.token) {
  const headers = token === null ? {} : { 'X-Freyja-Token': token }
  const res = await fetch(`${proxy.url}/capture${query}`, { headers })
  return { status: res.status, body: await res.text() }
}

const laptop = { id: 1, size: { width: 1512, height: 982 }, scaleFactor: 2 }
const monitor = { id: 69733382, size: { width: 2560, height: 1440 }, scaleFactor: 1 }

console.log('\nknown displays')
{
  displays = [laptop, monitor]
  sourceIds = null
  check('no token is a 401', (await get('', null)).status, 401)
  check('no display_id captures the primary display', await get(''), { status: 200, body: 'jpeg:display-1' })
  getSourcesCalls.length = 0
  check('a display_id captures that display', await get(`?display_id=${monitor.id}`), {
    status: 200,
    body: `jpeg:display-${monitor.id}`,
  })
  check('at its native pixel size', getSourcesCalls[0].thumbnailSize, { width: 2560, height: 1440 })
  check('png when asked', await get('?display_id=1&format=png'), { status: 200, body: 'png:display-1' })
}

console.log('\nunknown displays')
{
  displays = [laptop, monitor]
  sourceIds = null
  getSourcesCalls.length = 0
  check('an id that is not connected is a 404 that names the connected ids', await get('?display_id=99'), {
    status: 404,
    body:
      'display 99 is not connected (it may have been unplugged, or the id is from an old list); ' +
      `connected display ids: 1, ${monitor.id}`,
  })
  check('and nothing is captured for it', getSourcesCalls.length, 0)
  check('a display_id that is not a number is a 400', await get('?display_id=main'), {
    status: 400,
    body: 'bad display_id: main',
  })

  // The monitor is unplugged between screen.getAllDisplays and getSources.
  sourceIds = [laptop.id]
  check('a display with no capture source left is a 404, not the first source', await get(`?display_id=${monitor.id}`), {
    status: 404,
    body:
      `display ${monitor.id} is not connected (it may have been unplugged, or the id is from an old list); ` +
      'connected display ids: 1',
  })
  check('no display_id still falls back to the first source', await get(''), { status: 200, body: 'jpeg:display-1' })

  displays = [laptop]
  sourceIds = null
  check('after an unplug the old id is a 404', (await get(`?display_id=${monitor.id}`)).status, 404)
  check('and no display_id still captures the primary', await get(''), { status: 200, body: 'jpeg:display-1' })
}

console.log('\nwindows')
{
  displays = [laptop]
  check('window_id ignores display_id', await get('?window_id=7&display_id=99'), { status: 200, body: 'jpeg:window-7' })
  check('an unknown window is still a 404', (await get('?window_id=8')).status, 404)
}

proxy.close()
console.log(failures ? `\n${failures} failed\n` : '\nall passed\n')
process.exit(failures ? 1 : 0)
