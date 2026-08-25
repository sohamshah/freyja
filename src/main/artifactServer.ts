import fs from 'node:fs'
import fsp from 'node:fs/promises'
import http from 'node:http'
import path from 'node:path'
import crypto from 'node:crypto'
import { AddressInfo } from 'node:net'

import { isStreamableType, mimeTypeForPath } from '../shared/mime.js'

/**
 * Localhost HTTP origin for artifact previews.
 *
 * Why an origin at all, when the files are right there on disk
 * ───────────────────────────────────────────────────────────
 * Previewing an artifact from `file://` fails three different ways:
 *
 *  1. SCRIPTS. A `file://` iframe can only run scripts if it is granted
 *     `allow-scripts`, and to also read its own directory it would need
 *     `allow-same-origin`. Those two together are the combination the HTML
 *     spec explicitly warns about — the framed page can reach up and remove
 *     its own sandbox attribute — and on a `file://` origin it additionally
 *     means agent-authored HTML can read the operator's filesystem. Over HTTP
 *     the page has a real URL, so `allow-scripts` alone is enough and the
 *     frame stays in an opaque origin it cannot escape.
 *
 *  2. ASSET PATHS. Generated pages routinely reference `/styles.css` or
 *     `/favicon/favicon-32x32.png`. Under `file://` a root-absolute path
 *     resolves to the filesystem root and 404s, so the preview renders
 *     unstyled. Serving each artifact from a document root makes those
 *     resolve the way they would in a browser.
 *
 *  3. SEEKING. `<video>` needs HTTP Range to scrub. Base64-ing a video
 *     through the existing `artifact:read` IPC cannot seek at all, and would
 *     move the whole file through the renderer's memory to do it.
 *
 * Scope
 * ─────
 * A token names one document root — the directory the artifact lives in.
 * Requests are resolved against that root and refused if they escape it, so
 * a preview can reach its own sibling assets and nothing else. Tokens are
 * random and per-session; the port is ephemeral and bound to 127.0.0.1 only,
 * so nothing here is reachable from off-machine.
 */

export interface ArtifactServer {
  url: string
  /** Register a file and get a URL that serves it, rooted at its directory. */
  serve(filePath: string): { url: string; token: string } | null
  close(): void
}

/** Roots are kept for the life of the process — a preview reopened later must
 *  still resolve. One entry per directory previewed; bounded by how many
 *  distinct artifacts the operator opens in a session. */
const roots = new Map<string, string>()
const tokensByRoot = new Map<string, string>()


export async function startArtifactServer(homeDir: string): Promise<ArtifactServer> {
  const realHome = (() => {
    try {
      return fs.realpathSync(homeDir)
    } catch {
      return homeDir
    }
  })()

  const server = http.createServer(async (req, res) => {
    const fail = (code: number, msg: string) => {
      res.writeHead(code, { 'Content-Type': 'text/plain; charset=utf-8' })
      res.end(msg)
    }

    try {
      if (!req.url) return fail(400, 'no url')
      // Parsed against a dummy origin purely to get pathname + decoding.
      const parsed = new URL(req.url, 'http://127.0.0.1')
      const parts = parsed.pathname.split('/').filter(Boolean)
      if (parts[0] !== 'a' || parts.length < 2) return fail(404, 'not found')

      const token = parts[1]
      const root = roots.get(token)
      if (!root) return fail(404, 'unknown artifact')

      const rel = parts.slice(2).map(decodeURIComponent).join('/')
      const target = path.resolve(root, rel || '')

      // Containment: the request must land inside the token's own root. This
      // is what stops `../../.ssh/id_rsa` — and, because `root` is itself
      // under the home directory, stops the whole class.
      if (target !== root && !target.startsWith(root + path.sep)) {
        return fail(403, 'outside artifact root')
      }
      if (!target.startsWith(realHome + path.sep)) {
        return fail(403, 'outside home directory')
      }

      let stat: fs.Stats
      try {
        stat = await fsp.stat(target)
      } catch {
        return fail(404, 'not found')
      }
      if (!stat.isFile()) return fail(404, 'not a file')

      const type = mimeTypeForPath(target)
      const range = req.headers.range

      if (range && isStreamableType(type)) {
        // Byte-range: this is what makes a <video> scrubbable rather than a
        // play-from-the-start-only blob.
        const m = /^bytes=(\d*)-(\d*)$/.exec(range.trim())
        if (m) {
          const startRaw = m[1]
          const endRaw = m[2]
          let start: number
          let end: number
          if (startRaw === '') {
            // Suffix range: the last N bytes.
            const suffix = Number(endRaw || 0)
            start = Math.max(0, stat.size - suffix)
            end = stat.size - 1
          } else {
            start = Number(startRaw)
            end = endRaw === '' ? stat.size - 1 : Math.min(Number(endRaw), stat.size - 1)
          }
          if (!Number.isFinite(start) || !Number.isFinite(end) || start > end || start >= stat.size) {
            res.writeHead(416, { 'Content-Range': `bytes */${stat.size}` })
            return res.end()
          }
          res.writeHead(206, {
            'Content-Type': type,
            'Content-Length': String(end - start + 1),
            'Content-Range': `bytes ${start}-${end}/${stat.size}`,
            'Accept-Ranges': 'bytes',
            'Cache-Control': 'no-store',
          })
          if (req.method === 'HEAD') return res.end()
          return void fs.createReadStream(target, { start, end }).pipe(res)
        }
      }

      res.writeHead(200, {
        'Content-Type': type,
        'Content-Length': String(stat.size),
        // Artifacts are edited in place, so a cached copy goes stale the
        // moment the operator saves.
        'Cache-Control': 'no-store',
        ...(isStreamableType(type) ? { 'Accept-Ranges': 'bytes' } : {}),
      })
      if (req.method === 'HEAD') return res.end()
      fs.createReadStream(target).pipe(res)
    } catch (err) {
      try {
        fail(500, String(err))
      } catch {
        /* response already gone */
      }
    }
  })

  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve))
  const port = (server.address() as AddressInfo).port
  const base = `http://127.0.0.1:${port}`

  return {
    url: base,
    serve(filePath: string) {
      let real: string
      try {
        real = fs.realpathSync(path.resolve(filePath))
      } catch {
        return null
      }
      if (!real.startsWith(realHome + path.sep)) return null

      const root = path.dirname(real)
      let token = tokensByRoot.get(root)
      if (!token) {
        token = crypto.randomBytes(12).toString('hex')
        tokensByRoot.set(root, token)
        roots.set(token, root)
      }
      const rel = path.relative(root, real).split(path.sep).map(encodeURIComponent).join('/')
      return { url: `${base}/a/${token}/${rel}`, token }
    },
    close() {
      server.close()
    },
  }
}
