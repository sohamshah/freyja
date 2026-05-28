/**
 * Main-process helpers for the gateway / Slack onboarding IPC.
 *
 * The renderer calls these via the IPC channels declared in
 * src/shared/events.ts. Most are thin shells around the existing
 * `freyja` Python CLI — we invoke it with -m bridge.gateway.cli so
 * the path doesn't depend on the user's $PATH layout (the bundled
 * Python venv ships with bridge.gateway as a module).
 */

import { execFile, spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { promisify } from 'node:util'
import type {
  GatewayStatus,
  SimpleResult,
  SlackManifestResult,
  SlackVerifyResult,
} from '../shared/events.js'

const execFileP = promisify(execFile)

const FREYJA_HOME = path.join(os.homedir(), '.freyja')
const ENV_FILE = path.join(FREYJA_HOME, '.env')
const GATEWAY_PID_FILE = path.join(FREYJA_HOME, '.gateway.pid')
const GATEWAY_LOG_FILE = path.join(FREYJA_HOME, 'logs', 'gateway.log')
const GATEWAY_ERR_FILE = path.join(FREYJA_HOME, 'logs', 'gateway.err')
const PLIST_PATH = path.join(
  os.homedir(),
  'Library',
  'LaunchAgents',
  'co.freyja.gateway.plist',
)

/** Resolve which Python interpreter + cli args to invoke.
 *
 *  Picks the first candidate whose interpreter can import our hard
 *  dependencies (slack_sdk + slack_bolt + bridge.gateway). Probing
 *  is necessary because dev trees often have BOTH a leftover
 *  ``python-bundle/`` (from a previous package experiment) AND a
 *  populated ``.venv/`` — and only one of them will actually have
 *  our deps installed. Picking the wrong one yields
 *  ModuleNotFoundError at the worst possible moment (verify step).
 *
 *  If no candidate satisfies the import check we fall back to the
 *  first one that exists at all — better to surface a clear runtime
 *  error from a real Python than silently use system python3. */
function resolvePythonCli(harnessRoot: string): { bin: string; baseArgs: string[]; freyjaBin: string | null } {
  const candidates = [
    // Dev: source-of-truth venv with all editable deps installed.
    path.join(harnessRoot, '.venv', 'bin', 'python'),
    path.resolve(__dirname, '..', '.venv', 'bin', 'python'),
    // Packaged: bundled python with deps baked into site-packages.
    path.join(harnessRoot, 'python-bundle', 'bin', 'python3'),
  ]
  const existing = candidates.filter((c) => fs.existsSync(c))
  // Probe each existing candidate — pick the first that has our deps.
  for (const c of existing) {
    if (_canImportSlackDeps(c, harnessRoot)) {
      const sibling = path.join(path.dirname(c), 'freyja')
      const freyjaBin = fs.existsSync(sibling) ? sibling : null
      return { bin: c, baseArgs: ['-m', 'bridge.gateway.cli'], freyjaBin }
    }
  }
  if (existing.length > 0) {
    // Best-effort fallback: a Python exists but lacks deps. Let the
    // caller's downstream error message surface the import failure.
    const c = existing[0]
    const sibling = path.join(path.dirname(c), 'freyja')
    const freyjaBin = fs.existsSync(sibling) ? sibling : null
    return { bin: c, baseArgs: ['-m', 'bridge.gateway.cli'], freyjaBin }
  }
  // Last resort: hope `python3` is on PATH.
  return { bin: 'python3', baseArgs: ['-m', 'bridge.gateway.cli'], freyjaBin: null }
}

/** Cheap import probe — synchronous, returns true if the interpreter
 *  has both slack_sdk and slack_bolt available. Cached by bin path
 *  to avoid re-spawning python on every IPC call.
 *
 *  Uses a *scrubbed* env (same vars as pythonSpawnEnv strips) so the
 *  probe matches the real spawn conditions and isn't fooled by an
 *  inherited PYTHONHOME. */
const _depProbeCache = new Map<string, boolean>()
const _PROBE_STRIP_VARS = [
  'PYTHONHOME', 'PYTHONPATH', 'PYTHONSTARTUP', 'PYTHONEXECUTABLE',
  'PYTHONNOUSERSITE', 'VIRTUAL_ENV', 'PYENV_VERSION', 'PYENV_DIR',
  'CONDA_PREFIX', 'CONDA_DEFAULT_ENV',
]
function _probeEnv(harnessRoot: string): NodeJS.ProcessEnv {
  const out: NodeJS.ProcessEnv = {}
  const strip = new Set(_PROBE_STRIP_VARS)
  for (const [k, v] of Object.entries(process.env)) {
    if (!strip.has(k) && v != null) out[k] = v
  }
  out.PYTHONPATH = harnessRoot
  return out
}
function _canImportSlackDeps(bin: string, harnessRoot: string): boolean {
  const cached = _depProbeCache.get(bin)
  if (cached !== undefined) return cached
  try {
    require('node:child_process').execFileSync(
      bin,
      ['-c', 'import slack_sdk, slack_bolt, bridge.gateway.cli'],
      { stdio: 'pipe', timeout: 4000, cwd: harnessRoot, env: _probeEnv(harnessRoot) },
    )
    _depProbeCache.set(bin, true)
    return true
  } catch {
    _depProbeCache.set(bin, false)
    return false
  }
}

interface GatewayContext {
  harnessRoot: string
}

let _ctx: GatewayContext | null = null

export function configureGatewayBridge(ctx: GatewayContext): void {
  _ctx = ctx
}

function ctx(): GatewayContext {
  if (!_ctx) throw new Error('gatewayBridge not configured — call configureGatewayBridge first')
  return _ctx
}

/** Build an environment for spawning Python subprocesses that won't
 *  trip over inherited `PYTHONHOME` / `VIRTUAL_ENV` / pyenv vars from
 *  Electron's launch context. Electron-on-macOS occasionally inherits
 *  a stale `PYTHONHOME` from its app bundle plist or a wrapping
 *  launcher, which makes the venv Python fail to locate even its own
 *  stdlib (`ModuleNotFoundError: No module named 'encodings'`). We
 *  whitelist a known-safe set of vars and let our own PYTHONPATH /
 *  FREYJA_HOME pass through.
 *
 *  Extras get merged in last so callers can pass tool-specific
 *  inputs like the Slack tokens. */
function pythonSpawnEnv(extras: Record<string, string> = {}): NodeJS.ProcessEnv {
  const harnessRoot = ctx().harnessRoot
  const { bin } = resolvePythonCli(harnessRoot)
  // Vars that interfere with a venv's stdlib discovery if set
  // incorrectly. We *always* strip these — the venv's pyvenv.cfg has
  // the right values baked in, and Electron Helper sometimes leaks a
  // stale PYTHONHOME from its app bundle that breaks stdlib import.
  const STRIP = new Set([
    'PYTHONHOME',
    'PYTHONPATH',           // we set our own below
    'PYTHONSTARTUP',
    'PYTHONEXECUTABLE',
    'PYTHONNOUSERSITE',
    'VIRTUAL_ENV',
    'PYENV_VERSION',
    'PYENV_DIR',
    'CONDA_PREFIX',
    'CONDA_DEFAULT_ENV',
  ])
  const cleaned: NodeJS.ProcessEnv = {}
  for (const [k, v] of Object.entries(process.env)) {
    if (STRIP.has(k)) continue
    if (v != null) cleaned[k] = v
  }
  cleaned.PYTHONPATH = harnessRoot
  cleaned.PYTHONUNBUFFERED = '1'
  cleaned.FREYJA_HOME = FREYJA_HOME
  // For the bundled python case we MUST set PYTHONHOME (the bundle
  // dir is the prefix). The venv case stays unset so pyvenv.cfg
  // discovery handles it.
  if (bin.includes('python-bundle')) {
    cleaned.PYTHONHOME = path.resolve(bin, '..', '..')
  }
  return { ...cleaned, ...extras }
}

/** Spawn the freyja CLI and capture stdout/stderr.
 *
 *  Setting cwd + PYTHONPATH explicitly so the subprocess can find the
 *  `bridge` package regardless of how Electron was launched (Finder
 *  drops most of the user's shell env, so relying on Node's inherited
 *  cwd is fragile in production builds). */
async function runCli(args: string[], timeoutMs = 30_000): Promise<{
  code: number
  stdout: string
  stderr: string
}> {
  const { bin, baseArgs } = resolvePythonCli(ctx().harnessRoot)
  const harnessRoot = ctx().harnessRoot
  try {
    const result = await execFileP(bin, [...baseArgs, ...args], {
      timeout: timeoutMs,
      maxBuffer: 16 * 1024 * 1024,
      cwd: harnessRoot,
      env: pythonSpawnEnv(),
    })
    return { code: 0, stdout: result.stdout, stderr: result.stderr }
  } catch (err: any) {
    return {
      code: err?.code ?? 1,
      stdout: err?.stdout ?? '',
      stderr: err?.stderr ?? String(err?.message ?? err),
    }
  }
}

/** Read a parsed .env into a plain object. */
function readEnvFile(): Record<string, string> {
  if (!fs.existsSync(ENV_FILE)) return {}
  const out: Record<string, string> = {}
  for (const raw of fs.readFileSync(ENV_FILE, 'utf8').split(/\r?\n/)) {
    const line = raw.trim()
    if (!line || line.startsWith('#') || !line.includes('=')) continue
    const eq = line.indexOf('=')
    let key = line.slice(0, eq).trim()
    if (key.startsWith('export ')) key = key.slice('export '.length).trim()
    let value = line.slice(eq + 1).trim()
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1)
    }
    out[key] = value
  }
  return out
}

function pidIsAlive(pid: number): boolean {
  try {
    process.kill(pid, 0)
    return true
  } catch (err: any) {
    return err?.code === 'EPERM'
  }
}

function readPidFile(): number | null {
  if (!fs.existsSync(GATEWAY_PID_FILE)) return null
  try {
    const raw = fs.readFileSync(GATEWAY_PID_FILE, 'utf8').trim()
    const pid = parseInt(raw, 10)
    if (!Number.isFinite(pid) || pid <= 0) return null
    return pidIsAlive(pid) ? pid : null
  } catch {
    return null
  }
}

// ── Public IPC handlers ──────────────────────────────────────────

export async function handleGatewayStatus(): Promise<GatewayStatus> {
  const { freyjaBin } = resolvePythonCli(ctx().harnessRoot)
  const env = readEnvFile()
  const slackConfigured = Boolean(
    env.SLACK_BOT_TOKEN && env.SLACK_APP_TOKEN,
  )

  // Workspace introspection: pull known team_ids from the allowlist
  // config on disk. This is the cheap signal — no subprocess, no
  // Slack API call. Until we wire a real "list active connections"
  // endpoint on the gateway daemon, the allowlist is the closest
  // proxy for "what workspaces is this install configured for".
  const workspaces: GatewayStatus['workspaces'] = []
  if (slackConfigured) {
    try {
      const cfg = await handleSlackGetConfig()
      if (cfg.ok && cfg.allowedByWorkspace) {
        for (const [teamId, allowlist] of Object.entries(cfg.allowedByWorkspace)) {
          workspaces.push({ teamId, allowlist })
        }
      }
    } catch {
      // best-effort — leave workspaces empty
    }
  }

  return {
    pid: readPidFile(),
    freyjaBin,
    plistInstalled: fs.existsSync(PLIST_PATH),
    plistPath: PLIST_PATH,
    logPath: GATEWAY_LOG_FILE,
    errPath: GATEWAY_ERR_FILE,
    slackConfigured,
    workspaces,
  }
}

export async function handleGatewayInstall(): Promise<SimpleResult> {
  if (os.platform() !== 'darwin') {
    return { ok: false, error: 'macOS-only — use `freyja gateway run` manually on Linux/Windows' }
  }
  const result = await runCli(['gateway', 'install'], 30_000)
  if (result.code !== 0) {
    return { ok: false, error: result.stderr || result.stdout || 'install failed' }
  }
  return { ok: true, message: result.stdout.trim() }
}

export async function handleGatewayUninstall(): Promise<SimpleResult> {
  const result = await runCli(['gateway', 'uninstall'])
  if (result.code !== 0) {
    return { ok: false, error: result.stderr || result.stdout }
  }
  return { ok: true, message: result.stdout.trim() }
}

export async function handleGatewayStart(): Promise<SimpleResult> {
  const result = await runCli(['gateway', 'start'])
  if (result.code !== 0) {
    return { ok: false, error: result.stderr || result.stdout }
  }
  return { ok: true, message: result.stdout.trim() }
}

export async function handleGatewayStop(): Promise<SimpleResult> {
  const result = await runCli(['gateway', 'stop'])
  if (result.code !== 0) {
    return { ok: false, error: result.stderr || result.stdout }
  }
  return { ok: true, message: result.stdout.trim() }
}

export async function handleSlackManifest(): Promise<SlackManifestResult> {
  const manifestPath = path.join(FREYJA_HOME, 'slack-manifest.json')
  // Try to regenerate via the CLI first so we pick up any code changes
  // to the manifest schema. Don't fail the IPC if the subprocess
  // misbehaves — fall through to reading whatever's already on disk.
  let subprocessError: string | null = null
  const writeResult = await runCli(['slack', 'manifest', '--write'])
  if (writeResult.code !== 0) {
    subprocessError = (writeResult.stderr || writeResult.stdout || '').trim()
  }
  try {
    const json = fs.readFileSync(manifestPath, 'utf8')
    return { ok: true, manifestJson: json, manifestPath }
  } catch (readErr: any) {
    // The file doesn't exist and we couldn't regenerate. Surface both
    // failure reasons so the wizard can show something useful.
    const readMsg = String(readErr?.message ?? readErr)
    const detail = subprocessError
      ? `${subprocessError} (file read also failed: ${readMsg})`
      : readMsg
    return { ok: false, error: detail }
  }
}

export async function handleSlackCopyManifest(): Promise<SimpleResult> {
  const manifestPath = path.join(FREYJA_HOME, 'slack-manifest.json')
  if (!fs.existsSync(manifestPath)) {
    return { ok: false, error: 'manifest not yet generated — call slackManifest first' }
  }
  if (os.platform() !== 'darwin') {
    return { ok: false, error: 'clipboard copy only supported on macOS in v1' }
  }
  const json = fs.readFileSync(manifestPath, 'utf8')
  const proc = spawn('pbcopy')
  proc.stdin.write(json)
  proc.stdin.end()
  return new Promise<SimpleResult>((resolve) => {
    proc.on('exit', (code) => {
      if (code === 0) resolve({ ok: true })
      else resolve({ ok: false, error: `pbcopy exited ${code}` })
    })
  })
}

/** Run a small Python one-liner that calls Slack's auth.test for both
 *  tokens and returns a structured result. Avoids reimplementing Slack
 *  signing in Node. */
export async function handleSlackVerifyTokens(
  botToken: string,
  appToken: string,
): Promise<SlackVerifyResult> {
  if (!botToken || !appToken) {
    return { ok: false, error: 'both bot and app tokens are required' }
  }
  if (!botToken.startsWith('xoxb-')) {
    return { ok: false, error: 'bot token must start with xoxb-' }
  }
  if (!appToken.startsWith('xapp-')) {
    return { ok: false, error: 'app token must start with xapp-' }
  }
  // Pass tokens via env to avoid leaking them into ps + shell history.
  const { bin } = resolvePythonCli(ctx().harnessRoot)
  const script = `
import asyncio, json, os, sys
from slack_sdk.web.async_client import AsyncWebClient
async def go():
    bot = os.environ['_SLACK_BOT']
    client = AsyncWebClient(token=bot)
    r = await client.auth_test()
    out = {
        'ok': bool(r.get('ok')),
        'botName': r.get('user'),
        'botUserId': r.get('user_id'),
        'teamId': r.get('team_id'),
        'teamName': r.get('team'),
    }
    if not r.get('ok'):
        out['error'] = r.get('error') or 'auth_test rejected'
    print(json.dumps(out))
asyncio.run(go())
`
  try {
    const result = await execFileP(bin, ['-c', script], {
      timeout: 15_000,
      cwd: ctx().harnessRoot,
      env: pythonSpawnEnv({ _SLACK_BOT: botToken, _SLACK_APP: appToken }),
    })
    const parsed = JSON.parse(result.stdout.trim()) as SlackVerifyResult
    return parsed
  } catch (err: any) {
    return { ok: false, error: err?.stderr || err?.message || String(err) }
  }
}

/** LLM provider keys that the gateway daemon needs to call models on
 *  the agent's behalf. The desktop process env has these loaded
 *  (Electron read them from the project .env at startup); we
 *  propagate any that are present into ``~/.freyja/.env`` so the
 *  launchd-spawned daemon — which doesn't inherit the desktop's env
 *  — can read them at startup via `_load_env_into_os_environ`.
 *
 *  Without this propagation, the daemon will respond to Slack
 *  messages with "ANTHROPIC_API_KEY is not set" because the plist
 *  no longer carries secrets (and shouldn't — it's world-readable). */
const LLM_PROVIDER_KEYS = [
  'ANTHROPIC_API_KEY',
  'OPENAI_API_KEY',
  'GOOGLE_API_KEY',
  'FIREWORKS_API_KEY',
  'CEREBRAS_API_KEY',
  'GROQ_API_KEY',
  'XAI_API_KEY',
  'GROK_API_KEY',
  'PARALLEL_API_KEY',
]

/** Keys that strongly indicate the user can drive a real conversation
 *  (frontier-tier providers). If both are missing, the daemon
 *  effectively can't reply to anything no matter what other keys are
 *  set — the wizard should hard-warn. */
const LLM_FRONTIER_KEYS = ['ANTHROPIC_API_KEY', 'OPENAI_API_KEY']

/** Probe which LLM provider keys the daemon will have at runtime.
 *
 *  "Present" means the key is non-empty in either:
 *    (a) the desktop's `process.env` (will be propagated to
 *        ~/.freyja/.env when the wizard saves tokens), OR
 *    (b) ~/.freyja/.env directly (someone added it by hand or it was
 *        propagated by a prior wizard run).
 *
 *  This is read-only — we never *write* anything, just report state.
 *  The wizard uses the result to warn the operator if launching from
 *  Finder (no shell env) means the daemon would have no LLM access. */
export async function handleLlmKeysProbe(): Promise<import('../shared/events.js').LlmKeysProbeResult> {
  try {
    // Read whatever's currently in ~/.freyja/.env so we don't false-warn
    // if the operator already populated keys by hand.
    let onDiskEnv: Record<string, string> = {}
    if (fs.existsSync(ENV_FILE)) {
      for (const raw of fs.readFileSync(ENV_FILE, 'utf8').split(/\r?\n/)) {
        const line = raw.trim()
        if (!line || line.startsWith('#') || !line.includes('=')) continue
        const eq = line.indexOf('=')
        let key = line.slice(0, eq).trim()
        if (key.startsWith('export ')) key = key.slice('export '.length).trim()
        let value = line.slice(eq + 1).trim()
        if (
          (value.startsWith('"') && value.endsWith('"')) ||
          (value.startsWith("'") && value.endsWith("'"))
        ) {
          value = value.slice(1, -1)
        }
        if (value) onDiskEnv[key] = value
      }
    }
    const present: string[] = []
    const missing: string[] = []
    for (const k of LLM_PROVIDER_KEYS) {
      const inProcess = (process.env[k] || '').length > 0
      const inDisk = (onDiskEnv[k] || '').length > 0
      if (inProcess || inDisk) present.push(k)
      else missing.push(k)
    }
    const hasFrontierKey = present.some((k) => LLM_FRONTIER_KEYS.includes(k))
    return { ok: true, present, missing, hasFrontierKey }
  } catch (err: any) {
    return {
      ok: false,
      present: [],
      missing: LLM_PROVIDER_KEYS,
      hasFrontierKey: false,
      error: String(err?.message ?? err),
    }
  }
}

export async function handleSlackSaveTokens(
  botToken: string,
  appToken: string,
): Promise<SimpleResult> {
  if (!botToken.startsWith('xoxb-') || !appToken.startsWith('xapp-')) {
    return { ok: false, error: 'token format invalid' }
  }
  const { bin } = resolvePythonCli(ctx().harnessRoot)
  // Build the full set of keys to write: Slack tokens (always) + any
  // LLM provider keys present in the desktop's process.env. We only
  // include keys that are actually set — never write empty values.
  const valuesToSave: Record<string, string> = {
    SLACK_BOT_TOKEN: botToken,
    SLACK_APP_TOKEN: appToken,
  }
  for (const k of LLM_PROVIDER_KEYS) {
    const v = process.env[k]
    if (v && v.length > 0) valuesToSave[k] = v
  }
  const script = `
import json, os
from bridge.gateway.setup.env_writer import save_env_values
payload = json.loads(os.environ['_PAYLOAD'])
save_env_values(payload)
print('ok')
`
  try {
    await execFileP(bin, ['-c', script], {
      timeout: 10_000,
      cwd: ctx().harnessRoot,
      env: pythonSpawnEnv({ _PAYLOAD: JSON.stringify(valuesToSave) }),
    })
    return { ok: true }
  } catch (err: any) {
    return { ok: false, error: err?.stderr || String(err?.message ?? err) }
  }
}

export async function handleSlackSetAllowlist(
  teamId: string,
  userIds: string[],
  enforce: boolean,
): Promise<SimpleResult> {
  const { bin } = resolvePythonCli(ctx().harnessRoot)
  const script = `
import json, os
from bridge.gateway.config import GatewayConfig, write_config
cfg = GatewayConfig.load()
cfg.slack.enforce_workspace_allowlist = json.loads(os.environ['_ENFORCE'])
team_id = os.environ['_TEAM'].strip()
ids = json.loads(os.environ['_IDS'])
if team_id:
    cfg.slack.allowed_user_ids[team_id] = ids
write_config(cfg)
print('ok')
`
  try {
    await execFileP(bin, ['-c', script], {
      timeout: 10_000,
      cwd: ctx().harnessRoot,
      env: pythonSpawnEnv({
        _TEAM: teamId,
        _IDS: JSON.stringify(userIds),
        _ENFORCE: JSON.stringify(enforce),
      }),
    })
    return { ok: true }
  } catch (err: any) {
    return { ok: false, error: err?.stderr || String(err?.message ?? err) }
  }
}

export async function handleSlackGetConfig(): Promise<{
  ok: boolean
  enforce?: boolean
  allowedByWorkspace?: Record<string, string[]>
  error?: string
}> {
  const { bin } = resolvePythonCli(ctx().harnessRoot)
  try {
    const result = await execFileP(bin, [
      '-c',
      `
import json
from bridge.gateway.config import GatewayConfig
cfg = GatewayConfig.load()
print(json.dumps({
  'enforce': cfg.slack.enforce_workspace_allowlist,
  'allowedByWorkspace': cfg.slack.allowed_user_ids,
}))
`,
    ], {
      timeout: 10_000,
      cwd: ctx().harnessRoot,
      env: pythonSpawnEnv(),
    })
    const parsed = JSON.parse(result.stdout.trim())
    return { ok: true, ...parsed }
  } catch (err: any) {
    return { ok: false, error: err?.stderr || String(err?.message ?? err) }
  }
}
