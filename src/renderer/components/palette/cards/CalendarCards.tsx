import React, { useEffect, useMemo, useState } from 'react'
import { useHarness } from '../../../state/store'
import { requiredSlots, slotFilled, type PaletteSlots, type TimeSlot } from '../../../lib/paletteIntent'
import { CAL_SERVER, freeSlots, listEvents, mcpServerState, openExternal, type CalEvent } from '../../../lib/paletteData'
import { Btn, Chip, Frame, Label, Note, Pending, Progress, Row, RowAction, Slot, Well, fmtTime, useAsync, type WidgetProps } from '../chrome'

const SETUP_SNIPPET = `gcloud services enable calendarmcp.googleapis.com --project=ema-dev-401514
python3 - <<'EOF'
import json, copy, pathlib
p = pathlib.Path.home() / '.freyja' / 'mcp.json'
d = json.loads(p.read_text()); servers = d.get('servers', d)
cal = copy.deepcopy(servers['google_drive_hosted'])
cal['url'] = 'https://calendarmcp.googleapis.com/mcp/v1'; cal['enabled'] = True
cal['oauth']['resource_metadata_url'] = 'https://calendarmcp.googleapis.com/.well-known/oauth-protected-resource/mcp/v1'
cal['oauth']['scope'] = 'https://www.googleapis.com/auth/calendar.events https://www.googleapis.com/auth/calendar.readonly'
servers['google_calendar_hosted'] = cal
p.write_text(json.dumps(d, indent=2) + '\\n')
EOF
# then in Freyja: /mcp reload · /mcp login google_calendar_hosted`

function useCalendarState() {
  const servers = useHarness((s) => s.mcp.servers)
  return useMemo(() => mcpServerState(CAL_SERVER), [servers])
}

function CalendarConnect({ onToast }: { onToast: (m: string) => void }) {
  const state = useCalendarState()
  const send = useHarness((s) => s.sendMcpCommand)
  return (
    <Well className="space-y-2">
      <Label>Google Calendar · {state === 'missing' ? 'not connected' : state}</Label>
      <div className="text-[10.5px] leading-relaxed text-fg-2">
        {state === 'missing'
          ? 'Freyja talks to Google’s hosted Calendar MCP the same way it reaches Drive. One-time setup: enable the API on the project that owns the OAuth client, add the server entry, log in.'
          : 'The server is configured but not active yet. Log in to finish the connection.'}
      </div>
      <div className="flex flex-wrap gap-1.5">
        {state === 'missing' ? (
          <Chip
            onClick={() => {
              void navigator.clipboard.writeText(SETUP_SNIPPET)
              onToast('Setup commands copied')
            }}
          >
            copy setup commands
          </Chip>
        ) : (
          <>
            <Chip onClick={() => send('login', { server: CAL_SERVER })}>log in</Chip>
            <Chip onClick={() => send('reload', { surface: 'panel' })}>reload servers</Chip>
          </>
        )}
      </div>
    </Well>
  )
}

function rangeFor(slots: PaletteSlots): { start: Date; end: Date; label: string; days: number } {
  const now = new Date()
  const startOf = (d: Date) => {
    const x = new Date(d)
    x.setHours(0, 0, 0, 0)
    return x
  }
  if (slots.range === 'week' || slots.range === 'next_week') {
    const s = startOf(now)
    const dow = (s.getDay() + 6) % 7
    s.setDate(s.getDate() - dow + (slots.range === 'next_week' ? 7 : 0))
    const e = new Date(s)
    e.setDate(e.getDate() + 7)
    return { start: s, end: e, label: slots.range === 'week' ? 'this week' : 'next week', days: 7 }
  }
  const s = slots.day ? startOf(new Date(`${slots.day.iso}T00:00:00`)) : startOf(now)
  const e = new Date(s)
  e.setDate(e.getDate() + 1)
  return { start: s, end: e, label: slots.day ? `${slots.day.label} · ${slots.day.weekday}` : 'today', days: 1 }
}

function overlaps(e: CalEvent, hour: number, minute: number, day: Date, durMin = 30) {
  const t = new Date(day)
  t.setHours(hour, minute, 0, 0)
  const t2 = t.getTime() + durMin * 60_000
  return !e.allDay && e.start.getTime() < t2 && e.end.getTime() > t.getTime()
}

function matchesPerson(e: CalEvent, who?: string) {
  if (!who) return true
  const w = who.toLowerCase()
  return e.attendees.some((a) => a.email.toLowerCase().includes(w) || (a.name ?? '').toLowerCase().includes(w)) || e.title.toLowerCase().includes(w)
}

// ─── view ────────────────────────────────────────────────────────────────

export function CalendarView({ slots, crumbs, onClose, onToast, handoff }: WidgetProps) {
  const state = useCalendarState()
  const active = state === 'active'
  const sendMessage = useHarness((s) => s.sendMessage)
  const range = useMemo(() => rangeFor(slots), [slots.day?.iso, slots.range])
  const who = slots.attendees[0] ?? slots.person
  const { data, loading, error } = useAsync(() => listEvents(range.start, range.end), `${range.start.toISOString()}|${range.end.toISOString()}`, active)

  const events = useMemo(() => (data?.events ?? []).filter((e) => matchesPerson(e, who)), [data, who])
  const now = Date.now()
  const next = events.find((e) => e.end.getTime() > now && !e.allDay)
  const probe = slots.time
  const conflicts = probe ? events.filter((e) => overlaps(e, probe.hour, probe.minute, range.start)) : []

  const fallback = `List my Google Calendar events for ${range.label}${who ? ` that involve ${who}` : ''}${
    probe ? `, and say whether I am free around ${probe.label}` : ''
  }. Show time, title, attendees, and the meeting link.`

  const dayUrl = `https://calendar.google.com/calendar/r/${range.days === 7 ? 'week' : 'day'}/${range.start.getFullYear()}/${range.start.getMonth() + 1}/${range.start.getDate()}`

  return (
    <Frame
      kicker="Calendar"
      crumbs={crumbs}
      title={`Agenda · ${range.label}${who ? ` · with ${who}` : ''}`}
      badge={active ? (loading ? 'loading…' : `${events.length} event${events.length === 1 ? '' : 's'}`) : 'not connected'}
      footer={
        <>
          <Btn
            onClick={() => {
              useHarness.getState().setInputDraft(fallback)
              onClose()
            }}
          >
            Ask Freyja
          </Btn>
          {active && next?.joinUrl ? (
            <Btn
              primary
              onClick={() => {
                openExternal(next.joinUrl!)
                onClose()
              }}
            >
              Join “{next.title.slice(0, 28)}”
            </Btn>
          ) : active ? (
            <Btn
              primary
              onClick={() => {
                openExternal(dayUrl)
                onClose()
              }}
            >
              Open in Google Calendar
            </Btn>
          ) : (
            <Btn
              primary
              onClick={() => {
                void sendMessage(fallback)
                onToast(`Fetching agenda for ${range.label}`)
                onClose()
              }}
            >
              Fetch via agent
            </Btn>
          )}
        </>
      }
    >
      {!active && <CalendarConnect onToast={onToast} />}
      {active && probe && (
        <Note tone={conflicts.length ? 'warn' : 'ok'}>
          {conflicts.length
            ? `Busy around ${probe.label}: ${conflicts.map((c) => c.title).join(', ')}`
            : `Free around ${probe.label}${who ? ` (on your calendar; ${who}'s not checked)` : ''}`}
        </Note>
      )}
      {active && error && <Note tone="danger">{error}</Note>}
      {active && loading && !data && <Pending label="Reading your calendar…" />}
      {active && data && (
        <div className="max-h-[260px] space-y-1 overflow-y-auto">
          {events.map((e) => {
            const isConflict = conflicts.includes(e)
            const past = e.end.getTime() < now
            const others = e.attendees.filter((a) => !a.self)
            return (
              <Row
                key={e.id}
                active={isConflict}
                title={<span className={past ? 'text-fg-3' : ''}>{e.title}</span>}
                meta={
                  <>
                    {e.allDay ? 'all day' : `${fmtTime(e.start)} – ${fmtTime(e.end)}`}
                    {range.days === 7 && ` · ${e.start.toLocaleDateString(undefined, { weekday: 'short' })}`}
                    {others.length > 0 && ` · ${others
                      .slice(0, 3)
                      .map((a) => a.name ?? a.email.split('@')[0])
                      .join(', ')}${others.length > 3 ? ` +${others.length - 3}` : ''}`}
                    {e.location && !e.joinUrl && ` · ${e.location}`}
                  </>
                }
                onClick={() => e.link && openExternal(e.link)}
                right={e === next ? <span className="kbd text-[8.5px]">next</span> : undefined}
                actions={
                  <>
                    {e.joinUrl && <RowAction onClick={() => openExternal(e.joinUrl!)}>join</RowAction>}
                    <RowAction
                      title="Open a create card pre-filled from this event"
                      onClick={() =>
                        handoff({
                          type: 'calendar_create',
                          label: 'Reschedule',
                          slots: {
                            attendees: others.map((a) => a.name ?? a.email.split('@')[0]).slice(0, 4),
                            topic: e.title,
                            durationMin: Math.max(15, Math.round((e.end.getTime() - e.start.getTime()) / 60_000)),
                          },
                          context: { rescheduleOf: { id: e.id, title: e.title, start: e.start.toISOString(), link: e.link } },
                        })
                      }
                    >
                      reschedule
                    </RowAction>
                  </>
                }
              />
            )
          })}
          {events.length === 0 && <div className="py-3 text-center text-[11px] text-fg-3">Nothing on the calendar{who ? ` with ${who}` : ''} {range.label}.</div>}
        </div>
      )}
    </Frame>
  )
}

// ─── create ──────────────────────────────────────────────────────────────

function timeRange(t: TimeSlot | undefined, dur: number) {
  if (!t) return undefined
  const end = new Date(2000, 0, 1, t.hour, t.minute + dur)
  return `${t.label} – ${fmtTime(end)}`
}

function slotFromDate(d: Date): TimeSlot {
  return { label: fmtTime(d), hour: d.getHours(), minute: d.getMinutes() }
}

export function CalendarCreate({ type, slots, context, crumbs, onClose, onToast }: WidgetProps) {
  const state = useCalendarState()
  const active = state === 'active'
  const sendMessage = useHarness((s) => s.sendMessage)
  const reschedule = context?.rescheduleOf as { id: string; title: string; start: string; link?: string } | undefined

  const [pickedTime, setPickedTime] = useState<TimeSlot | null>(null)
  useEffect(() => setPickedTime(null), [slots.time?.hour, slots.time?.minute])
  const time = slots.time ?? pickedTime ?? undefined

  const req = requiredSlots(type)
  const filled = req.filter((k) => (k === 'time' ? !!time : slotFilled(slots, k))).length
  const ready = filled === req.length
  const dur = slots.durationMin ?? 30
  const who = slots.attendees.join(', ')
  const title = slots.topic ? slots.topic : who ? `Sync with ${who}` : undefined

  const day = slots.day ? new Date(`${slots.day.iso}T00:00:00`) : undefined
  const dayEnd = day ? new Date(day.getTime() + 86_400_000) : undefined
  const { data, loading } = useAsync(() => listEvents(day!, dayEnd!), slots.day?.iso ?? '', active && !!day)
  const dayEvents = data?.events ?? []
  const conflicts = time && day ? dayEvents.filter((e) => overlaps(e, time.hour, time.minute, day, dur)) : []
  const free = day ? freeSlots(day, dayEvents, dur).slice(0, 6) : []

  const instruction = () => {
    const parts = reschedule ? [`Move my Google Calendar event "${reschedule.title}" (event id ${reschedule.id}, currently ${new Date(reschedule.start).toLocaleString()})`] : ['Create a Google Calendar event']
    if (!reschedule && title) parts.push(`titled "${title}"`)
    if (who) parts.push(`with ${who}`)
    if (slots.day) parts.push(`${reschedule ? 'to' : 'on'} ${slots.day.weekday} ${slots.day.iso}`)
    if (time) parts.push(`at ${time.label}`)
    parts.push(`for ${dur} minutes`)
    return parts.join(' ') + `.${active ? ' Use the google_calendar_hosted MCP.' : ''} Send the invite and confirm the event link.`
  }

  return (
    <Frame
      kicker="Calendar"
      crumbs={crumbs}
      title={reschedule ? `Reschedule · ${reschedule.title}` : (title ?? 'New event')}
      badge={<Progress filled={filled} total={req.length} />}
      footer={
        <>
          <Btn
            onClick={() => {
              useHarness.getState().setInputDraft(instruction())
              onClose()
            }}
          >
            Edit as prompt
          </Btn>
          <Btn
            primary
            disabled={!ready}
            title={conflicts.length ? 'Overlaps an existing event' : undefined}
            onClick={() => {
              void sendMessage(instruction())
              onToast(`${reschedule ? 'Moving' : 'Creating'} event${who ? ` with ${who}` : ''} · ${slots.day?.label} ${time?.label}`)
              onClose()
            }}
          >
            {reschedule ? 'Move event' : 'Create event'}
            {conflicts.length ? ' anyway' : ''}
          </Btn>
        </>
      }
    >
      <Well className="space-y-1.5">
        <Slot label="With" value={who || undefined} hint="who? add “with nari”" />
        <Slot label="When" value={slots.day ? `${slots.day.label.toLowerCase() === slots.day.weekday.toLowerCase() ? '' : `${slots.day.label} · `}${slots.day.weekday} ${slots.day.iso}` : undefined} hint="which day? “tomorrow”, “next tuesday”" />
        <Slot label="Time" value={timeRange(time, dur)} hint={free.length ? 'pick a free slot below or type “at 1pm”' : 'what time? “at 1pm”'} />
        <Slot label="Length" value={`${dur} min${slots.durationMin ? '' : ' (default)'}`} hint="" />
        {slots.topic && <Slot label="About" value={slots.topic} hint="" />}
      </Well>
      {slots.day && !active && <CalendarConnect onToast={onToast} />}
      {slots.day && active && (
        <div className="space-y-1.5">
          <div className="flex items-center justify-between">
            <Label>{slots.day.weekday} · {loading && !data ? 'checking your calendar…' : `${dayEvents.filter((e) => !e.allDay).length} busy, ${free.length} open slot${free.length === 1 ? '' : 's'} for ${dur} min`}</Label>
          </div>
          {conflicts.length > 0 && (
            <Note tone="warn">
              Overlaps {conflicts.map((c) => `“${c.title}” (${fmtTime(c.start)}–${fmtTime(c.end)})`).join(', ')}
            </Note>
          )}
          {free.length > 0 && (
            <div className="flex flex-wrap gap-1">
              {free.map((f) => {
                const on = time?.hour === f.start.getHours() && time?.minute === f.start.getMinutes()
                return (
                  <Chip key={f.start.toISOString()} on={on} tone="ok" onClick={() => setPickedTime(slotFromDate(f.start))}>
                    {fmtTime(f.start)}
                    {f.end.getTime() - f.start.getTime() > dur * 60_000 * 1.5 ? ` – ${fmtTime(f.end)}` : ''}
                  </Chip>
                )
              })}
            </div>
          )}
          {data && dayEvents.length > 0 && (
            <div className="max-h-[120px] space-y-0.5 overflow-y-auto">
              {dayEvents
                .filter((e) => !e.allDay)
                .map((e) => (
                  <div key={e.id} className={`flex items-center justify-between rounded px-2 py-0.5 text-[10.5px] ${conflicts.includes(e) ? 'bg-warn/10 text-warn' : 'text-fg-3'}`}>
                    <span className="truncate">{e.title}</span>
                    <span className="ml-2 shrink-0">
                      {fmtTime(e.start)} – {fmtTime(e.end)}
                    </span>
                  </div>
                ))}
            </div>
          )}
        </div>
      )}
    </Frame>
  )
}
