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
 *    - window_id: optional CGWindowID. Captures that one window instead of
 *      a display (appshots). display_id is ignored then.
 *    - w, h: with window_id, the window's size in points (CGWindowList
 *      bounds). The thumbnail is requested at that size times the scale of
 *      the display that holds the window, so a Retina window comes back at
 *      full resolution. Without them the request is 3840x2400 and the
 *      capturer fits the window into that box.
 *    - x, y: with window_id, the window's origin in points. Only used to
 *      find which display holds the window and so which scale factor to use.
 *    - max_dim: optional long-edge cap in pixels. Omit for full res.
 *    - format: 'jpeg' (default) or 'png'.
 *    - quality: JPEG quality 1-100 (ignored for PNG).
 *
 *  Response body: raw image bytes. 200 on success, 4xx/5xx on error. A
 *  window_id that is not among the capturable windows (closed or
 *  minimized) gets a 404 with a plain-text reason.
 *
 *  We bind to 127.0.0.1 exclusively and pick an ephemeral port so the
 *  endpoint is not reachable from off-machine. The URL is passed to
 *  the Python bridge via FREYJA_CAPTURE_URL env var.
 */

export interface CaptureProxy {
  url: string
  close(): void
}

/** Downscale to `maxDimParam` (long edge, pixels) when given, encode, and
 *  answer. Shared by the display and the window path. */
function sendImage(
  res: http.ServerResponse,
  source: Electron.NativeImage,
  maxDimParam: string | null,
  format: string,
  quality: number,
): void {
  let img = source
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
}

/** Scale factor of the display that holds the point (x, y), else 2, which
 *  is what every current Mac panel uses. */
function scaleAt(x: number | null, y: number | null): number {
  if (x === null || y === null) return 2
  try {
    return screen.getDisplayNearestPoint({ x: Math.round(x), y: Math.round(y) }).scaleFactor || 2
  } catch {
    return 2
  }
}

function numberParam(url: URL, key: string): number | null {
  const raw = url.searchParams.get(key)
  if (raw === null || raw === '') return null
  const n = Number(raw)
  return Number.isFinite(n) ? n : null
}

/** Capture one window. desktopCapturer names a window source
 *  `window:<CGWindowID>:<n>`, so the id Python got from CGWindowList finds
 *  its source directly. */
async function serveWindow(
  url: URL,
  windowIdParam: string,
  maxDimParam: string | null,
  format: string,
  quality: number,
  res: http.ServerResponse,
): Promise<void> {
  const windowId = parseInt(windowIdParam, 10)
  if (!Number.isFinite(windowId)) {
    res.writeHead(400, { 'Content-Type': 'text/plain' })
    res.end(`bad window_id: ${windowIdParam}`)
    return
  }
  const w = numberParam(url, 'w')
  const h = numberParam(url, 'h')
  const scale = scaleAt(numberParam(url, 'x'), numberParam(url, 'y'))
  const thumbnailSize =
    w !== null && h !== null && w > 0 && h > 0
      ? { width: Math.ceil(w * scale), height: Math.ceil(h * scale) }
      : { width: 3840, height: 2400 }

  const sources = await desktopCapturer.getSources({
    types: ['window'],
    thumbnailSize,
    fetchWindowIcons: false,
  })
  const prefix = `window:${windowId}:`
  const src = sources.find((s) => s.id === `${prefix}0`) ?? sources.find((s) => s.id.startsWith(prefix))
  if (!src) {
    res.writeHead(404, { 'Content-Type': 'text/plain' })
    res.end(
      `window ${windowId} is not among the ${sources.length} capturable windows ` +
        '(it may be closed or minimized)',
    )
    return
  }
  if (src.thumbnail.isEmpty()) {
    res.writeHead(500, { 'Content-Type': 'text/plain' })
    res.end('thumbnail empty — Screen Recording permission missing for this process')
    return
  }
  sendImage(res, src.thumbnail, maxDimParam, format, quality)
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

      const windowIdParam = url.searchParams.get('window_id')
      if (windowIdParam) {
        await serveWindow(url, windowIdParam, maxDimParam, format, quality, res)
        return
      }

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

      sendImage(res, img, maxDimParam, format, quality)
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
