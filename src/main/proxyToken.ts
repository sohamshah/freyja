import crypto from 'node:crypto'
import type http from 'node:http'

/**
 * Per-launch secret for the loopback proxies (captureProxy, inputProxy).
 *
 * The proxies listen on 127.0.0.1 only, but any local process can find
 * the port. Each proxy makes a random token when it starts. Electron main
 * puts it in the bridge's env next to the proxy's URL (FREYJA_CAPTURE_TOKEN,
 * FREYJA_INPUT_TOKEN), and freyja_native sends it back in this header. A
 * request without the right token gets a 401.
 */
export const PROXY_TOKEN_HEADER = 'x-freyja-token'

export function newProxyToken(): string {
  return crypto.randomBytes(32).toString('hex')
}

export function hasProxyToken(req: http.IncomingMessage, token: string): boolean {
  const sent = req.headers[PROXY_TOKEN_HEADER]
  if (typeof sent !== 'string') return false
  const a = Buffer.from(sent)
  const b = Buffer.from(token)
  return a.length === b.length && crypto.timingSafeEqual(a, b)
}
