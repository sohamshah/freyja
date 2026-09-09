import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import type { McpCommandAction, McpServerRow } from '@shared/events'
import { useHarness, type McpCommandResultRecord } from '../../state/store'
import {
  buildAddServerArgs,
  formatRelative,
  mcpStateTone,
  rowDescription,
  rowLabel,
  toEpochMs,
} from '../../lib/mcp'
import { useNowTicker } from './McpOAuthNotice'
import { BTN, BTN_ACCENT, BTN_DANGER, INPUT, TONE_PILL, TONE_TEXT } from './mcpUi'

type SendFn = (action: McpCommandAction, opts?: { server?: string; args?: string[] }) => string

function isRemoteTransport(row: McpServerRow): boolean {
  const t = (row.transportInUse || row.transport || '').toLowerCase()
  return t !== '' && t !== 'stdio'
}

function pill(state: string, needsAuth?: boolean) {
  const tone = mcpStateTone(state, needsAuth)
  const label = needsAuth && state !== 'needs-auth' ? `${state} · needs-auth` : state
  return (
    <span className={`inline-block rounded px-1.5 py-[1px] font-mono text-[10px] uppercase tracking-[0.06em] ring-1 ${TONE_PILL[tone]}`}>
      {label}
    </span>
  )
}

/** Inline reply renderer: message as monospace block, optional rows as a
 *  compact list (tools / test report entries). */
export function McpInlineResult({
  result,
  onClose,
}: {
  result: McpCommandResultRecord
  onClose?: () => void
}) {
  const rows = result.rows ?? []
  return (
    <div
      className={`mt-1 rounded-md px-2.5 py-2 ring-1 ${result.ok ? 'bg-black/35 ring-white/10' : 'bg-danger/[0.06] ring-danger/30'}`}
      data-testid="mcp-inline-result"
    >
      <div className="mb-1 flex items-center gap-2">
        <span className={`font-mono text-[10px] uppercase tracking-[0.08em] ${result.ok ? 'text-accent' : 'text-danger'}`}>
          {result.action ?? 'result'} · {result.ok ? 'ok' : 'error'}
        </span>
        {onClose && (
          <button type="button" onClick={onClose} className="ml-auto font-mono text-[10px] uppercase text-fg-3 hover:text-fg-1">
            hide
          </button>
        )}
      </div>
      {result.message && (
        <pre className={`selectable whitespace-pre-wrap break-words font-mono text-[10.5px] leading-[1.5] ${result.ok ? 'text-fg-1' : 'text-danger'}`}>
          {result.message}
        </pre>
      )}
      {rows.length > 0 && (
        <ul className="mt-1.5 space-y-1">
          {rows.map((r, i) => {
            const name = rowLabel(r)
            const desc = rowDescription(r)
            const status = typeof r.ok === 'boolean' ? r.ok : typeof r.status === 'string' ? r.status : undefined
            return (
              <li key={`${name}-${i}`} className="flex items-baseline gap-2 font-mono text-[10.5px]">
                {status !== undefined && (
                  <span className={status === true || status === 'ok' || status === 'pass' ? 'text-ok' : 'text-danger'}>
                    {status === true || status === 'ok' || status === 'pass' ? '●' : '✕'}
                  </span>
                )}
                <span className="text-fg-0">{name || `row ${i + 1}`}</span>
                {desc && <span className="min-w-0 flex-1 truncate text-fg-2" title={desc}>{desc}</span>}
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

/** Inline "Add server" form → `mcp_command add` with free-token args. */
export function McpAddServerForm({
  onSubmit,
  onCancel,
  busy,
}: {
  onSubmit: (args: string[], name?: string) => void
  onCancel: () => void
  busy?: boolean
}) {
  const [target, setTarget] = useState('')
  const [name, setName] = useState('')
  const [transport, setTransport] = useState('auto')
  const [headers, setHeaders] = useState('')
  const [enable, setEnable] = useState(true)
  const valid = target.trim().length > 0
  return (
    <form
      className="mb-3 rounded-md bg-white/[0.025] p-3 ring-hairline"
      data-testid="mcp-add-form"
      onSubmit={(e) => {
        e.preventDefault()
        if (!valid) return
        onSubmit(
          buildAddServerArgs({
            target,
            name,
            transport,
            headers: headers.split('\n'),
            enable,
          }),
          name.trim() || undefined,
        )
      }}
    >
      <div className="mb-2 label text-fg-1">add server</div>
      <div className="grid grid-cols-[1fr_1fr] gap-2">
        <label className="col-span-2 block">
          <div className="mb-1 font-mono text-[10px] uppercase tracking-[0.06em] text-fg-2">url or command *</div>
          <input
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            placeholder="https://mcp.example.com/mcp  ·  npx -y @scope/server"
            className={INPUT}
            autoFocus
          />
        </label>
        <label className="block">
          <div className="mb-1 font-mono text-[10px] uppercase tracking-[0.06em] text-fg-2">name</div>
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder="(derived)" className={INPUT} />
        </label>
        <label className="block">
          <div className="mb-1 font-mono text-[10px] uppercase tracking-[0.06em] text-fg-2">transport</div>
          <select value={transport} onChange={(e) => setTransport(e.target.value)} className={INPUT}>
            <option value="auto">auto</option>
            <option value="stdio">stdio</option>
            <option value="http">http (streamable)</option>
            <option value="sse">sse</option>
          </select>
        </label>
        <label className="col-span-2 block">
          <div className="mb-1 font-mono text-[10px] uppercase tracking-[0.06em] text-fg-2">headers (K=V, one per line)</div>
          <textarea
            value={headers}
            onChange={(e) => setHeaders(e.target.value)}
            rows={2}
            placeholder="Authorization=Bearer ${MY_TOKEN}"
            className={`${INPUT} resize-y`}
          />
        </label>
      </div>
      <div className="mt-2 flex items-center gap-3">
        <label className="flex cursor-pointer items-center gap-1.5 font-mono text-[10.5px] text-fg-1">
          <input type="checkbox" checked={enable} onChange={(e) => setEnable(e.target.checked)} className="h-3 w-3 accent-accent" />
          enable immediately
        </label>
        <div className="ml-auto flex items-center gap-2">
          <button type="button" className={BTN} onClick={onCancel}>
            cancel
          </button>
          <button type="submit" className={BTN_ACCENT} disabled={!valid || busy}>
            add
          </button>
        </div>
      </div>
    </form>
  )
}

/** Catalog browser: list/search → rows with Install buttons. */
export function McpCatalogBrowser({
  result,
  onSearch,
  onInstall,
  onClose,
  loading,
}: {
  result: McpCommandResultRecord | undefined
  onSearch: (query: string) => void
  onInstall: (id: string, enable: boolean) => void
  onClose: () => void
  loading?: boolean
}) {
  const [query, setQuery] = useState('')
  const [enable, setEnable] = useState(true)
  const rows = useMemo(() => {
    if (!result) return []
    if (Array.isArray(result.rows) && result.rows.length) return result.rows
    const d = result.data as any
    if (d && typeof d === 'object') {
      for (const k of ['rows', 'entries', 'items', 'results', 'catalog']) {
        if (Array.isArray(d[k])) return d[k] as Array<Record<string, unknown>>
      }
    }
    if (Array.isArray(d)) return d as Array<Record<string, unknown>>
    return []
  }, [result])
  return (
    <div className="mb-3 rounded-md bg-white/[0.025] p-3 ring-hairline" data-testid="mcp-catalog">
      <div className="mb-2 flex items-center gap-2">
        <span className="label text-fg-1">catalog</span>
        <form
          className="ml-auto flex items-center gap-2"
          onSubmit={(e) => {
            e.preventDefault()
            onSearch(query.trim())
          }}
        >
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="search…"
            className={`${INPUT} w-[180px]`}
          />
          <button type="submit" className={BTN} disabled={loading}>
            {query.trim() ? 'search' : 'list'}
          </button>
        </form>
        <label className="flex cursor-pointer items-center gap-1.5 font-mono text-[10px] uppercase tracking-[0.06em] text-fg-2">
          <input type="checkbox" checked={enable} onChange={(e) => setEnable(e.target.checked)} className="h-3 w-3 accent-accent" />
          --enable
        </label>
        <button type="button" className={BTN} onClick={onClose}>
          close
        </button>
      </div>
      {loading && <div className="text-[11px] italic text-fg-3">loading catalog…</div>}
      {!loading && result && !result.ok && (
        <pre className="whitespace-pre-wrap font-mono text-[10.5px] text-danger">{result.message}</pre>
      )}
      {!loading && result && result.ok && rows.length === 0 && (
        <pre className="whitespace-pre-wrap font-mono text-[10.5px] text-fg-1">{result.message || 'no catalog entries'}</pre>
      )}
      {rows.length > 0 && (
        <ul className="max-h-[220px] space-y-1 overflow-y-auto">
          {rows.map((r, i) => {
            const id = String(r.id ?? r.name ?? r.server ?? '')
            const label = rowLabel(r)
            const desc = rowDescription(r)
            const transport = String(r.transport ?? (r.url ? 'http' : r.command ? 'stdio' : ''))
            const installed = r.installed === true
            return (
              <li key={`${id}-${i}`} className="flex items-center gap-2 rounded bg-black/30 px-2 py-1.5 ring-hairline">
                <div className="min-w-0 flex-1">
                  <div className="flex items-baseline gap-2">
                    <span className="font-mono text-[11px] text-fg-0">{label || id}</span>
                    {transport && <span className="font-mono text-[10px] text-fg-3">{transport}</span>}
                    {installed && <span className="font-mono text-[10px] text-ok">installed</span>}
                  </div>
                  {desc && <div className="truncate text-[10.5px] text-fg-2" title={desc}>{desc}</div>}
                </div>
                <button type="button" className={BTN_ACCENT} disabled={!id || installed} onClick={() => onInstall(id, enable)}>
                  install
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </div>
  )
}

interface RowUiState {
  expanded?: 'tools' | 'test' | 'result'
  requestId?: string
  confirmRemove?: boolean
}

/**
 * Server table with per-row actions. Pure w.r.t. the store: rows,
 * results, and `send` come in as props so it renders headlessly.
 */
export function McpServerTable({
  servers,
  results,
  send,
  now,
  onConsume,
}: {
  servers: McpServerRow[]
  results: Record<string, McpCommandResultRecord>
  send: SendFn
  now: number
  onConsume?: (requestId: string) => void
}) {
  const [ui, setUi] = useState<Record<string, RowUiState>>({})
  const patch = (server: string, p: Partial<RowUiState>) =>
    setUi((prev) => ({ ...prev, [server]: { ...prev[server], ...p } }))

  if (servers.length === 0) {
    return (
      <div className="rounded-md bg-white/[0.025] px-3 py-3 text-[11px] italic text-fg-3 ring-hairline" data-testid="mcp-empty">
        No MCP servers configured — add one below or edit ~/.freyja/mcp.json.
      </div>
    )
  }

  return (
    <div className="overflow-hidden rounded-md ring-hairline" data-testid="mcp-server-table">
      <table className="w-full table-fixed border-collapse text-left">
        <thead>
          <tr className="bg-white/[0.03] font-mono text-[10px] uppercase tracking-[0.06em] text-fg-2">
            <th className="w-[26%] px-2.5 py-1.5">server</th>
            <th className="w-[14%] px-2 py-1.5">transport</th>
            <th className="w-[22%] px-2 py-1.5">state</th>
            <th className="w-[10%] px-2 py-1.5 text-right">tools</th>
            <th className="w-[10%] px-2 py-1.5 text-right">quar.</th>
            <th className="w-[18%] px-2 py-1.5">token</th>
          </tr>
        </thead>
        <tbody>
          {servers.map((row) => {
            const st = ui[row.server] ?? {}
            const res = st.requestId ? results[st.requestId] : undefined
            const remote = isRemoteTransport(row)
            const needsAuth = !!row.needsAuth || row.state === 'needs-auth'
            const enabled = row.enabled !== false && row.state !== 'disabled'
            const tokenMs = toEpochMs(row.tokenExpiresAt)
            const tokenText = tokenMs ? formatRelative(tokenMs, now) : remote ? (needsAuth ? 'none' : '—') : '—'
            const tokenTone = tokenMs && tokenMs < now ? 'text-danger' : tokenMs && tokenMs - now < 3_600_000 ? 'text-warn' : 'text-fg-2'
            const transport = row.transportInUse && row.transportInUse !== row.transport
              ? `${row.transport ?? '?'}→${row.transportInUse}`
              : row.transportInUse || row.transport || '—'
            const fire = (action: McpCommandAction, expanded: RowUiState['expanded'], args?: string[]) => {
              const requestId = send(action, { server: row.server, args })
              patch(row.server, { requestId, expanded, confirmRemove: false })
            }
            return (
              <Fragment key={row.server}>
                <tr className="align-top hairline-t font-mono text-[11px] text-fg-1" data-testid={`mcp-row-${row.server}`}>
                  <td className="px-2.5 py-2">
                    <div className="truncate text-fg-0" title={row.server}>{row.server}</div>
                    {(row.reason || row.lastError) && (
                      <div className="mt-0.5 truncate text-[10px] text-fg-3" title={row.lastError || row.reason}>
                        {row.lastError || row.reason}
                      </div>
                    )}
                  </td>
                  <td className="px-2 py-2 text-fg-2">{transport}</td>
                  <td className="px-2 py-2">{pill(row.state, needsAuth)}</td>
                  <td className="px-2 py-2 text-right tabular-nums">{row.toolCount ?? '—'}</td>
                  <td className={`px-2 py-2 text-right tabular-nums ${row.quarantined ? 'text-warn' : ''}`}>{row.quarantined ?? 0}</td>
                  <td className={`px-2 py-2 text-[10.5px] ${tokenTone}`}>{tokenText}</td>
                </tr>
                <tr className="font-mono text-[10px]">
                  <td colSpan={6} className="px-2.5 pb-2">
                    <div className="flex flex-wrap items-center gap-1.5">
                      <button
                        type="button"
                        className={BTN}
                        onClick={() => fire(enabled ? 'disable' : 'enable', 'result')}
                        title={enabled ? 'disable (unregister tools)' : 'enable'}
                      >
                        {enabled ? 'disable' : 'enable'}
                      </button>
                      {remote && (
                        <button
                          type="button"
                          className={needsAuth ? BTN_ACCENT : BTN}
                          onClick={() => fire('login', 'result')}
                        >
                          login
                        </button>
                      )}
                      {remote && (
                        <button type="button" className={BTN} onClick={() => fire('reauth', 'result')}>
                          reauth
                        </button>
                      )}
                      <button type="button" className={BTN} onClick={() => fire('test', 'test')}>
                        test
                      </button>
                      <button
                        type="button"
                        className={BTN}
                        onClick={() => {
                          if (st.expanded === 'tools' && res) {
                            patch(row.server, { expanded: undefined })
                            return
                          }
                          fire('tools', 'tools')
                        }}
                      >
                        tools
                      </button>
                      {st.confirmRemove ? (
                        <span className="ml-auto flex items-center gap-1.5">
                          <span className="text-danger">remove {row.server}?</span>
                          <button type="button" className={BTN_DANGER} onClick={() => fire('remove', 'result')}>
                            yes, remove
                          </button>
                          <button type="button" className={BTN} onClick={() => patch(row.server, { confirmRemove: false })}>
                            no
                          </button>
                        </span>
                      ) : (
                        <button
                          type="button"
                          className={`${BTN} ml-auto text-danger/80 hover:text-danger`}
                          onClick={() => patch(row.server, { confirmRemove: true })}
                        >
                          remove
                        </button>
                      )}
                    </div>
                    {st.expanded && st.requestId && (
                      res ? (
                        <McpInlineResult
                          result={res}
                          onClose={() => {
                            patch(row.server, { expanded: undefined })
                            onConsume?.(st.requestId!)
                          }}
                        />
                      ) : (
                        <div className="mt-1 text-[10.5px] italic text-fg-3">waiting for bridge…</div>
                      )
                    )}
                  </td>
                </tr>
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/**
 * Settings → "MCP servers" section. Store-connected: reads the mcp slice
 * and issues panel-scoped requests (requestId prefixed `panel-`) so the
 * replies land in `mcp.lastResults` rather than the conversation.
 */
export function McpServersPanel() {
  const servers = useHarness((s) => s.mcp.servers)
  const results = useHarness((s) => s.mcp.lastResults)
  const statusAt = useHarness((s) => s.mcp.statusAt)
  const sendMcp = useHarness((s) => s.sendMcpCommand)
  const clearResult = useHarness((s) => s.clearMcpResult)
  const now = useNowTicker(true, 15_000)

  const send: SendFn = (action, opts) => sendMcp(action, { ...opts, surface: 'panel' })

  const [adding, setAdding] = useState(false)
  const [catalogOpen, setCatalogOpen] = useState(false)
  const [catalogReq, setCatalogReq] = useState<string | null>(null)
  const [topReq, setTopReq] = useState<string | null>(null)
  const requestedOnce = useRef(false)

  // Prime the table on first open.
  useEffect(() => {
    if (requestedOnce.current) return
    requestedOnce.current = true
    send('status')
  }, [])

  const topResult = topReq ? results[topReq] : undefined
  const catalogResult = catalogReq ? results[catalogReq] : undefined
  const catalogLoading = !!catalogReq && !catalogResult

  const active = servers.filter((r) => r.state === 'active').length
  const needsAuth = servers.filter((r) => r.needsAuth || r.state === 'needs-auth').length
  const quarantined = servers.reduce((n, r) => n + (r.quarantined ?? 0), 0)

  return (
    <div data-testid="mcp-servers-panel">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <div className="font-mono text-[10.5px] text-fg-2">
          <span className={active ? TONE_TEXT.ok : 'text-fg-3'}>{active} active</span>
          <span className="text-fg-4"> · </span>
          <span>{servers.length} configured</span>
          {needsAuth > 0 && (
            <>
              <span className="text-fg-4"> · </span>
              <span className={TONE_TEXT.warn}>{needsAuth} needs auth</span>
            </>
          )}
          {quarantined > 0 && (
            <>
              <span className="text-fg-4"> · </span>
              <span className={TONE_TEXT.warn}>{quarantined} quarantined</span>
            </>
          )}
          {statusAt > 0 && (
            <span className="text-fg-3"> · updated {formatRelative(statusAt - 1000, Math.max(now, statusAt)).replace('expired ', '')}</span>
          )}
        </div>
        <div className="ml-auto flex items-center gap-1.5">
          <button type="button" className={BTN} onClick={() => setTopReq(send('reload'))} title="re-read mcp.json and reconnect">
            reload
          </button>
          <button type="button" className={BTN} onClick={() => setTopReq(send('status'))}>
            refresh
          </button>
          <button
            type="button"
            className={adding ? BTN_ACCENT : BTN}
            onClick={() => {
              setAdding((v) => !v)
              setCatalogOpen(false)
            }}
          >
            add server
          </button>
          <button
            type="button"
            className={catalogOpen ? BTN_ACCENT : BTN}
            onClick={() => {
              const next = !catalogOpen
              setCatalogOpen(next)
              setAdding(false)
              if (next && !catalogReq) setCatalogReq(send('catalog', { args: ['list'] }))
            }}
          >
            catalog
          </button>
        </div>
      </div>

      {adding && (
        <McpAddServerForm
          onCancel={() => setAdding(false)}
          onSubmit={(args, name) => {
            setTopReq(send('add', { server: name, args }))
            setAdding(false)
          }}
        />
      )}

      {catalogOpen && (
        <McpCatalogBrowser
          result={catalogResult}
          loading={catalogLoading}
          onClose={() => setCatalogOpen(false)}
          onSearch={(q) => setCatalogReq(send('catalog', { args: q ? ['search', ...q.split(/\s+/)] : ['list'] }))}
          onInstall={(id, enable) => setTopReq(send('catalog', { args: ['install', id, ...(enable ? ['--enable'] : [])] }))}
        />
      )}

      {topReq && (
        topResult ? (
          <div className="mb-3">
            <McpInlineResult
              result={topResult}
              onClose={() => {
                clearResult(topReq)
                setTopReq(null)
              }}
            />
          </div>
        ) : (
          <div className="mb-3 text-[10.5px] italic text-fg-3">waiting for bridge…</div>
        )
      )}

      <McpServerTable servers={servers} results={results} send={send} now={now} onConsume={clearResult} />

      <div className="mt-2 font-mono text-[10px] text-fg-3">
        config: ~/.freyja/mcp.json · slash: /mcp &lt;status|enable|disable|reload|login|logout|reauth|add|remove|test|tools|catalog&gt; [server]
      </div>
    </div>
  )
}
