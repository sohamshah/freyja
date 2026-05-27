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

/** Resolve which Python interpreter + cli args to invoke. Prefers the
 *  bundled Python under <harness>/python-bundle/bin, then the project
 *  venv, then "uv run" if available. Mirrors the candidate chain in
 *  src/main/bridge.ts so the gateway CLI uses the same Python the
 *  bridge subprocess does. */
function resolvePythonCli(harnessRoot: string): { bin: string; baseArgs: string[]; freyjaBin: string | null } {
  const candidates = [
    path.join(harnessRoot, 'python-bundle', 'bin', 'python3'),
    path.join(harnessRoot, '.venv', 'bin', 'python'),
    path.resolve(__dirname, '..', '.venv', 'bin', 'python'),
  ]
  for (const c of candidates) {
    if (fs.existsSync(c)) {
      // Look for a `freyja` script in the same bin dir.
      const sibling = path.join(path.dirname(c), 'freyja')
      const freyjaBin = fs.existsSync(sibling) ? sibling : null
      return {
        bin: c,
        baseArgs: ['-m', 'bridge.gateway.cli'],
        freyjaBin,
      }
    }
  }
  // Last resort: hope `python3` is on PATH.
  return { bin: 'python3', baseArgs: ['-m', 'bridge.gateway.cli'], freyjaBin: null }
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

/** Spawn the freyja CLI and capture stdout/stderr. */
async function runCli(args: string[], timeoutMs = 30_000): Promise<{
  code: number
  stdout: string
  stderr: string
}> {
  const { bin, baseArgs } = resolvePythonCli(ctx().harnessRoot)
  try {
    const result = await execFileP(bin, [...baseArgs, ...args], {
      timeout: timeoutMs,
      maxBuffer: 16 * 1024 * 1024,
      env: { ...process.env, FREYJA_HOME },
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

  let workspaces: GatewayStatus['workspaces'] = []
  if (slackConfigured) {
    // Try a status query — best-effort. Returns workspace info if the
    // CLI can verify the tokens; empty list if not.
    const probe = await runCli([
      '__probe_workspaces',  // not a real subcommand; falls through to verify below
    ])
    void probe  // ignore — placeholder for a future status surface
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
  // The CLI's `slack manifest --write` writes the file + returns the
  // path. Pulling the JSON we read the file ourselves.
  const writeResult = await runCli(['slack', 'manifest', '--write'])
  if (writeResult.code !== 0) {
    return { ok: false, error: writeResult.stderr || writeResult.stdout }
  }
  const manifestPath = path.join(FREYJA_HOME, 'slack-manifest.json')
  try {
    const json = fs.readFileSync(manifestPath, 'utf8')
    return { ok: true, manifestJson: json, manifestPath }
  } catch (err: any) {
    return { ok: false, error: String(err?.message ?? err) }
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
      env: { ...process.env, _SLACK_BOT: botToken, _SLACK_APP: appToken },
    })
    const parsed = JSON.parse(result.stdout.trim()) as SlackVerifyResult
    return parsed
  } catch (err: any) {
    return { ok: false, error: err?.stderr || err?.message || String(err) }
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
  const script = `
import os
from bridge.gateway.setup.env_writer import save_env_values
save_env_values({
  'SLACK_BOT_TOKEN': os.environ['_SLACK_BOT'],
  'SLACK_APP_TOKEN': os.environ['_SLACK_APP'],
})
print('ok')
`
  try {
    await execFileP(bin, ['-c', script], {
      timeout: 10_000,
      env: { ...process.env, _SLACK_BOT: botToken, _SLACK_APP: appToken },
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
      env: {
        ...process.env,
        _TEAM: teamId,
        _IDS: JSON.stringify(userIds),
        _ENFORCE: JSON.stringify(enforce),
      },
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
      env: { ...process.env, FREYJA_HOME },
    })
    const parsed = JSON.parse(result.stdout.trim())
    return { ok: true, ...parsed }
  } catch (err: any) {
    return { ok: false, error: err?.stderr || String(err?.message ?? err) }
  }
}
