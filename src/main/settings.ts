import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'

/**
 * Disk-backed user settings for the desktop app.
 *
 * Lives at `~/.freyja/settings.json`. One file, atomic read/write,
 * version-stamped so we can migrate painlessly later.
 *
 * Scope today: permission policy only. The shape is intentionally nested
 * under `permissions` so future sections (model defaults, appearance,
 * workspace path overrides) can sit alongside without a migration.
 */

export type PermissionTier = 'none' | 'low' | 'medium' | 'high' | 'yolo'

export interface ComputerControlSettings {
  enabled: boolean
  wizardState: 'never' | 'done' | 'rewizard'
  allowlist: string[]
  blocklist: string[]
  maxStepsDefault: number
  showScreenshotsInline: boolean
}

/** Mirrors `AppearanceTheme` in shared/events.ts. */
export type AppearanceTheme = 'system' | 'dark' | 'light'

export interface AppearanceSettings {
  theme: AppearanceTheme
}

/** Mirrors `IslandSettings` in shared/events.ts. */
export interface IslandSettings {
  enabled: boolean
  notchWidth: number | null
}

export interface DesktopSettings {
  version: 1
  permissions: {
    autoApprove: PermissionTier
  }
  computer: ComputerControlSettings
  appearance: AppearanceSettings
  island: IslandSettings
}

const SETTINGS_DIR = path.join(os.homedir(), '.freyja')
const SETTINGS_FILE = path.join(SETTINGS_DIR, 'settings.json')

const DEFAULT_COMPUTER_BLOCKLIST: string[] = [
  'com.agilebits.onepassword7',
  'com.agilebits.onepassword8',
  'com.apple.keychainaccess',
  'com.bitwarden.desktop',
  'com.lastpass.LastPass',
  'com.dashlane.mac',
]

export const DEFAULT_SETTINGS: DesktopSettings = {
  version: 1,
  permissions: {
    autoApprove: 'low',
  },
  computer: {
    enabled: false,
    wizardState: 'never',
    allowlist: [],
    blocklist: DEFAULT_COMPUTER_BLOCKLIST,
    maxStepsDefault: 60,
    showScreenshotsInline: true,
  },
  appearance: {
    theme: 'system',
  },
  island: {
    enabled: true,
    notchWidth: null,
  },
}

const APPEARANCE_THEMES: ReadonlySet<string> = new Set(['system', 'dark', 'light'])

/** A hand-edited settings.json may carry anything; fall back to `system`. */
export function normalizeAppearanceTheme(value: unknown): AppearanceTheme {
  return typeof value === 'string' && APPEARANCE_THEMES.has(value)
    ? (value as AppearanceTheme)
    : 'system'
}

function ensureDir(): void {
  if (!fs.existsSync(SETTINGS_DIR)) {
    fs.mkdirSync(SETTINGS_DIR, { recursive: true })
  }
}

/** Deep-merge utility -- preserves unknown fields the user may have hand-edited. */
function merge<T extends Record<string, any>>(base: T, patch: Partial<T>): T {
  const out: any = { ...base }
  for (const [k, v] of Object.entries(patch ?? {})) {
    if (v && typeof v === 'object' && !Array.isArray(v) && out[k] && typeof out[k] === 'object') {
      out[k] = merge(out[k], v as any)
    } else if (v !== undefined) {
      out[k] = v
    }
  }
  return out
}

export function loadSettings(): DesktopSettings {
  ensureDir()
  try {
    const raw = fs.readFileSync(SETTINGS_FILE, 'utf8')
    const parsed = JSON.parse(raw) as Partial<DesktopSettings>
    if (!parsed || parsed.version !== 1) return { ...DEFAULT_SETTINGS }
    return merge(DEFAULT_SETTINGS, parsed)
  } catch {
    return { ...DEFAULT_SETTINGS }
  }
}

export function saveSettings(patch: Partial<DesktopSettings>): DesktopSettings {
  ensureDir()
  const current = loadSettings()
  const next = merge(current, patch)
  const tmp = SETTINGS_FILE + '.tmp'
  fs.writeFileSync(tmp, JSON.stringify(next, null, 2), 'utf8')
  fs.renameSync(tmp, SETTINGS_FILE)
  return next
}
