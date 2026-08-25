// The local preview origin: what it serves, what it refuses, and the Range
// support that makes a <video> scrubbable. Run from the repo root:
//   npx tsx test-artifact-server.mjs
//
// Previews used to be `file://` iframes with `allow-same-origin` and no
// `allow-scripts` — the exact inverse of what a preview wants. This server is
// what lets the sandbox be `allow-scripts` alone: the frame gets a real HTTP
// document it can run scripts in, and an opaque origin it cannot escape.
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

const HOME = fs.mkdtempSync(path.join(os.tmpdir(), 'freyja-server-test-'))
const { startArtifactServer } = await import('./src/main/artifactServer.ts')
const { mimeTypeForPath, isBinaryType, isStreamableType } = await import('./src/shared/mime.ts')

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

// ── fixture ──────────────────────────────────────────────────────────────

const artDir = path.join(HOME, 'projects', 'demo')
fs.mkdirSync(path.join(artDir, 'assets'), { recursive: true })
fs.writeFileSync(path.join(artDir, 'index.html'), '<!doctype html><script>window.ran=1</script>')
fs.writeFileSync(path.join(artDir, 'assets', 'app.css'), 'body{color:red}')
// 10 KB of deterministic bytes, so Range slices can be checked exactly.
const media = Buffer.alloc(10_000)
for (let i = 0; i < media.length; i++) media[i] = i % 251
fs.writeFileSync(path.join(artDir, 'clip.mp4'), media)
// A secret OUTSIDE the artifact root but inside the home — the thing a
// traversal would be reaching for.
fs.writeFileSync(path.join(HOME, 'secret.txt'), 'do not serve me')

const server = await startArtifactServer(HOME)
const entry = server.serve(path.join(artDir, 'index.html'))
const root = entry.url.replace(/\/[^/]*$/, '')

async function get(url, headers) {
  const res = await fetch(url, { headers })
  return {
    status: res.status,
    type: res.headers.get('content-type'),
    range: res.headers.get('content-range'),
    acceptRanges: res.headers.get('accept-ranges'),
    body: Buffer.from(await res.arrayBuffer()),
  }
}

// ── serving ──────────────────────────────────────────────────────────────

console.log('\nserving an artifact and its siblings')
{
  const page = await get(entry.url)
  check('entry file is served', page.status, 200)
  check('as HTML, so the frame parses it as a document', page.type, 'text/html; charset=utf-8')
  check('with its script intact', page.body.toString().includes('window.ran=1'), true)

  // Relative sibling assets are the whole point of rooting at the directory.
  const css = await get(`${root}/assets/app.css`)
  check('a sibling asset is served', css.status, 200)
  check('with the right type', css.type, 'text/css; charset=utf-8')

  check('a missing sibling is a 404, not an error', (await get(`${root}/nope.css`)).status, 404)
}

console.log('\ncontainment')
{
  // The token roots at the artifact's own directory. Everything below is a
  // way of asking for something outside it.
  const cases = [
    ['plain traversal', `${root}/../secret.txt`],
    ['encoded traversal', `${root}/%2e%2e/secret.txt`],
    ['deep traversal', `${root}/../../../../../../etc/passwd`],
    ['encoded deep traversal', `${root}/%2e%2e%2f%2e%2e%2fsecret.txt`],
    ['absolute-looking path', `${root}//etc/passwd`],
  ]
  for (const [label, url] of cases) {
    const res = await get(url)
    check(`${label} is refused`, res.status !== 200, true)
    check(`${label} leaks nothing`, res.body.toString().includes('do not serve me'), false)
  }

  check('an unknown token is refused', (await get(`${root.replace(/\/a\/.*$/, '')}/a/deadbeef/index.html`)).status, 404)
  check('a non-artifact path is refused', (await get(`${root.replace(/\/a\/.*$/, '')}/etc/passwd`)).status, 404)
  check('a directory is not served', (await get(`${root}/assets`)).status, 404)
}

console.log('\nthe home boundary')
{
  // serve() itself refuses anything outside the home directory, so a token is
  // never minted for it in the first place.
  check('a path outside home gets no token', server.serve('/etc/passwd'), null)
  check('a nonexistent path gets no token', server.serve(path.join(artDir, 'ghost.html')), null)
}

console.log('\nrange requests — what makes a video scrubbable')
{
  const full = await get(`${root}/clip.mp4`)
  check('a full GET returns everything', full.body.length, 10_000)
  check('media advertises range support', full.acceptRanges, 'bytes')
  check('with a video content type', full.type, 'video/mp4')

  const mid = await get(`${root}/clip.mp4`, { Range: 'bytes=1000-1999' })
  check('a range is a 206', mid.status, 206)
  check('of exactly the requested length', mid.body.length, 1000)
  check('with the right Content-Range', mid.range, 'bytes 1000-1999/10000')
  check('and the right bytes', mid.body[0], 1000 % 251)

  const openEnded = await get(`${root}/clip.mp4`, { Range: 'bytes=9500-' })
  check('an open-ended range runs to the end', openEnded.body.length, 500)
  check('reported correctly', openEnded.range, 'bytes 9500-9999/10000')

  const suffix = await get(`${root}/clip.mp4`, { Range: 'bytes=-100' })
  check('a suffix range returns the tail', suffix.body.length, 100)
  check('reported correctly', suffix.range, 'bytes 9900-9999/10000')

  const past = await get(`${root}/clip.mp4`, { Range: 'bytes=99999-' })
  check('a range past the end is a 416', past.status, 416)

  const clamped = await get(`${root}/clip.mp4`, { Range: 'bytes=0-99999' })
  check('an over-long range clamps to the file', clamped.body.length, 10_000)

  // Range on a non-streamable type is ignored — a 206 on an HTML document
  // would leave the frame with a truncated page.
  const html = await get(entry.url, { Range: 'bytes=0-10' })
  check('range is ignored for non-media', html.status, 200)
}

console.log('\ntokens')
{
  const again = server.serve(path.join(artDir, 'index.html'))
  check('the same directory reuses its token', again.token, entry.token)
  const sibling = server.serve(path.join(artDir, 'clip.mp4'))
  check('a sibling shares the directory token', sibling.token, entry.token)

  const otherDir = path.join(HOME, 'projects', 'other')
  fs.mkdirSync(otherDir, { recursive: true })
  fs.writeFileSync(path.join(otherDir, 'x.html'), 'x')
  const other = server.serve(path.join(otherDir, 'x.html'))
  check('a different directory gets its own token', other.token !== entry.token, true)
  // And one token cannot reach the other's files.
  check(
    'tokens are isolated from each other',
    (await get(`${root}/../other/x.html`)).status !== 200,
    true,
  )
}

// ── the MIME table ───────────────────────────────────────────────────────

console.log('\nMIME — the table that used to be missing every media type')
{
  // These four are why generated media rendered as an opaque "binary file"
  // card: the old copy of this table had no entry for any of them, so they
  // resolved to text/plain and the preview's video/audio/pdf checks never
  // fired.
  check('pdf', mimeTypeForPath('/a/report.pdf'), 'application/pdf')
  check('mp4', mimeTypeForPath('/a/clip.mp4'), 'video/mp4')
  check('mov', mimeTypeForPath('/a/clip.mov'), 'video/quicktime')
  check('wav', mimeTypeForPath('/a/tone.wav'), 'audio/wav')
  check('mp3', mimeTypeForPath('/a/song.mp3'), 'audio/mpeg')
  check('webm', mimeTypeForPath('/a/clip.webm'), 'video/webm')

  check('html', mimeTypeForPath('/a/page.html'), 'text/html; charset=utf-8')
  check('svg', mimeTypeForPath('/a/logo.svg'), 'image/svg+xml')
  check('png', mimeTypeForPath('/a/shot.PNG'), 'image/png')
  check('extension case is ignored', mimeTypeForPath('/a/shot.PnG'), 'image/png')

  check('unknown extension takes the fallback', mimeTypeForPath('/a/thing.zzz', 'text/plain'), 'text/plain')
  check('no extension takes the fallback', mimeTypeForPath('/a/Makefile', 'text/plain'), 'text/plain')
  check('default fallback is binary', mimeTypeForPath('/a/thing.zzz'), 'application/octet-stream')

  check('images are binary', isBinaryType('image/png'), true)
  check('pdf is binary', isBinaryType('application/pdf'), true)
  check('video is binary', isBinaryType('video/mp4'), true)
  check('html is not binary', isBinaryType('text/html; charset=utf-8'), false)
  check('markdown is not binary', isBinaryType('text/markdown; charset=utf-8'), false)

  check('video streams', isStreamableType('video/mp4'), true)
  check('audio streams', isStreamableType('audio/wav'), true)
  check('pdf does not stream', isStreamableType('application/pdf'), false)
  check('html does not stream', isStreamableType('text/html; charset=utf-8'), false)
}

server.close()
fs.rmSync(HOME, { recursive: true, force: true })
console.log(failures === 0 ? '\nAll artifact-server checks passed.\n' : `\n${failures} check(s) failed.\n`)
process.exit(failures === 0 ? 0 : 1)
