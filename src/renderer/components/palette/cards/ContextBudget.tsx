import React from 'react'
import { useHarness } from '../../../state/store'
import { Btn, Frame, Label, Note, Slot, Well, type WidgetProps } from '../chrome'

function band(pct: number): { label: string; tone: 'ok' | 'warn' | 'danger'; advice: string } {
  if (pct >= 70) return { label: 'forced band', tone: 'danger', advice: 'The runtime will compact on its own soon; a forced summary keeps less than a timed one.' }
  if (pct >= 55) return { label: 'strong band', tone: 'danger', advice: 'Compact before the next long tool chain.' }
  if (pct >= 40) return { label: 'soft band', tone: 'warn', advice: 'Compact at the next clean seam.' }
  return { label: 'comfortable', tone: 'ok', advice: 'No pressure yet.' }
}

const fmtK = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 100_000 ? 0 : 1)}k` : String(n))

export function ContextBudget({ slots, crumbs, onClose, onToast }: WidgetProps) {
  const usage = useHarness((s) => s.usage)
  const sessionId = useHarness((s) => s.activeSessionId)
  const model = useHarness((s) => s.model)
  const reasoningLevel = useHarness((s) => s.reasoningLevel)
  const used = usage.currentContextTokens || usage.lastTurnInputTokens || 0
  const window_ = usage.contextWindow || 1
  const pct = Math.min(100, Math.round((used / window_) * 100))
  const b = band(pct)
  const parts = [
    { k: 'system prompt', v: usage.systemPromptTokens ?? 0 },
    { k: 'tool schemas', v: usage.toolTokens ?? 0 },
    { k: 'transcript', v: usage.transcriptTokens ?? Math.max(0, used - (usage.systemPromptTokens ?? 0) - (usage.toolTokens ?? 0)) },
  ].filter((p) => p.v > 0)
  const wantsCompact = /\bcompact|summari[sz]e|compress|free up\b/i.test(slots.keyword ?? '')

  const compact = () => {
    const api = (window as any).harness
    if (!api?.sendCommand || !sessionId) {
      onToast('No active session to compact')
      return
    }
    void api.sendCommand({ type: 'compact', sessionId, model, reasoningLevel })
    onToast('Compacting context…')
    onClose()
  }

  return (
    <Frame
      kicker="Context"
      crumbs={crumbs}
      title={`${pct}% of ${fmtK(window_)} · ${b.label}`}
      badge={`${fmtK(used)} tokens`}
      footer={
        <>
          <Btn
            onClick={() => {
              useHarness.getState().toggleMissionDashboard(true, 'telemetry')
              onClose()
            }}
          >
            Telemetry
          </Btn>
          <Btn primary={pct >= 40 || wantsCompact} onClick={compact}>
            Compact now
          </Btn>
        </>
      }
    >
      <div className="space-y-1.5">
        <div className="relative h-2 overflow-hidden rounded-full bg-fg-4/60">
          <div className={`absolute inset-y-0 left-0 rounded-full ${b.tone === 'ok' ? 'bg-ok' : b.tone === 'warn' ? 'bg-warn' : 'bg-danger'}`} style={{ width: `${pct}%` }} />
          {[40, 55, 70].map((m) => (
            <span key={m} className="absolute inset-y-0 w-px bg-bg-0/80" style={{ left: `${m}%` }} />
          ))}
        </div>
        <div className="flex justify-between text-[9.5px] text-fg-3">
          <span>0</span>
          <span>40 soft</span>
          <span>55 strong</span>
          <span>70 forced</span>
          <span>{fmtK(window_)}</span>
        </div>
      </div>
      <Note tone={b.tone}>{b.advice}</Note>
      {parts.length > 0 && (
        <Well className="space-y-1.5">
          <Label>What is in the window</Label>
          {parts.map((p) => (
            <div key={p.k} className="flex items-center gap-2 text-[10.5px]">
              <span className="w-24 shrink-0 text-fg-3">{p.k}</span>
              <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-fg-4/60">
                <div className="h-full rounded-full bg-accent/70" style={{ width: `${Math.min(100, (p.v / Math.max(used, 1)) * 100)}%` }} />
              </div>
              <span className="w-12 shrink-0 text-right text-fg-2">{fmtK(p.v)}</span>
            </div>
          ))}
        </Well>
      )}
      <Well className="space-y-1">
        <Slot label="Session" value={`${fmtK(usage.totalInputTokens)} in · ${fmtK(usage.totalOutputTokens)} out · ${fmtK(usage.totalCacheReadTokens)} cached`} hint="" />
        <Slot label="Cost" value={usage.totalCost ? `$${usage.totalCost.toFixed(3)}` : undefined} hint="no cost reported yet" />
        <Slot label="Last turn" value={usage.lastTurnInputTokens ? `${fmtK(usage.lastTurnInputTokens)} in · ${fmtK(usage.lastTurnOutputTokens)} out` : undefined} hint="no turn yet" />
      </Well>
    </Frame>
  )
}
