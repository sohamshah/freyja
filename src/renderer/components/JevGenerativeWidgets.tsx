import React, { useState } from 'react'
import { useHarness } from '../state/store'
import { getEffectiveTheme, oppositeTheme } from '../lib/theme'

export interface JevWidgetSpec {
  type: string
  version: string
  meta: {
    title: string
    badge: string
    ready: boolean
  }
  data: any
}

export function JevWidgetRenderer({
  spec,
  onClose,
  onToast,
}: {
  spec: JevWidgetSpec
  onClose: () => void
  onToast: (msg: string) => void
}) {
  switch (spec.type) {
    case 'CalendarAgendaWidget':
      return <CalendarAgendaView spec={spec} onClose={onClose} onToast={onToast} />
    case 'CalendarWidget':
      return <CalendarView spec={spec} onClose={onClose} onToast={onToast} />
    case 'ToolDetailWidget':
      return <ToolDetailView spec={spec} onClose={onClose} onToast={onToast} />
    case 'DiagnosticLogsWidget':
      return <DiagnosticLogsView spec={spec} onClose={onClose} onToast={onToast} />
    case 'SwarmStatusWidget':
      return <SwarmStatusView spec={spec} onClose={onClose} onToast={onToast} />
    case 'AppearanceControlWidget':
      return <AppearanceControlView spec={spec} onClose={onClose} onToast={onToast} />
    case 'MicControlWidget':
      return <MicControlView spec={spec} onClose={onClose} onToast={onToast} />
    case 'MorningBriefingWidget':
      return <MorningBriefingView spec={spec} onClose={onClose} onToast={onToast} />
    case 'TokenUsageWidget':
      return <TokenUsageView spec={spec} onClose={onClose} onToast={onToast} />
    case 'ModelPickerWidget':
      return <ModelPickerView spec={spec} onClose={onClose} onToast={onToast} />
    case 'ArtifactExplorerWidget':
      return <ArtifactExplorerView spec={spec} onClose={onClose} onToast={onToast} />
    case 'VoiceLauncherWidget':
      return <VoiceLauncherView spec={spec} onClose={onClose} onToast={onToast} />
    case 'MemoryWidget':
      return <MemoryView spec={spec} onClose={onClose} onToast={onToast} />
    case 'FinanceWidget':
      return <FinanceView spec={spec} onClose={onClose} onToast={onToast} />
    case 'AgentTaskWidget':
      return <AgentTaskView spec={spec} onClose={onClose} onToast={onToast} />
    case 'QuickControlWidget':
      return <QuickControlView spec={spec} onClose={onClose} onToast={onToast} />
    default:
      return null
  }
}

/** 1. Tool Inspector / Details Widget */
function ToolDetailView({
  spec,
  onClose,
  onToast,
}: {
  spec: JevWidgetSpec
  onClose: () => void
  onToast: (msg: string) => void
}) {
  const d = spec.data
  return (
    <div className="p-3.5 space-y-3 font-mono animate-in fade-in duration-150">
      <div className="flex items-center justify-between pb-2 hairline-b">
        <div className="flex items-center gap-2 text-[12px] font-medium text-fg-0">
          <span className="text-[10px] text-fg-3 uppercase tracking-wider">TOOL</span>
          <span className="text-fg-1">·</span>
          <span>{d.name}</span>
        </div>
        <div className="flex items-center gap-1.5">
          <span className="kbd text-[9.5px]">risk: {d.risk || 'LOW'}</span>
          <span className="kbd text-[9.5px]">{d.category}</span>
        </div>
      </div>

      <div className="rounded-lg border border-ink/[0.08] bg-ink/[0.025] p-3 space-y-2">
        <div className="text-[12px] text-fg-1 font-sans leading-relaxed">
          {d.description}
        </div>
        {d.example && (
          <div className="bg-ink/[0.03] p-2 rounded text-[10.5px] text-fg-2 font-mono border-l-2 border-accent">
            <code>{d.example}</code>
          </div>
        )}
      </div>

      <div className="space-y-1.5">
        <div className="text-[9.5px] uppercase tracking-[0.16em] text-fg-3">PARAMETER SCHEMA</div>
        <div className="border border-ink/[0.08] rounded-lg overflow-hidden bg-ink/[0.02] max-h-[160px] overflow-y-auto">
          <table className="w-full text-[11px] border-collapse">
            <thead>
              <tr className="border-b border-ink/[0.08] bg-ink/[0.03] text-fg-3 text-[10px] text-left">
                <th className="py-1.5 px-2.5 font-medium">NAME</th>
                <th className="py-1.5 px-2.5 font-medium">TYPE</th>
                <th className="py-1.5 px-2.5 font-medium">REQ</th>
                <th className="py-1.5 px-2.5 font-medium">DESCRIPTION</th>
              </tr>
            </thead>
            <tbody>
              {d.parameters?.map((p: any) => (
                <tr key={p.name} className="border-b border-ink/[0.04] last:border-none">
                  <td className="py-1.5 px-2.5 font-medium text-fg-0">{p.name}</td>
                  <td className="py-1.5 px-2.5 text-fg-3 text-[10px]">{p.type}</td>
                  <td className="py-1.5 px-2.5 text-[10px]">{p.required ? <span className="text-accent">yes</span> : 'no'}</td>
                  <td className="py-1.5 px-2.5 text-fg-2 font-sans text-[11px]">{p.description}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="flex items-center justify-end gap-2 pt-2 hairline-t">
        <button
          type="button"
          onClick={() => {
            void navigator.clipboard.writeText(d.name)
            onToast(`Copied tool name "${d.name}" to clipboard`)
          }}
          className="px-2.5 py-1 rounded text-[11px] bg-ink/[0.04] hover:bg-ink/[0.08] text-fg-2 hover:text-fg-0 border border-ink/[0.08] transition-colors"
        >
          Copy Tool Name
        </button>
        <button
          type="button"
          onClick={() => {
            onToast(`Navigating to tool documentation for ${d.name}`)
            onClose()
          }}
          className="px-3 py-1 rounded text-[11px] font-medium bg-accent/20 hover:bg-accent/30 text-fg-0 border border-accent/40 transition-all shadow-sm"
        >
          View Full Docs
        </button>
      </div>
    </div>
  )
}

/** 2. Diagnostic Logs / System Health Widget */
function DiagnosticLogsView({
  spec,
  onClose,
  onToast,
}: {
  spec: JevWidgetSpec
  onClose: () => void
  onToast: (msg: string) => void
}) {
  const d = spec.data
  const [filter, setFilter] = useState<'ALL' | 'ERROR' | 'WARN' | 'INFO'>('ALL')

  const filteredLogs = (d.logs || []).filter((l: any) => {
    if (filter === 'ALL') return true
    return l.level === filter
  })

  return (
    <div className="p-3.5 space-y-3 font-mono animate-in fade-in duration-150">
      <div className="flex items-center justify-between pb-2 hairline-b">
        <div className="flex items-center gap-2 text-[12px] font-medium text-fg-0">
          <span className="text-[10px] text-fg-3 uppercase tracking-wider">DIAGNOSTICS</span>
          <span className="text-fg-1">·</span>
          <span>System & Subsystem Logs</span>
        </div>
        <div className="flex items-center gap-1">
          {(['ALL', 'ERROR', 'WARN', 'INFO'] as const).map((lvl) => (
            <button
              key={lvl}
              onClick={() => setFilter(lvl)}
              className={`px-1.5 py-0.5 rounded text-[9px] font-mono transition-colors ${
                filter === lvl
                  ? 'bg-accent/25 text-fg-0 border border-accent/40'
                  : 'text-fg-3 hover:text-fg-1'
              }`}
            >
              {lvl}
            </button>
          ))}
        </div>
      </div>

      <div className="rounded-lg border border-ink/[0.08] bg-ink/[0.025] p-2 space-y-1 font-mono text-[10.5px] max-h-[220px] overflow-y-auto">
        {filteredLogs.map((l: any, idx: number) => {
          const lvlColor =
            l.level === 'ERROR' ? 'text-red-400' : l.level === 'WARN' ? 'text-amber-400' : 'text-fg-3'
          return (
            <div key={idx} className="flex items-start gap-2 py-0.5 border-b border-ink/[0.03] last:border-none">
              <span className="text-fg-4 select-none">{l.time}</span>
              <span className={`text-[9px] px-1 rounded bg-ink/[0.04] ${lvlColor}`}>{l.subsystem}</span>
              <span className="text-fg-1 flex-1 break-words">{l.message}</span>
            </div>
          )
        })}
      </div>

      <div className="flex items-center justify-end gap-2 pt-2 hairline-t">
        <button
          type="button"
          onClick={() => {
            const raw = filteredLogs.map((l: any) => `[${l.time}] [${l.subsystem}] ${l.message}`).join('\n')
            void navigator.clipboard.writeText(raw)
            onToast('Diagnostic log stream copied to clipboard')
          }}
          className="px-2.5 py-1 rounded text-[11px] bg-ink/[0.04] hover:bg-ink/[0.08] text-fg-2 hover:text-fg-0 border border-ink/[0.08] transition-colors"
        >
          Copy Logs
        </button>
        <button
          type="button"
          onClick={() => {
            onToast('Opening full session action ledger')
            useHarness.getState().toggleMissionDashboard(true, 'logs')
            onClose()
          }}
          className="px-3 py-1 rounded text-[11px] font-medium bg-accent/20 hover:bg-accent/30 text-fg-0 border border-accent/40 transition-all shadow-sm"
        >
          Open Log Viewer
        </button>
      </div>
    </div>
  )
}

/** 3. Swarm Status / Multi-agent Activity Widget */
function SwarmStatusView({
  spec,
  onClose,
  onToast,
}: {
  spec: JevWidgetSpec
  onClose: () => void
  onToast: (msg: string) => void
}) {
  const d = spec.data
  return (
    <div className="p-3.5 space-y-3 font-mono animate-in fade-in duration-150">
      <div className="flex items-center justify-between pb-2 hairline-b">
        <div className="flex items-center gap-2 text-[12px] font-medium text-fg-0">
          <span className="text-[10px] text-fg-3 uppercase tracking-wider">SWARM</span>
          <span className="text-fg-1">·</span>
          <span>Active Worker Lanes</span>
        </div>
        <span className="kbd text-[9.5px]">{d.running_count || 0} active workers</span>
      </div>

      <div className="space-y-1.5">
        {d.lanes?.map((w: any) => (
          <div key={w.label} className="p-2.5 rounded-lg border border-ink/[0.08] bg-ink/[0.025] space-y-1">
            <div className="flex items-center justify-between text-[11.5px]">
              <div className="flex items-center gap-1.5">
                <span className={`w-1.5 h-1.5 rounded-full ${w.status === 'running' ? 'bg-accent animate-pulse' : 'bg-fg-3'}`} />
                <span className="font-medium text-fg-0">{w.label}</span>
              </div>
              <span className="kbd text-[9px]">profile: {w.profile}</span>
            </div>
            <div className="text-[10.5px] text-fg-2 flex justify-between">
              <span>Tool executing: <code className="text-fg-1">{w.current_tool}</code></span>
              <span>Elapsed: {w.elapsed}</span>
            </div>
          </div>
        ))}
      </div>

      <div className="flex items-center justify-end gap-2 pt-2 hairline-t">
        <button
          type="button"
          onClick={() => {
            onToast('Opening Swarm Dashboard (⌘O)')
            useHarness.getState().toggleMissionDashboard(true, 'swarm')
            onClose()
          }}
          className="px-3 py-1 rounded text-[11px] font-medium bg-accent/20 hover:bg-accent/30 text-fg-0 border border-accent/40 transition-all shadow-sm"
        >
          Open Swarm Dashboard (⌘O)
        </button>
      </div>
    </div>
  )
}

/** 4. Google Calendar Agenda View (MCP style) */
function CalendarAgendaView({
  spec,
  onClose,
  onToast,
}: {
  spec: JevWidgetSpec
  onClose: () => void
  onToast: (msg: string) => void
}) {
  const d = spec.data
  return (
    <div className="p-3.5 space-y-3 font-mono animate-in fade-in duration-150">
      <div className="flex items-center justify-between pb-2 hairline-b">
        <div className="flex items-center gap-2 text-[12px] font-medium text-fg-0">
          <span className="text-[10px] text-fg-3 uppercase tracking-wider">CALENDAR</span>
          <span className="text-fg-1">·</span>
          <span>{d.date_full || spec.meta.title}</span>
        </div>
        <span className="kbd text-[9.5px]">{spec.meta.badge}</span>
      </div>

      <div className="text-[11px] text-fg-2 pb-0.5">{d.summary}</div>

      <div className="space-y-2">
        {d.events?.map((ev: any, idx: number) => (
          <div key={idx} className="rounded-lg border border-ink/[0.08] bg-ink/[0.025] p-2.5 space-y-1">
            <div className="flex items-center justify-between text-[11px]">
              <span className="font-semibold text-fg-0">{ev.time}</span>
              <span className="text-[10px] text-fg-3">{ev.link}</span>
            </div>
            <div className="text-[12.5px] text-fg-1 font-medium font-sans">{ev.title}</div>
            <div className="text-[10.5px] text-fg-2">
              With: {ev.attendees?.join(', ')}
            </div>
          </div>
        ))}
      </div>

      <div className="flex items-center justify-end gap-2 pt-2 hairline-t">
        <button
          type="button"
          onClick={() => {
            onToast('Opening Google Calendar')
            onClose()
          }}
          className="px-2.5 py-1 rounded text-[11px] bg-ink/[0.04] hover:bg-ink/[0.08] text-fg-2 hover:text-fg-0 border border-ink/[0.08] transition-colors"
        >
          Open Google Calendar
        </button>
        <button
          type="button"
          onClick={() => {
            onToast('Joining Google Meet: 09:30 Autopilot Architecture Sync')
            onClose()
          }}
          className="px-3 py-1 rounded text-[11px] font-medium bg-accent/20 hover:bg-accent/30 text-fg-0 border border-accent/40 flex items-center gap-1.5 transition-all shadow-sm"
        >
          <svg className="w-3 h-3 text-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <polygon points="5 3 19 12 5 21 5 3"/>
          </svg>
          Join Next Meeting
        </button>
      </div>
    </div>
  )
}

/** 5. Focused Appearance Control */
function AppearanceControlView({
  spec,
  onClose,
  onToast,
}: {
  spec: JevWidgetSpec
  onClose: () => void
  onToast: (msg: string) => void
}) {
  const d = spec.data
  const setAppearance = useHarness((s) => s.setAppearanceTheme)
  const [selectedTheme, setSelectedTheme] = useState(d.active_theme || 'dark')

  const handleApply = (themeId: string) => {
    setSelectedTheme(themeId)
    void setAppearance(themeId === 'system' ? 'system' : themeId === 'dark' ? 'dark' : 'light')
    onToast(`Appearance switched to ${themeId}`)
    setTimeout(onClose, 600)
  }

  return (
    <div className="p-3.5 space-y-3 font-mono animate-in fade-in duration-150">
      <div className="flex items-center justify-between pb-2 hairline-b">
        <div className="flex items-center gap-2 text-[12px] font-medium text-fg-0">
          <span className="text-[10px] text-fg-3 uppercase tracking-wider">APPEARANCE</span>
          <span className="text-fg-1">·</span>
          <span>Theme Selector</span>
        </div>
        <span className="kbd text-[9.5px]">{spec.meta.badge}</span>
      </div>

      <div className="space-y-1.5">
        {d.options?.map((opt: any) => {
          const isSelected = opt.id === selectedTheme
          return (
            <div
              key={opt.id}
              onClick={() => handleApply(opt.id)}
              className={`p-2.5 rounded-lg border cursor-pointer transition-all flex items-center justify-between ${
                isSelected
                  ? 'bg-accent/15 border-accent/40 text-fg-0'
                  : 'bg-ink/[0.025] border-ink/[0.08] text-fg-1 hover:bg-ink/[0.05]'
              }`}
            >
              <div>
                <div className="text-[12px] font-medium text-fg-0">{opt.label}</div>
                <div className="text-[10.5px] text-fg-3">{opt.description}</div>
              </div>
              {isSelected ? (
                <span className="text-accent text-[12px]">● Active</span>
              ) : (
                <span className="text-[10.5px] text-fg-3">Select</span>
              )}
            </div>
          )
        })}
      </div>

      <div className="flex items-center justify-end gap-2 pt-2 hairline-t">
        <button
          type="button"
          onClick={() => handleApply(selectedTheme)}
          className="px-3 py-1 rounded text-[11px] font-medium bg-accent/20 hover:bg-accent/30 text-fg-0 border border-accent/40 transition-all shadow-sm"
        >
          Done
        </button>
      </div>
    </div>
  )
}

/** 6. Focused Microphone Control */
function MicControlView({
  spec,
  onClose,
  onToast,
}: {
  spec: JevWidgetSpec
  onClose: () => void
  onToast: (msg: string) => void
}) {
  const d = spec.data
  const [muted, setMuted] = useState(d.muted)

  const toggle = () => {
    const next = !muted
    setMuted(next)
    onToast(`Microphone ${next ? 'Muted' : 'Unmuted'}`)
  }

  return (
    <div className="p-3.5 space-y-3 font-mono animate-in fade-in duration-150">
      <div className="flex items-center justify-between pb-2 hairline-b">
        <div className="flex items-center gap-2 text-[12px] font-medium text-fg-0">
          <span className="text-[10px] text-fg-3 uppercase tracking-wider">AUDIO</span>
          <span className="text-fg-1">·</span>
          <span>Input Control</span>
        </div>
        <span className="kbd text-[9.5px]">{spec.meta.badge}</span>
      </div>

      <div className="rounded-lg border border-ink/[0.08] bg-ink/[0.025] p-3 flex items-center justify-between">
        <div>
          <div className="text-[12.5px] font-medium text-fg-0">{d.device}</div>
          <div className="text-[10.5px] text-fg-3">Status: {muted ? 'Muted' : 'Active (Input level: 42%)'}</div>
        </div>
        <button
          type="button"
          onClick={toggle}
          className={`px-3 py-1.5 rounded text-[11px] font-medium border transition-colors ${
            muted ? 'bg-ink/[0.05] border-ink/[0.1] text-fg-3' : 'bg-accent/20 border-accent/40 text-fg-0'
          }`}
        >
          {muted ? 'Unmute Mic' : 'Mute Mic'}
        </button>
      </div>

      <div className="flex items-center justify-end gap-2 pt-2 hairline-t">
        <button
          type="button"
          onClick={onClose}
          className="px-3 py-1 rounded text-[11px] font-medium bg-accent/20 hover:bg-accent/30 text-fg-0 border border-accent/40 transition-all shadow-sm"
        >
          Done
        </button>
      </div>
    </div>
  )
}

/** 7. Morning Room / Daily Briefing Widget */
function MorningBriefingView({
  spec,
  onClose,
  onToast,
}: {
  spec: JevWidgetSpec
  onClose: () => void
  onToast: (msg: string) => void
}) {
  const d = spec.data
  const toggleMorningRoom = useHarness((s) => s.toggleMorningRoom)

  return (
    <div className="p-3.5 space-y-3 font-mono animate-in fade-in duration-150">
      <div className="flex items-center justify-between pb-2 hairline-b">
        <div className="flex items-center gap-2 text-[12px] font-medium text-fg-0">
          <span className="text-[10px] text-fg-3 uppercase tracking-wider">BRIEFING</span>
          <span className="text-fg-1">·</span>
          <span>{d.date}</span>
        </div>
        <span className="kbd text-[9.5px]">{spec.meta.badge}</span>
      </div>

      <div className="space-y-1">
        <div className="text-[9.5px] uppercase tracking-[0.16em] text-fg-3">STAGED PLAN TODAY</div>
        <div className="space-y-1.5">
          {d.staged_plan?.map((p: any) => (
            <div key={p.step} className="rounded border border-ink/[0.06] bg-ink/[0.02] p-2 flex items-center justify-between text-[11.5px]">
              <div className="flex items-center gap-2">
                <span className="kbd text-[9.5px]">{p.step}</span>
                <span className="text-fg-0">{p.task}</span>
              </div>
              <span className={`text-[10px] ${p.status === 'active' ? 'text-accent' : 'text-fg-3'}`}>
                {p.status}
              </span>
            </div>
          ))}
        </div>
      </div>

      <div className="space-y-1">
        <div className="text-[9.5px] uppercase tracking-[0.16em] text-fg-3">CRITICAL DECISIONS & INVARIANTS</div>
        <div className="space-y-1">
          {d.decisions?.map((dec: any, idx: number) => (
            <div key={idx} className="text-[11px] text-fg-2 bg-ink/[0.02] p-2 rounded border-l-2 border-accent/40 font-sans">
              <strong className="text-fg-1 font-mono text-[10.5px]">{dec.title}:</strong> {dec.note}
            </div>
          ))}
        </div>
      </div>

      <div className="flex items-center justify-end gap-2 pt-2 hairline-t">
        <button
          type="button"
          onClick={() => {
            onToast('Opening full Morning Room briefing')
            toggleMorningRoom(true)
            onClose()
          }}
          className="px-3 py-1 rounded text-[11px] font-medium bg-accent/20 hover:bg-accent/30 text-fg-0 border border-accent/40 flex items-center gap-1.5 transition-all shadow-sm"
        >
          Open Morning Room (⌘⇧B)
        </button>
      </div>
    </div>
  )
}

/** 8. Token Usage & Spend Widget */
function TokenUsageView({
  spec,
  onClose,
  onToast,
}: {
  spec: JevWidgetSpec
  onClose: () => void
  onToast: (msg: string) => void
}) {
  const d = spec.data

  return (
    <div className="p-3.5 space-y-3 font-mono animate-in fade-in duration-150">
      <div className="flex items-center justify-between pb-2 hairline-b">
        <div className="flex items-center gap-2 text-[12px] font-medium text-fg-0">
          <span className="text-[10px] text-fg-3 uppercase tracking-wider">TELEMETRY</span>
          <span className="text-fg-1">·</span>
          <span>{spec.meta.title}</span>
        </div>
        <span className="kbd text-[9.5px]">{spec.meta.badge}</span>
      </div>

      <div className="flex items-baseline justify-between">
        <div>
          <div className="text-[9.5px] uppercase tracking-[0.16em] text-fg-3">TOTAL USAGE (7 DAYS)</div>
          <div className="text-[24px] font-medium text-fg-0">{d.total_spend} · <span className="text-[13px] text-fg-2">{d.total_tokens}</span></div>
        </div>
        <div className="text-[11px] text-ok">Cache Savings: {d.cache_savings} ({d.cache_read_rate})</div>
      </div>

      <div className="space-y-1.5">
        {d.models?.map((m: any) => (
          <div key={m.name} className="flex items-center justify-between text-[11px] p-1.5 rounded bg-ink/[0.02] border border-ink/[0.05]">
            <span className="text-fg-1 font-medium">{m.name}</span>
            <div className="flex items-center gap-3">
              <span className="text-fg-3">{m.tokens}</span>
              <span className="text-fg-0 font-medium w-14 text-right">{m.spend}</span>
            </div>
          </div>
        ))}
      </div>

      <div className="flex items-center justify-end gap-2 pt-2 hairline-t">
        <button
          type="button"
          onClick={() => {
            onToast('Token usage CSV exported')
            onClose()
          }}
          className="px-2.5 py-1 rounded text-[11px] bg-ink/[0.04] hover:bg-ink/[0.08] text-fg-2 hover:text-fg-0 border border-ink/[0.08] transition-colors"
        >
          Export CSV
        </button>
        <button
          type="button"
          onClick={() => {
            onToast('Opening Metrics Dashboard')
            useHarness.getState().toggleMissionDashboard(true, 'telemetry')
            onClose()
          }}
          className="px-3 py-1 rounded text-[11px] font-medium bg-accent/20 hover:bg-accent/30 text-fg-0 border border-accent/40 transition-all shadow-sm"
        >
          View Full Dashboard
        </button>
      </div>
    </div>
  )
}

/** 9. Model Picker Widget */
function ModelPickerView({
  spec,
  onClose,
  onToast,
}: {
  spec: JevWidgetSpec
  onClose: () => void
  onToast: (msg: string) => void
}) {
  const d = spec.data
  const [selectedModel, setSelectedModel] = useState<string>(
    d.models?.find((m: any) => m.selected)?.id || 'claude-sonnet-5-5'
  )

  const handleSwitch = (modelId: string, modelName: string) => {
    setSelectedModel(modelId)
    onToast(`Switched active session model to ${modelName}`)
    setTimeout(onClose, 600)
  }

  return (
    <div className="p-3.5 space-y-3 font-mono animate-in fade-in duration-150">
      <div className="flex items-center justify-between pb-2 hairline-b">
        <div className="flex items-center gap-2 text-[12px] font-medium text-fg-0">
          <span className="text-[10px] text-fg-3 uppercase tracking-wider">MODELS</span>
          <span className="text-fg-1">·</span>
          <span>Routing & Switching</span>
        </div>
        <span className="kbd text-[9.5px]">Active: {d.active_model}</span>
      </div>

      <div className="space-y-1.5 max-h-[260px] overflow-y-auto">
        {d.models?.map((m: any) => {
          const isSelected = m.id === selectedModel
          return (
            <div
              key={m.id}
              onClick={() => handleSwitch(m.id, m.name)}
              className={`p-2 rounded-lg border cursor-pointer transition-all flex items-center justify-between ${
                isSelected
                  ? 'bg-accent/15 border-accent/40 text-fg-0'
                  : 'bg-ink/[0.025] border-ink/[0.08] text-fg-1 hover:bg-ink/[0.05]'
              }`}
            >
              <div>
                <div className="text-[12px] font-medium text-fg-0 flex items-center gap-1.5">
                  <span>{m.name}</span>
                  {isSelected && <span className="kbd text-[8.5px]">current</span>}
                </div>
                <div className="text-[10px] text-fg-3 font-sans">{m.recommended}</div>
              </div>
              <div className="text-right text-[10.5px]">
                <div className="text-fg-0">{m.cost}</div>
                <div className="text-fg-3">{m.latency}</div>
              </div>
            </div>
          )
        })}
      </div>

      <div className="flex items-center justify-end gap-2 pt-2 hairline-t">
        <button
          type="button"
          onClick={onClose}
          className="px-3 py-1 rounded text-[11px] font-medium bg-accent/20 hover:bg-accent/30 text-fg-0 border border-accent/40 transition-all shadow-sm"
        >
          Done
        </button>
      </div>
    </div>
  )
}

/** 10. Artifact Explorer Widget */
function ArtifactExplorerView({
  spec,
  onClose,
  onToast,
}: {
  spec: JevWidgetSpec
  onClose: () => void
  onToast: (msg: string) => void
}) {
  const d = spec.data
  const toggleArtifacts = useHarness((s) => s.toggleArtifactsBrowser)

  return (
    <div className="p-3.5 space-y-3 font-mono animate-in fade-in duration-150">
      <div className="flex items-center justify-between pb-2 hairline-b">
        <div className="flex items-center gap-2 text-[12px] font-medium text-fg-0">
          <span className="text-[10px] text-fg-3 uppercase tracking-wider">ARTIFACTS</span>
          <span className="text-fg-1">·</span>
          <span>Recent Deliverables</span>
        </div>
        <span className="kbd text-[9.5px]">{spec.meta.badge}</span>
      </div>

      <div className="space-y-1.5">
        {d.artifacts?.map((art: any) => (
          <div key={art.name} className="p-2 rounded border border-ink/[0.06] bg-ink/[0.02] flex items-center justify-between">
            <div>
              <div className="text-[12px] font-medium text-fg-0">{art.name}</div>
              <div className="text-[10px] text-fg-3">{art.session} · {art.type} · {art.age}</div>
            </div>
            <div className="flex items-center gap-2">
              <span className="text-[10px] text-fg-3">{art.size}</span>
              <button
                type="button"
                onClick={() => onToast(`Copied path to ${art.name}`)}
                className="kbd text-[9.5px] hover:text-fg-0"
              >
                copy path
              </button>
            </div>
          </div>
        ))}
      </div>

      <div className="flex items-center justify-end gap-2 pt-2 hairline-t">
        <button
          type="button"
          onClick={() => {
            onToast('Opening Artifacts Browser')
            toggleArtifacts(true)
            onClose()
          }}
          className="px-3 py-1 rounded text-[11px] font-medium bg-accent/20 hover:bg-accent/30 text-fg-0 border border-accent/40 transition-all shadow-sm"
        >
          Browse All Artifacts (⌘⇧A)
        </button>
      </div>
    </div>
  )
}

/** 11. Real-time Voice Launcher Widget */
function VoiceLauncherView({
  spec,
  onClose,
  onToast,
}: {
  spec: JevWidgetSpec
  onClose: () => void
  onToast: (msg: string) => void
}) {
  const d = spec.data

  return (
    <div className="p-3.5 space-y-3 font-mono animate-in fade-in duration-150">
      <div className="flex items-center justify-between pb-2 hairline-b">
        <div className="flex items-center gap-2 text-[12px] font-medium text-fg-0">
          <span className="text-[10px] text-fg-3 uppercase tracking-wider">VOICE</span>
          <span className="text-fg-1">·</span>
          <span>{d.persona}</span>
        </div>
        <span className="kbd text-[9.5px]">{spec.meta.badge}</span>
      </div>

      <div className="rounded-lg border border-ink/[0.08] bg-ink/[0.025] p-3 text-center space-y-2">
        <div className="flex justify-center items-center gap-1.5 py-2">
          {[4, 12, 18, 24, 16, 20, 10, 6].map((h, i) => (
            <div key={i} style={{ height: `${h}px` }} className="w-1 rounded-full bg-accent animate-pulse" />
          ))}
        </div>
        <div className="text-[12px] text-fg-0 font-medium">{d.status}</div>
        <div className="text-[10.5px] text-fg-3">Device: {d.input_device} · {d.latency}</div>
      </div>

      <div className="flex items-center justify-end gap-2 pt-2 hairline-t">
        <button
          type="button"
          onClick={() => {
            onToast('Voice channel initiated')
            onClose()
          }}
          className="px-3.5 py-1.5 rounded text-[11px] font-medium bg-accent/20 hover:bg-accent/30 text-fg-0 border border-accent/40 flex items-center gap-1.5 transition-all shadow-sm"
        >
          <svg className="w-3.5 h-3.5 text-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
            <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
            <line x1="12" y1="19" x2="12" y2="23"/>
            <line x1="8" y1="23" x2="16" y2="23"/>
          </svg>
          Connect Voice Channel
        </button>
      </div>
    </div>
  )
}

/** 12. Calendar Create / Scheduling */
function CalendarView({
  spec,
  onClose,
  onToast,
}: {
  spec: JevWidgetSpec
  onClose: () => void
  onToast: (msg: string) => void
}) {
  const d = spec.data
  const [selectedSlot, setSelectedSlot] = useState<string>(
    d.availability?.find((s: any) => s.selected)?.time || '16:00'
  )

  const handleSendInvite = () => {
    onToast(`Meeting invite sent to ${d.attendee} for ${d.date} at ${selectedSlot}`)
    setTimeout(onClose, 800)
  }

  return (
    <div className="p-3.5 space-y-3 font-mono animate-in fade-in duration-150">
      <div className="flex items-center justify-between pb-2 hairline-b">
        <div className="flex items-center gap-2 text-[12px] font-medium text-fg-0">
          <span className="text-[10px] text-fg-3 uppercase tracking-wider">EVENT</span>
          <span className="text-fg-1">·</span>
          <span>{spec.meta.title}</span>
        </div>
        <span className="kbd text-[9.5px]">{spec.meta.badge}</span>
      </div>

      <div className="rounded-lg border border-ink/[0.08] bg-ink/[0.025] p-3 space-y-2.5">
        <div className="flex items-center justify-between">
          <div className="text-[13px] font-medium text-fg-0">{d.title}</div>
          <span className="text-[10px] text-fg-3 uppercase tracking-wider">SLOT PARSED</span>
        </div>

        <div className="flex items-center gap-2 text-[11.5px] text-fg-1">
          <svg className="w-3.5 h-3.5 text-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
            <circle cx="12" cy="12" r="10"/>
            <polyline points="12 6 12 12 16 14"/>
          </svg>
          <span>{d.date} · <strong>{selectedSlot} – {selectedSlot === '16:00' ? '16:30' : 'End'} PDT</strong></span>
        </div>

        <div className="flex items-center gap-2 text-[11px] text-fg-2">
          <div className="w-4 h-4 rounded-full bg-accent/20 text-accent flex items-center justify-center font-medium text-[9.5px]">
            {d.attendee?.charAt(0) || 'K'}
          </div>
          <span>{d.attendee} ({d.attendee_email})</span>
          <span className="text-ok text-[10.5px] ml-1">● Free at {selectedSlot}</span>
        </div>
      </div>

      <div className="space-y-1.5">
        <div className="text-[9.5px] uppercase tracking-[0.16em] text-fg-3">
          {d.attendee}'S SCHEDULE (TUESDAY)
        </div>
        <div className="grid grid-cols-7 gap-1.5">
          {d.availability?.map((slot: any) => {
            const isSelected = slot.time === selectedSlot
            const isFree = slot.status === 'free'
            return (
              <button
                key={slot.time}
                type="button"
                disabled={!isFree}
                onClick={() => setSelectedSlot(slot.time)}
                className={`py-1.5 px-1 rounded text-[10.5px] font-mono text-center transition-all ${
                  isSelected
                    ? 'bg-accent/20 text-fg-0 font-medium border border-accent/40 shadow-sm'
                    : isFree
                    ? 'bg-ink/[0.03] text-fg-1 hover:bg-accent/10 hover:text-fg-0 border border-ink/[0.08]'
                    : 'bg-ink/[0.01] text-fg-4 cursor-not-allowed border border-ink/[0.04]'
                }`}
              >
                {slot.time}
              </button>
            )
          })}
        </div>
      </div>

      <div className="flex items-center justify-end gap-2 pt-2 hairline-t">
        <button
          type="button"
          onClick={() => {
            onToast('Opening full calendar in browser')
            onClose()
          }}
          className="px-2.5 py-1 rounded text-[11px] bg-ink/[0.04] hover:bg-ink/[0.08] text-fg-2 hover:text-fg-0 border border-ink/[0.08] transition-colors"
        >
          Edit in Calendar
        </button>
        <button
          type="button"
          onClick={handleSendInvite}
          className="px-3 py-1 rounded text-[11px] font-medium bg-accent/20 hover:bg-accent/30 text-fg-0 border border-accent/40 flex items-center gap-1.5 transition-all shadow-sm"
        >
          <svg className="w-3 h-3 text-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <line x1="22" y1="2" x2="11" y2="13"/>
            <polygon points="22 2 15 22 11 13 2 9 22 2"/>
          </svg>
          Send Invite
        </button>
      </div>
    </div>
  )
}

/** 13. Memory Recall */
function MemoryView({
  spec,
  onClose,
  onToast,
}: {
  spec: JevWidgetSpec
  onClose: () => void
  onToast: (msg: string) => void
}) {
  const d = spec.data

  const handleCopy = (snippet: string) => {
    void navigator.clipboard.writeText(snippet)
    onToast('Recommendation copied to clipboard')
  }

  return (
    <div className="p-3.5 space-y-3 font-mono animate-in fade-in duration-150">
      <div className="flex items-center justify-between pb-2 hairline-b">
        <div className="flex items-center gap-2 text-[12px] font-medium text-fg-0">
          <span className="text-[10px] text-fg-3 uppercase tracking-wider">MEMORY</span>
          <span className="text-fg-1">·</span>
          <span>{spec.meta.title}</span>
        </div>
        <span className="kbd text-[9.5px]">{spec.meta.badge}</span>
      </div>

      <div className="space-y-2 max-h-[260px] overflow-y-auto">
        {d.matches?.map((m: any) => (
          <div key={m.id} className="rounded-lg border border-ink/[0.08] bg-ink/[0.025] p-3 space-y-2">
            <div className="flex items-center justify-between text-[11px] text-fg-3">
              <span className="font-medium text-fg-1">{m.title}</span>
              <span>{m.source} · {m.date}</span>
            </div>
            <div className="text-[12px] text-fg-0 bg-ink/[0.03] p-2.5 rounded border-l-2 border-accent/60 leading-relaxed font-sans">
              "{m.snippet}"
            </div>
            <div className="flex items-center justify-between pt-0.5">
              <div className="flex gap-1.5">
                {m.tags?.map((t: string) => (
                  <span key={t} className="text-[9.5px] px-1.5 py-0.5 rounded bg-ink/[0.04] text-fg-3 border border-ink/[0.06]">
                    #{t}
                  </span>
                ))}
              </div>
              <span className="text-[9.5px] text-fg-3">timestamp [{m.timestamp}]</span>
            </div>
          </div>
        ))}
      </div>

      <div className="flex items-center justify-end gap-2 pt-2 hairline-t">
        <button
          type="button"
          onClick={() => {
            onToast('Transcript opened')
            onClose()
          }}
          className="px-2.5 py-1 rounded text-[11px] bg-ink/[0.04] hover:bg-ink/[0.08] text-fg-2 hover:text-fg-0 border border-ink/[0.08] transition-colors"
        >
          Open Transcript
        </button>
        <button
          type="button"
          onClick={() => handleCopy(d.matches?.[0]?.snippet || '')}
          className="px-3 py-1 rounded text-[11px] font-medium bg-accent/20 hover:bg-accent/30 text-fg-0 border border-accent/40 flex items-center gap-1.5 transition-all shadow-sm"
        >
          <svg className="w-3 h-3 text-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
            <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
          </svg>
          Copy Recommendation
        </button>
      </div>
    </div>
  )
}

/** 14. Finance Widget */
function FinanceView({
  spec,
  onClose,
  onToast,
}: {
  spec: JevWidgetSpec
  onClose: () => void
  onToast: (msg: string) => void
}) {
  const d = spec.data

  return (
    <div className="p-3.5 space-y-3 font-mono animate-in fade-in duration-150">
      <div className="flex items-center justify-between pb-2 hairline-b">
        <div className="flex items-center gap-2 text-[12px] font-medium text-fg-0">
          <span className="text-[10px] text-fg-3 uppercase tracking-wider">FINANCE</span>
          <span className="text-fg-1">·</span>
          <span>{spec.meta.title}</span>
        </div>
        <span className="kbd text-[9.5px]">{spec.meta.badge}</span>
      </div>

      <div className="flex items-baseline justify-between">
        <div>
          <div className="text-[9.5px] uppercase tracking-[0.16em] text-fg-3">TOTAL SPEND ({d.period})</div>
          <div className="text-[24px] font-medium tracking-tight text-fg-0">{d.total}</div>
        </div>
        <div className="text-[11px] text-ok">{d.trend}</div>
      </div>

      <div className="flex h-1.5 rounded-full overflow-hidden bg-ink/[0.08]">
        {d.categories?.map((c: any, idx: number) => {
          const colors = ['bg-accent/80', 'bg-accent/40', 'bg-accent/20']
          return (
            <div
              key={c.name}
              style={{ width: `${c.share}%` }}
              className={colors[idx % colors.length]}
              title={`${c.name}: ${c.amount}`}
            />
          )
        })}
      </div>

      <div className="flex gap-4">
        {d.categories?.map((c: any, idx: number) => {
          const dots = ['bg-accent', 'bg-accent/60', 'bg-accent/30']
          return (
            <div key={c.name} className="flex items-center gap-1.5 text-[10.5px] text-fg-2">
              <span className={`w-1.5 h-1.5 rounded-sm ${dots[idx % dots.length]}`} />
              <span>{c.name} (<strong>{c.amount}</strong>)</span>
            </div>
          )
        })}
      </div>

      <div className="border border-ink/[0.08] rounded-lg overflow-hidden bg-ink/[0.02]">
        <table className="w-full text-[11px] border-collapse">
          <tbody>
            {d.transactions?.slice(0, 4).map((tx: any, idx: number) => (
              <tr key={idx} className="border-b border-ink/[0.05] last:border-none">
                <td className="py-1.5 px-2.5 text-fg-3 w-16">{tx.date}</td>
                <td className="py-1.5 px-2.5 font-medium text-fg-1">{tx.merchant}</td>
                <td className="py-1.5 px-2.5 text-fg-3">{tx.category}</td>
                <td className="py-1.5 px-2.5 text-right font-mono text-fg-0">{tx.amount}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-end gap-2 pt-2 hairline-t">
        <button
          type="button"
          onClick={() => {
            onToast('CSV exported to Downloads')
            onClose()
          }}
          className="px-2.5 py-1 rounded text-[11px] bg-ink/[0.04] hover:bg-ink/[0.08] text-fg-2 hover:text-fg-0 border border-ink/[0.08] transition-colors"
        >
          Export CSV
        </button>
        <button
          type="button"
          onClick={() => {
            onToast('Expense report filed to Expensify')
            onClose()
          }}
          className="px-3 py-1 rounded text-[11px] font-medium bg-accent/20 hover:bg-accent/30 text-fg-0 border border-accent/40 transition-all shadow-sm"
        >
          File Expense Report
        </button>
      </div>
    </div>
  )
}

/** 15. Agent Task Delegation */
function AgentTaskView({
  spec,
  onClose,
  onToast,
}: {
  spec: JevWidgetSpec
  onClose: () => void
  onToast: (msg: string) => void
}) {
  const d = spec.data
  const setDraft = useHarness((s) => s.setInputDraft)

  const handleSpawn = () => {
    setDraft(`Please delegate this task to sub-agent profile "${d.suggested_profile}": ${d.task_title}. Context: ${d.objective}`)
    onToast(`Sub-agent task drafted: ${d.task_title}`)
    onClose()
  }

  return (
    <div className="p-3.5 space-y-3 font-mono animate-in fade-in duration-150">
      <div className="flex items-center justify-between pb-2 hairline-b">
        <div className="flex items-center gap-2 text-[12px] font-medium text-fg-0">
          <span className="text-[10px] text-fg-3 uppercase tracking-wider">AGENT TASK</span>
          <span className="text-fg-1">·</span>
          <span>{spec.meta.title}</span>
        </div>
        <span className="kbd text-[9.5px]">{spec.meta.badge}</span>
      </div>

      <div className="rounded-lg border border-ink/[0.08] bg-ink/[0.025] p-3 space-y-2">
        <div className="flex items-center justify-between">
          <span className="text-[12.5px] font-medium text-fg-0">{d.task_title}</span>
          <span className="kbd text-[9.5px]">profile: {d.suggested_profile}</span>
        </div>
        <div className="text-[10.5px] text-fg-3">Model: {d.model}</div>
        <div className="text-[11.5px] text-fg-1 leading-relaxed font-sans">{d.objective}</div>
        <div className="flex gap-1.5 pt-0.5">
          {d.flags?.map((f: string) => (
            <span key={f} className="text-[9.5px] px-1.5 py-0.5 rounded bg-ink/[0.04] text-fg-3 border border-ink/[0.06]">
              flag: {f}
            </span>
          ))}
        </div>
      </div>

      <div className="flex items-center justify-end gap-2 pt-2 hairline-t">
        <button
          type="button"
          onClick={() => {
            onToast('Opening GitHub pull request')
            onClose()
          }}
          className="px-2.5 py-1 rounded text-[11px] bg-ink/[0.04] hover:bg-ink/[0.08] text-fg-2 hover:text-fg-0 border border-ink/[0.08] transition-colors"
        >
          View on GitHub
        </button>
        <button
          type="button"
          onClick={handleSpawn}
          className="px-3 py-1 rounded text-[11px] font-medium bg-accent/20 hover:bg-accent/30 text-fg-0 border border-accent/40 flex items-center gap-1.5 transition-all shadow-sm"
        >
          <svg className="w-3 h-3 text-accent" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
            <polygon points="5 3 19 12 5 21 5 3"/>
          </svg>
          Spawn Sub-agent
        </button>
      </div>
    </div>
  )
}

/** 16. General Quick Controls Grid */
function QuickControlView({
  spec,
  onClose,
  onToast,
}: {
  spec: JevWidgetSpec
  onClose: () => void
  onToast: (msg: string) => void
}) {
  const d = spec.data
  const [controls, setControls] = useState(d.controls || [])
  const setAppearance = useHarness((s) => s.setAppearanceTheme)

  const toggleControl = (id: string) => {
    setControls((prev: any[]) =>
      prev.map((c) => {
        if (c.id === id) {
          const nextVal = !c.value
          if (id === 'dark_mode') {
            void setAppearance(nextVal ? 'dark' : oppositeTheme(getEffectiveTheme()))
          }
          onToast(`${c.label} toggled ${nextVal ? 'ON' : 'OFF'}`)
          return { ...c, value: nextVal }
        }
        return c
      })
    )
  }

  return (
    <div className="p-3.5 space-y-3 font-mono animate-in fade-in duration-150">
      <div className="flex items-center justify-between pb-2 hairline-b">
        <div className="flex items-center gap-2 text-[12px] font-medium text-fg-0">
          <span className="text-[10px] text-fg-3 uppercase tracking-wider">CONTROLS</span>
          <span className="text-fg-1">·</span>
          <span>{spec.meta.title}</span>
        </div>
        <span className="kbd text-[9.5px]">{spec.meta.badge}</span>
      </div>

      <div className="grid grid-cols-2 gap-2">
        {controls.map((c: any) => (
          <div key={c.id} className="rounded-lg border border-ink/[0.08] bg-ink/[0.025] p-2.5 flex items-center justify-between">
            <div className="space-y-0.5">
              <div className="text-[11.5px] font-medium text-fg-0">{c.label}</div>
              <div className="text-[10px] text-fg-3">{c.description}</div>
            </div>
            {c.type === 'toggle' ? (
              <button
                type="button"
                onClick={() => toggleControl(c.id)}
                className={`w-8 h-4 rounded-full p-0.5 transition-colors border ${
                  c.value ? 'bg-accent/30 border-accent/50' : 'bg-ink/[0.06] border-ink/[0.1]'
                }`}
              >
                <div
                  className={`w-3 h-3 rounded-full bg-fg-0 transition-transform ${
                    c.value ? 'translate-x-4' : 'translate-x-0'
                  }`}
                />
              </button>
            ) : (
              <span className="kbd text-[10px] text-fg-1">{c.value}</span>
            )}
          </div>
        ))}
      </div>

      <div className="flex items-center justify-end gap-2 pt-2 hairline-t">
        <button
          type="button"
          onClick={() => {
            onToast('Settings applied')
            onClose()
          }}
          className="px-3 py-1 rounded text-[11px] font-medium bg-accent/20 hover:bg-accent/30 text-fg-0 border border-accent/40 transition-all shadow-sm"
        >
          Done
        </button>
      </div>
    </div>
  )
}
