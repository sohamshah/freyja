import { useEffect, useState } from 'react'
import type { IslandLive } from '../../shared/island'

// Small parts and helpers shared by the island's strip and sheet.

export function Elapsed({ since, now }: { since: number; now: number }) {
  return <span className="island-clock">{elapsed(now - since)}</span>
}

export function Check({ ok }: { ok: boolean }) {
  return (
    <svg className={`island-check ${ok ? '' : 'island-check-bad'}`} viewBox="0 0 14 14" width="12" height="12" aria-hidden="true">
      {ok ? (
        <path d="M2.5 7.5 L5.8 10.6 L11.5 3.8" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" />
      ) : (
        <path d="M3.5 3.5 L10.5 10.5 M10.5 3.5 L3.5 10.5" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      )}
    </svg>
  )
}

/** A chevron that turns when a row is open. */
export function Chevron({ open }: { open: boolean }) {
  return (
    <svg className="island-chev" data-open={open ? 'true' : 'false'} viewBox="0 0 10 10" width="9" height="9" aria-hidden="true">
      <path d="M3.2 1.8 L6.6 5 L3.2 8.2" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

/** Three bars that follow the mic. Driven by the level, so it is a meter, not a decoration. */
export function Meter({ level, live }: { level: number; live: boolean }) {
  const l = Math.max(0, Math.min(1, level))
  const heights = [0.35 + l * 0.65, 0.2 + l * 0.8, 0.45 + l * 0.55]
  return (
    <span className={`island-meter ${live ? 'island-meter-live' : ''}`} aria-hidden="true">
      {heights.map((h, i) => (
        <span key={i} style={{ transform: `scaleY(${h.toFixed(2)})` }} />
      ))}
    </span>
  )
}

export function useNow(everyMs: number): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), everyMs)
    return () => window.clearInterval(id)
  }, [everyMs])
  return now
}

function pad(n: number): string {
  return String(n).padStart(2, '0')
}

export function elapsed(ms: number): string {
  const s = Math.max(0, Math.floor(ms / 1000))
  const m = Math.floor(s / 60)
  const h = Math.floor(m / 60)
  if (h > 0) return `${h}:${pad(m % 60)}:${pad(s % 60)}`
  return `${m}:${pad(s % 60)}`
}

export function clock(ts: number): string {
  const d = new Date(ts)
  const today = new Date()
  const time = `${pad(d.getHours())}:${pad(d.getMinutes())}`
  if (d.toDateString() === today.toDateString()) return time
  const tomorrow = new Date(today)
  tomorrow.setDate(today.getDate() + 1)
  if (d.toDateString() === tomorrow.toDateString()) return `tomorrow ${time}`
  return `${d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })} ${time}`
}

export function ago(ts: number, now: number): string {
  const minutes = Math.round((now - ts) / 60000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.round(hours / 24)}d ago`
}

export function toolWord(name: string): string {
  return name.replace(/^mcp__/, '').replace(/[_-]+/g, ' ')
}

/** What a live row is doing, in a word: thinking, writing, or the tool. */
export function liveWord(row: IslandLive): string {
  return row.thinking ? 'thinking' : row.tool ? toolWord(row.tool.name) : 'writing'
}

/** What the strip shows beside the word: the tool's argument, or the tail of the reply. */
export function liveDetail(row: IslandLive): string {
  if (row.thinking) return ''
  if (row.tool) return row.tool.summary
  return row.text.replace(/^…/, '').trim()
}

/** The live rows as the sheet lists them: roots in start order, each with its helpers under it, then helpers whose parent is not live, grouped by parent. */
export interface LiveGroup {
  /** The parent session's id, or the root row's id. */
  id: string
  title: string
  /** The root's own row, when the root is live. */
  root: IslandLive | null
  children: IslandLive[]
}

export function groupLive(live: IslandLive[]): LiveGroup[] {
  const groups: LiveGroup[] = []
  const byId = new Map<string, LiveGroup>()
  for (const row of live) {
    if (row.kind !== 'sub' || !row.parentId) {
      const g = byId.get(row.id)
      if (g) g.root = row
      else {
        const made = { id: row.id, title: row.title, root: row, children: [] }
        byId.set(row.id, made)
        groups.push(made)
      }
    }
  }
  for (const row of live) {
    if (row.kind !== 'sub' || !row.parentId) continue
    let g = byId.get(row.parentId)
    if (!g) {
      g = { id: row.parentId, title: row.parentTitle ?? row.parentId.slice(0, 8), root: null, children: [] }
      byId.set(row.parentId, g)
      groups.push(g)
    }
    g.children.push(row)
  }
  return groups
}
