import { app, BrowserWindow, screen, type Display } from 'electron'
import { IPC } from '../shared/events.js'
import type { IslandControl, IslandDigest, IslandGeometry, IslandPaletteIndex } from '../shared/island.js'

/** Every notched MacBook so far (14" and 16") has a 185pt notch. The setting
 *  `island.notchWidth` overrides it for anything Apple ships next. */
const DEFAULT_NOTCH_WIDTH = 185
/** A notched display has a menu bar at least this tall; plain Macs have 24–25. */
const NOTCHED_MENU_BAR_MIN = 30
/** The window is oversized and transparent: the island paints at its top
 *  centre and everything else is click-through, so the island can grow into
 *  its sheet without the window ever moving or resizing. */
const WINDOW_WIDTH = 760
const WINDOW_HEIGHT = 600
/** How long after taking the keyboard the page is asked whether it really has it. */
const KEYBOARD_CHECK_MS = 250
/** A page that is repaired once is not repaired again this soon, so a stuck one cannot loop. */
const REPAIR_COOLDOWN_MS = 15_000

export interface IslandWindowOptions {
  preload: string
  load: { url: string } | { file: string }
  notchWidth?: number | null
  /** Called when taking the keyboard also activated the app: the caller sends the app's other windows back. */
  keepBack?: () => void
}

/**
 * The island window. A non-activating panel at screen-saver level, so it
 * draws over the menu bar (and the notch) and over full-screen apps, and
 * typing into it never brings Freyja forward. It never takes focus on its
 * own: it is shown inactive and ignores the mouse until the page reports
 * the pointer is over the drawn island.
 */
export class IslandWindow {
  private win: BrowserWindow | null = null
  private digest: IslandDigest | null = null
  private geometry: IslandGeometry | null = null
  private pageReady = false
  private notchWidth: number | null
  private hotkey: string | null = null
  private repairedAt = 0
  /** The page is reloading to repair its keyboard; reopen the sheet once it is back. */
  private reopenAfterReady = false

  constructor(private readonly opts: IslandWindowOptions) {
    this.notchWidth = opts.notchWidth ?? null
  }

  get isOpen(): boolean {
    return this.win !== null && !this.win.isDestroyed()
  }

  open(): void {
    if (this.isOpen) return
    const win = new BrowserWindow({
      width: WINDOW_WIDTH,
      height: WINDOW_HEIGHT,
      show: false,
      frame: false,
      transparent: true,
      backgroundColor: '#00000000',
      hasShadow: false,
      alwaysOnTop: true,
      resizable: false,
      movable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      skipTaskbar: true,
      hiddenInMissionControl: true,
      roundedCorners: false,
      acceptFirstMouse: true,
      type: 'panel',
      webPreferences: {
        preload: this.opts.preload,
        contextIsolation: true,
        nodeIntegration: false,
        sandbox: false,
        backgroundThrottling: false,
      },
    })
    this.win = win
    this.pageReady = false
    win.setAlwaysOnTop(true, 'screen-saver', 1)
    win.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true, skipTransformProcessType: true })
    try {
      win.setWindowButtonVisibility(false)
    } catch {
      /* not every window type has buttons */
    }
    win.setIgnoreMouseEvents(true, { forward: true })
    win.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
    // A reload (devtools, crash recovery) must wait for the page to say ready again.
    win.webContents.on('did-start-loading', () => {
      this.pageReady = false
    })
    win.on('closed', () => {
      this.win = null
      this.pageReady = false
    })
    screen.on('display-added', this.replace)
    screen.on('display-removed', this.replace)
    screen.on('display-metrics-changed', this.replace)
    this.place()
    if ('url' in this.opts.load) void win.loadURL(this.opts.load.url)
    else void win.loadFile(this.opts.load.file)
    win.once('ready-to-show', () => {
      if (!win.isDestroyed()) win.showInactive()
    })
  }

  close(): void {
    const win = this.win
    this.win = null
    this.pageReady = false
    screen.removeListener('display-added', this.replace)
    screen.removeListener('display-removed', this.replace)
    screen.removeListener('display-metrics-changed', this.replace)
    if (win && !win.isDestroyed()) win.destroy()
  }

  setDigest(digest: IslandDigest): void {
    this.digest = digest
    this.pushState()
    // Computer use captures the whole screen: step out of the shot.
    const win = this.win
    if (win && !win.isDestroyed()) {
      if (digest.veil && win.isVisible()) win.hide()
      else if (!digest.veil && !win.isVisible()) win.showInactive()
    }
  }

  /** The ⌘K rows the main renderer answered with; relayed to the page. */
  setPalette(index: IslandPaletteIndex): void {
    const win = this.win
    if (this.pageReady && win && !win.isDestroyed()) win.webContents.send(IPC.islandPalette, index)
  }

  setNotchWidth(width: number | null): void {
    this.notchWidth = width
    this.place()
  }

  /** The registered hotkey, as glyphs, for the page's legend. */
  setHotkey(label: string | null): void {
    this.hotkey = label
    this.place()
  }

  /** The hotkey: open the island if it is closed, otherwise ask the page to expand or collapse. */
  toggle(): void {
    if (!this.isOpen) {
      this.open()
      return
    }
    const win = this.win!
    if (!win.isVisible()) win.showInactive()
    win.webContents.send(IPC.islandToggle)
  }

  control(msg: IslandControl): void {
    const win = this.win
    if (!win || win.isDestroyed()) return
    switch (msg.type) {
      case 'ready':
        this.pageReady = true
        this.pushGeometry()
        this.pushState()
        if (this.reopenAfterReady) {
          this.reopenAfterReady = false
          win.webContents.send(IPC.islandToggle)
        }
        break
      case 'hover':
        win.setIgnoreMouseEvents(!msg.over, { forward: true })
        break
      case 'focus': {
        // A non-activating panel can become the key window without the
        // app coming forward, so typing lands here and Freyja stays back.
        // When the window server activates the app anyway, the caller
        // sends Freyja's other windows back so they do not cover the app
        // the person was in.
        const wasActive = app.isActive()
        win.focus()
        if (!wasActive && app.isActive()) this.opts.keepBack?.()
        this.verifyKeyboard(win)
        break
      }
      case 'release':
        win.blur()
        break
    }
  }

  private readonly replace = (): void => {
    this.place()
  }

  /**
   * The page asked for the keyboard and the window is the key window; make
   * sure the page was told. A panel that has lived for days can become key
   * with its first responder in place while its renderer is never told it has
   * focus (seen live on 2026-11-03: `isFocused()` true, the caret in the
   * field, `document.hasFocus()` false, every key swallowed between the
   * browser and the page). Nothing short of a reload mends it; a reload
   * does, and the sheet is reopened with the keyboard once the page is back.
   */
  private verifyKeyboard(win: BrowserWindow): void {
    const at = Date.now()
    setTimeout(() => {
      if (this.win !== win || win.isDestroyed() || !win.isFocused()) return
      win.webContents
        .executeJavaScript('document.hasFocus()', true)
        .then((has: unknown) => {
          if (has !== false || this.win !== win || win.isDestroyed() || !win.isFocused()) return
          if (at - this.repairedAt < REPAIR_COOLDOWN_MS) return
          this.repairedAt = at
          console.warn('[island] the page never received the keyboard; reloading it')
          this.reopenAfterReady = true
          win.webContents.reload()
        })
        .catch(() => {})
    }, KEYBOARD_CHECK_MS)
  }

  private place(): void {
    const win = this.win
    if (!win || win.isDestroyed()) return
    const display = pickDisplay()
    this.geometry = geometryFor(display, this.notchWidth, this.hotkey)
    const x = Math.round(display.bounds.x + (display.bounds.width - WINDOW_WIDTH) / 2)
    win.setBounds({ x, y: display.bounds.y, width: WINDOW_WIDTH, height: WINDOW_HEIGHT })
    this.pushGeometry()
  }

  private pushGeometry(): void {
    if (this.pageReady && this.win && !this.win.isDestroyed() && this.geometry) {
      this.win.webContents.send(IPC.islandGeometry, this.geometry)
    }
  }

  private pushState(): void {
    if (this.pageReady && this.win && !this.win.isDestroyed() && this.digest) {
      this.win.webContents.send(IPC.islandState, this.digest)
    }
  }
}

/** The built-in display when there is one (that is where the notch is), else the primary. */
function pickDisplay(): Display {
  return screen.getAllDisplays().find((d) => d.internal) ?? screen.getPrimaryDisplay()
}

export function geometryFor(display: Display, notchWidth: number | null, hotkey: string | null = null): IslandGeometry {
  const menuBar = Math.max(0, display.workArea.y - display.bounds.y)
  const notched = !!display.internal && menuBar >= NOTCHED_MENU_BAR_MIN
  return {
    displayId: display.id,
    notched,
    notchWidth: notched ? (notchWidth ?? DEFAULT_NOTCH_WIDTH) : 0,
    stripHeight: notched ? menuBar : Math.max(menuBar, 26),
    width: WINDOW_WIDTH,
    height: WINDOW_HEIGHT,
    scale: display.scaleFactor,
    hotkey,
  }
}

/** An Electron accelerator as the glyphs the menu bar would print: Control+Alt+Command+I → ⌃⌥⌘I. */
export function hotkeyGlyphs(accelerator: string | null): string | null {
  if (!accelerator) return null
  const glyph: Record<string, string> = {
    control: '⌃',
    ctrl: '⌃',
    alt: '⌥',
    option: '⌥',
    shift: '⇧',
    command: '⌘',
    cmd: '⌘',
    super: '⌘',
    meta: '⌘',
    commandorcontrol: '⌘',
    cmdorctrl: '⌘',
    space: '␣',
  }
  return accelerator
    .split('+')
    .map((part) => glyph[part.trim().toLowerCase()] ?? part.trim().toUpperCase())
    .join('')
}
