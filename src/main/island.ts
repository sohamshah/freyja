import { BrowserWindow, screen, type Display } from 'electron'
import { IPC } from '../shared/events.js'
import type { IslandControl, IslandDigest, IslandGeometry } from '../shared/island.js'
import { GLASS_MATERIAL, type Glass } from './glass.js'

/** Every notched MacBook so far (14" and 16") has a 185pt notch. The setting
 *  `island.notchWidth` overrides it for anything Apple ships next. */
const DEFAULT_NOTCH_WIDTH = 185
/** A notched display has a menu bar at least this tall; plain Macs have 24–25. */
const NOTCHED_MENU_BAR_MIN = 30
/** The window is oversized and transparent: the island paints at its top
 *  centre and everything else is click-through, so the island can grow into
 *  its sheet without the window ever moving or resizing. */
const WINDOW_WIDTH = 760
const WINDOW_HEIGHT = 480

export interface IslandWindowOptions {
  preload: string
  load: { url: string } | { file: string }
  notchWidth?: number | null
  /** The backdrop-blur shim, when it built (src/main/glass.ts). */
  glass?: Glass | null
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
  private glass: Glass | null
  private handle: Buffer | null = null
  private dark = true

  constructor(private readonly opts: IslandWindowOptions) {
    this.notchWidth = opts.notchWidth ?? null
    this.glass = opts.glass ?? null
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
    // The glass: a masked blur view behind the page, sized by the page's
    // `shape` messages so it is exactly the drawn island and nothing else.
    if (this.glass) {
      try {
        this.handle = win.getNativeWindowHandle()
        if (!this.glass.attach(this.handle)) throw new Error('attach returned false')
        this.glass.setStyle(this.handle, this.dark, this.dark ? GLASS_MATERIAL.dark : GLASS_MATERIAL.light)
      } catch (err) {
        console.warn('[island] backdrop blur unavailable:', err)
        this.glass = null
        this.handle = null
      }
    }
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
      this.handle = null
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
    this.handle = null
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

  setNotchWidth(width: number | null): void {
    this.notchWidth = width
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
        break
      case 'hover':
        win.setIgnoreMouseEvents(!msg.over, { forward: true })
        break
      case 'focus':
        // A non-activating panel can become the key window without the
        // app coming forward, so typing lands here and Freyja stays back.
        win.focus()
        break
      case 'release':
        win.blur()
        break
      case 'shape':
        if (this.glass && this.handle) {
          this.glass.setShape(this.handle, msg.x, msg.y, msg.width, msg.height, msg.radiusTop, msg.radiusBottom)
        }
        break
      case 'theme':
        this.dark = msg.dark
        if (this.glass && this.handle) {
          this.glass.setStyle(this.handle, msg.dark, msg.dark ? GLASS_MATERIAL.dark : GLASS_MATERIAL.light)
        }
        break
    }
  }

  private readonly replace = (): void => {
    this.place()
  }

  private place(): void {
    const win = this.win
    if (!win || win.isDestroyed()) return
    const display = pickDisplay()
    this.geometry = geometryFor(display, this.notchWidth, this.glass !== null)
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

export function geometryFor(display: Display, notchWidth: number | null, glass = false): IslandGeometry {
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
    glass,
  }
}
