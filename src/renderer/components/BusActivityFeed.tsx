import { useEffect, useRef, useState } from 'react'
import { useHarness } from '../state/store'
import type { BusMessageRecord } from '@shared/events'
import { Spinner } from '../lib/spinner'

/**
 * Live message bus activity feed for the SwarmMonitor.
 *
 * Renders as a collapsible rail at the bottom of the swarm monitor
 * showing bus messages flowing between agents in real time. Each message
 * has a topic-colored diamond, sender label, content, and fade-in animation.
 */

const TOPIC_COLORS: Record<string, { bg: string; text: string; border: string; label: string }> = {
  findings: { bg: 'bg-accent/10', text: 'text-accent', border: 'border-accent/30', label: 'finding' },
  errors:   { bg: 'bg-danger/10', text: 'text-danger', border: 'border-danger/30', label: 'error' },
  progress: { bg: 'bg-ok/10',     text: 'text-ok',     border: 'border-ok/30',     label: 'progress' },
  read:     { bg: 'bg-white/[0.03]', text: 'text-fg-3', border: 'border-white/[0.06]', label: 'read' },
}

export function BusActivityFeed() {
  const busMessages = useHarness((s) => s.busMessages)
  const [collapsed, setCollapsed] = useState(false)
  const [prevCount, setPrevCount] = useState(0)
  const scrollRef = useRef<HTMLDivElement>(null)

  // Auto-scroll on new messages
  useEffect(() => {
    if (collapsed) return
    if (busMessages.length > prevCount) {
      setPrevCount(busMessages.length)
      const el = scrollRef.current
      if (el) {
        requestAnimationFrame(() => {
          el.scrollTop = el.scrollHeight
        })
      }
    }
  }, [busMessages.length, prevCount, collapsed])

  const hasMessages = busMessages.length > 0

  return (
    <div className="border-t border-white/[0.06]">
      {/* Header rail — always visible */}
      <button
        onClick={() => setCollapsed((v) => !v)}
        className="flex w-full items-center gap-2.5 px-4 py-2 text-left transition-colors hover:bg-white/[0.03]"
      >
        <BusPulse active={hasMessages} />
        <span className="font-mono text-[10px] uppercase tracking-[0.1em] text-fg-2">
          message bus
        </span>
        {hasMessages && (
          <span className="font-mono text-[10px] text-fg-3">
            {busMessages.length} message{busMessages.length !== 1 ? 's' : ''}
          </span>
        )}
        {/* Topic breakdown badges */}
        {hasMessages && (
          <span className="ml-1 flex items-center gap-1.5">
            <TopicCount msgs={busMessages} topic="findings" />
            <TopicCount msgs={busMessages} topic="errors" />
            <TopicCount msgs={busMessages} topic="progress" />
          </span>
        )}
        <span className="ml-auto font-mono text-[9px] text-fg-3">
          {collapsed ? '▲ expand' : '▼ collapse'}
        </span>
      </button>

      {/* Scrollable message feed */}
      {!collapsed && (
        <div
          ref={scrollRef}
          className="max-h-[180px] overflow-y-auto px-2 pb-2"
        >
          {!hasMessages ? (
            <div className="px-3 py-4 text-center font-mono text-[10px] italic text-fg-3">
              No bus activity yet. Subagents will publish findings here.
            </div>
          ) : (
            <div className="space-y-[3px]">
              {busMessages.map((msg, i) => (
                <BusMessageRow
                  key={msg.index}
                  msg={msg}
                  isNew={i >= busMessages.length - 3}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function BusMessageRow({ msg, isNew }: { msg: BusMessageRecord; isNew: boolean }) {
  const theme = TOPIC_COLORS[msg.topic] ?? TOPIC_COLORS.findings

  return (
    <div
      className={`flex items-start gap-2 rounded-md px-2.5 py-1.5 transition-all ${
        isNew ? 'animate-fade-in' : ''
      } ${theme.bg} border ${theme.border}`}
    >
      {/* Diamond topic marker */}
      <span className="mt-[3px] shrink-0">
        <Diamond className={theme.text} />
      </span>

      {/* Content */}
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className={`font-mono text-[9px] font-bold uppercase tracking-[0.06em] ${theme.text}`}>
            {theme.label}
          </span>
          <span className="font-mono text-[9px] text-fg-3">
            from
          </span>
          <span className="truncate font-mono text-[9.5px] font-medium text-fg-1">
            {msg.senderLabel}
          </span>
          <span className="ml-auto shrink-0 font-mono text-[8.5px] text-fg-3">
            [{msg.index}]
          </span>
        </div>
        <div className="mt-[2px] text-[10.5px] leading-[1.5] text-fg-1">
          {msg.content}
        </div>
      </div>
    </div>
  )
}

function TopicCount({ msgs, topic }: { msgs: BusMessageRecord[]; topic: string }) {
  const count = msgs.filter((m) => m.topic === topic).length
  if (count === 0) return null
  const theme = TOPIC_COLORS[topic] ?? TOPIC_COLORS.findings
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-1.5 py-[1px] text-[8.5px] font-mono font-bold ${theme.bg} ${theme.text} border ${theme.border}`}>
      <Diamond className={theme.text} size={5} />
      {count}
    </span>
  )
}

function Diamond({ className = '', size = 7 }: { className?: string; size?: number }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 7 7"
      className={className}
    >
      <path d="M3.5 0 L7 3.5 L3.5 7 L0 3.5 Z" fill="currentColor" />
    </svg>
  )
}

function BusPulse({ active }: { active: boolean }) {
  if (!active) {
    return (
      <span className="block h-1.5 w-1.5 rounded-full bg-fg-3/40" />
    )
  }
  return <Spinner name="sparkle" className="text-accent" />
}

/**
 * Compact inline badge showing how many findings an agent has published.
 * Used on SubagentTile and SwarmMonitor sidebar items.
 */
export function BusPublishBadge({ agentId }: { agentId: string }) {
  const busMessages = useHarness((s) => s.busMessages)
  const count = busMessages.filter((m) => m.senderId === agentId).length
  if (count === 0) return null
  return (
    <span
      className="inline-flex items-center gap-[3px] rounded-full bg-accent/15 px-1.5 py-[1px] font-mono text-[8px] font-bold text-accent ring-1 ring-accent/20"
      title={`${count} finding${count !== 1 ? 's' : ''} published to bus`}
    >
      <Diamond className="text-accent" size={5} />
      {count}
    </span>
  )
}
