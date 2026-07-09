"""Apple-app reach verbs: Reminders, Notes, Messages, Contacts, Calendar, Mail.

These are the verbs that let the voice brain touch the Mac's OWN apps —
"remind me to call the plumber", "note that down", "text Ada I'm late",
"what's on my calendar", "any unread mail". Every one drives its app over
AppleScript through ``mac.run_osascript``; nothing here talks to a cloud
API, so the reach is exactly whatever those apps already sync.

TCC reality: Apple-app AppleScript needs the macOS **Automation** TCC
grant, which the packaged Freyja.app bundle owns but a bare dev shell
does NOT. When the grant is missing, osascript fails with "Not
authorized to send Apple events" / error -1743 (or the assistive-access
variant). Every verb runs its script through ``_run`` / detects that with
``_automation_denied`` and degrades to ``ok=False`` with a spoken setup
message + ``data.setup="automation"`` — never a raw traceback, never a
hang. Calendar and Mail scripts are also SLOW, so their reads use a
bounded query and a longer timeout, and a timeout degrades to a clean
"couldn't read … in time" rather than blocking the exchange.

Messages is the only confirm-tier verb here: a sent iMessage is outward
and has no undo, so — exactly like ``slack.send`` — the recipient is
resolved with unique-match-or-enumerate discipline (ambiguous → ask with
candidates, never guess the wrong Ada), and the service layer gates the
send behind a spoken confirmation token.

Tests monkeypatch ``mac.run_osascript`` (the single subprocess seam) and
assert the exact scripts; no test ever drives a real Apple app.
"""

from __future__ import annotations

from typing import Any, Optional

from bridge.voice.adapters import mac
from bridge.voice.adapters.mac import as_quoted
from bridge.voice.verbs import Verb, VerbRegistry, VerbResult

# Substrings that mean "the Automation TCC grant is missing" across the
# several shapes osascript uses (-1743 is the Apple-events auth code;
# the assistive-access line is the System-Events UI-scripting variant).
_AUTOMATION_DENIED_MARKERS = (
    "not authorized to send apple events",
    "-1743",
    "not allowed assistive access",
    "not permitted to send apple events",
)
_AUTOMATION_SETUP = "automation"

# Bare-string parse tokens the reminder-list/calendar scripts print
# between fields; a linefeed separates records, this separates columns.
_FIELD_SEP = "\x1f"  # ASCII unit separator — never appears in real text
_RECORD_SEP = "\x1e"  # ASCII record separator

_REMINDERS_LIST_CAP = 10
_CALENDAR_TIMEOUT_SEC = 15.0
_MAIL_TIMEOUT_SEC = 12.0
_MAIL_CAP = 8
_DEFAULT_NOTE = "Freyja"


def _automation_denied(out: str) -> bool:
    """True when an osascript failure is the missing-Automation-grant one."""
    low = (out or "").lower()
    return any(marker in low for marker in _AUTOMATION_DENIED_MARKERS)


def _denied_result(app: str, out: str) -> VerbResult:
    """The uniform TCC-denied refusal: a spoken setup line + data.setup."""
    return VerbResult(
        ok=False,
        summary=(
            f"needs Automation permission for {app} — grant it in System "
            "Settings > Privacy & Security > Automation"
        ),
        data={"setup": _AUTOMATION_SETUP},
        error=out or "automation_denied",
    )


async def _run(
    lines: list[str], app: str, timeout: float = 15.0
) -> tuple[bool, str, Optional[VerbResult]]:
    """Run an AppleScript, folding the TCC-denied case into a ready-made
    refusal. Returns (ok, out, denied_result): on the Automation-grant
    failure ``denied_result`` is the verb's whole VerbResult so callers
    just ``return`` it; on any other outcome it is None and callers own the
    (ok, out) handling (their errors are verb-specific).

    Default timeout is generous (15 s): the first hit on Reminders / Notes /
    Contacts in a session cold-launches the app under a non-GUI osascript,
    which can take 10 s+; it's fast once warm. The model covers the wait
    with a spoken preamble ("checking your reminders")."""
    ok, out = await mac.run_osascript_lines(lines, timeout=timeout)
    if not ok and _automation_denied(out):
        return False, out, _denied_result(app, out)
    return ok, out, None


# ── Reminders ────────────────────────────────────────────────────────────────
# `make new reminder` optionally takes a `due date` — but only when the
# caller passes one we could parse into an AppleScript `date "…"` literal.
# We hand the raw spoken due string to AppleScript's own `date` coercion
# (it parses "tomorrow 5pm", "July 10", etc. in the user's locale) rather
# than reimplement a date parser here; an unparseable string just drops
# the due clause instead of failing the whole create.


def _reminder_create_lines(text: str, list_name: str, due: str) -> list[str]:
    props = f"name:{as_quoted(text)}"
    if due:
        props += f", due date:(date {as_quoted(due)})"
    target = f'list {as_quoted(list_name)}' if list_name else "default list"
    return [
        'tell application "Reminders"',
        f"set r to make new reminder at end of {target} with properties {{{props}}}",
        "id of r",
        "end tell",
    ]


async def _reminders_create(args: dict[str, Any]) -> VerbResult:
    text = str(args.get("text") or "").strip()
    if not text:
        return VerbResult(ok=False, summary="remind you of what?", error="missing_text")
    list_name = str(args.get("list") or "").strip()
    due = str(args.get("due") or "").strip()
    ok, out, denied = await _run(
        _reminder_create_lines(text, list_name, due), "Reminders"
    )
    if denied is not None:
        return denied
    if not ok and due:
        # The only likely non-TCC failure is AppleScript rejecting the due
        # coercion; retry without it so a bad date still leaves the reminder.
        ok, out, denied = await _run(
            _reminder_create_lines(text, list_name, ""), "Reminders"
        )
        if denied is not None:
            return denied
    if not ok:
        return VerbResult(ok=False, summary="couldn't add that reminder", error=out)
    reminder_id = out.strip()

    async def undo() -> VerbResult:
        # Best-effort: delete the reminder we just made by its id.
        u_ok, u_out, u_denied = await _run(
            [
                'tell application "Reminders"',
                f"delete (first reminder whose id is {as_quoted(reminder_id)})",
                "end tell",
            ],
            "Reminders",
        )
        if u_denied is not None:
            return u_denied
        if not u_ok:
            return VerbResult(ok=False, summary="couldn't remove that reminder", error=u_out)
        return VerbResult(ok=True, summary=f"removed reminder: {text[:40]}")

    return VerbResult(
        ok=True,
        summary=f"⊕ reminder: {text[:40]}",
        data={"id": reminder_id, "text": text},
        undo=undo,
    )


def _reminders_list_lines(list_name: str) -> list[str]:
    # Incomplete reminders only; newest first via `reverse of`. We print
    # name + optional due, one record per line, fields unit-separated —
    # AppleScript list-text with a delimiter is far cheaper than N calls.
    target = f'list {as_quoted(list_name)}' if list_name else "default list"
    return [
        'set out to ""',
        'tell application "Reminders"',
        f"set rs to (reverse of (every reminder of {target} whose completed is false))",
        "repeat with r in rs",
        "set d to \"\"",
        "if due date of r is not missing value then set d to (due date of r as string)",
        f'set out to out & (name of r) & "{_FIELD_SEP}" & d & "{_RECORD_SEP}"',
        "end repeat",
        "end tell",
        "out",
    ]


async def _reminders_list(args: dict[str, Any]) -> VerbResult:
    list_name = str(args.get("list") or "").strip()
    try:
        cap = int(args.get("count") or _REMINDERS_LIST_CAP)
    except (TypeError, ValueError):
        cap = _REMINDERS_LIST_CAP
    cap = max(1, min(_REMINDERS_LIST_CAP, cap))
    ok, out, denied = await _run(_reminders_list_lines(list_name), "Reminders")
    if denied is not None:
        return denied
    if not ok:
        return VerbResult(ok=False, summary="couldn't read reminders", error=out)
    reminders: list[dict[str, str]] = []
    for record in out.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record.strip():
            continue
        parts = record.split(_FIELD_SEP)
        name = parts[0].strip()
        if not name:
            continue
        item = {"text": name}
        if len(parts) > 1 and parts[1].strip():
            item["due"] = parts[1].strip()
        reminders.append(item)
        if len(reminders) >= cap:
            break
    n = len(reminders)
    where = f" in {list_name}" if list_name else ""
    return VerbResult(
        ok=True,
        summary=f"{n} reminder{'s' if n != 1 else ''}{where}",
        data={"reminders": reminders},
    )


# ── Notes ────────────────────────────────────────────────────────────────────
# Notes' AppleScript addresses a note by name within the default account.
# For `notes.append` we find-or-create a note by name and append a line
# under a fresh timestamp; the body is HTML, so a line break is <br>.


def _notes_append_lines(note_name: str, line: str) -> list[str]:
    quoted_name = as_quoted(note_name)
    # Build the appended fragment: a timestamp line then the text, both as
    # HTML so Notes renders them on their own lines.
    frag = f'"<div>" & (current date as string) & "</div><div>" & {as_quoted(line)} & "</div>"'
    return [
        'tell application "Notes"',
        f"if not (note {quoted_name} exists) then",
        f"make new note with properties {{name:{quoted_name}, body:{quoted_name}}}",
        "end if",
        f"set n to note {quoted_name}",
        f"set body of n to (body of n) & {frag}",
        "end tell",
    ]


async def _notes_append(args: dict[str, Any]) -> VerbResult:
    text = str(args.get("text") or "").strip()
    if not text:
        return VerbResult(ok=False, summary="note what?", error="missing_text")
    note_name = str(args.get("note") or "").strip() or _DEFAULT_NOTE
    ok, out, denied = await _run(_notes_append_lines(note_name, text), "Notes")
    if denied is not None:
        return denied
    if not ok:
        return VerbResult(ok=False, summary="couldn't append to that note", error=out)
    return VerbResult(ok=True, summary="✎ noted", data={"note": note_name})


def _notes_create_lines(title: str, body: str) -> list[str]:
    # Notes wants the title as the first HTML line of the body, so a plain
    # note reads with its name on top.
    if body:
        html = f'"<div>" & {as_quoted(title)} & "</div><div>" & {as_quoted(body)} & "</div>"'
    else:
        html = f'"<div>" & {as_quoted(title)} & "</div>"'
    return [
        'tell application "Notes"',
        f"make new note with properties {{name:{as_quoted(title)}, body:{html}}}",
        "end tell",
    ]


async def _notes_create(args: dict[str, Any]) -> VerbResult:
    title = str(args.get("title") or "").strip()
    if not title:
        return VerbResult(ok=False, summary="title the note?", error="missing_title")
    body = str(args.get("body") or "").strip()
    ok, out, denied = await _run(_notes_create_lines(title, body), "Notes")
    if denied is not None:
        return denied
    if not ok:
        return VerbResult(ok=False, summary="couldn't create that note", error=out)
    return VerbResult(ok=True, summary=f"✎ note: {title[:40]}", data={"title": title})


# ── Contacts ─────────────────────────────────────────────────────────────────
# A person lookup that both answers "what's Ada's number" AND backs the
# Messages recipient resolver. Returns matched name + phones/emails; an
# ambiguous name enumerates the candidate full names so the model asks.


def _contacts_find_lines(name: str) -> list[str]:
    # For each person whose name contains the query, emit:
    #   fullname US phone1,phone2 US email1,email2 RS
    return [
        'set out to ""',
        'tell application "Contacts"',
        f"set ps to (every person whose name contains {as_quoted(name)})",
        "repeat with p in ps",
        'set ph to ""',
        "repeat with v in (value of every phone of p)",
        'set ph to ph & (v as string) & ","',
        "end repeat",
        'set em to ""',
        "repeat with v in (value of every email of p)",
        'set em to em & (v as string) & ","',
        "end repeat",
        f'set out to out & (name of p) & "{_FIELD_SEP}" & ph & "{_FIELD_SEP}" '
        f'& em & "{_RECORD_SEP}"',
        "end repeat",
        "end tell",
        "out",
    ]


def _parse_contacts(out: str) -> list[dict[str, Any]]:
    people: list[dict[str, Any]] = []
    for record in out.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record.strip():
            continue
        parts = record.split(_FIELD_SEP)
        full = parts[0].strip()
        if not full:
            continue
        phones = [p.strip() for p in (parts[1] if len(parts) > 1 else "").split(",") if p.strip()]
        emails = [e.strip() for e in (parts[2] if len(parts) > 2 else "").split(",") if e.strip()]
        people.append({"name": full, "phones": phones, "emails": emails})
    return people


async def _contacts_find(args: dict[str, Any]) -> VerbResult:
    name = str(args.get("name") or "").strip()
    if not name:
        return VerbResult(ok=False, summary="find who?", error="missing_name")
    ok, out, denied = await _run(_contacts_find_lines(name), "Contacts")
    if denied is not None:
        return denied
    if not ok:
        return VerbResult(ok=False, summary="couldn't search Contacts", error=out)
    people = _parse_contacts(out)
    if not people:
        return VerbResult(ok=False, summary=f"no contact matching {name}", error="not_found")
    if len(people) > 1:
        names = [p["name"] for p in people]
        return VerbResult(
            ok=False,
            summary=f"{len(names)} contacts match {name}",
            data={"candidates": names},
            error=f"multiple contacts match {name}: {', '.join(names)}. Ask which one.",
        )
    p = people[0]
    first = (p["phones"] or p["emails"] or [""])[0]
    tail = f": {first}" if first else ""
    return VerbResult(
        ok=True,
        summary=f"{p['name']}{tail}",
        data={"name": p["name"], "phones": p["phones"], "emails": p["emails"]},
    )


# ── Messages ─────────────────────────────────────────────────────────────────
# Confirm-tier, outward, no undo. `to` may already be a phone/handle (has a
# digit or an @) — then we send straight to it as a text-message buddy. A
# bare name is resolved through Contacts first: a UNIQUE contact with at
# least one phone/email wins; anything ambiguous or unknown asks (returns
# candidates) rather than guess the wrong person — the same discipline as
# slack.send, and the reason Messages is confirm-tier.


def _looks_like_handle(to: str) -> bool:
    return "@" in to or any(ch.isdigit() for ch in to)


async def _resolve_message_target(to: str) -> tuple[Optional[str], Optional[VerbResult]]:
    """(handle, refusal). On success handle is a phone/email/handle string
    and refusal is None; otherwise refusal is the ready VerbResult to
    return (ambiguous, unknown, or a Contacts/TCC failure)."""
    if _looks_like_handle(to):
        return to, None
    ok, out, denied = await _run(_contacts_find_lines(to), "Contacts")
    if denied is not None:
        return None, denied
    if not ok:
        return None, VerbResult(ok=False, summary="couldn't look up that contact", error=out)
    people = _parse_contacts(out)
    reachable = [p for p in people if p["phones"] or p["emails"]]
    if not reachable:
        return None, VerbResult(
            ok=False, summary=f"no way to message {to}", error="no_handle"
        )
    if len(reachable) > 1:
        names = [p["name"] for p in reachable]
        return None, VerbResult(
            ok=False,
            summary=f"which {to}? {len(names)} matches",
            data={"candidates": names},
            error=f"multiple contacts match {to}: {', '.join(names)}. Ask which one.",
        )
    p = reachable[0]
    handle = (p["phones"] or p["emails"])[0]
    return handle, None


def _messages_send_lines(handle: str, text: str) -> list[str]:
    # Send over the iMessage service; `participant` matches a phone/email/
    # handle (a `buddy` is roster-only and misses fresh numbers).
    return [
        'tell application "Messages"',
        'set svc to 1st service whose service type = iMessage',
        f"set p to participant {as_quoted(handle)} of svc",
        f"send {as_quoted(text)} to p",
        "end tell",
    ]


async def _messages_send(args: dict[str, Any]) -> VerbResult:
    to = str(args.get("to") or "").strip()
    text = str(args.get("text") or "").strip()
    if not to:
        return VerbResult(ok=False, summary="message who?", error="missing_to")
    if not text:
        return VerbResult(ok=False, summary="nothing to send", error="missing_text")
    handle, refusal = await _resolve_message_target(to)
    if refusal is not None:
        return refusal
    ok, out, denied = await _run(_messages_send_lines(handle, text), "Messages")
    if denied is not None:
        return denied
    if not ok:
        return VerbResult(ok=False, summary=f"couldn't message {to}", error=out)
    # No undo: a sent message is sent (mirrors slack.send).
    return VerbResult(
        ok=True,
        summary=f"→ {to}: {text[:40]}",
        data={"to": to, "handle": handle},
    )


# ── Calendar ─────────────────────────────────────────────────────────────────
# Calendar's AppleScript is notoriously slow, so both reads use a BOUNDED
# `whose` query (never enumerate every event) and a 15 s timeout; a
# timeout degrades to a clean spoken line, never a hang. Each record is
# title US start US end US sortkey, one per line. The sortkey is the start
# time as seconds relative to a reference (`(start date) - ref`) so Python
# can order events NUMERICALLY — the locale date STRING ("Friday, …" vs
# "Thursday, …") sorts by weekday name, not by time, which is wrong.


def _calendar_event_emit() -> str:
    # Shared per-event line: the four fields, sortkey last.
    return (
        f'set out to out & (summary of e) & "{_FIELD_SEP}" & (start date of e as string) '
        f'& "{_FIELD_SEP}" & (end date of e as string) & "{_FIELD_SEP}" '
        f'& (((start date of e) - ref) as string) & "{_RECORD_SEP}"'
    )


def _calendar_today_lines() -> list[str]:
    return [
        'set out to ""',
        "set ref to (current date)",
        "set d0 to (current date)",
        "set time of d0 to 0",
        "set d1 to d0 + (1 * days)",
        'tell application "Calendar"',
        "set evs to (every event of every calendar whose start date ≥ d0 and start date < d1)",
        "repeat with e in evs",
        _calendar_event_emit(),
        "end repeat",
        "end tell",
        "out",
    ]


def _calendar_next_lines() -> list[str]:
    # The next upcoming event: everything starting from now, sorted client-
    # side (AppleScript can't sort), so we fetch a bounded forward window
    # (now → +14 days) and pick the earliest. 14 days keeps the `whose`
    # query cheap while covering any realistic "what's next".
    return [
        'set out to ""',
        "set ref to (current date)",
        "set d0 to (current date)",
        "set d1 to d0 + (14 * days)",
        'tell application "Calendar"',
        "set evs to (every event of every calendar whose start date ≥ d0 and start date < d1)",
        "repeat with e in evs",
        _calendar_event_emit(),
        "end repeat",
        "end tell",
        "out",
    ]


def _hhmm(raw: str) -> str:
    """Pull HH:MM out of an AppleScript date string. The locale form is
    e.g. "Thursday, July 9, 2026 at 3:30:00 PM" — grab the clock, drop the
    seconds, keep any AM/PM. Falls back to the raw string if unrecognized."""
    import re

    m = re.search(r"(\d{1,2}):(\d{2})(?::\d{2})?\s*([AaPp][Mm])?", raw)
    if not m:
        return raw.strip()
    hour, minute, ampm = m.group(1), m.group(2), (m.group(3) or "").upper()
    return f"{hour}:{minute}{(' ' + ampm) if ampm else ''}"


def _parse_events(out: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for record in out.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record.strip():
            continue
        parts = record.split(_FIELD_SEP)
        title = parts[0].strip()
        if not title:
            continue
        ev: dict[str, Any] = {"title": title}
        if len(parts) > 1 and parts[1].strip():
            ev["start"] = _hhmm(parts[1])
        if len(parts) > 2 and parts[2].strip():
            ev["end"] = _hhmm(parts[2])
        # 4th field is the numeric start offset (seconds); sort by it so
        # "tomorrow 2pm" never orders before "today 11am". Missing/garbled
        # keys sort last (+inf) rather than crashing the read.
        key = parts[3].strip() if len(parts) > 3 else ""
        try:
            ev["_sort"] = float(key) if key else float("inf")
        except (TypeError, ValueError):
            ev["_sort"] = float("inf")
        events.append(ev)
    return events


async def _calendar_today(args: dict[str, Any]) -> VerbResult:
    ok, out, denied = await _run(
        _calendar_today_lines(), "Calendar", timeout=_CALENDAR_TIMEOUT_SEC
    )
    if denied is not None:
        return denied
    if not ok:
        if "timeout" in (out or "").lower():
            return VerbResult(ok=False, summary="couldn't read Calendar in time", error=out)
        return VerbResult(ok=False, summary="couldn't read Calendar", error=out)
    events = _parse_events(out)
    events.sort(key=lambda e: e.get("_sort", float("inf")))
    for e in events:
        e.pop("_sort", None)
    n = len(events)
    return VerbResult(
        ok=True,
        summary=f"{n} event{'s' if n != 1 else ''} today",
        data={"events": events},
    )


async def _calendar_next(args: dict[str, Any]) -> VerbResult:
    ok, out, denied = await _run(
        _calendar_next_lines(), "Calendar", timeout=_CALENDAR_TIMEOUT_SEC
    )
    if denied is not None:
        return denied
    if not ok:
        if "timeout" in (out or "").lower():
            return VerbResult(ok=False, summary="couldn't read Calendar in time", error=out)
        return VerbResult(ok=False, summary="couldn't read Calendar", error=out)
    events = _parse_events(out)
    if not events:
        return VerbResult(ok=True, summary="nothing coming up", data={"events": []})
    events.sort(key=lambda e: e.get("_sort", float("inf")))
    nxt = events[0]
    nxt.pop("_sort", None)
    when = nxt.get("start", "")
    return VerbResult(
        ok=True,
        summary=f"next: {nxt['title']}{(' at ' + when) if when else ''}",
        data={"events": [nxt]},
    )


# ── Mail ─────────────────────────────────────────────────────────────────────
# Last N unread in the inbox: sender + subject only (never the body — no
# secrets read aloud). Mail's AppleScript is slow → 12 s bounded timeout,
# graceful. We read from `inbox` (the unified inbox) and stop at the cap.


def _mail_unread_lines(cap: int) -> list[str]:
    return [
        'set out to ""',
        "set c to 0",
        'tell application "Mail"',
        "set msgs to (messages of inbox whose read status is false)",
        "repeat with m in msgs",
        f"if c ≥ {cap} then exit repeat",
        f'set out to out & (sender of m) & "{_FIELD_SEP}" & (subject of m) & "{_RECORD_SEP}"',
        "set c to c + 1",
        "end repeat",
        "end tell",
        "out",
    ]


async def _mail_unread(args: dict[str, Any]) -> VerbResult:
    try:
        cap = int(args.get("count") or _MAIL_CAP)
    except (TypeError, ValueError):
        cap = _MAIL_CAP
    cap = max(1, min(_MAIL_CAP, cap))
    ok, out, denied = await _run(_mail_unread_lines(cap), "Mail", timeout=_MAIL_TIMEOUT_SEC)
    if denied is not None:
        return denied
    if not ok:
        if "timeout" in (out or "").lower():
            return VerbResult(ok=False, summary="couldn't read Mail in time", error=out)
        return VerbResult(ok=False, summary="couldn't read Mail", error=out)
    messages: list[dict[str, str]] = []
    for record in out.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record.strip():
            continue
        parts = record.split(_FIELD_SEP)
        sender = parts[0].strip()
        subject = parts[1].strip() if len(parts) > 1 else ""
        if not sender and not subject:
            continue
        messages.append({"from": sender, "subject": subject})
        if len(messages) >= cap:
            break
    n = len(messages)
    return VerbResult(
        ok=True,
        summary=f"{n} unread" if n else "inbox clear",
        data={"messages": messages},
    )


def register(registry: VerbRegistry) -> None:
    registry.register(
        Verb(
            name="reminders.create",
            description="Add a reminder (optional list and natural-language due date)",
            params={
                "text": {"type": "string"},
                "list": {"type": "string", "description": "reminder list name"},
                "due": {"type": "string", "description": "e.g. 'tomorrow 5pm'"},
            },
            required=["text"],
            tier="auto",
            run=_reminders_create,
        )
    )
    registry.register(
        Verb(
            name="reminders.list",
            description="List incomplete reminders (newest first, up to 10)",
            params={
                "list": {"type": "string", "description": "reminder list name"},
                "count": {"type": "integer", "minimum": 1, "maximum": _REMINDERS_LIST_CAP},
            },
            required=[],
            tier="auto",
            run=_reminders_list,
        )
    )
    registry.register(
        Verb(
            name="notes.append",
            description="Append a timestamped line to a note (creates it if missing)",
            params={
                "text": {"type": "string"},
                "note": {"type": "string", "description": "note name (default Freyja)"},
            },
            required=["text"],
            tier="auto",
            run=_notes_append,
        )
    )
    registry.register(
        Verb(
            name="notes.create",
            description="Create a new note with a title and optional body",
            params={
                "title": {"type": "string"},
                "body": {"type": "string"},
            },
            required=["title"],
            tier="auto",
            run=_notes_create,
        )
    )
    registry.register(
        Verb(
            name="messages.send",
            description="Send an iMessage to a person (by name) or a phone/handle",
            params={
                "to": {"type": "string", "description": "contact name, phone, or handle"},
                "text": {"type": "string"},
            },
            required=["to", "text"],
            tier="confirm",
            run=_messages_send,
        )
    )
    registry.register(
        Verb(
            name="contacts.find",
            description="Look up a contact's phone numbers and emails by name",
            params={"name": {"type": "string"}},
            required=["name"],
            tier="auto",
            run=_contacts_find,
        )
    )
    registry.register(
        Verb(
            name="calendar.today",
            description="Today's calendar events",
            params={},
            required=[],
            tier="auto",
            run=_calendar_today,
        )
    )
    registry.register(
        Verb(
            name="calendar.next",
            description="The next upcoming calendar event",
            params={},
            required=[],
            tier="auto",
            run=_calendar_next,
        )
    )
    registry.register(
        Verb(
            name="mail.unread",
            description="Senders and subjects of the last few unread inbox messages",
            params={"count": {"type": "integer", "minimum": 1, "maximum": _MAIL_CAP}},
            required=[],
            tier="auto",
            run=_mail_unread,
        )
    )
