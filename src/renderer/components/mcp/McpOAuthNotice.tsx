import { useEffect, useState } from 'react'
import { useHarness, type McpPendingLogin } from '../../state/store'
import { formatRelative, toEpochMs } from '../../lib/mcp'
import { BTN, BTN_ACCENT, copyText, formatCountdown, openExternalUrl } from './mcpUi'

/** 1 Hz clock shared by countdown displays. */
export function useNowTicker(enabled = true, intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!enabled) return
    const id = setInterval(() => setNow(Date.now()), intervalMs)
    return () => clearInterval(id)
  }, [enabled, intervalMs])
  return now
}

/**
 * Persistent (non-auto-clearing) notice for an OAuth flow. While
 * `waiting` it says "Authorize <server> — waiting for browser consent
 * (expires in N s)" with Open link / Copy URL; once `mcp_oauth_result`
 * lands the store flips the record to ok/failed and this shows the
 * outcome until dismissed.
 */
export function McpOAuthNoticeCard({
  login,
  now,
  onDismiss,
}: {
  login: McpPendingLogin
  now: number
  onDismiss: () => void
}) {
  const [copied, setCopied] = useState(false)
  const remainingS = login.expiresAt ? Math.max(0, Math.round((login.expiresAt - now) / 1000)) : null
  const expired = remainingS === 0 && login.status === 'waiting'

  const tone =
    login.status === 'ok'
      ? 'text-ok ring-ok/30'
      : login.status === 'failed' || expired
        ? 'text-danger ring-danger/30'
        : 'text-warn ring-warn/30'

  return (
    <div
      className={`pointer-events-auto w-[380px] rounded-xl glass-strong px-4 py-3 shadow-2xl ring-1 ${tone}`}
      data-testid="mcp-oauth-notice"
      role="status"
    >
      <div className="flex items-center gap-2">
        <span className="font-mono text-[11px]">
          {login.status === 'ok' ? '●' : login.status === 'failed' || expired ? '✕' : '◔'}
        </span>
        <span className="label">
          {login.status === 'waiting' ? 'authorize' : login.status === 'ok' ? 'authorized' : 'authorization failed'}
        </span>
        <span className="font-mono text-[11px] text-fg-0">{login.server}</span>
        <button
          type="button"
          onClick={onDismiss}
          className="ml-auto font-mono text-[10px] uppercase tracking-[0.08em] text-fg-3 hover:text-fg-1"
          aria-label="dismiss"
        >
          dismiss
        </button>
      </div>

      {login.status === 'waiting' && (
        <>
          <div className="mt-1.5 text-[11px] leading-[1.5] text-fg-1">
            {expired
              ? 'The authorization link has expired. Run /mcp login again.'
              : login.opened
                ? 'The browser was opened — finish consent there, then come back here.'
                : 'Waiting for browser consent — open the link below to continue.'}
            {remainingS != null && !expired && (
              <span className="text-fg-2"> (expires in {formatCountdown(remainingS)})</span>
            )}
          </div>
          {login.url && (
            <div className="mt-2 flex items-center gap-2">
              <button type="button" className={BTN_ACCENT} onClick={() => openExternalUrl(login.url!)}>
                open link
              </button>
              <button
                type="button"
                className={BTN}
                onClick={() => {
                  void copyText(login.url!).then((ok) => {
                    setCopied(ok)
                    setTimeout(() => setCopied(false), 1200)
                  })
                }}
              >
                {copied ? '✓ copied' : 'copy url'}
              </button>
              {login.redirectUri && (
                <span className="ml-auto truncate font-mono text-[10px] text-fg-3" title={login.redirectUri}>
                  ↩ {login.redirectUri}
                </span>
              )}
            </div>
          )}
        </>
      )}

      {login.status === 'ok' && (
        <div className="mt-1.5 text-[11px] leading-[1.5] text-fg-1">
          Token stored.
          {login.tokenExpiresAt != null && (
            <span className="text-fg-2"> Expires {formatRelative(toEpochMs(login.tokenExpiresAt), now)}.</span>
          )}
          {login.scopes && login.scopes.length > 0 && (
            <div className="mt-1 flex flex-wrap gap-1">
              {login.scopes.map((s) => (
                <span key={s} className="rounded bg-white/[0.05] px-1.5 py-[1px] font-mono text-[10px] text-fg-2 ring-hairline">
                  {s}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {login.status === 'failed' && (
        <div className="mt-1.5 text-[11px] leading-[1.5] text-fg-1">
          <span className="text-danger">{login.error ?? 'authorization failed'}</span>
          <div className="mt-1 text-fg-2">
            Try <span className="font-mono">/mcp login {login.server}</span> again or check the server's OAuth settings.
          </div>
        </div>
      )}
    </div>
  )
}

/** Store-connected stack: one card per server with a pending/finished
 *  OAuth flow. Lives at the top-right of the shell, under the title bar. */
export function McpOAuthNotices() {
  const pendingLogins = useHarness((s) => s.mcp.pendingLogins)
  const dismiss = useHarness((s) => s.dismissMcpLogin)
  const logins = Object.values(pendingLogins).sort((a, b) => a.updatedAt - b.updatedAt)
  const anyWaiting = logins.some((l) => l.status === 'waiting' && l.expiresAt)
  const now = useNowTicker(anyWaiting)

  // Successful logins auto-clear after a grace period so the corner
  // doesn't accumulate green cards; failures + waiting stay put.
  useEffect(() => {
    const ok = logins.filter((l) => l.status === 'ok')
    if (ok.length === 0) return
    const timers = ok.map((l) =>
      setTimeout(() => {
        if (useHarness.getState().mcp.pendingLogins[l.server]?.status === 'ok') dismiss(l.server)
      }, Math.max(0, 15_000 - (Date.now() - l.updatedAt))),
    )
    return () => timers.forEach(clearTimeout)
  }, [logins.map((l) => `${l.server}:${l.status}:${l.updatedAt}`).join('|'), dismiss])

  if (logins.length === 0) return null
  return (
    <div className="pointer-events-none fixed right-4 top-[54px] z-30 flex flex-col gap-2">
      {logins.map((l) => (
        <McpOAuthNoticeCard key={l.server} login={l} now={now} onDismiss={() => dismiss(l.server)} />
      ))}
    </div>
  )
}
