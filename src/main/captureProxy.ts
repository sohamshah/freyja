import http from 'node:http'
import { AddressInfo } from 'node:net'
import { desktopCapturer, screen } from 'electron'

/**
 * Localhost HTTP proxy for screen capture.
 *
 * ────────────────────────────────────────────────────────────────────────
 *
 *  Why this exists: macOS Screen Recording TCC permission does NOT
 *  inherit via subprocess responsibility the way Accessibility does.
 *  Even if the user grants the Electron .app Screen Recording, the
 *  Python bridge subprocess it spawns still has effective SR=denied
 *  and `CGDisplayCreateImage` silently returns a privacy-filtered
 *  image (wallpaper + menu bar only, every other app's window
 *  redacted). Apple does this on purpose to prevent SR laundering
 *  through child processes.
 *
 *  Fix: capture from the Electron main process itself (which DOES have
 *  effective SR), and expose the capture to the Python subprocess
 *  over localhost HTTP. The Python wrapper detects the env var we set
 *  when spawning the bridge and transparently routes screenshot() calls
 *  through this endpoint when its own preflight check fails.
 *
 *  Endpoint: GET /capture?display_id=<n>&max_dim=<n>&format=jpeg&quality=75
 *    - display_id: optional numeric CGDirectDisplayID. Omit for main.
 *    - max_dim: optional long-edge cap in pixels. Omit for full res.
 *    - format: 'jpeg' (default) or 'png'.
 *    - quality: JPEG quality 1-100 (ignored for PNG).
 *
 *  Response body: raw image bytes. 200 on success, 4xx/5xx on error.
 *
 *  We bind to 127.0.0.1 exclusively and pick an ephemeral port so the
 *  endpoint is not reachable from off-machine. The URL is passed to
 *  the Python bridge via FREYJA_CAPTURE_URL env var.
 */

export interface CaptureProxy {
  url: string
  close(): void
}

export async function startCaptureProxy(): Promise<CaptureProxy> {
  const server = http.createServer(async (req, res) => {
    try {
      if (!req.url || !req.url.startsWith('/capture')) {
        res.writeHead(404, { 'Content-Type': 'text/plain' })
        res.end('not found')
        return
      }

      const url = new URL(req.url, 'http://127.0.0.1')
      const displayIdParam = url.searchParams.get('display_id')
      const maxDimParam = url.searchParams.get('max_dim')
      const format = (url.searchParams.get('format') || 'jpeg').toLowerCase()
      const quality = parseInt(url.searchParams.get('quality') || '75', 10)

      const displays = screen.getAllDisplays()
      let target = screen.getPrimaryDisplay()
      if (displayIdParam) {
        const id = parseInt(displayIdParam, 10)
        const found = displays.find((d) => d.id === id)
        if (found) target = found
      }

      // desktopCapturer returns thumbnails at the requested size.
      // We ask for the display's NATIVE pixel size so we get a
      // full-resolution frame, then downscale via NativeImage.resize
      // if the caller specified max_dim. This keeps the capture path
      // single-sourced and matches what the Rust path does.
      const nativePx = {
        width: target.size.width * target.scaleFactor,
        height: target.size.height * target.scaleFactor,
      }
      const sources = await desktopCapturer.getSources({
        types: ['screen'],
        thumbnailSize: nativePx,
      })

      // Match the source that corresponds to our target display.
      // display_id from Electron's screen API maps to source.display_id
      // as a string.
      const targetIdStr = String(target.id)
      let src = sources.find((s) => s.display_id === targetIdStr)
      if (!src) src = sources[0]
      if (!src) {
        res.writeHead(500, { 'Content-Type': 'text/plain' })
        res.end('no sources available')
        return
      }

      let img = src.thumbnail
      if (img.isEmpty()) {
        res.writeHead(500, { 'Content-Type': 'text/plain' })
        res.end('thumbnail empty — Screen Recording permission missing for this process')
        return
      }

      // Optional downscale.
      if (maxDimParam) {
        const maxDim = parseInt(maxDimParam, 10)
        const size = img.getSize()
        const longEdge = Math.max(size.width, size.height)
        if (longEdge > maxDim) {
          const scale = maxDim / longEdge
          img = img.resize({
            width: Math.round(size.width * scale),
            height: Math.round(size.height * scale),
            quality: 'better',
          })
        }
      }

      let bytes: Buffer
      let contentType: string
      if (format === 'png') {
        bytes = img.toPNG()
        contentType = 'image/png'
      } else {
        bytes = img.toJPEG(Math.max(1, Math.min(100, quality)))
        contentType = 'image/jpeg'
      }

      res.writeHead(200, {
        'Content-Type': contentType,
        'Content-Length': bytes.length,
        'Cache-Control': 'no-store',
      })
      res.end(bytes)
    } catch (err) {
      res.writeHead(500, { 'Content-Type': 'text/plain' })
      res.end(`error: ${String(err)}`)
    }
  })

  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(0, '127.0.0.1', () => resolve())
  })
  const addr = server.address() as AddressInfo
  const url = `http://127.0.0.1:${addr.port}`
  console.log(`[captureProxy] listening at ${url}`)

  return {
    url,
    close() {
      try {
        server.close()
      } catch {}
    },
  }
}
