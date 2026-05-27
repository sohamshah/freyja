import { app, BrowserWindow, dialog, globalShortcut, ipcMain, nativeImage, shell, nativeTheme } from 'electron'
import {
  configureGatewayBridge,
  handleGatewayInstall,
  handleGatewayStart,
  handleGatewayStatus,
  handleGatewayStop,
  handleGatewayUninstall,
  handleSlackCopyManifest,
  handleSlackGetConfig,
  handleSlackManifest,
  handleSlackSaveTokens,
  handleSlackSetAllowlist,
  handleSlackVerifyTokens,
} from './gatewayBridge.js'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { HarnessBridge } from './bridge.js'
import { startCaptureProxy, type CaptureProxy } from './captureProxy.js'
import { startInputProxy, type InputProxy } from './inputProxy.js'
import { IPC, type AppInfo, type BridgeCommand } from '../shared/events.js'
import {
  deleteSession as persistDeleteSession,
  exportSessionToFile as persistExportSession,
  listSessions as persistListSessions,
  loadSession as persistLoadSession,
  saveSessionIndex as persistSaveSessionIndex,
  saveSession as persistSaveSession,
  type PersistedSession,
  type PersistedSessionMeta,
} from './persistence.js'
import {
  loadSettings,
  saveSettings,
  type DesktopSettings,
} from './settings.js'

// Persistent crash sink for the Electron main process. Mirrors the
// bridge-side log so a hard crash of either the Python subprocess OR
// the Electron main process leaves a paper trail at
// ~/.freyja/main-events.jsonl. Without these handlers, an uncaught
// exception in main would tear down the app silently with no signal
// for post-mortem.
const MAIN_CRASH_LOG = path.join(os.homedir(), '.freyja', 'main-events.jsonl')
function logMainCrash(entry: Record<string, unknown>): void {
  try {
    fs.mkdirSync(path.dirname(MAIN_CRASH_LOG), { recursive: true })
    fs.appendFileSync(
      MAIN_CRASH_LOG,
      JSON.stringify({ _t: Date.now() / 1000, source: 'main', ...entry }) + '\n',
    )
  } catch {
    // never block app teardown on logging
  }
}
process.on('uncaughtException', (err) => {
  logMainCrash({
    event: { type: 'error', level: 'fatal', kind: 'uncaughtException' },
    message: err?.message ?? String(err),
    stack: err?.stack,
  })
  console.error('[main] uncaughtException:', err)
})
process.on('unhandledRejection', (reason) => {
  const err = reason as Error | undefined
  logMainCrash({
    event: { type: 'error', level: 'fatal', kind: 'unhandledRejection' },
    message: err?.message ?? String(reason),
    stack: err?.stack,
  })
  console.error('[main] unhandledRejection:', reason)
})
app.on('render-process-gone', (_event, _webContents, details) => {
  logMainCrash({
    event: { type: 'log', level: 'error', kind: 'render-process-gone' },
    reason: details.reason,
    exitCode: details.exitCode,
  })
  console.error('[main] render-process-gone:', details)
})
app.on('child-process-gone', (_event, details) => {
  logMainCrash({
    event: { type: 'log', level: 'warn', kind: 'child-process-gone' },
    type_: details.type,
    reason: details.reason,
    exitCode: details.exitCode,
    name: details.name,
  })
  console.error('[main] child-process-gone:', details)
})

// In dev we load vite at http://localhost:5179
// In prod we load the built renderer from dist-renderer/index.html
const isDev = !app.isPackaged && process.env.NODE_ENV !== 'production'
const DEV_URL = process.env.VITE_DEV_SERVER_URL || 'http://localhost:5179'

// When built to dist-main/main.cjs, __dirname is <freyja>/dist-main.
// ROOT here is the app root (package.json sits there).
const ROOT = path.resolve(__dirname, '..')

// Freyja is self-contained: engine/ and bridge/ live inside the app root.
// When packaged, the asar archive is at Resources/app.asar/, and Python
// can't exec scripts from inside it. The bridge is also copied to
// Resources/bridge/ via extraResources, so when packaged we use
// process.resourcesPath as the base. In dev ROOT works directly.
const HARNESS_ROOT = app.isPackaged
  ? (process.resourcesPath ?? ROOT)
  : ROOT

function resolveUserWorkspace(): string {
  const candidates = [
    process.env.FREYJA_WORKSPACE,
    process.env.INIT_CWD,
    process.cwd(),
    os.homedir(),
  ].filter((value): value is string => !!value && value.trim().length > 0)

  for (const candidate of candidates) {
    const expanded = candidate.startsWith('~')
      ? path.join(os.homedir(), candidate.slice(1))
      : candidate
    const resolved = path.resolve(expanded)
    if (resolved === path.parse(resolved).root) continue
    if (resolved.includes('.app/Contents/Resources')) continue
    if (resolved.endsWith('.asar')) continue
    try {
      if (!fs.existsSync(resolved) || !fs.statSync(resolved).isDirectory()) continue
    } catch {
      continue
    }
    return resolved
  }

  return os.homedir()
}

const USER_WORKSPACE = resolveUserWorkspace()

let mainWindow: BrowserWindow | null = null
let bridge: HarnessBridge | null = null
let captureProxy: CaptureProxy | null = null
let inputProxy: InputProxy | null = null
// Buffer bridge events that fire before the renderer is ready.
// The `ready` event (with the full model list) typically arrives in
// ~1-2 seconds, but the Electron window + React app takes 2-3s to
// mount its event listener. Without buffering, the renderer never
// sees the model capabilities and falls back to 3 hardcoded Claudes.
let rendererReady = false
const earlyEvents: any[] = []
const WINDOW_VIBRANCY =
  (process.env.FREYJA_VIBRANCY as Electron.BrowserWindowConstructorOptions['vibrancy'] | undefined) ??
  'fullscreen-ui'

function createWindow() {
  nativeTheme.themeSource = 'dark'

  mainWindow = new BrowserWindow({
    width: 1320,
    height: 860,
    minWidth: 960,
    minHeight: 600,
    show: false,
    titleBarStyle: 'hiddenInset',
    trafficLightPosition: { x: 16, y: 16 },
    // Transparent window + macOS vibrancy. Vibrancy is REQUIRED — without
    // it, CSS `backdrop-filter: blur()` has nothing to blur (on macOS the
    // desktop behind the window is NOT a CSS backdrop), so glass panels
    // read as either fully opaque or fully transparent with nothing in
    // between. Vibrancy paints a translucent macOS system material behind
    // the DOM that backdrop-filter can sample from. `fullscreen-ui` is
    // noticeably clearer than the old `under-window` material in dark mode.
    backgroundColor: '#00000000',
    transparent: true,
    vibrancy: WINDOW_VIBRANCY,
    visualEffectState: 'active',
    roundedCorners: true,
    hasShadow: true,
    webPreferences: {
      preload: path.resolve(ROOT, 'dist-preload', 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      webSecurity: !isDev,
    },
  })

  mainWindow.once('ready-to-show', () => {
    mainWindow?.show()
    mainWindow?.focus()
  })

  const withBackdrop = !!process.env.FREYJA_SCREENSHOT
  if (isDev) {
    mainWindow.loadURL(DEV_URL + (withBackdrop ? '?backdrop=1' : ''))
    mainWindow.webContents.openDevTools({ mode: 'detach' })
  } else {
    mainWindow.loadFile(path.join(ROOT, 'dist-renderer', 'index.html'), {
      query: withBackdrop ? { backdrop: '1' } : undefined,
    })
  }

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url).catch(() => {})
    return { action: 'deny' }
  })

  mainWindow.on('closed', () => {
    mainWindow = null
  })
}

function initBridge() {
  bridge = new HarnessBridge({
    harnessRoot: HARNESS_ROOT,
    workspace: USER_WORKSPACE,
    captureProxyUrl: captureProxy?.url,
    inputProxyUrl: inputProxy?.url,
    onEvent: (event) => {
      if (rendererReady) {
        mainWindow?.webContents.send(IPC.bridgeEvent, event)
      } else {
        // Buffer until the renderer calls rendererReady IPC
        earlyEvents.push(event)
      }
    },
  })
  bridge
    .start()
    .then(async () => {
      // Push the user's persisted permission policy down to the bridge
      // immediately after startup so their choice applies to the first
      // tool call.
      try {
        const settings = loadSettings()
        if (settings.permissions?.autoApprove) {
          await bridge?.sendCommand({
            type: 'set_permission_policy',
            autoApprove: settings.permissions.autoApprove,
          })
        }
        if (settings.computer?.enabled) {
          await bridge?.sendCommand({
            type: 'set_computer_enabled',
            enabled: true,
          })
        }
      } catch {}
    })
    .catch((err) => {
      console.error('[main] bridge start failed:', err)
    })
}

function setupIpc() {
  ipcMain.handle(IPC.appInfo, (): AppInfo => {
    return {
      version: app.getVersion(),
      electronVersion: process.versions.electron || '',
      platform: process.platform,
      workspace: USER_WORKSPACE,
      harnessRoot: HARNESS_ROOT,
    }
  })

  ipcMain.handle(IPC.getMode, () => {
    // The renderer calls this on mount. Flush any buffered events
    // (especially the `ready` event with the model list) that fired
    // before the renderer's event listener was registered.
    if (!rendererReady) {
      rendererReady = true
      for (const ev of earlyEvents) {
        mainWindow?.webContents.send(IPC.bridgeEvent, ev)
      }
      earlyEvents.length = 0
    }
    return bridge?.getMode() ?? 'error'
  })

  ipcMain.handle(IPC.sendCommand, async (_event, cmd: BridgeCommand) => {
    if (!bridge) return { ok: false, error: 'bridge not ready' }
    try {
      await bridge.sendCommand(cmd)
      return { ok: true }
    } catch (err) {
      return { ok: false, error: String(err) }
    }
  })

  ipcMain.handle(IPC.requestDemoBurst, async () => {
    bridge?.triggerDemoBurst()
    return { ok: true }
  })

  ipcMain.handle(IPC.restartBridge, async () => {
    // Kill + respawn the Python bridge subprocess so code changes
    // under bridge/ and engine/ pick up without needing a
    // full app quit/relaunch. The renderer's `ready` handler
    // re-hydrates the session state once the replacement bridge
    // emits its ready event.
    if (!bridge) return { ok: false, error: 'bridge not initialized' }
    try {
      await bridge.restart()
      return { ok: true }
    } catch (err) {
      return { ok: false, error: String(err) }
    }
  })

  ipcMain.handle(IPC.openExternal, async (_event, url: string) => {
    await shell.openExternal(url)
    return { ok: true }
  })

  // Read an artifact file from disk. Sandboxed: only allows paths under
  // ~/.freyja project/session artifacts or the user's home directory — NOT
  // arbitrary filesystem access. Returns text for text files, base64 for
  // binary (images, etc).
  ipcMain.handle(IPC.artifactRead, async (_event, filePath: string) => {
    try {
      const resolved = path.resolve(filePath)
      const home = os.homedir()
      // Permissive but sanity-checked — artifacts can live in ~/.freyja
      // or anywhere under the user's home (since agents write to workspace).
      if (!resolved.startsWith(home)) {
        return {
          ok: false,
          content: null,
          binary: null,
          mimeType: '',
          size: 0,
          error: 'Path outside user home directory',
        }
      }
      const stat = fs.statSync(resolved)
      if (!stat.isFile()) {
        return {
          ok: false,
          content: null,
          binary: null,
          mimeType: '',
          size: 0,
          error: 'Not a regular file',
        }
      }
      // 5MB cap — don't try to load giant files in the preview pane.
      const MAX = 5 * 1024 * 1024
      if (stat.size > MAX) {
        return {
          ok: false,
          content: null,
          binary: null,
          mimeType: '',
          size: stat.size,
          error: `File too large (${Math.round(stat.size / 1024 / 1024)}MB > 5MB)`,
        }
      }
      const ext = path.extname(resolved).toLowerCase().replace('.', '')
      const binaryExts = new Set([
        'png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico',
        'pdf', 'zip', 'tar', 'gz', 'mp3', 'mp4', 'mov', 'wav',
      ])
      const mimeMap: Record<string, string> = {
        md: 'text/markdown', markdown: 'text/markdown',
        html: 'text/html', htm: 'text/html',
        json: 'application/json', yaml: 'application/yaml', yml: 'application/yaml',
        toml: 'application/toml', csv: 'text/csv', tsv: 'text/tab-separated-values',
        xml: 'application/xml', svg: 'image/svg+xml',
        js: 'text/javascript', ts: 'text/typescript', tsx: 'text/typescript',
        jsx: 'text/javascript', py: 'text/x-python', rs: 'text/x-rust',
        go: 'text/x-go', java: 'text/x-java', c: 'text/x-c', h: 'text/x-c',
        cpp: 'text/x-c++', css: 'text/css', scss: 'text/scss',
        sh: 'text/x-shellscript', bash: 'text/x-shellscript', zsh: 'text/x-shellscript',
        sql: 'text/x-sql', png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg',
        gif: 'image/gif', webp: 'image/webp', bmp: 'image/bmp', ico: 'image/x-icon',
        txt: 'text/plain', log: 'text/plain',
      }
      const mimeType = mimeMap[ext] ?? 'text/plain'

      if (binaryExts.has(ext)) {
        const buf = fs.readFileSync(resolved)
        return {
          ok: true,
          content: null,
          binary: buf.toString('base64'),
          mimeType,
          size: stat.size,
        }
      }
      const content = fs.readFileSync(resolved, 'utf8')
      return {
        ok: true,
        content,
        binary: null,
        mimeType,
        size: stat.size,
      }
    } catch (err) {
      return {
        ok: false,
        content: null,
        binary: null,
        mimeType: '',
        size: 0,
        error: String(err),
      }
    }
  })

  // Write an artifact file — sandboxed the same way as read.
  ipcMain.handle(IPC.artifactWrite, async (_event, filePath: string, content: string) => {
    try {
      const resolved = path.resolve(filePath)
      const home = os.homedir()
      if (!resolved.startsWith(home)) {
        return { ok: false, error: 'Path outside user home directory' }
      }
      fs.writeFileSync(resolved, content, 'utf8')
      return { ok: true }
    } catch (err) {
      return { ok: false, error: String(err) }
    }
  })

  ipcMain.handle(IPC.sessionList, async () => {
    try {
      return { ok: true, sessions: persistListSessions() }
    } catch (err) {
      return { ok: false, error: String(err) }
    }
  })

  ipcMain.handle(IPC.sessionLoad, async (_event, id: string) => {
    try {
      return { ok: true, session: persistLoadSession(id) }
    } catch (err) {
      return { ok: false, error: String(err) }
    }
  })

  ipcMain.handle(IPC.sessionSave, async (_event, payload: PersistedSession) => {
    try {
      const start = Date.now()
      const bytes = await persistSaveSession(payload)
      const durationMs = Date.now() - start
      console.log(
        `[persistence] saved session ${payload.id} (${Math.round(bytes / 1024)}KB in ${durationMs}ms)`,
      )
      return { ok: true, bytes, durationMs }
    } catch (err) {
      return { ok: false, error: String(err) }
    }
  })

  ipcMain.handle(IPC.sessionIndexSave, async (_event, payload: PersistedSessionMeta[]) => {
    try {
      const start = Date.now()
      const bytes = await persistSaveSessionIndex(payload)
      const durationMs = Date.now() - start
      console.log(
        `[persistence] saved session index (${payload.length} rows, ${Math.round(bytes / 1024)}KB in ${durationMs}ms)`,
      )
      return { ok: true, bytes, durationMs }
    } catch (err) {
      return { ok: false, error: String(err) }
    }
  })

  ipcMain.handle(IPC.sessionDelete, async (_event, id: string) => {
    try {
      return { ok: persistDeleteSession(id) }
    } catch (err) {
      return { ok: false, error: String(err) }
    }
  })

  // Read the compaction telemetry JSONL written by the Python bridge.
  // The metrics dashboard calls this on open + on refresh to aggregate
  // across past sessions. We tail-read up to ~10k rows so the response
  // stays under a few hundred KB.
  ipcMain.handle(IPC.compactionMetrics, async () => {
    try {
      const fs = await import('node:fs/promises')
      const path = await import('node:path')
      const os = await import('node:os')
      const filePath = path.join(
        os.homedir(),
        '.freyja',
        'telemetry',
        'compaction.jsonl',
      )
      let raw: string
      try {
        raw = await fs.readFile(filePath, 'utf8')
      } catch (err: any) {
        if (err?.code === 'ENOENT') return { ok: true, rows: [] }
        throw err
      }
      const lines = raw.split('\n').filter((l) => l.trim().length > 0)
      // Tail-trim if huge.
      const tailed = lines.length > 10_000 ? lines.slice(-10_000) : lines
      const rows: unknown[] = []
      for (const line of tailed) {
        try {
          rows.push(JSON.parse(line))
        } catch {
          // skip malformed
        }
      }
      return { ok: true, rows }
    } catch (err) {
      return { ok: false, rows: [], error: String(err) }
    }
  })

  ipcMain.handle(IPC.sessionExport, async (_event, id: string) => {
    // Show a save dialog on the main process, then write the full
    // session JSON + a condensed .trace.txt sibling so the exported
    // bundle is easy to both reload and eyeball. Returns the final
    // paths so the renderer can show a "reveal in Finder" affordance.
    try {
      const focused = BrowserWindow.getFocusedWindow() ?? BrowserWindow.getAllWindows()[0]
      const defaultName = `${id}-${new Date().toISOString().replace(/[:.]/g, '-')}.json`
      const saveRes = await dialog.showSaveDialog(focused ?? undefined as any, {
        title: 'Export session trace',
        defaultPath: defaultName,
        filters: [{ name: 'JSON', extensions: ['json'] }],
      })
      if (saveRes.canceled || !saveRes.filePath) {
        return { ok: false, cancelled: true }
      }
      const res = persistExportSession(id, saveRes.filePath)
      if (!res.ok) return res
      // Reveal the exported file in Finder so the user doesn't have
      // to go hunting for it.
      shell.showItemInFolder(res.jsonPath)
      return res
    } catch (err) {
      return { ok: false, error: String(err) }
    }
  })

  ipcMain.handle(IPC.settingsGet, async () => {
    try {
      return { ok: true, settings: loadSettings() }
    } catch (err) {
      return { ok: false, error: String(err) }
    }
  })

  ipcMain.handle(IPC.settingsUpdate, async (_event, patch: Partial<DesktopSettings>) => {
    try {
      const next = saveSettings(patch)
      // Push the new permission policy down to the bridge so in-flight
      // sessions see it immediately without needing a restart.
      if (bridge && next.permissions?.autoApprove) {
        await bridge
          .sendCommand({
            type: 'set_permission_policy',
            autoApprove: next.permissions.autoApprove,
          })
          .catch(() => {})
      }
      // Same for computer control: if the toggle changed, tell the
      // bridge so it rebuilds each session's tool registry.
      if (bridge && typeof patch.computer?.enabled === 'boolean') {
        await bridge
          .sendCommand({
            type: 'set_computer_enabled',
            enabled: patch.computer.enabled,
          })
          .catch(() => {})
      }
      return { ok: true, settings: next }
    } catch (err) {
      return { ok: false, error: String(err) }
    }
  })

  // ── Gateway / Slack onboarding ─────────────────────────────────
  // Configure the helper module with our harness root so it can find
  // the bundled Python + the freyja CLI script.
  configureGatewayBridge({ harnessRoot: HARNESS_ROOT })

  ipcMain.handle(IPC.gatewayStatus, async () => handleGatewayStatus())
  ipcMain.handle(IPC.gatewayInstall, async () => handleGatewayInstall())
  ipcMain.handle(IPC.gatewayUninstall, async () => handleGatewayUninstall())
  ipcMain.handle(IPC.gatewayStart, async () => handleGatewayStart())
  ipcMain.handle(IPC.gatewayStop, async () => handleGatewayStop())

  ipcMain.handle(IPC.slackManifest, async () => handleSlackManifest())
  ipcMain.handle(IPC.slackCopyManifest, async () => handleSlackCopyManifest())
  ipcMain.handle(
    IPC.slackVerifyTokens,
    async (_e, botToken: string, appToken: string) =>
      handleSlackVerifyTokens(botToken, appToken),
  )
  ipcMain.handle(
    IPC.slackSaveTokens,
    async (_e, botToken: string, appToken: string) =>
      handleSlackSaveTokens(botToken, appToken),
  )
  ipcMain.handle(
    IPC.slackSetAllowlist,
    async (_e, teamId: string, userIds: string[], enforce: boolean) =>
      handleSlackSetAllowlist(teamId, userIds, enforce),
  )
  ipcMain.handle(IPC.slackGetConfig, async () => handleSlackGetConfig())
}

// Hidden flag for automated UI screenshot capture:
//   FREYJA_SCREENSHOT=/tmp/out.png electron .
// Launches the window, waits a bit, saves a PNG, quits.
const SCREENSHOT_PATH = process.env.FREYJA_SCREENSHOT || ''
const SCREENSHOT_DELAY_MS = Number(process.env.FREYJA_SCREENSHOT_DELAY || 5000)
const SCREENSHOT_BURST = process.env.FREYJA_SCREENSHOT_BURST === '1'

app.whenReady().then(async () => {
  setupIpc()
  // Override the dock icon in dev runs (npm run dev) so developers see
  // the new topographic mark instead of Electron's default circle.
  // Packaged builds pick up icon.icns automatically via electron-builder,
  // so this is only necessary for unpacked / dev runs.
  if (process.platform === 'darwin' && !app.isPackaged && app.dock) {
    try {
      const dockIcon = nativeImage.createFromPath(
        path.resolve(ROOT, 'assets', 'icon.png'),
      )
      if (!dockIcon.isEmpty()) app.dock.setIcon(dockIcon)
    } catch (err) {
      console.warn('[main] failed to set dev dock icon:', err)
    }
  }
  // Start the capture proxy FIRST so we have the URL to thread into
  // the bridge spawn env. Failing to start the proxy is non-fatal —
  // the Python side will fall back to its native capture path.
  try {
    captureProxy = await startCaptureProxy()
  } catch (err) {
    console.warn('[main] capture proxy failed to start:', err)
  }
  try {
    inputProxy = await startInputProxy(HARNESS_ROOT)
  } catch (err) {
    console.warn('[main] input proxy failed to start:', err)
  }
  initBridge()
  createWindow()

  // Global emergency stop — works even when the app isn't focused.
  // Uses Electron's globalShortcut so the OS routes Cmd+Shift+Esc to us no
  // matter what the user is interacting with.
  try {
    globalShortcut.register('CommandOrControl+Shift+Escape', () => {
      console.warn('[main] Cmd+Shift+Esc pressed — firing emergency stop')
      bridge
        ?.sendCommand({ type: 'computer.emergency_stop', reason: 'global-cmd-shift-esc' })
        .catch(() => {})
    })
  } catch (err) {
    console.warn('[main] failed to register Cmd+Shift+Esc:', err)
  }
  // Cmd+Shift+K — open the computer-use hotkey overlay. We ship this as a
  // renderer-side event; the main window listens for it and shows the
  // overlay component.
  try {
    globalShortcut.register('CommandOrControl+Shift+K', () => {
      mainWindow?.webContents.send(IPC.bridgeEvent, {
        type: 'system_event',
        subtype: 'open_computer_hotkey',
        message: 'Open computer-use hotkey overlay',
      })
      mainWindow?.show()
      mainWindow?.focus()
    })
  } catch (err) {
    console.warn('[main] failed to register Cmd+Shift+K:', err)
  }

  if (SCREENSHOT_PATH && mainWindow) {
    const w = mainWindow
    w.webContents.once('did-finish-load', async () => {
      try {
        // The URL was loaded with ?backdrop=1, so the faux desktop is
        // already painted behind the app for screenshot capture.

        if (SCREENSHOT_BURST) {
          setTimeout(() => {
            const prompt = process.env.FREYJA_SCREENSHOT_PROMPT ||
              'Map the architecture of this codebase. Show 3 tools I have access to as a markdown table with columns Name, Description, Tier. Then list 3 ways the harness could be extended. Be concise.'
            const js = `(() => {
              const store = window.__harnessStore?.getState?.();
              if (store && store.mode === 'live') {
                store.sendMessage(${JSON.stringify(prompt)});
              } else if (window.__harnessDemo) {
                window.__harnessDemo.burst();
              }
            })()`
            w.webContents.executeJavaScript(js, true).catch(() => {})
          }, 1500)
        }
        await new Promise((r) => setTimeout(r, SCREENSHOT_DELAY_MS))

        // For transparent glass windows, webContents.capturePage() composites
        // against black and the vibrancy material is lost. We use macOS
        // screencapture(1) against the live window id to get the real
        // composited pixels. We fall back to capturePage if screencapture
        // fails (e.g. missing screen recording permission).
        let captured = false
        if (process.platform === 'darwin') {
          try {
            const nativeId = String(w.getNativeWindowHandle().readUInt32LE(0))
            const { spawnSync } = await import('node:child_process')
            // Use the window id (not contentBounds) so we capture with all
            // the native shadow/rounded corners and transparency intact.
            const result = spawnSync(
              'screencapture',
              ['-x', '-o', '-t', 'png', `-l${nativeId}`, SCREENSHOT_PATH],
              { stdio: 'pipe' },
            )
            if (result.status === 0) {
              captured = true
              console.log(
                `[screenshot] saved ${SCREENSHOT_PATH} via screencapture (window ${nativeId})`,
              )
            } else {
              const stderr = result.stderr?.toString() || ''
              console.warn(`[screenshot] screencapture failed: ${stderr || result.status}`)
              if (/could not create image|not authorized|permission/i.test(stderr)) {
                console.warn(
                  '[screenshot] Electron needs Screen Recording permission to produce',
                )
                console.warn(
                  '[screenshot] real glass screenshots. Grant it in:',
                )
                console.warn(
                  '[screenshot]   System Settings > Privacy & Security > Screen Recording',
                )
                console.warn(
                  '[screenshot] then add the "Electron" / "Freyja" app and relaunch.',
                )
                console.warn(
                  '[screenshot] Falling back to capturePage (composited against black).',
                )
              }
            }
          } catch (err) {
            console.warn('[screenshot] screencapture error:', err)
          }
        }
        if (!captured) {
          const img = await w.webContents.capturePage()
          const fs = await import('node:fs')
          fs.writeFileSync(SCREENSHOT_PATH, img.toPNG())
          console.log(`[screenshot] saved ${SCREENSHOT_PATH} via capturePage (transparent>black)`)
        }
      } catch (err) {
        console.error('[screenshot] failed:', err)
      } finally {
        app.quit()
      }
    })
  }

  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', () => {
  try {
    globalShortcut.unregisterAll()
  } catch {}
  bridge?.stop().catch(() => {})
  captureProxy?.close()
  inputProxy?.close()
})
