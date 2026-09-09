import type { McpCommandAction, McpServerRow } from '@shared/events'

/**
 * Pure helpers for the MCP v2 management surface — shared by the store
 * (slash grammar + event normalization), the components, and the
 * headless smoke test. No React, no window.
 */

export const MCP_ACTIONS: McpCommandAction[] = [
  'status',
  'enable',
  'disable',
  'reload',
  'login',
  'logout',
  'reauth',
  'add',
  'remove',
  'test',
  'tools',
  'catalog',
  'call',
  'approve',
]

/** Subcommands whose first positional token is a server name. */
const SERVER_FIRST: ReadonlySet<McpCommandAction> = new Set<McpCommandAction>([
  'enable',
  'disable',
  'login',
  'logout',
  'reauth',
  'remove',
  'test',
  'tools',
  'call',
  'approve',
])

/** Subcommands that refuse to run without a server. */
const SERVER_REQUIRED: ReadonlySet<McpCommandAction> = new Set<McpCommandAction>([
  'enable',
  'disable',
  'login',
  'logout',
  'reauth',
  'remove',
  'test',
  'tools',
  'call',
  'approve',
])

export const MCP_USAGE =
  'usage: /mcp <status|enable|disable|reload|login|logout|reauth|add|remove|test|tools|approve|catalog> [server] [args…]'

export type ParsedMcpCommand =
  | {
      ok: true
      action: McpCommandAction
      server?: string
      args: string[]
      /** `call <server> <tool> [json]` extras. */
      tool?: string
      arguments?: Record<string, unknown>
    }
  | { ok: false; usage: string }

/** Split on whitespace but keep double/single-quoted spans intact so
 *  `add "npx -y foo" --header "Authorization=Bearer x y"` survives. */
export function tokenizeMcpArgs(raw: string): string[] {
  const out: string[] = []
  const re = /"([^"]*)"|'([^']*)'|(\S+)/g
  let m: RegExpExecArray | null
  while ((m = re.exec(raw)) !== null) {
    out.push(m[1] ?? m[2] ?? m[3] ?? '')
  }
  return out.filter((t) => t.length > 0)
}

/**
 * `/mcp <action> [server] [args…]` → wire fields.
 *
 *  - `status` / `reload`: optional server, remaining tokens as args.
 *  - server-first actions (enable/disable/login/logout/reauth/remove/
 *    test/tools/call): `parts[1]` is the server (required), rest → args.
 *  - `add`: everything after the subcommand is args (`<url|cmd> [--name n]
 *    [--transport t] [--header K=V] [--enable]`); at least one token.
 *  - `catalog`: args default to `['list']`.
 *  - `call`: `<server> <tool> [json-object]` also fills tool/arguments.
 */
export function parseMcpSlash(raw: string | undefined): ParsedMcpCommand {
  const parts = tokenizeMcpArgs((raw ?? '').trim())
  const first = (parts[0] || 'status').toLowerCase()
  if (!(MCP_ACTIONS as string[]).includes(first)) {
    return { ok: false, usage: MCP_USAGE }
  }
  const action = first as McpCommandAction
  if (action === 'add') {
    const args = parts.slice(1)
    if (args.length === 0) {
      return {
        ok: false,
        usage: 'usage: /mcp add <url|command> [--name <n>] [--transport stdio|http|sse] [--header K=V] [--enable]',
      }
    }
    return { ok: true, action, args }
  }
  if (action === 'catalog') {
    const args = parts.slice(1)
    return { ok: true, action, args: args.length ? args : ['list'] }
  }
  if (SERVER_FIRST.has(action)) {
    const server = parts[1]
    if (!server && SERVER_REQUIRED.has(action)) {
      const tail =
        action === 'call'
          ? ' <tool> [json]'
          : action === 'reauth'
            ? ' [scopes…]'
            : ''
      return { ok: false, usage: `usage: /mcp ${action} <server>${tail}` }
    }
    const args = parts.slice(2)
    if (action === 'call') {
      const tool = args[0]
      if (!tool) return { ok: false, usage: 'usage: /mcp call <server> <tool> [json]' }
      let parsedArgs: Record<string, unknown> | undefined
      const jsonText = args.slice(1).join(' ').trim()
      if (jsonText) {
        try {
          const v = JSON.parse(jsonText)
          if (v && typeof v === 'object' && !Array.isArray(v)) parsedArgs = v
        } catch {
          // leave undefined; the bridge reports the parse error
        }
      }
      return { ok: true, action, server, args, tool, arguments: parsedArgs }
    }
    return { ok: true, action, server, args }
  }
  // status / reload
  const server = parts[1]
  return { ok: true, action, server, args: parts.slice(2) }
}

function snakeToCamel(key: string): string {
  return key.replace(/_([a-z0-9])/g, (_m, c: string) => c.toUpperCase())
}

/**
 * Accept a row in either snake_case (what the bridge emits today) or
 * camelCase (the v2 contract). Every key is exposed in camelCase; the
 * original keys are preserved too so nothing is lost.
 */
export function normalizeMcpRow(raw: Record<string, unknown>): McpServerRow {
  const out: Record<string, unknown> = { ...raw }
  for (const [k, v] of Object.entries(raw)) {
    if (k.includes('_')) {
      const camel = snakeToCamel(k)
      if (out[camel] === undefined) out[camel] = v
    }
  }
  const server = String(out.server ?? out.name ?? out.id ?? '')
  const state = String(out.state ?? out.status ?? 'unknown')
  const enabledRaw = out.enabled
  const needsAuthRaw = out.needsAuth
  return {
    ...out,
    server,
    state,
    enabled: typeof enabledRaw === 'boolean' ? enabledRaw : enabledRaw == null ? undefined : Boolean(enabledRaw),
    needsAuth:
      typeof needsAuthRaw === 'boolean'
        ? needsAuthRaw
        : state === 'needs-auth' || state === 'needs_auth'
          ? true
          : needsAuthRaw == null
            ? undefined
            : Boolean(needsAuthRaw),
    toolCount: typeof out.toolCount === 'number' ? out.toolCount : undefined,
    quarantined:
      typeof out.quarantined === 'number'
        ? out.quarantined
        : Array.isArray(out.quarantined)
          ? out.quarantined.length
          : undefined,
    transport: typeof out.transport === 'string' ? out.transport : undefined,
    transportInUse: typeof out.transportInUse === 'string' ? out.transportInUse : undefined,
    reason: typeof out.reason === 'string' && out.reason ? out.reason : undefined,
    lastError: typeof out.lastError === 'string' && out.lastError ? out.lastError : undefined,
    tokenExpiresAt:
      typeof out.tokenExpiresAt === 'number' || typeof out.tokenExpiresAt === 'string'
        ? out.tokenExpiresAt
        : undefined,
  }
}

/** Merge a list of rows into the existing list (upsert by server name,
 *  preserving order for known servers, appending new ones). */
export function upsertMcpRows(existing: McpServerRow[], incoming: McpServerRow[]): McpServerRow[] {
  const byName = new Map(existing.map((r) => [r.server, r]))
  for (const row of incoming) {
    if (!row.server) continue
    const prev = byName.get(row.server)
    byName.set(row.server, prev ? { ...prev, ...row } : row)
  }
  return Array.from(byName.values())
}

export type McpStateTone = 'ok' | 'accent' | 'warn' | 'danger' | 'muted'

export function mcpStateTone(state: string | undefined, needsAuth?: boolean): McpStateTone {
  const s = (state ?? '').toLowerCase()
  if (needsAuth || s === 'needs-auth' || s === 'needs_auth' || s === 'unauthorized') return 'warn'
  if (s === 'active' || s === 'connected' || s === 'ready' || s === 'ok') return 'ok'
  if (s === 'connecting' || s === 'starting' || s === 'reconnecting' || s === 'handshake') return 'accent'
  if (s === 'failed' || s === 'error' || s === 'dead' || s === 'crashed') return 'danger'
  if (s === 'parked' || s === 'backoff' || s === 'degraded' || s === 'quarantined') return 'warn'
  return 'muted'
}

/** Epoch seconds/ms or ISO string → ms, else null. */
export function toEpochMs(v: unknown): number | null {
  if (v == null) return null
  if (typeof v === 'number') {
    if (!Number.isFinite(v) || v <= 0) return null
    return v < 1e12 ? v * 1000 : v
  }
  if (typeof v === 'string') {
    const n = Number(v)
    if (Number.isFinite(n) && n > 0) return n < 1e12 ? n * 1000 : n
    const t = Date.parse(v)
    return Number.isFinite(t) ? t : null
  }
  return null
}

/** "in 2h 5m" / "expired 3m ago" — coarse, for token expiry columns. */
export function formatRelative(ms: number | null, now = Date.now()): string {
  if (ms == null) return '—'
  const delta = ms - now
  const abs = Math.abs(delta)
  const units: Array<[number, string]> = [
    [86_400_000, 'd'],
    [3_600_000, 'h'],
    [60_000, 'm'],
    [1000, 's'],
  ]
  const parts: string[] = []
  let rem = abs
  for (const [size, label] of units) {
    if (rem >= size && parts.length < 2) {
      const n = Math.floor(rem / size)
      parts.push(`${n}${label}`)
      rem -= n * size
    }
  }
  const text = parts.length ? parts.join(' ') : '0s'
  return delta >= 0 ? `in ${text}` : `expired ${text} ago`
}

/** Heuristic: multi-line, table-ish, or long → render as a block in the
 *  conversation instead of (only) a short toast. */
export function mcpMessageIsOneLiner(message: string): boolean {
  if (!message) return true
  if (message.includes('\n')) return false
  if (message.length > 140) return false
  return true
}

/** Messages that look like fixed-width tables get monospace treatment. */
export function mcpMessageLooksTabular(message: string): boolean {
  if (!message) return false
  if (/[│┃|]\s.*\s[│┃|]/.test(message)) return true
  if (/^[-─═]{3,}/m.test(message)) return true
  const lines = message.split('\n')
  if (lines.length >= 2) {
    // Two or more lines sharing runs of 2+ spaces at similar columns → aligned columns.
    const aligned = lines.filter((l) => /\S\s{2,}\S/.test(l)).length
    if (aligned >= Math.max(2, Math.floor(lines.length / 2))) return true
  }
  return false
}

/** Build the `args` array for `mcp_command add` from the Settings form. */
export function buildAddServerArgs(input: {
  target: string
  name?: string
  transport?: string
  headers?: string[]
  enable?: boolean
}): string[] {
  const args: string[] = [input.target.trim()]
  const name = (input.name ?? '').trim()
  if (name) args.push('--name', name)
  const transport = (input.transport ?? '').trim()
  if (transport && transport !== 'auto') args.push('--transport', transport)
  for (const h of input.headers ?? []) {
    const hv = h.trim()
    if (hv && hv.includes('=')) args.push('--header', hv)
  }
  if (input.enable) args.push('--enable')
  return args
}

/** Coerce a form field's raw string/boolean into the JSON-schema type. */
export function coerceElicitationValue(
  raw: string | boolean | undefined,
  type: string | string[] | undefined,
): unknown {
  const t = Array.isArray(type) ? type.find((x) => x !== 'null') ?? 'string' : type ?? 'string'
  if (t === 'boolean') return typeof raw === 'boolean' ? raw : raw === 'true' || raw === 'on'
  if (typeof raw !== 'string') return raw
  const s = raw.trim()
  if (t === 'integer') {
    if (s === '') return undefined
    const n = Number.parseInt(s, 10)
    return Number.isFinite(n) ? n : undefined
  }
  if (t === 'number') {
    if (s === '') return undefined
    const n = Number(s)
    return Number.isFinite(n) ? n : undefined
  }
  return s === '' ? undefined : s
}

/** Pluck a name-ish label from a lenient row (tools / catalog rows). */
export function rowLabel(row: Record<string, unknown>): string {
  return String(row.name ?? row.id ?? row.server ?? row.tool ?? row.title ?? '')
}

export function rowDescription(row: Record<string, unknown>): string {
  return String(row.description ?? row.summary ?? row.desc ?? '')
}
