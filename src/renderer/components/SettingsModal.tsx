import { useEffect, useState } from 'react'
import { useHarness } from '../state/store'
import { useVoiceStore } from '../state/voice-store'
import { McpServersPanel } from './mcp/McpServersPanel'
import { useEffectiveTheme } from '../lib/theme'
import type { AppearanceTheme, PermissionTier, VoiceConfig } from '@shared/events'

interface AppearanceOption {
  theme: AppearanceTheme
  label: string
  hint: string
}

const APPEARANCE_OPTIONS: AppearanceOption[] = [
  { theme: 'system', label: 'System', hint: 'follows macOS' },
  { theme: 'dark', label: 'Dark', hint: 'frosted charcoal' },
  { theme: 'light', label: 'Light', hint: 'warm paper' },
]

interface PermissionOption {
  tier: PermissionTier
  label: string
  badge: string
  color: string
  description: string
  covers: string
}

const PERMISSION_OPTIONS: PermissionOption[] = [
  {
    tier: 'none',
    label: 'Prompt for everything',
    badge: 'STRICT',
    color: 'text-warn',
    description:
      'Every permission-gated tool call pops a prompt, even read-only commands like `ls` or `git status`. Best when you want full visibility into what the agent is doing.',
    covers: 'Auto-approves nothing',
  },
  {
    tier: 'low',
    label: 'Auto-approve safe commands',
    badge: 'DEFAULT',
    color: 'text-ok',
    description:
      'Read-only bash (`ls`, `cat`, `git status`, `pwd`, `which`, ...) runs without a prompt. Everything else still asks. Sensible default for most sessions.',
    covers: 'Auto-approves: LOW',
  },
  {
    tier: 'medium',
    label: 'Auto-approve routine commands',
    badge: 'RELAXED',
    color: 'text-accent',
    description:
      'Also auto-approves unknown commands that aren\'t obviously risky. You\'ll still be prompted for writes, network calls, package installs, etc.',
    covers: 'Auto-approves: LOW + MEDIUM',
  },
  {
    tier: 'high',
    label: 'Auto-approve everything except dangerous',
    badge: 'LOOSE',
    color: 'text-warn',
    description:
      'Auto-approves file modifications, network calls, and package installs. DANGEROUS commands (`rm -rf`, `git push --force`, `DROP TABLE`, `sudo`) still prompt.',
    covers: 'Auto-approves: LOW + MEDIUM + HIGH',
  },
  {
    tier: 'yolo',
    label: 'Auto-approve everything -- yolo',
    badge: 'DANGEROUS',
    color: 'text-danger',
    description:
      'No prompts, ever. Destructive commands run without confirmation. Only use in throwaway workspaces you can afford to lose.',
    covers: 'Auto-approves: LOW + MEDIUM + HIGH + DANGEROUS',
  },
]

export function SettingsModal() {
  const open = useHarness((s) => s.settingsOpen)
  const toggle = useHarness((s) => s.toggleSettings)
  const settings = useHarness((s) => s.settings)
  const setPermissionTier = useHarness((s) => s.setPermissionTier)
  const setAppearanceTheme = useHarness((s) => s.setAppearanceTheme)
  const effectiveTheme = useEffectiveTheme()
  const setComputerEnabled = useHarness((s) => s.setComputerEnabled)
  const voiceHotkey = useVoiceStore((s) => s.hotkeyLabel)
  const openWizard = useHarness((s) => s.openComputerWizard)

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        toggle(false)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, toggle])

  if (!open) return null

  const currentTier = settings.permissions.autoApprove

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center pt-[10vh]">
      <div className="absolute inset-0 scrim backdrop-blur-[2px]" onClick={() => toggle(false)} />
      <div className="relative w-[640px] max-h-[82vh] flex flex-col overflow-hidden rounded-2xl modal-opaque shadow-2xl ring-hairline-strong">
        <div className="flex items-center gap-3 px-5 py-4 hairline-b">
          <span className="label text-fg-0">settings</span>
          <span className="label text-fg-3">preferences for this machine</span>
          <button
            onClick={() => toggle(false)}
            className="ml-auto rounded bg-white/[0.05] px-2 py-[3px] font-mono text-[10px] uppercase tracking-[0.08em] text-fg-1 ring-hairline hover:bg-white/[0.08]"
          >
            close
          </button>
        </div>
        <div className="flex-1 overflow-y-auto px-5 py-5">
          <Section
            title="appearance"
            description="Dark is the original frosted-charcoal shell; light is the same glass on warm paper. System follows the macOS appearance, including its sunset schedule. ⌘⇧L flips between dark and light from anywhere."
          >
            <AppearancePicker
              value={settings.appearance?.theme ?? 'system'}
              effective={effectiveTheme}
              onChange={(theme) => void setAppearanceTheme(theme)}
            />
          </Section>

          <Section title="permissions + tool approvals" description="Controls when the agent can run permission-gated tools (bash, write_file, ...) without asking first.">
            <div className="space-y-1.5">
              {PERMISSION_OPTIONS.map((opt) => {
                const isActive = opt.tier === currentTier
                return (
                  <button
                    key={opt.tier}
                    onClick={() => setPermissionTier(opt.tier)}
                    className={`group flex w-full items-start gap-3 rounded-lg px-3 py-3 text-left transition-colors ${
                      isActive
                        ? 'bg-accent/15 ring-1 ring-accent/30'
                        : 'hover:bg-white/[0.035] ring-hairline'
                    }`}
                  >
                    <div className="mt-[3px] flex h-4 w-4 shrink-0 items-center justify-center">
                      {isActive ? (
                        <span className="text-accent">▸</span>
                      ) : (
                        <span className="text-fg-3">·</span>
                      )}
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-baseline gap-2">
                        <span
                          className={`font-mono text-[12px] ${isActive ? 'text-fg-0' : 'text-fg-1'}`}
                        >
                          {opt.label}
                        </span>
                        <span
                          className={`font-mono text-[9.5px] uppercase tracking-[0.08em] ${opt.color}`}
                        >
                          {opt.badge}
                        </span>
                      </div>
                      <div className="mt-[3px] text-[11.5px] leading-[1.55] text-fg-2">
                        {opt.description}
                      </div>
                      <div className="mt-1 font-mono text-[10px] uppercase tracking-[0.08em] text-fg-3">
                        {opt.covers}
                      </div>
                    </div>
                  </button>
                )
              })}
            </div>
          </Section>

          <Section
            title="computer control + drive the desktop"
            description="Let the agent take screenshots, click, type, and read the accessibility tree. Off by default. Requires Screen Recording + Accessibility permissions."
          >
            <div className="space-y-3">
              <button
                onClick={() => setComputerEnabled(!settings.computer.enabled)}
                className={`group flex w-full items-start gap-3 rounded-lg px-3 py-3 text-left transition-colors ${
                  settings.computer.enabled
                    ? 'bg-warn/10 ring-1 ring-warn/30'
                    : 'hover:bg-white/[0.035] ring-hairline'
                }`}
              >
                <div className="mt-[2px] flex h-4 w-4 shrink-0 items-center justify-center">
                  <div
                    className={`h-3 w-3 rounded-full ring-1 ${
                      settings.computer.enabled
                        ? 'bg-warn/80 ring-warn'
                        : 'bg-fg-3/30 ring-fg-3'
                    }`}
                  />
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="font-mono text-[12px] text-fg-0">
                      {settings.computer.enabled
                        ? 'Computer control is ENABLED'
                        : 'Computer control is DISABLED'}
                    </span>
                    <span
                      className={`font-mono text-[9.5px] uppercase tracking-[0.08em] ${
                        settings.computer.enabled ? 'text-warn' : 'text-fg-3'
                      }`}
                    >
                      {settings.computer.enabled ? 'ON' : 'OFF'}
                    </span>
                  </div>
                  <div className="mt-[3px] text-[11.5px] leading-[1.55] text-fg-2">
                    {settings.computer.enabled
                      ? 'The agent can drive the desktop. Emergency stop: triple-Esc, Cmd+Shift+Esc, or the floating red button.'
                      : 'Enable to unlock the computer_use tool plus atomic primitives (screenshot, click, type, read_ax_tree, ...).'}
                  </div>
                </div>
              </button>

              {settings.computer.enabled && (
                <>
                  <button
                    onClick={() => openWizard(true)}
                    className="w-full rounded-md bg-white/[0.04] px-3 py-2 text-left text-[11.5px] text-fg-1 ring-hairline hover:bg-white/[0.08]"
                  >
                    <div className="flex items-center gap-2">
                      <span className="font-mono text-accent">▸</span>
                      <span>Re-run permission wizard</span>
                      <span className="ml-auto font-mono text-[9.5px] text-fg-3">
                        {settings.computer.wizardState}
                      </span>
                    </div>
                  </button>

                  <div className="rounded-md bg-black/30 p-3 ring-hairline">
                    <div className="mb-2 label text-fg-2">blocklist (always refused)</div>
                    <div className="space-y-0.5 font-mono text-[10px] text-fg-2">
                      {settings.computer.blocklist.length === 0 ? (
                        <div className="italic text-fg-3">(empty)</div>
                      ) : (
                        settings.computer.blocklist.map((b) => (
                          <div key={b}>· {b}</div>
                        ))
                      )}
                    </div>
                    <div className="mt-2 text-[10px] text-fg-3">
                      Ships with password managers + Keychain Access blocked.
                      Edit via <span className="font-mono">~/.freyja/settings.json</span>.
                    </div>
                  </div>

                  <div className="rounded-md bg-black/30 p-3 ring-hairline">
                    <div className="mb-1 label text-fg-2">default max steps</div>
                    <div className="font-mono text-[12px] text-fg-0">
                      {settings.computer.maxStepsDefault}
                    </div>
                    <div className="mt-1 text-[10px] text-fg-3">
                      Cap on action count per <span className="font-mono">computer_use</span>{' '}
                      call. Safety net against runaway loops.
                    </div>
                  </div>
                </>
              )}
            </div>
          </Section>

          <Section
            title="voice · galdr"
            description={`Speak to the Mac. ${voiceHotkey ? `${voiceHotkey} opens an exchange` : 'Click the title-bar sigil to open an exchange'}; the mic is live only while the sigil is lit, and the session auto-closes after silence.`}
          >
            <VoiceSettings />
          </Section>

          <Section
            title="MCP servers"
            description="Model Context Protocol servers whose tools the agent can call. Enable/disable, sign in to OAuth-protected servers, run a connection test, or install from the catalog. Same actions as /mcp."
          >
            <McpServersPanel />
          </Section>

          <Section
            title="about"
            description="Stored on this machine only. No telemetry, no sync."
          >
            <div className="space-y-1 font-mono text-[10.5px] text-fg-2">
              <div>
                <span className="text-fg-3">settings file</span>{' '}
                ~/.freyja/settings.json
              </div>
              <div>
                <span className="text-fg-3">session archive</span>{' '}
                ~/.freyja/sessions/
              </div>
              <div>
                <span className="text-fg-3">env override</span>{' '}
                FREYJA_PERMISSION_AUTO=low|medium|high|yolo
              </div>
            </div>
          </Section>
        </div>
        <div className="hairline-t flex items-center justify-between bg-bg-1/40 px-5 py-2.5 font-mono text-[10px] text-fg-3">
          <div>
            current policy:{' '}
            <span className="text-fg-1">{currentTier}</span>
          </div>
          <div>
            <span>Cmd+,</span> toggle · <span>esc</span> close
          </div>
        </div>
      </div>
    </div>
  )
}

/**
 * Voice (Galdr) settings group. The bridge owns the file
 * (~/.freyja/voice/config.json) and echoes the merged config back as a
 * voice_config event — we patch the store optimistically so the toggle
 * doesn't lag a bridge round-trip, then let the echo settle it.
 */
function VoiceSettings() {
  const config = useVoiceStore((s) => s.config)
  const setConfigPatch = useVoiceStore((s) => s.setConfigPatch)
  // Idle timeout edits buffer locally so mid-typing values ("4" en
  // route to "40") aren't clamped and persisted; commit on blur/Enter.
  const [idleDraft, setIdleDraft] = useState<string | null>(null)

  if (!config) {
    return (
      <div className="text-[11px] italic text-fg-3">
        Voice service unavailable — bridge offline or still starting.
      </div>
    )
  }

  const patch = (p: Partial<Omit<VoiceConfig, 'available' | 'hasApiKey' | 'spotifySearch'>>) => {
    useVoiceStore.setState((s) => ({
      config: s.config ? { ...s.config, ...p } : s.config,
    }))
    setConfigPatch(p)
  }

  const commitIdle = () => {
    if (idleDraft === null) return
    const n = Math.round(Number(idleDraft))
    if (Number.isFinite(n)) {
      patch({ idleTimeoutSec: Math.max(10, Math.min(120, n)) })
    }
    setIdleDraft(null)
  }

  const setQuietHour = (which: 'start' | 'end', raw: string) => {
    const n = Math.round(Number(raw))
    if (!Number.isFinite(n)) return
    const h = Math.max(0, Math.min(23, n))
    patch({ quietHours: { ...config.quietHours, [which]: h } })
  }

  // The active value always appears in its dropdown even if the bridge's
  // availability probe didn't list it (config.model is a free string —
  // reserved seats for future Grok/Gemini realtime models).
  const models = config.available.models.includes(config.model)
    ? config.available.models
    : [config.model, ...config.available.models]
  const voices = config.available.voices.includes(config.voice)
    ? config.available.voices
    : [config.voice, ...config.available.voices]
  // GPT-Live (docs/GALDR-LIVE.md) is full duplex — no turn detection to
  // pick — and delegates reasoning + tools to a backend model instead.
  const live = config.model.startsWith('gpt-live')
  const liveBackend = config.liveBackend ?? 'gpt-6.1-sol'
  const backendOptions = config.available.backends ?? [liveBackend]
  const backends = backendOptions.includes(liveBackend)
    ? backendOptions
    : [liveBackend, ...backendOptions]

  const selectClass =
    'w-full rounded border border-white/[0.10] bg-black/[0.30] px-2 py-1.5 font-mono text-[11.5px] text-fg-0 focus:border-accent/[0.40] focus:outline-none'

  return (
    <div className="space-y-3">
      <button
        onClick={() => patch({ enabled: !config.enabled })}
        className={`group flex w-full items-start gap-3 rounded-lg px-3 py-3 text-left transition-colors ${
          config.enabled
            ? 'bg-accent/10 ring-1 ring-accent/30'
            : 'hover:bg-white/[0.035] ring-hairline'
        }`}
      >
        <div className="mt-[2px] flex h-4 w-4 shrink-0 items-center justify-center">
          <div
            className={`h-3 w-3 rounded-full ring-1 ${
              config.enabled ? 'bg-accent/80 ring-accent' : 'bg-fg-3/30 ring-fg-3'
            }`}
          />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[12px] text-fg-0">
              {config.enabled ? 'Voice is ENABLED' : 'Voice is DISABLED'}
            </span>
            <span
              className={`font-mono text-[9.5px] uppercase tracking-[0.08em] ${
                config.enabled ? 'text-accent' : 'text-fg-3'
              }`}
            >
              {config.enabled ? 'ON' : 'OFF'}
            </span>
          </div>
          <div className="mt-[3px] text-[11.5px] leading-[1.55] text-fg-2">
            {config.enabled
              ? '⌥Space or the title-bar sigil opens an exchange. Auto-closes after silence; every verb leaves a receipt.'
              : 'Enable to talk to the Mac — Spotify, volume, apps, timers, missions.'}
          </div>
        </div>
      </button>

      <div className="grid grid-cols-2 gap-3">
        <div>
          <div className="mb-1 label">model</div>
          <select
            value={config.model}
            onChange={(e) => patch({ model: e.target.value })}
            className={selectClass}
          >
            {models.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </div>
        <div>
          <div className="mb-1 label">voice</div>
          <select
            value={config.voice}
            onChange={(e) => patch({ voice: e.target.value })}
            className={selectClass}
          >
            {voices.map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </div>
        {live ? (
          <div>
            <div className="mb-1 label">backend (reasoning + tools)</div>
            <select
              value={liveBackend}
              onChange={(e) => patch({ liveBackend: e.target.value })}
              className={selectClass}
            >
              {backends.map((b) => (
                <option key={b} value={b}>
                  {b}
                </option>
              ))}
            </select>
          </div>
        ) : (
          <div>
            <div className="mb-1 label">turn detection</div>
            <select
              value={config.vadMode}
              onChange={(e) =>
                patch({ vadMode: e.target.value as VoiceConfig['vadMode'] })
              }
              className={selectClass}
            >
              <option value="semantic_vad">semantic — natural turn-taking</option>
              <option value="server_vad">server — silence-based</option>
            </select>
          </div>
        )}
        <div>
          <div className="mb-1 label">idle timeout (s)</div>
          <input
            type="number"
            min={10}
            max={120}
            value={idleDraft ?? String(config.idleTimeoutSec)}
            onChange={(e) => setIdleDraft(e.target.value)}
            onBlur={commitIdle}
            onKeyDown={(e) => {
              if (e.key === 'Enter') commitIdle()
            }}
            className={selectClass}
          />
        </div>
      </div>

      <button
        onClick={() => patch({ proactiveVoice: !config.proactiveVoice })}
        className={`group flex w-full items-start gap-3 rounded-lg px-3 py-3 text-left transition-colors ${
          config.proactiveVoice
            ? 'bg-accent/10 ring-1 ring-accent/30'
            : 'hover:bg-white/[0.035] ring-hairline'
        }`}
      >
        <div className="mt-[2px] flex h-4 w-4 shrink-0 items-center justify-center">
          <div
            className={`h-3 w-3 rounded-full ring-1 ${
              config.proactiveVoice ? 'bg-accent/80 ring-accent' : 'bg-fg-3/30 ring-fg-3'
            }`}
          />
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="font-mono text-[12px] text-fg-0">Speak up on their own</span>
            <span
              className={`font-mono text-[9.5px] uppercase tracking-[0.08em] ${
                config.proactiveVoice ? 'text-accent' : 'text-fg-3'
              }`}
            >
              {config.proactiveVoice ? 'ON' : 'OFF'}
            </span>
          </div>
          <div className="mt-[3px] text-[11.5px] leading-[1.55] text-fg-2">
            Let Freyja speak a line when a background task finishes. Off during quiet
            hours; never while you&rsquo;re already talking.
          </div>
        </div>
      </button>

      {config.proactiveVoice && (
        <div className="flex items-center gap-2 pl-3 text-[11px] text-fg-2">
          <span className="label text-fg-3">quiet hours</span>
          <input
            type="number"
            min={0}
            max={23}
            value={config.quietHours.start}
            onChange={(e) => setQuietHour('start', e.target.value)}
            aria-label="quiet hours start"
            className="w-14 rounded border border-white/[0.10] bg-black/[0.30] px-1.5 py-1 text-center font-mono text-[11.5px] text-fg-0 focus:border-accent/[0.40] focus:outline-none"
          />
          <span className="font-mono text-fg-3">→</span>
          <input
            type="number"
            min={0}
            max={23}
            value={config.quietHours.end}
            onChange={(e) => setQuietHour('end', e.target.value)}
            aria-label="quiet hours end"
            className="w-14 rounded border border-white/[0.10] bg-black/[0.30] px-1.5 py-1 text-center font-mono text-[11.5px] text-fg-0 focus:border-accent/[0.40] focus:outline-none"
          />
          <span className="font-mono text-[10px] text-fg-3">
            silent {String(config.quietHours.start).padStart(2, '0')}–
            {String(config.quietHours.end).padStart(2, '0')} (local, 24h)
          </span>
        </div>
      )}

      {!config.hasApiKey && (
        <div className="rounded-md bg-warn/[0.07] px-2.5 py-2 font-mono text-[10.5px] text-warn ring-1 ring-warn/20">
          OPENAI_API_KEY missing — voice disabled
        </div>
      )}
      {!config.spotifySearch && (
        <div className="text-[10.5px] leading-[1.5] text-fg-3">
          Spotify play-by-name needs SPOTIFY_CLIENT_ID/SECRET in .env
        </div>
      )}
    </div>
  )
}

/**
 * Three swatches — each a miniature of the shell in that theme (title
 * strip, two panels, a conversation column) so the choice is visible
 * before it is made. `system` shows whichever one macOS is currently
 * asking for and says so.
 */
function AppearancePicker({
  value,
  effective,
  onChange,
}: {
  value: AppearanceTheme
  effective: 'dark' | 'light'
  onChange: (theme: AppearanceTheme) => void
}) {
  return (
    <div role="radiogroup" aria-label="Appearance" className="grid grid-cols-3 gap-2.5">
      {APPEARANCE_OPTIONS.map((opt) => {
        const isActive = opt.theme === value
        const preview: 'dark' | 'light' = opt.theme === 'system' ? effective : opt.theme
        return (
          <button
            key={opt.theme}
            type="button"
            role="radio"
            aria-checked={isActive}
            onClick={() => onChange(opt.theme)}
            className={`group flex flex-col gap-2 rounded-lg p-2 text-left transition-colors ${
              isActive
                ? 'bg-accent/15 ring-1 ring-accent/40'
                : 'ring-hairline hover:bg-white/[0.035]'
            }`}
          >
            <ThemeSwatch theme={preview} split={opt.theme === 'system'} />
            <div className="flex items-baseline justify-between gap-2 px-0.5">
              <span className={`font-mono text-[11.5px] ${isActive ? 'text-fg-0' : 'text-fg-1'}`}>
                {opt.label}
              </span>
              <span className="font-mono text-[9.5px] uppercase tracking-[0.08em] text-fg-3">
                {opt.theme === 'system' ? `now ${effective}` : opt.hint}
              </span>
            </div>
          </button>
        )
      })}
    </div>
  )
}

/** A 3:2 miniature of the window in one theme. `split` shows both halves
 *  (dark left, light right) for the system option. Colours are literal on
 *  purpose: a swatch must show the other theme, not the current one. */
function ThemeSwatch({ theme, split }: { theme: 'dark' | 'light'; split?: boolean }) {
  const halves: Array<'dark' | 'light'> = split ? ['dark', 'light'] : [theme]
  return (
    <div
      aria-hidden
      className="relative flex h-[62px] w-full overflow-hidden rounded-md ring-1 ring-white/10"
    >
      {halves.map((half) => (
        <div key={half} className="relative flex-1" style={{ background: half === 'dark' ? '#17191e' : '#ece7df' }}>
          <SwatchShell theme={half} />
        </div>
      ))}
      {split && (
        <div className="pointer-events-none absolute inset-y-0 left-1/2 w-px bg-pure-white/30" />
      )}
    </div>
  )
}

function SwatchShell({ theme }: { theme: 'dark' | 'light' }) {
  const dark = theme === 'dark'
  const panel = dark ? 'rgba(255,255,255,0.08)' : 'rgba(250,248,243,0.9)'
  const line = dark ? 'rgba(255,255,255,0.28)' : 'rgba(34,30,24,0.36)'
  const faint = dark ? 'rgba(255,255,255,0.14)' : 'rgba(34,30,24,0.14)'
  const accent = dark ? '#a8d4fc' : '#2864b4'
  const bar = (w: string, c: string) => (
    <div className="h-[2px] rounded-full" style={{ width: w, background: c }} />
  )
  return (
    <div className="absolute inset-0 flex flex-col gap-[3px] p-[5px]">
      <div className="flex items-center gap-1">
        <div className="h-[5px] w-[5px] rounded-full" style={{ background: accent }} />
        {bar('18%', line)}
        {bar('10%', faint)}
        {bar('10%', faint)}
      </div>
      <div className="flex min-h-0 flex-1 gap-[3px]">
        <div className="flex w-[24%] flex-col gap-[3px] rounded-[3px] p-[3px]" style={{ background: panel }}>
          {bar('70%', line)}
          {bar('50%', faint)}
          {bar('60%', faint)}
        </div>
        <div className="flex flex-1 flex-col justify-end gap-[3px] px-[2px]">
          {bar('55%', faint)}
          {bar('80%', faint)}
          <div className="mt-[2px] h-[6px] rounded-[2px]" style={{ background: panel, boxShadow: `inset 0 0 0 0.5px ${faint}` }} />
        </div>
        <div className="flex w-[24%] flex-col gap-[3px] rounded-[3px] p-[3px]" style={{ background: panel }}>
          {bar('60%', line)}
          {bar('40%', accent)}
          {bar('55%', faint)}
        </div>
      </div>
    </div>
  )
}

function Section({
  title,
  description,
  children,
}: {
  title: string
  description?: string
  children: React.ReactNode
}) {
  return (
    <div className="mb-6">
      <div className="mb-1 label">{title}</div>
      {description && (
        <div className="mb-3 text-[11px] leading-[1.55] text-fg-2">{description}</div>
      )}
      {children}
    </div>
  )
}
