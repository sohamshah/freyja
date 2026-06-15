/**
 * Morning Room — the daily briefing landing view.
 *
 * Renders the letterpress briefing (see mockups/morning-room/
 * morning-room-o-letterpress.html for the design reference) from the
 * structured contract the briefer job writes to
 * ~/.freyja/briefing/{date}/briefing.json (authored in
 * bridge/briefing.py; typed as BriefingDoc in src/shared/events.ts).
 *
 * Design translation notes vs the mockup:
 *  · No Three.js wisp backdrop — keeps the bundle dependency-free; the
 *    SVG grain + vignette carry the atmosphere.
 *  · Hero descrambler is a small rAF loop instead of GSAP.
 *  · State encoded by glyph shape (● ◐ ▲ ·), hairline dividers, one
 *    steel-blue accent, ▸/→ text-link actions — all per the mockup.
 *
 * Action dispatch: every decision action / today line carries an
 * intent the user commits explicitly —
 *  · open_session → switch to that session and close the room
 *  · fire_job     → scheduler.run_job_now
 *  · prompt       → new session + send the staged prompt
 * The briefer only stages; nothing here runs without a click.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useHarness } from '../state/store'
import type {
  BriefingDoc,
  BriefingIntent,
  BriefingProject,
} from '@shared/events'

const STATE_GLYPH: Record<string, string> = {
  ready: '●',
  in_motion: '◐',
  blocked: '▲',
  quiet: '·',
}

const STATE_LABEL: Record<string, string> = {
  ready: 'ready',
  in_motion: 'in motion',
  blocked: 'blocked',
  quiet: 'quiet',
}

/** Local-calendar YYYY-MM-DD. The briefer writes its date dir with
 *  `date +%F` (local), so all comparisons must be local too —
 *  `toISOString()` is UTC and breaks for anyone east of Greenwich in
 *  the morning and west of it in the evening. en-CA formats as
 *  YYYY-MM-DD. */
export function localToday(): string {
  return new Date().toLocaleDateString('en-CA')
}

interface BriefingState {
  loading: boolean
  dates: string[]
  date: string | null
  doc: BriefingDoc | null
  markdown: string | null
  brieferJobId: string | null
}

export function MorningRoom() {
  const toggleMorningRoom = useHarness((s) => s.toggleMorningRoom)
  const switchSession = useHarness((s) => s.switchSession)
  const newSession = useHarness((s) => s.newSession)
  const sendMessage = useHarness((s) => s.sendMessage)
  const showToast = useHarness((s) => s.showToast)

  const [b, setB] = useState<BriefingState>({
    loading: true,
    dates: [],
    date: null,
    doc: null,
    markdown: null,
    brieferJobId: null,
  })
  const [generating, setGenerating] = useState(false)
  const [dispatched, setDispatched] = useState<Set<string>>(new Set())
  // Ref mirror of `dispatched` — async loops (runAllToday) read THIS so
  // an item dispatched individually mid-loop isn't double-run off a
  // stale closure. State drives render; the ref drives correctness.
  const dispatchedRef = useRef<Set<string>>(new Set())
  const markDispatched = useCallback((key: string) => {
    dispatchedRef.current.add(key)
    setDispatched(new Set(dispatchedRef.current))
  }, [])
  const unmarkDispatched = useCallback((key: string) => {
    dispatchedRef.current.delete(key)
    setDispatched(new Set(dispatchedRef.current))
  }, [])
  // generate-now poll timers — cleared on unmount so a closed room
  // doesn't keep a 10-minute interval alive with setState calls.
  const pollTimersRef = useRef<{ interval?: any; timeout?: any }>({})

  const load = useCallback(async (date?: string) => {
    const api = (window as any).harness
    if (!api?.getBriefing) {
      // Demo / browser mode without the preload bridge — show the empty
      // state instead of an eternal spinner.
      setB((p) => ({ ...p, loading: false }))
      return
    }
    const res = await api.getBriefing(date)
    setB({
      loading: false,
      dates: res.dates ?? [],
      date: res.date ?? null,
      doc: res.json ?? null,
      markdown: res.markdown ?? null,
      brieferJobId: res.brieferJobId ?? null,
    })
  }, [])

  useEffect(() => {
    load().catch(() => setB((p) => ({ ...p, loading: false })))
  }, [load])

  // Esc closes — but only when the room is actually the topmost
  // surface, and never on modified Esc (⌘Esc cancels the turn,
  // ⌘⇧Esc / triple-Esc is the computer emergency stop — those must
  // pass through untouched).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey) return
      const s = useHarness.getState()
      if (
        s.commandPaletteOpen ||
        s.missionDashboardOpen ||
        s.settingsOpen ||
        s.modelPickerOpen ||
        s.recallDrawer.open
      ) {
        return // a stacked overlay owns this Esc
      }
      e.stopPropagation()
      toggleMorningRoom(false)
    }
    // Bubble phase (not capture) so capture-phase listeners on stacked
    // overlays run first and can stopPropagation before we see it.
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [toggleMorningRoom])

  // ── Intent dispatch ─────────────────────────────────────────────
  const dispatchIntent = useCallback(
    async (intent: BriefingIntent | null | undefined, key: string) => {
      if (!intent) return
      if (dispatchedRef.current.has(key)) return
      markDispatched(key)
      try {
        if (intent.kind === 'open_session' && intent.session_id) {
          await switchSession(intent.session_id)
          toggleMorningRoom(false)
        } else if (intent.kind === 'fire_job' && intent.job_id) {
          const { schedulerApi } = await import('../state/scheduler-store')
          await schedulerApi.runJobNow(intent.job_id)
          showToast('Job started', 'info')
        } else if (intent.kind === 'prompt' && intent.prompt) {
          await newSession()
          await sendMessage(intent.prompt)
          toggleMorningRoom(false)
        }
      } catch (err) {
        showToast(`Action failed: ${String(err).slice(0, 80)}`, 'warn')
        unmarkDispatched(key)
      }
    },
    [
      switchSession,
      newSession,
      sendMessage,
      showToast,
      toggleMorningRoom,
      markDispatched,
      unmarkDispatched,
    ],
  )

  const runAllToday = useCallback(async () => {
    const items = b.doc?.today ?? []
    let started = 0
    for (let i = 0; i < items.length; i++) {
      const intent = items[i]?.intent
      // open_session items need the user present — batch dispatch skips
      // them WITHOUT marking, so their individual buttons stay live.
      if (!intent || intent.kind === 'open_session') continue
      const key = `today-${i}`
      // Read the ref, not the state closure — an item the user ran
      // individually while this loop awaited must not run twice.
      if (dispatchedRef.current.has(key)) continue
      markDispatched(key)
      try {
        if (intent.kind === 'fire_job' && intent.job_id) {
          const { schedulerApi } = await import('../state/scheduler-store')
          await schedulerApi.runJobNow(intent.job_id)
          started++
        } else if (intent.kind === 'prompt' && intent.prompt) {
          await newSession()
          await sendMessage(intent.prompt)
          started++
        }
      } catch {
        unmarkDispatched(key) // per-item failure shouldn't halt the batch
      }
    }
    showToast(
      started > 0 ? `${started} run${started === 1 ? '' : 's'} dispatched` : 'Nothing to dispatch',
      'info',
    )
  }, [b.doc, newSession, sendMessage, showToast, markDispatched, unmarkDispatched])

  const generateNow = useCallback(async () => {
    if (!b.brieferJobId || generating) return
    setGenerating(true)
    // Snapshot the current edition's stamp so the poll can tell a
    // FRESH briefing from the one already on screen — when rebriefing,
    // today's briefing.json already exists, so "file present" isn't
    // enough; we wait until generated_at_iso (or the date) changes.
    const beforeStamp = b.doc?.generated_at_iso ?? null
    const beforeDate = b.date
    try {
      const { schedulerApi } = await import('../state/scheduler-store')
      await schedulerApi.runJobNow(b.brieferJobId)
      showToast('Briefer running — this takes a couple of minutes', 'info')
      // Timers held in a ref + cleared on unmount so closing the room
      // doesn't leave a 10-minute orphan interval calling setState.
      clearInterval(pollTimersRef.current.interval)
      clearTimeout(pollTimersRef.current.timeout)
      pollTimersRef.current.interval = setInterval(async () => {
        const api = (window as any).harness
        const res = await api?.getBriefing?.()
        const isNew =
          res?.json &&
          (res.date !== beforeDate ||
            (res.json.generated_at_iso ?? null) !== beforeStamp)
        if (isNew) {
          clearInterval(pollTimersRef.current.interval)
          clearTimeout(pollTimersRef.current.timeout)
          setGenerating(false)
          load().catch(() => {})
          showToast('Briefing refreshed', 'info')
        }
      }, 5000)
      pollTimersRef.current.timeout = setTimeout(() => {
        clearInterval(pollTimersRef.current.interval)
        setGenerating(false)
      }, 600_000)
    } catch (err) {
      setGenerating(false)
      showToast(`Briefer failed to start: ${String(err).slice(0, 80)}`, 'warn')
    }
  }, [b.brieferJobId, b.doc, b.date, generating, load, showToast])

  // Clear any in-flight generate-now poll when the room unmounts.
  useEffect(() => {
    return () => {
      clearInterval(pollTimersRef.current.interval)
      clearTimeout(pollTimersRef.current.timeout)
    }
  }, [])

  const doc = b.doc
  // Only fire_job/prompt intents are batch-dispatchable — open_session
  // rows need the user present, so they don't count toward "run all N".
  const todayCount =
    doc?.today?.filter(
      (t) => t.intent && t.intent.kind !== 'open_session',
    ).length ?? 0

  return (
    <div className="mroom-overlay">
      <div className="mroom-grain" aria-hidden="true" />
      <header className="mroom-chrome">
        <div className="mroom-brand">
          <span className="mroom-mark" />
          <span className="mroom-brand-room">morning room</span>
          {b.date && b.date !== localToday() && (
            <span className="mroom-brand-stale">· {b.date}</span>
          )}
        </div>
        <div className="mroom-chrome-right">
          {(doc?.decisions?.length ?? 0) > 0 && (
            <span className="mroom-waiting">
              {doc!.decisions!.length} decision{doc!.decisions!.length === 1 ? '' : 's'} waiting
            </span>
          )}
          {b.dates.length > 1 && (
            <select
              className="mroom-date-select"
              value={b.date ?? ''}
              onChange={(e) => {
                setB((p) => ({ ...p, loading: true }))
                load(e.target.value).catch(() => {})
              }}
              title="View a past briefing"
            >
              {b.dates.map((d) => (
                <option key={d} value={d}>
                  {d === localToday() ? `${d} · today` : d}
                </option>
              ))}
            </select>
          )}
          {b.brieferJobId && (
            <button
              className="mroom-rebrief"
              onClick={generateNow}
              disabled={generating}
              title="Regenerate today’s briefing now"
            >
              {generating ? '↻ generating…' : '↻ rebrief'}
            </button>
          )}
          <button
            className="mroom-close"
            onClick={() => toggleMorningRoom(false)}
            aria-label="Close"
          >
            ESC · CLOSE
          </button>
        </div>
      </header>

      <main className="mroom-brief">
        {b.loading ? (
          <div className="mroom-empty">loading…</div>
        ) : !doc ? (
          <EmptyState
            markdown={b.markdown}
            generating={generating}
            canGenerate={!!b.brieferJobId}
            onGenerate={generateNow}
          />
        ) : (
          <>
            <Hero doc={doc} />
            {(doc.projects?.length ?? 0) > 0 && (
              <section className="mroom-section">
                <SectionLabel
                  label="Projects"
                  count={`${doc.projects!.filter((p) => p.state !== 'quiet').length} in motion`}
                />
                {doc.projects!.map((p, i) => (
                  <ProjectLine
                    key={i}
                    project={p}
                    onOpen={() =>
                      p.session_id
                        ? dispatchIntent(
                            { kind: 'open_session', session_id: p.session_id },
                            `proj-${i}`,
                          )
                        : undefined
                    }
                  />
                ))}
              </section>
            )}

            {(doc.decisions?.length ?? 0) > 0 && (
              <section className="mroom-section">
                <SectionLabel
                  label="Needs you"
                  count={`${doc.decisions!.length} decision${doc.decisions!.length === 1 ? '' : 's'}`}
                />
                {doc.decisions!.map((d, i) => (
                  <div className="mroom-decision" key={i}>
                    <div className="mroom-decision-idx">
                      {String(i + 1).padStart(2, '0')}
                    </div>
                    <div>
                      <div className="mroom-decision-head">
                        <span className="mroom-verb">{d.verb}</span>
                        <span className="mroom-div">·</span>
                        <span className="mroom-proj">{d.project}</span>
                        {d.ref && <span className="mroom-ref">{d.ref}</span>}
                        {d.meta && <span className="mroom-meta">{d.meta}</span>}
                      </div>
                      <div className="mroom-decision-body">{d.body}</div>
                      <div className="mroom-acts">
                        {(d.actions ?? []).map((a, j) => {
                          const key = `dec-${i}-${j}`
                          const done = dispatched.has(key)
                          return (
                            <button
                              key={j}
                              className={
                                a.kind === 'primary'
                                  ? 'mroom-act mroom-act-primary'
                                  : 'mroom-act'
                              }
                              disabled={done || !a.intent}
                              onClick={() => dispatchIntent(a.intent, key)}
                            >
                              {done ? '✓ ' : ''}
                              {a.label}
                            </button>
                          )
                        })}
                      </div>
                    </div>
                  </div>
                ))}
              </section>
            )}

            {(doc.today?.length ?? 0) > 0 && (
              <section className="mroom-section">
                <SectionLabel
                  label="Today"
                  count={`${doc.today!.length} staged`}
                />
                {doc.today!.map((t, i) => {
                  const key = `today-${i}`
                  const done = dispatched.has(key)
                  return (
                    <div className="mroom-today-line" key={i}>
                      <span className="mroom-today-num">{i + 1}</span>
                      <span className="mroom-today-when">{t.time ?? '—'}</span>
                      <span className="mroom-today-proj">{t.project}</span>
                      <span className="mroom-today-what">{t.what}</span>
                      <span className="mroom-today-dur">{t.duration ?? ''}</span>
                      <button
                        className="mroom-run"
                        disabled={done || !t.intent}
                        onClick={() => dispatchIntent(t.intent, key)}
                      >
                        {done ? 'started' : 'run'}
                      </button>
                    </div>
                  )
                })}
              </section>
            )}

            {doc.colophon && (
              <div className="mroom-colophon">
                <span>{doc.colophon}</span>
                <span>{doc.date}</span>
              </div>
            )}
          </>
        )}
      </main>

      {doc && todayCount > 0 && (
        <div className="mroom-commit">
          <div className="mroom-commit-left">
            <em>{todayCount}</em> staged for today
          </div>
          <button className="mroom-commit-primary" onClick={runAllToday}>
            run all {todayCount} in parallel
          </button>
        </div>
      )}

      <style>{STYLES}</style>
    </div>
  )
}

// ─── Pieces ──────────────────────────────────────────────────────────

function SectionLabel(props: { label: string; count?: string }) {
  return (
    <div className="mroom-section-label">
      <span className="mroom-bar">▌</span>
      <span className="mroom-lbl">{props.label}</span>
      {props.count && <span className="mroom-count">{props.count}</span>}
    </div>
  )
}

function ProjectLine(props: { project: BriefingProject; onOpen?: () => void }) {
  const { project: p, onOpen } = props
  return (
    <div className={p.attention ? 'mroom-proj-line attention' : 'mroom-proj-line'}>
      <span className="mroom-glyph">{STATE_GLYPH[p.state] ?? '·'}</span>
      <span className="mroom-proj-name">{p.name}</span>
      <span className="mroom-proj-stat">{STATE_LABEL[p.state] ?? p.state}</span>
      <span className="mroom-proj-summary">{p.summary}</span>
      {p.session_id ? (
        <button className="mroom-open" onClick={onOpen}>
          open
        </button>
      ) : (
        <span />
      )}
    </div>
  )
}

function Hero(props: { doc: BriefingDoc }) {
  const { doc } = props
  const ref = useRef<HTMLHeadingElement | null>(null)
  const projects = doc.hero?.projects_in_motion ?? doc.projects?.length ?? 0
  const events = doc.hero?.events_since ?? 0
  const since = doc.since_label ?? 'your last visit'
  const headline = useMemo(
    () =>
      `${projects} project${projects === 1 ? '' : 's'} in motion. ` +
      `${events} thing${events === 1 ? '' : 's'} since ${since}.`,
    [projects, events, since],
  )

  // Small rAF descrambler — chars resolve left-to-right over ~900ms.
  useEffect(() => {
    const el = ref.current
    if (!el) return
    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches
    if (reduced) {
      el.textContent = headline
      return
    }
    const GLYPHS = '▖▗▘▙▚▛▜▝▞▟abcdefghijklmnop0123456789'
    const start = performance.now()
    const duration = 900
    let raf = 0
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / duration)
      const lockCount = Math.floor(t * headline.length)
      let out = ''
      for (let i = 0; i < headline.length; i++) {
        if (i < lockCount || headline[i] === ' ') out += headline[i]
        else out += GLYPHS[Math.floor(Math.random() * GLYPHS.length)]
      }
      el.textContent = out
      if (t < 1) raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [headline])

  const kicker = useMemo(() => {
    const d = doc.date ? new Date(doc.date + 'T12:00:00') : new Date()
    const day = d.toLocaleDateString(undefined, { weekday: 'long' }).toLowerCase()
    const md = d
      .toLocaleDateString(undefined, { month: 'long', day: '2-digit' })
      .toLowerCase()
    return `${day} · ${md}`
  }, [doc.date])

  return (
    <div className="mroom-hero">
      <div className="mroom-kicker">
        <span className="mroom-pulse" />
        <span>{kicker}</span>
        {doc.generated_at_iso && (
          <>
            <span>·</span>
            <span className="mroom-kicker-em">
              {new Date(doc.generated_at_iso).toLocaleTimeString(undefined, {
                hour: '2-digit',
                minute: '2-digit',
              })}
            </span>
          </>
        )}
      </div>
      <h1 ref={ref}>{headline}</h1>
    </div>
  )
}

function EmptyState(props: {
  markdown: string | null
  generating: boolean
  canGenerate: boolean
  onGenerate: () => void
}) {
  return (
    <div className="mroom-empty">
      <div className="mroom-empty-glyph">·</div>
      <p>No briefing for today yet.</p>
      {props.markdown ? (
        <p className="mroom-empty-sub">
          A narrative briefing exists but its structured form didn't parse —
          regenerate, or read briefing.md in ~/.freyja/briefing/.
        </p>
      ) : (
        <p className="mroom-empty-sub">
          The briefer runs every morning at 06:00. It reads your sessions,
          projects, and scheduled jobs, and stages the day.
        </p>
      )}
      {props.canGenerate && (
        <button
          className="mroom-commit-primary"
          disabled={props.generating}
          onClick={props.onGenerate}
        >
          {props.generating ? 'generating…' : 'generate briefing now'}
        </button>
      )}
    </div>
  )
}

// ─── Styles — letterpress system from the mockup ─────────────────────

const STYLES = `
.mroom-overlay {
  /* z-45: above the shell, BELOW the z-50 modals (PermissionPrompt,
     MissionDashboard, ScheduledJobsDashboard) and the z-60 emergency
     panic — a hidden permission prompt must never be buried under the
     landing view. */
  position: fixed; inset: 0; z-index: 45;
  background: #06070b; color: #e8e8e8;
  font-family: 'Geist Mono', ui-monospace, 'SF Mono', Menlo, monospace;
  font-size: 12px; line-height: 1.55;
  overflow-y: auto;
  font-variant-numeric: tabular-nums;
}
.mroom-overlay::before {
  content: ""; position: fixed; inset: 0; pointer-events: none;
  background: radial-gradient(ellipse 110% 130% at 50% 50%, transparent 35%, rgba(6,7,11,0.40) 100%);
}
.mroom-grain {
  position: fixed; inset: 0; pointer-events: none; z-index: 1;
  opacity: 0.14; mix-blend-mode: screen;
  background-image: url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='560' height='560'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='1' stitchTiles='stitch' seed='3'/><feColorMatrix values='1.5 0 0 0 -0.45 0 1.5 0 0 -0.45 0 0 1.5 0 -0.45 0 0 0 1 0'/></filter><rect width='100%25' height='100%25' filter='url(%23n)'/></svg>");
  background-size: 560px 560px;
}
.mroom-chrome {
  position: sticky; top: 0; z-index: 30;
  display: flex; justify-content: space-between; align-items: center;
  /* Left inset clears the macOS traffic lights (OS-drawn at ~0-78px);
     88px matches the app's other full-screen modals (MissionDashboard,
     ScheduledJobsDashboard pl-[88px]). 46px height matches the real
     title bar so the takeover lines up with the window frame. */
  height: 46px;
  padding: 0 16px 0 88px;
  background: linear-gradient(180deg, rgba(6,7,11,0.96), rgba(6,7,11,0.82));
  backdrop-filter: blur(16px) saturate(140%);
  border-bottom: 1px solid rgba(255,255,255,0.06);
  font-size: 10px; letter-spacing: 0.18em; text-transform: uppercase;
  color: #6e6e6e;
  /* The strip is a drag handle so the window still moves; controls
     below opt out via .no-drag equivalents. */
  -webkit-app-region: drag;
}
.mroom-chrome button,
.mroom-chrome select { -webkit-app-region: no-drag; }
.mroom-brand { display: flex; align-items: center; gap: 10px; }
.mroom-mark {
  width: 11px; height: 11px; border-radius: 2px;
  background: linear-gradient(135deg, #c4e0fc, #7aafea);
  box-shadow: 0 0 10px rgba(168,212,252,0.55);
}
.mroom-brand-room { color: #e8e8e8; letter-spacing: 0.18em; }
.mroom-brand-stale { color: #b8a078; letter-spacing: 0.1em; }
.mroom-chrome-right { display: flex; align-items: center; gap: 12px; }
.mroom-waiting { color: #a8d4fc; letter-spacing: 0.1em; }
.mroom-date-select {
  background: rgba(255,255,255,0.04); color: #a8a8a8;
  border: 1px solid rgba(255,255,255,0.08); border-radius: 4px;
  font-family: inherit; font-size: 9px; letter-spacing: 0.06em;
  padding: 3px 6px; cursor: pointer; text-transform: none;
}
.mroom-rebrief, .mroom-close {
  background: none; border: 1px solid rgba(255,255,255,0.08);
  color: #6e6e6e; font-family: inherit; font-size: 9px;
  letter-spacing: 0.14em; padding: 4px 8px; border-radius: 4px;
  cursor: pointer; text-transform: uppercase;
}
.mroom-rebrief:hover, .mroom-close:hover {
  color: #e8e8e8; border-color: rgba(255,255,255,0.18);
}
.mroom-rebrief { color: #a8d4fc; }
.mroom-rebrief:hover { color: #c4e0fc; border-color: rgba(168,212,252,0.4); }
.mroom-rebrief:disabled { color: #4a4a4a; cursor: default; }
.mroom-brief {
  position: relative; z-index: 5;
  max-width: 760px; margin: 0 auto;
  padding: 38px 32px 140px;
}
.mroom-hero { margin-bottom: 32px; }
.mroom-kicker {
  display: flex; align-items: center; gap: 10px;
  font-size: 10px; letter-spacing: 0.30em; text-transform: uppercase;
  color: #4a4a4a; margin-bottom: 14px;
}
.mroom-kicker-em { color: #a8d4fc; }
.mroom-pulse {
  width: 6px; height: 6px; border-radius: 999px; background: #a8d4fc;
  box-shadow: 0 0 8px #a8d4fc;
  animation: mroom-pulse 2.4s ease-in-out infinite;
}
@keyframes mroom-pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.4; } }
@media (prefers-reduced-motion: reduce) { .mroom-pulse { animation: none; } }
.mroom-hero h1 {
  font-weight: 400; font-size: 30px; line-height: 1.3;
  margin: 0; color: #e8e8e8; text-wrap: balance;
  min-height: 1.3em;
}
.mroom-section { margin-bottom: 36px; }
.mroom-section-label {
  display: flex; align-items: baseline; gap: 8px;
  margin-bottom: 4px;
  font-size: 9.5px; letter-spacing: 0.22em; text-transform: uppercase;
  color: #4a4a4a;
}
.mroom-bar { color: #a8d4fc; }
.mroom-lbl { color: #a8a8a8; }
.mroom-count { margin-left: auto; color: #4a4a4a; letter-spacing: 0.1em; }
.mroom-proj-line {
  display: grid;
  grid-template-columns: 18px minmax(150px, auto) 80px 1fr auto;
  gap: 14px; padding: 13px 0; align-items: baseline;
  border-bottom: 1px solid rgba(255,255,255,0.06);
  font-size: 12px;
}
.mroom-proj-line:last-child { border-bottom: none; }
.mroom-glyph { font-size: 13px; color: #4a4a4a; text-align: center; }
.mroom-proj-line.attention .mroom-glyph { color: #a8d4fc; }
.mroom-proj-name { color: #e8e8e8; font-weight: 500; }
.mroom-proj-stat {
  color: #4a4a4a; font-size: 9.5px; letter-spacing: 0.22em;
  text-transform: uppercase;
}
.mroom-proj-line.attention .mroom-proj-stat { color: #a8d4fc; }
.mroom-proj-summary { color: #a8a8a8; line-height: 1.5; max-width: 60ch; }
.mroom-open, .mroom-run {
  background: none; border: none; padding: 0;
  font-family: inherit; font-size: 10px; letter-spacing: 0.10em;
  text-transform: uppercase; color: #4a4a4a; cursor: pointer;
  transition: color 120ms ease;
}
.mroom-open::before { content: '→ '; color: #2a2a2a; }
.mroom-open:hover, .mroom-open:hover::before { color: #a8d4fc; }
.mroom-run { color: #a8d4fc; }
.mroom-run::before { content: '▸ '; }
.mroom-run:hover { color: #c4e0fc; }
.mroom-run:disabled { color: #4a4a4a; cursor: default; }
.mroom-decision {
  display: grid; grid-template-columns: 38px 1fr; gap: 18px;
  padding: 24px 0 22px;
  border-bottom: 1px solid rgba(255,255,255,0.06);
}
.mroom-decision:last-child { border-bottom: none; }
.mroom-decision-idx {
  font-size: 9.5px; letter-spacing: 0.22em; color: #4a4a4a;
  text-align: right; padding-top: 2px;
}
.mroom-decision-head {
  display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap;
  margin-bottom: 12px;
}
.mroom-verb {
  color: #a8d4fc; font-size: 11.5px; font-weight: 600;
  letter-spacing: 0.22em; text-transform: uppercase;
}
.mroom-div { color: #2a2a2a; }
.mroom-proj {
  color: #a8a8a8; font-size: 10.5px; letter-spacing: 0.16em;
  text-transform: uppercase;
}
.mroom-ref { color: #6e6e6e; font-size: 11px; }
.mroom-meta {
  margin-left: auto; color: #4a4a4a; font-size: 9.5px;
  letter-spacing: 0.18em; text-transform: uppercase;
}
.mroom-decision-body {
  color: #e8e8e8; font-size: 12.5px; line-height: 1.7;
  max-width: 64ch; margin-bottom: 18px;
}
.mroom-acts { display: flex; flex-direction: column; gap: 7px; }
.mroom-act {
  background: none; border: none; padding: 0;
  font-family: inherit; font-size: 12px; color: #6e6e6e;
  cursor: pointer; text-align: left;
  display: inline-flex; align-items: baseline; gap: 10px;
  transition: color 120ms ease;
}
.mroom-act::before { content: '▸'; color: #4a4a4a; font-size: 11px; }
.mroom-act:hover { color: #e8e8e8; }
.mroom-act-primary { color: #a8d4fc; }
.mroom-act-primary::before { color: #a8d4fc; }
.mroom-act-primary:hover { color: #c4e0fc; }
.mroom-act:disabled { color: #4a4a4a; cursor: default; }
.mroom-today-line {
  display: grid;
  grid-template-columns: 22px 52px minmax(140px, auto) 1fr auto auto;
  gap: 14px; padding: 12px 0; align-items: baseline;
  border-bottom: 1px solid rgba(255,255,255,0.06);
  font-size: 12px;
}
.mroom-today-line:last-child { border-bottom: none; }
.mroom-today-num { color: #a8d4fc; font-weight: 600; font-size: 13px; }
.mroom-today-when { color: #a8a8a8; font-size: 11px; }
.mroom-today-proj {
  color: #6e6e6e; font-size: 10px; letter-spacing: 0.14em;
  text-transform: uppercase; padding-left: 10px;
  border-left: 1px solid #2a2a2a;
}
.mroom-today-what { color: #e8e8e8; line-height: 1.45; }
.mroom-today-dur { color: #4a4a4a; font-size: 10.5px; }
.mroom-colophon {
  margin-top: 32px; padding-top: 14px;
  border-top: 1px dashed rgba(255,255,255,0.06);
  display: flex; justify-content: space-between; align-items: baseline;
  font-size: 9.5px; letter-spacing: 0.18em; text-transform: uppercase;
  color: #4a4a4a;
}
.mroom-commit {
  position: fixed; bottom: 0; left: 0; right: 0; z-index: 40;
  display: flex; justify-content: space-between; align-items: center;
  padding: 14px 24px;
  background: linear-gradient(0deg, rgba(6,7,11,0.97), rgba(6,7,11,0.86));
  backdrop-filter: blur(16px);
  border-top: 1px solid rgba(255,255,255,0.06);
}
.mroom-commit-left { color: #6e6e6e; font-size: 11px; }
.mroom-commit-left em { color: #a8d4fc; font-style: normal; font-weight: 600; }
.mroom-commit-primary {
  font-family: inherit; font-size: 11px; letter-spacing: 0.08em;
  padding: 8px 18px; border-radius: 5px; border: none; cursor: pointer;
  background: linear-gradient(135deg, #c4e0fc, #7aafea); color: #06070b;
  font-weight: 600;
}
.mroom-commit-primary:hover { filter: brightness(1.08); }
.mroom-commit-primary:disabled { opacity: 0.5; cursor: default; }
.mroom-empty {
  padding: 80px 0; text-align: center; color: #6e6e6e;
  display: flex; flex-direction: column; align-items: center; gap: 12px;
}
.mroom-empty-glyph { font-size: 28px; color: #2a2a2a; }
.mroom-empty-sub { max-width: 46ch; color: #4a4a4a; font-size: 11px; }
`
