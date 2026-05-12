import React from 'react'

export type AgentChipStatus = 'active' | 'idle' | 'stale' | 'done' | 'inactive'

interface AgentChipProps {
  name: string
  status?: AgentChipStatus
  role?: string
  size?: 'sm' | 'md'
  className?: string
}

const STATUS_DOT: Record<AgentChipStatus, string> = {
  active: 'bg-accent shadow-[0_0_6px_rgba(168,212,252,0.65)] animate-pulse-soft',
  idle: 'bg-fg-3',
  stale: 'bg-danger',
  done: 'bg-ok',
  inactive: 'bg-fg-4',
}

const STATUS_TEXT: Record<AgentChipStatus, string> = {
  active: 'text-fg-0',
  idle: 'text-fg-2',
  stale: 'text-danger',
  done: 'text-fg-1',
  inactive: 'text-fg-3',
}

export function AgentChip({ name, status = 'idle', role, size = 'sm', className = '' }: AgentChipProps) {
  const dot = STATUS_DOT[status]
  const text = STATUS_TEXT[status]
  const dotSize = size === 'md' ? 'h-1.5 w-1.5' : 'h-1 w-1'
  const fontSize = size === 'md' ? 'text-xs' : 'text-[11px]'
  return (
    <span className={`inline-flex items-baseline gap-1.5 font-mono ${fontSize} ${className}`}>
      <span className={`rounded-full ${dotSize} ${dot} self-center`} aria-hidden />
      <span className={text}>{name}</span>
      {role ? (
        <span className="text-fg-3 text-[10px] uppercase tracking-[0.16em]">{role}</span>
      ) : null}
    </span>
  )
}
