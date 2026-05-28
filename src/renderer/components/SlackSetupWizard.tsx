/**
 * Slack onboarding wizard for the desktop app.
 *
 * Mirrors `freyja setup slack` but native. Step-by-step UI:
 *   1. Welcome + open api.slack.com
 *   2. Manifest generation + copy button + paste-into-Slack instructions
 *   3. App Token (xapp-) collection
 *   4. Bot Token (xoxb-) collection
 *   5. Live verification via Slack auth_test
 *   6. Allowlist configuration
 *   7. Install + start the gateway daemon
 *   8. Walk-in: how to test in Slack
 *
 * Each step calls into IPC handlers in src/main/gatewayBridge.ts.
 */

import React, { useEffect, useRef, useState } from 'react'
import type {
  GatewayStatus,
  LlmKeysProbeResult,
  SlackVerifyResult,
} from '../../shared/events'

declare global {
  interface Window {
    harness: any
  }
}

interface Props {
  open: boolean
  onClose: () => void
}

type Step =
  | 'welcome'
  | 'manifest'
  | 'app-token'
  | 'bot-token'
  | 'verify'
  | 'allowlist'
  | 'install'
  | 'done'

const STEP_ORDER: Step[] = [
  'welcome', 'manifest', 'app-token', 'bot-token',
  'verify', 'allowlist', 'install', 'done',
]

export function SlackSetupWizard({ open, onClose }: Props) {
  const [step, setStep] = useState<Step>('welcome')
  const [manifestJson, setManifestJson] = useState<string>('')
  const [manifestPath, setManifestPath] = useState<string>('')
  const [manifestCopied, setManifestCopied] = useState(false)
  const [manifestLoading, setManifestLoading] = useState(false)
  const [manifestError, setManifestError] = useState<string>('')
  const [manifestGeneratedAt, setManifestGeneratedAt] = useState<number | null>(null)
  const [manifestCmdCount, setManifestCmdCount] = useState<number>(0)
  const [appToken, setAppToken] = useState('')
  const [botToken, setBotToken] = useState('')
  const [verifying, setVerifying] = useState(false)
  const [verifyResult, setVerifyResult] = useState<SlackVerifyResult | null>(null)
  const [verifyError, setVerifyError] = useState<string>('')
  const [allowlistMode, setAllowlistMode] = useState<'workspace' | 'specific' | 'any'>('workspace')
  const [allowlistUsers, setAllowlistUsers] = useState('')
  const [installing, setInstalling] = useState(false)
  const [installResult, setInstallResult] = useState<string>('')
  const [installError, setInstallError] = useState<string>('')
  const [gatewayStatus, setGatewayStatus] = useState<GatewayStatus | null>(null)
  const [llmKeys, setLlmKeys] = useState<LlmKeysProbeResult | null>(null)

  // Tracks whether the wizard is currently open. async IPC calls (especially
  // verify + save) that resolve after the user closed the modal must NOT
  // silently mutate disk state — pressing ESC mid-verify shouldn't quietly
  // write tokens to ~/.freyja/.env behind the user's back.
  const liveRef = useRef(open)
  useEffect(() => {
    liveRef.current = open
  }, [open])

  // Re-entrancy guard for verifyTokens. React state (`verifying`) doesn't
  // commit synchronously, so a user mashing the "verify" or "retry"
  // button can fire multiple in-flight slackVerifyTokens IPC calls
  // before the button visually disables. Each spawns a Python subprocess
  // that calls Slack's auth.test — burst-rate-limit + waste. The ref
  // makes the guard synchronous.
  const verifyInFlightRef = useRef(false)

  // Reset state when the modal opens.
  useEffect(() => {
    if (open) {
      setStep('welcome')
      setManifestJson('')
      setManifestPath('')
      setManifestCopied(false)
      setManifestError('')
      setManifestLoading(false)
      setVerifyResult(null)
      setVerifyError('')
      setInstallResult('')
      setInstallError('')
      setLlmKeys(null)
      void refreshStatus()
      // Probe LLM keys at open time so the verify step has the result
      // ready by the time the user lands on it (no extra spinner).
      void probeLlmKeys()
    }
  }, [open])

  async function probeLlmKeys() {
    try {
      const res = await window.harness.llmKeysProbe()
      if (liveRef.current) setLlmKeys(res)
    } catch {
      // Best-effort — if the probe IPC fails, just skip the warning.
    }
  }

  // Force a fresh regenerate every time the user lands on the manifest
  // step. The Python module is the source of truth; we always re-shell
  // out so the on-disk file + the wizard pane match whatever the
  // operator's current code says. Otherwise edits to slack_manifest.py
  // can sit in cached state and the operator pastes stale JSON.
  useEffect(() => {
    if (!open) return
    if (step !== 'manifest') return
    void generateManifest()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, step])

  // ESC closes
  useEffect(() => {
    if (!open) return
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.preventDefault()
        onClose()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [open, onClose])

  async function refreshStatus() {
    try {
      const status = await window.harness.gatewayStatus()
      setGatewayStatus(status)
    } catch {
      // ignore
    }
  }

  async function generateManifest() {
    setManifestLoading(true)
    setManifestError('')
    try {
      const res = await window.harness.slackManifest()
      if (res?.ok) {
        setManifestJson(res.manifestJson || '')
        setManifestPath(res.manifestPath || '')
        setManifestGeneratedAt(Date.now())
        // Count slash commands so we can warn the operator if the
        // count looks stale vs what the source code declares.
        let count = 0
        try {
          const parsed = JSON.parse(res.manifestJson || '{}')
          count = parsed?.features?.slash_commands?.length ?? 0
        } catch {
          // ignore parse errors — the pre block will show the raw text
        }
        setManifestCmdCount(count)
        if (!res.manifestJson) {
          setManifestError('manifest returned empty — try regenerating')
        }
      } else {
        setManifestError(res?.error || 'manifest generation failed (no error returned)')
      }
    } catch (err) {
      setManifestError(`IPC error: ${err instanceof Error ? err.message : String(err)}`)
    } finally {
      setManifestLoading(false)
    }
  }

  async function copyManifest() {
    if (!manifestJson) await generateManifest()
    const res = await window.harness.slackCopyManifest()
    if (res?.ok) {
      setManifestCopied(true)
      setTimeout(() => setManifestCopied(false), 2400)
    } else {
      setManifestError(res?.error || 'copy failed')
    }
  }

  async function verifyTokens() {
    // Synchronous re-entrancy guard. setVerifying(true) below schedules
    // a re-render but doesn't commit until React's next cycle; rapid
    // clicks within that window would otherwise stack subprocess calls.
    if (verifyInFlightRef.current) return
    verifyInFlightRef.current = true
    setVerifying(true)
    setVerifyError('')
    try {
      const res: SlackVerifyResult = await window.harness.slackVerifyTokens(
        botToken, appToken,
      )
      // If the user closed the modal while verify was in flight, bail
      // out before writing anything. They thought they cancelled —
      // don't silently persist tokens or advance step state.
      if (!liveRef.current) return
      if (res.ok) {
        // Save tokens immediately so future runs see them.
        const save = await window.harness.slackSaveTokens(botToken, appToken)
        if (!liveRef.current) return
        if (!save.ok) {
          setVerifyError(save.error || 'tokens valid but save failed')
          setVerifyResult(null)
        } else {
          setVerifyResult(res)
          setStep('allowlist')
        }
      } else {
        setVerifyError(res.error || 'auth_test failed')
        setVerifyResult(null)
      }
    } catch (err) {
      if (!liveRef.current) return
      setVerifyError(String(err))
    } finally {
      verifyInFlightRef.current = false
      if (liveRef.current) setVerifying(false)
    }
  }

  async function saveAllowlistAndContinue() {
    if (!verifyResult?.teamId) return
    let userIds: string[] = []
    let enforce = true
    if (allowlistMode === 'any') {
      enforce = false
    } else if (allowlistMode === 'specific') {
      userIds = allowlistUsers
        .split(',')
        .map((u) => u.trim())
        .filter((u) => u.length > 0)
    }
    // workspace mode: enforce=true, userIds=[] (empty allowlist = allow any in workspace)
    const res = await window.harness.slackSetAllowlist(
      verifyResult.teamId, userIds, enforce,
    )
    if (res.ok) setStep('install')
  }

  async function installGateway() {
    setInstalling(true)
    setInstallError('')
    setInstallResult('')
    try {
      const res = await window.harness.gatewayInstall()
      if (res.ok) {
        setInstallResult(res.message || 'installed')
        await refreshStatus()
        setStep('done')
      } else {
        setInstallError(res.error || 'install failed')
      }
    } finally {
      setInstalling(false)
    }
  }

  async function skipInstall() {
    setInstallResult(
      'Skipped — run `freyja gateway run` manually when you want to start it.',
    )
    setStep('done')
  }

  if (!open) return null

  const stepIndex = STEP_ORDER.indexOf(step) + 1
  const stepTotal = STEP_ORDER.length - 1  // 'welcome' and 'done' are bookends; 7 steps user-facing

  return (
    <div className="fixed inset-0 z-[70] flex flex-col bg-black/70 backdrop-blur-[8px]">
      {/* Header — pl-[88px] clears macOS traffic lights */}
      <div className="flex items-center gap-4 border-b border-white/[0.06] bg-bg-0/95 py-3 pl-[88px] pr-4 backdrop-blur-[10px]">
        <span className="font-mono text-[10.5px] uppercase tracking-[0.18em] text-fg-3">
          slack setup
        </span>
        <span className="inline-flex items-center gap-1.5 rounded border border-white/[0.08] bg-white/[0.025] px-2 py-0.5 font-mono text-[10.5px] text-fg-2">
          <span className="h-1.5 w-1.5 rounded-full bg-accent shadow-[0_0_5px_rgba(168,212,252,0.55)]" />
          step {stepIndex} of {stepTotal}
        </span>
        <span className="ml-auto" />
        <button
          type="button"
          onClick={onClose}
          aria-label="Close"
          className="no-drag relative z-[1] flex h-7 items-center justify-center rounded border border-white/[0.08] bg-white/[0.025] px-3 font-mono text-[10.5px] uppercase tracking-[0.16em] text-fg-2 transition hover:border-white/[0.18] hover:bg-white/[0.08] hover:text-fg-0"
        >
          close · esc
        </button>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-[680px] px-10 py-10">
          {step === 'welcome' && (
            <WelcomeStep
              status={gatewayStatus}
              onContinue={() => setStep('manifest')}
            />
          )}

          {step === 'manifest' && (
            <ManifestStep
              json={manifestJson}
              path={manifestPath}
              copied={manifestCopied}
              loading={manifestLoading}
              error={manifestError}
              generatedAt={manifestGeneratedAt}
              cmdCount={manifestCmdCount}
              onCopy={copyManifest}
              onRegenerate={generateManifest}
              onContinue={() => setStep('app-token')}
              onBack={() => setStep('welcome')}
            />
          )}

          {step === 'app-token' && (
            <AppTokenStep
              token={appToken}
              onChange={setAppToken}
              onContinue={() => setStep('bot-token')}
              onBack={() => setStep('manifest')}
            />
          )}

          {step === 'bot-token' && (
            <BotTokenStep
              token={botToken}
              onChange={setBotToken}
              onContinue={() => {
                setStep('verify')
                void verifyTokens()
              }}
              onBack={() => setStep('app-token')}
            />
          )}

          {step === 'verify' && (
            <VerifyStep
              verifying={verifying}
              result={verifyResult}
              error={verifyError}
              llmKeys={llmKeys}
              onRetry={verifyTokens}
              onBack={() => {
                setVerifyError('')
                setStep('bot-token')
              }}
            />
          )}

          {step === 'allowlist' && verifyResult && (
            <AllowlistStep
              result={verifyResult}
              mode={allowlistMode}
              onModeChange={setAllowlistMode}
              users={allowlistUsers}
              onUsersChange={setAllowlistUsers}
              onContinue={saveAllowlistAndContinue}
              onBack={() => setStep('verify')}
            />
          )}

          {step === 'install' && (
            <InstallStep
              installing={installing}
              error={installError}
              onInstall={installGateway}
              onSkip={skipInstall}
              onBack={() => setStep('allowlist')}
            />
          )}

          {step === 'done' && verifyResult && (
            <DoneStep
              result={verifyResult}
              installMessage={installResult}
              gatewayPid={gatewayStatus?.pid ?? null}
              onClose={onClose}
            />
          )}
        </div>
      </div>
    </div>
  )
}

// ── individual steps ─────────────────────────────────────────────

function WelcomeStep({
  status,
  onContinue,
}: {
  status: GatewayStatus | null
  onContinue: () => void
}) {
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="m-0 mb-3 font-serif text-[28px] font-light leading-[1.3] text-fg-0">
          Connect Freyja to Slack
        </h1>
        <p className="m-0 font-mono text-[13px] leading-[1.7] text-fg-2">
          Set up a Slack app, paste two tokens, and Freyja will appear in
          your workspace as a bot. After this, anyone in your allowlist
          can DM Freyja directly or @mention her in channels — even when
          this desktop app is closed.
        </p>
      </div>
      <div className="rounded-md border border-white/[0.06] bg-white/[0.018] p-4">
        <div className="mb-2 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
          what you'll do
        </div>
        <ol className="m-0 flex list-decimal flex-col gap-1.5 pl-5 font-mono text-[12.5px] leading-[1.6] text-fg-1">
          <li>Create a Slack app from a generated manifest</li>
          <li>Enable Socket Mode → get the App Token</li>
          <li>Install the app to your workspace → get the Bot Token</li>
          <li>Paste both tokens here</li>
          <li>Pick who's allowed to talk to the bot</li>
          <li>Install Freyja as a background service</li>
        </ol>
      </div>
      {status?.slackConfigured && (
        <div className="rounded-md border border-warn/[0.22] bg-warn/[0.05] p-3 font-mono text-[12px] text-fg-1">
          <span className="font-bold text-warn">Already configured.</span>{' '}
          Continuing will overwrite the existing tokens.
        </div>
      )}
      <div className="flex justify-end gap-3 pt-2">
        <button
          type="button"
          onClick={onContinue}
          className="rounded-md border border-accent/[0.32] bg-accent/[0.10] px-4 py-2 font-mono text-[11px] uppercase tracking-[0.18em] text-accent transition hover:bg-accent/[0.18]"
        >
          let's go →
        </button>
      </div>
    </div>
  )
}

function ManifestStep({
  json, path, copied, loading, error, generatedAt, cmdCount,
  onCopy, onRegenerate, onContinue, onBack,
}: {
  json: string
  path: string
  copied: boolean
  loading: boolean
  error: string
  generatedAt: number | null
  cmdCount: number
  onCopy: () => void
  onRegenerate: () => void
  onContinue: () => void
  onBack: () => void
}) {
  const freshness = generatedAt
    ? `regenerated ${Math.max(0, Math.round((Date.now() - generatedAt) / 1000))}s ago · ${cmdCount} slash commands`
    : null
  return (
    <div className="flex flex-col gap-6">
      <SectionHeading
        title="Step 1 — Create your Slack app"
        subtitle="One-time. Takes about 90 seconds."
      />
      <ol className="m-0 flex list-decimal flex-col gap-3 pl-5 font-mono text-[13px] leading-[1.7] text-fg-1">
        <li>
          Open{' '}
          <a
            href="https://api.slack.com/apps?new_app=1"
            target="_blank"
            rel="noreferrer noopener"
            onClick={(e) => {
              e.preventDefault()
              window.harness.openExternal('https://api.slack.com/apps?new_app=1')
            }}
            className="font-mono text-accent underline decoration-accent/[0.40] hover:decoration-accent"
          >
            api.slack.com/apps?new_app=1
          </a>{' '}
          and click <span className="text-fg-0">From an app manifest</span>.
        </li>
        <li>Pick your workspace.</li>
        <li>
          Paste the manifest below into the JSON editor Slack shows you.
        </li>
        <li>Click <span className="text-fg-0">Create</span>.</li>
      </ol>

      <div className="rounded-md border border-white/[0.06] bg-bg-2/30">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-white/[0.06] px-3 py-2">
          <span className="font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
            manifest
          </span>
          {path && (
            <span className="truncate font-mono text-[10.5px] text-fg-4">
              {path}
            </span>
          )}
          {freshness && !loading && (
            <span className="font-mono text-[10.5px] text-ok">
              ✓ {freshness}
            </span>
          )}
          {loading && (
            <span className="inline-flex items-center gap-1.5 font-mono text-[10.5px] text-fg-3">
              <span className="relative inline-flex h-2 w-2">
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-50" />
                <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-accent" />
              </span>
              generating…
            </span>
          )}
          <div className="ml-auto flex items-center gap-1.5">
            <button
              type="button"
              onClick={onRegenerate}
              disabled={loading}
              title="Regenerate the manifest from the current code"
              className="rounded border border-white/[0.08] bg-white/[0.02] px-2.5 py-1 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-2 transition hover:border-white/[0.18] hover:bg-white/[0.06] hover:text-fg-0 disabled:cursor-not-allowed disabled:opacity-40"
            >
              regenerate
            </button>
            <button
              type="button"
              onClick={onCopy}
              disabled={!json && !loading}
              className="rounded border border-accent/[0.22] bg-accent/[0.06] px-2.5 py-1 font-mono text-[10.5px] uppercase tracking-[0.14em] text-accent transition hover:border-accent/[0.32] hover:bg-accent/[0.12] disabled:cursor-not-allowed disabled:opacity-40"
            >
              {copied ? '✓ copied' : 'copy to clipboard'}
            </button>
          </div>
        </div>
        {error && (
          <div className="border-b border-warn/[0.18] bg-warn/[0.05] px-3 py-2 font-mono text-[11px] leading-[1.55] text-warn">
            <div className="mb-0.5 font-mono text-[10px] uppercase tracking-[0.14em]">
              generation issue
            </div>
            <span className="select-text text-fg-1">{error}</span>
          </div>
        )}
        <pre className="m-0 max-h-[360px] select-text overflow-auto px-3 py-3 font-mono text-[11px] leading-[1.55] text-fg-1">
          {json || (loading
            ? 'generating manifest from bridge.gateway.platforms.slack_manifest…'
            : 'no manifest yet — click "regenerate" above')}
        </pre>
      </div>

      <NavRow onBack={onBack} onContinue={onContinue} continueLabel="created the app →" />
    </div>
  )
}

function AppTokenStep({
  token, onChange, onContinue, onBack,
}: {
  token: string
  onChange: (v: string) => void
  onContinue: () => void
  onBack: () => void
}) {
  const valid = token.startsWith('xapp-') && token.length > 20
  return (
    <div className="flex flex-col gap-6">
      <SectionHeading
        title="Step 2 — Enable Socket Mode + grab the App Token"
        subtitle="The App Token is what lets Freyja receive messages over an outbound WebSocket — no public URL or tunneling needed."
      />
      <ol className="m-0 flex list-decimal flex-col gap-3 pl-5 font-mono text-[13px] leading-[1.7] text-fg-1">
        <li>
          In your new app, sidebar → <span className="text-fg-0">Settings → Socket Mode</span>.
        </li>
        <li>Toggle <span className="text-fg-0">Enable Socket Mode</span> to ON.</li>
        <li>Click <span className="text-fg-0">Generate an app-level token</span>.</li>
        <li>
          Name it anything (e.g. <span className="text-fg-0">freyja-socket</span>).
          Scope: <span className="text-fg-0">connections:write</span>.
        </li>
        <li>
          Click <span className="text-fg-0">Generate</span> and copy the token —
          it starts with <span className="text-fg-0">xapp-</span>.
        </li>
      </ol>

      <div className="flex flex-col gap-1.5">
        <label className="font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
          App Token (xapp-...)
        </label>
        <input
          type="password"
          autoFocus
          value={token}
          onChange={(e) => onChange(e.target.value)}
          placeholder="xapp-1-…"
          className="w-full rounded-md border border-white/[0.06] bg-white/[0.02] px-3 py-2 font-mono text-[13px] text-fg-0 outline-none placeholder:text-fg-4 focus:border-accent/[0.32] focus:bg-white/[0.04]"
        />
        {token && !valid && (
          <span className="font-mono text-[11px] text-warn">
            Doesn't look right — app tokens start with <code>xapp-</code>.
          </span>
        )}
      </div>

      <NavRow
        onBack={onBack}
        onContinue={onContinue}
        continueLabel="next →"
        continueDisabled={!valid}
      />
    </div>
  )
}

function BotTokenStep({
  token, onChange, onContinue, onBack,
}: {
  token: string
  onChange: (v: string) => void
  onContinue: () => void
  onBack: () => void
}) {
  const valid = token.startsWith('xoxb-') && token.length > 20
  return (
    <div className="flex flex-col gap-6">
      <SectionHeading
        title="Step 3 — Install the app + grab the Bot Token"
        subtitle="This is what Freyja uses to actually send messages, read history, and download files."
      />
      <ol className="m-0 flex list-decimal flex-col gap-3 pl-5 font-mono text-[13px] leading-[1.7] text-fg-1">
        <li>Sidebar → <span className="text-fg-0">Settings → Install App</span>.</li>
        <li>
          Click <span className="text-fg-0">Install to &lt;Your Workspace&gt;</span>{' '}
          and authorize the scopes the manifest declared.
        </li>
        <li>
          Copy the <span className="text-fg-0">Bot User OAuth Token</span> —
          starts with <span className="text-fg-0">xoxb-</span>.
        </li>
      </ol>

      <div className="flex flex-col gap-1.5">
        <label className="font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
          Bot Token (xoxb-...)
        </label>
        <input
          type="password"
          autoFocus
          value={token}
          onChange={(e) => onChange(e.target.value)}
          placeholder="xoxb-…"
          className="w-full rounded-md border border-white/[0.06] bg-white/[0.02] px-3 py-2 font-mono text-[13px] text-fg-0 outline-none placeholder:text-fg-4 focus:border-accent/[0.32] focus:bg-white/[0.04]"
        />
        {token && !valid && (
          <span className="font-mono text-[11px] text-warn">
            Doesn't look right — bot tokens start with <code>xoxb-</code>.
          </span>
        )}
      </div>

      <NavRow
        onBack={onBack}
        onContinue={onContinue}
        continueLabel="verify connection →"
        continueDisabled={!valid}
      />
    </div>
  )
}

function VerifyStep({
  verifying, result, error, llmKeys, onRetry, onBack,
}: {
  verifying: boolean
  result: SlackVerifyResult | null
  error: string
  llmKeys: LlmKeysProbeResult | null
  onRetry: () => void
  onBack: () => void
}) {
  return (
    <div className="flex flex-col gap-6">
      <SectionHeading
        title="Step 4 — Verifying"
        subtitle="Calling Slack's auth.test with your tokens."
      />
      {verifying && (
        <div className="rounded-md border border-accent/[0.22] bg-accent/[0.04] p-4 font-mono text-[12.5px] text-fg-1">
          <span className="relative mr-2 inline-flex h-2 w-2 items-center justify-center align-middle">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-50" />
            <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-accent" />
          </span>
          connecting to Slack…
        </div>
      )}
      {!verifying && result?.ok && (
        <>
          <div className="rounded-md border border-ok/[0.22] bg-ok/[0.04] p-4">
            <div className="mb-1 font-mono text-[10.5px] uppercase tracking-[0.14em] text-ok">
              ✓ authenticated
            </div>
            <p className="m-0 font-mono text-[13px] leading-[1.7] text-fg-1">
              Connected as <span className="font-bold text-fg-0">@{result.botName}</span>{' '}
              in workspace{' '}
              <span className="font-bold text-fg-0">{result.teamName}</span>{' '}
              (team <code>{result.teamId}</code>).
            </p>
          </div>
          {llmKeys && !llmKeys.hasFrontierKey && (
            <div className="rounded-md border border-warn/[0.32] bg-warn/[0.06] p-4 font-mono text-[12.5px] leading-[1.7] text-fg-1">
              <div className="mb-1 font-mono text-[10.5px] uppercase tracking-[0.14em] text-warn">
                ⚠ no LLM API keys found
              </div>
              <p className="m-0 mb-2">
                The gateway daemon couldn't find <code>ANTHROPIC_API_KEY</code>{' '}
                or <code>OPENAI_API_KEY</code> in either your shell environment
                or <code>~/.freyja/.env</code>. Without one of these, Freyja
                won't be able to reply to Slack messages.
              </p>
              <p className="m-0 text-fg-2">
                Fix: export the key in your shell{' '}
                <em>before</em> launching Freyja, or add it to{' '}
                <code>~/.freyja/.env</code> directly and{' '}
                <code>launchctl stop co.freyja.gateway && launchctl start co.freyja.gateway</code>.
              </p>
            </div>
          )}
          {llmKeys?.hasFrontierKey && llmKeys.missing.length > 0 && (
            <div className="rounded-md border border-white/[0.08] bg-white/[0.018] p-3 font-mono text-[11.5px] leading-[1.6] text-fg-2">
              <span className="text-fg-3">heads-up: </span>
              {llmKeys.present.length} LLM key{llmKeys.present.length === 1 ? '' : 's'} found
              ({llmKeys.present.map((k) => k.replace('_API_KEY', '')).join(', ').toLowerCase()}).
              Daemon will work fine; this is just informational.
            </div>
          )}
        </>
      )}
      {!verifying && error && (
        <div className="rounded-md border border-warn/[0.22] bg-warn/[0.05] p-4 font-mono text-[12.5px] text-fg-1">
          <div className="mb-1 font-mono text-[10.5px] uppercase tracking-[0.14em] text-warn">
            verification failed
          </div>
          <p className="m-0 mb-3 select-text">{error}</p>
          <p className="m-0 text-fg-3">
            Common causes: token swapped (bot vs app), app not installed to
            workspace yet, or token revoked.
          </p>
        </div>
      )}
      <div className="flex justify-between gap-3 pt-2">
        <button
          type="button"
          onClick={onBack}
          className="rounded-md border border-white/[0.06] bg-white/[0.02] px-3.5 py-2 font-mono text-[11px] uppercase tracking-[0.16em] text-fg-2 transition hover:border-white/[0.18] hover:bg-white/[0.06] hover:text-fg-0"
        >
          ← back
        </button>
        {error && (
          <button
            type="button"
            onClick={onRetry}
            disabled={verifying}
            className="rounded-md border border-accent/[0.32] bg-accent/[0.10] px-3.5 py-2 font-mono text-[11px] uppercase tracking-[0.18em] text-accent transition hover:bg-accent/[0.18] disabled:opacity-50"
          >
            retry
          </button>
        )}
      </div>
    </div>
  )
}

function AllowlistStep({
  result, mode, onModeChange, users, onUsersChange, onContinue, onBack,
}: {
  result: SlackVerifyResult
  mode: 'workspace' | 'specific' | 'any'
  onModeChange: (m: 'workspace' | 'specific' | 'any') => void
  users: string
  onUsersChange: (v: string) => void
  onContinue: () => void
  onBack: () => void
}) {
  return (
    <div className="flex flex-col gap-6">
      <SectionHeading
        title="Step 5 — Who's allowed to talk to your bot?"
        subtitle="Without an allowlist, anyone in your workspace can DM the bot. Pick a stricter scope for production / demos."
      />
      <div className="flex flex-col gap-2">
        {([
          {
            id: 'workspace' as const,
            title: 'Anyone in this workspace',
            sub: `All members of ${result.teamName} can talk to the bot. Other workspaces denied.`,
          },
          {
            id: 'specific' as const,
            title: 'Just these users',
            sub: 'Most secure for demos. Paste comma-separated Slack user IDs.',
          },
          {
            id: 'any' as const,
            title: 'Anyone, anywhere (dev only)',
            sub: 'No allowlist — every user in every workspace where the app is installed.',
          },
        ]).map((opt) => {
          const active = mode === opt.id
          return (
            <button
              key={opt.id}
              type="button"
              onClick={() => onModeChange(opt.id)}
              className={`flex flex-col gap-1.5 rounded-md border px-3.5 py-3 text-left transition ${
                active
                  ? 'border-accent/[0.4] bg-accent/[0.10]'
                  : 'border-white/[0.06] bg-white/[0.018] hover:border-white/[0.16] hover:bg-white/[0.04]'
              }`}
            >
              <div className="flex items-center gap-2">
                <span className={`h-2 w-2 rounded-full ${active ? 'bg-accent' : 'border border-white/[0.20]'}`} />
                <span className="font-mono text-[12.5px] text-fg-0">{opt.title}</span>
              </div>
              <span className="pl-4 font-mono text-[11.5px] leading-[1.55] text-fg-3">
                {opt.sub}
              </span>
            </button>
          )
        })}
      </div>
      {mode === 'specific' && (
        <div className="flex flex-col gap-1.5">
          <label className="font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
            Allowed user IDs
          </label>
          <input
            type="text"
            value={users}
            onChange={(e) => onUsersChange(e.target.value)}
            placeholder="U001ABCD2, U002WXYZ3"
            className="w-full rounded-md border border-white/[0.06] bg-white/[0.02] px-3 py-2 font-mono text-[13px] text-fg-0 outline-none placeholder:text-fg-4 focus:border-accent/[0.32] focus:bg-white/[0.04]"
          />
          <span className="font-mono text-[11px] italic text-fg-3">
            Find a Slack user ID: their profile → ⋮ → "Copy member ID"
          </span>
        </div>
      )}
      <NavRow
        onBack={onBack}
        onContinue={onContinue}
        continueLabel="save →"
        continueDisabled={mode === 'specific' && users.trim().length === 0}
      />
    </div>
  )
}

function InstallStep({
  installing, error, onInstall, onSkip, onBack,
}: {
  installing: boolean
  error: string
  onInstall: () => void
  onSkip: () => void
  onBack: () => void
}) {
  return (
    <div className="flex flex-col gap-6">
      <SectionHeading
        title="Step 6 — Install the gateway daemon"
        subtitle="Installs a launchd service that starts the gateway at login. Survives closing this desktop app."
      />
      <div className="rounded-md border border-white/[0.06] bg-white/[0.018] p-4">
        <div className="mb-2 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
          what gets installed
        </div>
        <ul className="m-0 flex list-disc flex-col gap-1.5 pl-5 font-mono text-[12px] leading-[1.6] text-fg-1">
          <li><code>~/Library/LaunchAgents/co.freyja.gateway.plist</code></li>
          <li>auto-starts at user login</li>
          <li>auto-restarts on crash (unless you stop it cleanly)</li>
          <li>logs to <code>~/.freyja/logs/gateway.log</code></li>
        </ul>
      </div>
      {installing && (
        <div className="rounded-md border border-accent/[0.22] bg-accent/[0.04] p-3 font-mono text-[12.5px] text-fg-1">
          <span className="relative mr-2 inline-flex h-2 w-2 items-center justify-center align-middle">
            <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-accent opacity-50" />
            <span className="relative inline-flex h-1.5 w-1.5 rounded-full bg-accent" />
          </span>
          installing…
        </div>
      )}
      {error && (
        <div className="rounded-md border border-warn/[0.22] bg-warn/[0.05] p-3 font-mono text-[12.5px] text-fg-1">
          <div className="mb-1 font-mono text-[10.5px] uppercase tracking-[0.14em] text-warn">
            install failed
          </div>
          <p className="m-0 select-text">{error}</p>
        </div>
      )}
      <div className="flex justify-between gap-3 pt-2">
        <button
          type="button"
          onClick={onBack}
          className="rounded-md border border-white/[0.06] bg-white/[0.02] px-3.5 py-2 font-mono text-[11px] uppercase tracking-[0.16em] text-fg-2 transition hover:border-white/[0.18] hover:bg-white/[0.06] hover:text-fg-0"
        >
          ← back
        </button>
        <div className="flex gap-2">
          <button
            type="button"
            onClick={onSkip}
            disabled={installing}
            className="rounded-md border border-white/[0.08] bg-white/[0.02] px-3.5 py-2 font-mono text-[11px] uppercase tracking-[0.16em] text-fg-2 transition hover:border-white/[0.18] hover:bg-white/[0.06] hover:text-fg-0 disabled:opacity-50"
          >
            skip — i'll run it manually
          </button>
          <button
            type="button"
            onClick={onInstall}
            disabled={installing}
            className="rounded-md border border-accent/[0.32] bg-accent/[0.10] px-4 py-2 font-mono text-[11px] uppercase tracking-[0.18em] text-accent transition hover:bg-accent/[0.18] disabled:opacity-50"
          >
            install + start →
          </button>
        </div>
      </div>
    </div>
  )
}

function DoneStep({
  result, installMessage, gatewayPid, onClose,
}: {
  result: SlackVerifyResult
  installMessage: string
  gatewayPid: number | null
  onClose: () => void
}) {
  return (
    <div className="flex flex-col gap-6">
      <div>
        <h1 className="m-0 mb-3 font-serif text-[28px] font-light leading-[1.3] text-fg-0">
          Setup complete
        </h1>
        <p className="m-0 font-mono text-[13px] leading-[1.7] text-fg-2">
          Freyja is live in <span className="font-bold text-fg-0">{result.teamName}</span>{' '}
          as <span className="font-bold text-fg-0">@{result.botName}</span>.
          {gatewayPid && (
            <> Gateway daemon running (pid <code>{gatewayPid}</code>).</>
          )}
        </p>
      </div>

      <div className="rounded-md border border-ok/[0.22] bg-ok/[0.04] p-4">
        <div className="mb-2 font-mono text-[10.5px] uppercase tracking-[0.14em] text-ok">
          try it now
        </div>
        <ol className="m-0 flex list-decimal flex-col gap-1.5 pl-5 font-mono text-[12.5px] leading-[1.6] text-fg-1">
          <li>Open Slack</li>
          <li>Find <span className="text-fg-0">@{result.botName}</span> in your DMs (likely under "Apps" in the sidebar)</li>
          <li>Type a message: <em className="text-fg-2">"hey, what can you do?"</em></li>
        </ol>
      </div>

      {installMessage && (
        <div className="rounded-md border border-white/[0.06] bg-white/[0.018] p-3 font-mono text-[11.5px] text-fg-2">
          {installMessage}
        </div>
      )}

      <div className="rounded-md border border-white/[0.06] bg-white/[0.018] p-4">
        <div className="mb-2 font-mono text-[10.5px] uppercase tracking-[0.14em] text-fg-3">
          slash commands available in Slack
        </div>
        <div className="grid grid-cols-2 gap-1.5 font-mono text-[11.5px] leading-[1.55] text-fg-1">
          <div><code>/freyja</code> <span className="text-fg-3">help card</span></div>
          <div><code>/freyja status</code> <span className="text-fg-3">session info</span></div>
          <div><code>/goal &lt;obj&gt;</code> <span className="text-fg-3">arm goal loop</span></div>
          <div><code>/mode bus|goal|kanban</code> <span className="text-fg-3">change strategy</span></div>
          <div><code>/model &lt;id&gt;</code> <span className="text-fg-3">switch model</span></div>
          <div><code>/stop</code> <span className="text-fg-3">interrupt</span></div>
          <div><code>/reset</code> <span className="text-fg-3">fresh thread</span></div>
          <div><code>/perms</code> <span className="text-fg-3">tool surface</span></div>
        </div>
      </div>

      <div className="flex justify-end gap-3 pt-2">
        <button
          type="button"
          onClick={onClose}
          className="rounded-md border border-accent/[0.32] bg-accent/[0.10] px-4 py-2 font-mono text-[11px] uppercase tracking-[0.18em] text-accent transition hover:bg-accent/[0.18]"
        >
          done
        </button>
      </div>
    </div>
  )
}

// ── shared bits ──────────────────────────────────────────────────

function SectionHeading({ title, subtitle }: { title: string; subtitle: string }) {
  return (
    <div>
      <h2 className="m-0 mb-2 font-serif text-[22px] font-light leading-[1.3] text-fg-0">
        {title}
      </h2>
      <p className="m-0 max-w-[560px] font-mono text-[12.5px] leading-[1.6] text-fg-2">
        {subtitle}
      </p>
    </div>
  )
}

function NavRow({
  onBack, onContinue, continueLabel, continueDisabled,
}: {
  onBack: () => void
  onContinue: () => void
  continueLabel: string
  continueDisabled?: boolean
}) {
  return (
    <div className="flex justify-between gap-3 pt-2">
      <button
        type="button"
        onClick={onBack}
        className="rounded-md border border-white/[0.06] bg-white/[0.02] px-3.5 py-2 font-mono text-[11px] uppercase tracking-[0.16em] text-fg-2 transition hover:border-white/[0.18] hover:bg-white/[0.06] hover:text-fg-0"
      >
        ← back
      </button>
      <button
        type="button"
        onClick={onContinue}
        disabled={continueDisabled}
        className="rounded-md border border-accent/[0.32] bg-accent/[0.10] px-4 py-2 font-mono text-[11px] uppercase tracking-[0.18em] text-accent transition hover:bg-accent/[0.18] disabled:cursor-not-allowed disabled:opacity-40"
      >
        {continueLabel}
      </button>
    </div>
  )
}
