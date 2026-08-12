"""Apple-app adapter behavior: exact AppleScript, TCC-denied degradation,
messages confirm-tier + recipient resolution, calendar/mail timeout.

Nothing here touches a real Apple app — `mac.run_osascript` is the single
subprocess seam and every test monkeypatches it. In particular NO test
drives Calendar/Reminders/Notes/Messages/Mail/Contacts live (that would
trip a blocking Automation-permission dialog).
"""

import pytest

from bridge.voice.adapters import apple, mac
from bridge.voice.verbs import build_default_registry

# The three shapes of the Automation-denied osascript failure (contract §3).
_DENIED_MINUS_1743 = (
    "execution error: Not authorized to send Apple events to Reminders. (-1743)"
)
_DENIED_ASSISTIVE = (
    "execution error: osascript is not allowed assistive access. (-25211)"
)


class ScriptRecorder:
    """Stands in for mac.run_osascript; replays canned (ok, out) replies."""

    def __init__(self, replies=None):
        self.scripts = []
        self.replies = list(replies or [])

    async def __call__(self, script, timeout=6.0):
        self.scripts.append(script)
        return self.replies.pop(0) if self.replies else (True, "")


@pytest.fixture
def osa(monkeypatch):
    rec = ScriptRecorder()
    # run_osascript_lines resolves run_osascript through module globals at
    # call time, so this one patch intercepts both entry points.
    monkeypatch.setattr(mac, "run_osascript", rec)
    return rec


@pytest.fixture
def reg():
    return build_default_registry()


# ── registration + tiers ─────────────────────────────────────────────────


def test_all_apple_verbs_registered(reg):
    for name in (
        "reminders.create",
        "reminders.list",
        "notes.append",
        "notes.create",
        "messages.send",
        "contacts.find",
        "calendar.today",
        "calendar.next",
        "mail.unread",
    ):
        assert reg.get(name) is not None, name


def test_messages_send_is_confirm_tier(reg):
    # Outward, no undo → the only confirm-tier apple verb.
    assert reg.get("messages.send").tier == "confirm"
    for name in ("reminders.create", "notes.append", "calendar.today", "mail.unread"):
        assert reg.get(name).tier == "auto", name


# ── reminders ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reminders_create_script_and_summary(reg, osa):
    osa.replies = [(True, "x-apple-reminder://ABC")]
    res = await reg.get("reminders.create").run({"text": "call the plumber"})
    assert res.ok and res.summary == "⊕ reminder: call the plumber"
    script = osa.scripts[0]
    assert 'tell application "Reminders"' in script
    assert "make new reminder at end of default list" in script
    assert 'name:"call the plumber"' in script
    assert "id of r" in script


@pytest.mark.asyncio
async def test_reminders_create_with_list_and_due(reg, osa):
    osa.replies = [(True, "x-apple-reminder://ABC")]
    res = await reg.get("reminders.create").run(
        {"text": "milk", "list": "Groceries", "due": "tomorrow 5pm"}
    )
    assert res.ok
    script = osa.scripts[0]
    assert 'make new reminder at end of list "Groceries"' in script
    assert 'due date:(date "tomorrow 5pm")' in script


@pytest.mark.asyncio
async def test_reminders_create_retries_without_bad_due(reg, osa):
    # A due AppleScript can't coerce fails the first make; we retry without
    # the due clause so the reminder still lands.
    osa.replies = [
        (False, "execution error: Invalid date"),
        (True, "x-apple-reminder://ABC"),
    ]
    res = await reg.get("reminders.create").run({"text": "milk", "due": "the 32nd"})
    assert res.ok and res.summary == "⊕ reminder: milk"
    assert 'due date' in osa.scripts[0]
    assert 'due date' not in osa.scripts[1]


@pytest.mark.asyncio
async def test_reminders_create_undo_deletes(reg, osa):
    osa.replies = [(True, "x-apple-reminder://ABC")]
    res = await reg.get("reminders.create").run({"text": "call the plumber"})
    assert res.undo is not None
    osa.replies = [(True, "")]
    undo_res = await res.undo()
    assert undo_res.ok and undo_res.summary == "removed reminder: call the plumber"
    assert 'delete (first reminder whose id is "x-apple-reminder://ABC")' in osa.scripts[-1]


@pytest.mark.asyncio
async def test_reminders_create_requires_text(reg, osa):
    res = await reg.get("reminders.create").run({})
    assert not res.ok
    assert osa.scripts == []


@pytest.mark.asyncio
async def test_reminders_create_tcc_denied(reg, osa):
    osa.replies = [(False, _DENIED_MINUS_1743)]
    res = await reg.get("reminders.create").run({"text": "x"})
    assert not res.ok
    assert res.data["setup"] == "automation"
    assert "Automation permission for Reminders" in res.summary


@pytest.mark.asyncio
async def test_reminders_list_parses_records(reg, osa):
    # name US due RS, one record per line; newest first is the script's job.
    fs, rs = apple._FIELD_SEP, apple._RECORD_SEP
    out = f"call plumber{fs}Thursday, July 9, 2026{rs}buy milk{fs}{rs}"
    osa.replies = [(True, out)]
    res = await reg.get("reminders.list").run({})
    assert res.ok and res.summary == "2 reminders"
    assert res.data["reminders"] == [
        {"text": "call plumber", "due": "Thursday, July 9, 2026"},
        {"text": "buy milk"},
    ]
    assert "whose completed is false" in osa.scripts[0]


@pytest.mark.asyncio
async def test_reminders_list_caps_at_ten(reg, osa):
    fs, rs = apple._FIELD_SEP, apple._RECORD_SEP
    out = rs.join(f"item {i}{fs}" for i in range(20)) + rs
    osa.replies = [(True, out)]
    res = await reg.get("reminders.list").run({"count": 50})
    assert len(res.data["reminders"]) == 10


# ── notes ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_notes_append_creates_if_missing(reg, osa):
    res = await reg.get("notes.append").run({"text": "buy milk"})
    assert res.ok and res.summary == "✎ noted"
    script = osa.scripts[0]
    assert 'if not (note "Freyja" exists) then' in script
    assert 'set n to note "Freyja"' in script
    assert 'set body of n to (body of n) &' in script
    assert '"buy milk"' in script


@pytest.mark.asyncio
async def test_notes_append_named_note(reg, osa):
    res = await reg.get("notes.append").run({"text": "hi", "note": "Journal"})
    assert res.ok
    assert 'set n to note "Journal"' in osa.scripts[0]


@pytest.mark.asyncio
async def test_notes_create_script(reg, osa):
    res = await reg.get("notes.create").run({"title": "Ideas", "body": "one"})
    assert res.ok and res.summary == "✎ note: Ideas"
    script = osa.scripts[0]
    assert 'make new note with properties {name:"Ideas"' in script
    assert '"one"' in script


@pytest.mark.asyncio
async def test_notes_create_tcc_denied(reg, osa):
    osa.replies = [(False, _DENIED_ASSISTIVE)]
    res = await reg.get("notes.create").run({"title": "Ideas"})
    assert not res.ok and res.data["setup"] == "automation"
    assert "Automation permission for Notes" in res.summary


# ── contacts ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_contacts_find_unique(reg, osa):
    fs, rs = apple._FIELD_SEP, apple._RECORD_SEP
    out = f"Ada Lovelace{fs}+15551234567,{fs}ada@x.com,{rs}"
    osa.replies = [(True, out)]
    res = await reg.get("contacts.find").run({"name": "Ada"})
    assert res.ok and res.summary == "Ada Lovelace: +15551234567"
    assert res.data == {
        "name": "Ada Lovelace",
        "phones": ["+15551234567"],
        "emails": ["ada@x.com"],
    }


@pytest.mark.asyncio
async def test_contacts_find_ambiguous_enumerates(reg, osa):
    fs, rs = apple._FIELD_SEP, apple._RECORD_SEP
    out = f"Ada Lovelace{fs}+1,{fs}{rs}Ada Byron{fs}+2,{fs}{rs}"
    osa.replies = [(True, out)]
    res = await reg.get("contacts.find").run({"name": "Ada"})
    assert not res.ok
    assert res.data["candidates"] == ["Ada Lovelace", "Ada Byron"]


@pytest.mark.asyncio
async def test_contacts_find_none(reg, osa):
    osa.replies = [(True, "")]
    res = await reg.get("contacts.find").run({"name": "Nobody"})
    assert not res.ok and "no contact matching Nobody" in res.summary


# ── messages ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_messages_send_to_handle_directly(reg, osa):
    # A phone/handle skips the Contacts lookup entirely.
    res = await reg.get("messages.send").run({"to": "+15551234567", "text": "on my way"})
    assert res.ok and res.summary == "→ +15551234567: on my way"
    script = osa.scripts[0]
    assert 'tell application "Messages"' in script
    assert "service type = iMessage" in script
    assert 'participant "+15551234567"' in script
    assert 'send "on my way" to p' in script


@pytest.mark.asyncio
async def test_messages_send_resolves_name_via_contacts(reg, osa):
    fs, rs = apple._FIELD_SEP, apple._RECORD_SEP
    # First script = Contacts lookup; second = the actual send.
    osa.replies = [
        (True, f"Ada Lovelace{fs}+15551234567,{fs}{rs}"),
        (True, ""),
    ]
    res = await reg.get("messages.send").run({"to": "Ada", "text": "hi"})
    assert res.ok and res.summary == "→ Ada: hi"
    assert "Contacts" in osa.scripts[0]
    assert 'participant "+15551234567"' in osa.scripts[1]


@pytest.mark.asyncio
async def test_messages_send_ambiguous_recipient_asks(reg, osa):
    fs, rs = apple._FIELD_SEP, apple._RECORD_SEP
    osa.replies = [(True, f"Ada Lovelace{fs}+1,{fs}{rs}Ada Byron{fs}+2,{fs}{rs}")]
    res = await reg.get("messages.send").run({"to": "Ada", "text": "hi"})
    assert not res.ok
    assert res.data["candidates"] == ["Ada Lovelace", "Ada Byron"]
    # Only the Contacts lookup ran — nothing was sent to the wrong Ada.
    assert len(osa.scripts) == 1
    assert "Messages" not in osa.scripts[0]


@pytest.mark.asyncio
async def test_messages_send_unknown_recipient(reg, osa):
    osa.replies = [(True, "")]  # Contacts finds nobody
    res = await reg.get("messages.send").run({"to": "Ghost", "text": "hi"})
    assert not res.ok and "no way to message Ghost" in res.summary
    assert len(osa.scripts) == 1


@pytest.mark.asyncio
async def test_messages_send_tcc_denied(reg, osa):
    osa.replies = [(False, _DENIED_MINUS_1743)]
    res = await reg.get("messages.send").run({"to": "+1555", "text": "hi"})
    assert not res.ok and res.data["setup"] == "automation"
    assert "Automation permission for Messages" in res.summary


@pytest.mark.asyncio
async def test_messages_send_requires_to_and_text(reg, osa):
    assert not (await reg.get("messages.send").run({"text": "hi"})).ok
    assert not (await reg.get("messages.send").run({"to": "+1"})).ok
    assert osa.scripts == []


# ── calendar ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_calendar_today_bounded_query_and_parse(reg, osa):
    fs, rs = apple._FIELD_SEP, apple._RECORD_SEP
    # title US start US end US numeric-sortkey
    out = (
        f"Standup{fs}Thursday, July 9, 2026 at 9:30:00 AM{fs}"
        f"Thursday, July 9, 2026 at 9:45:00 AM{fs}3600{rs}"
    )
    osa.replies = [(True, out)]
    res = await reg.get("calendar.today").run({})
    assert res.ok and res.summary == "1 event today"
    assert res.data["events"] == [{"title": "Standup", "start": "9:30 AM", "end": "9:45 AM"}]
    script = osa.scripts[0]
    # Bounded: start-date window, never a full enumeration.
    assert "start date ≥ d0 and start date < d1" in script
    # A numeric sort key rides in the 4th field (weekday-name strings would
    # sort "Friday" before "Thursday").
    assert "((start date of e) - ref)" in script


@pytest.mark.asyncio
async def test_calendar_today_timeout_degrades(reg, osa):
    # run_osascript's timeout failure shape.
    osa.replies = [(False, "osascript timeout")]
    res = await reg.get("calendar.today").run({})
    assert not res.ok and res.summary == "couldn't read Calendar in time"


@pytest.mark.asyncio
async def test_calendar_next_picks_earliest(reg, osa):
    fs, rs = apple._FIELD_SEP, apple._RECORD_SEP
    # "Later" comes first in the stream and its weekday name ("Friday")
    # sorts BEFORE "Thursday" alphabetically — the numeric sort key
    # (90000 vs 3600 s) is what correctly picks "Sooner".
    out = (
        f"Later{fs}Friday, July 10, 2026 at 2:00:00 PM{fs}{fs}90000{rs}"
        f"Sooner{fs}Thursday, July 9, 2026 at 11:00:00 AM{fs}{fs}3600{rs}"
    )
    osa.replies = [(True, out)]
    res = await reg.get("calendar.next").run({})
    assert res.ok
    assert res.data["events"][0]["title"] == "Sooner"
    assert res.data["events"][0]["start"] == "11:00 AM"


@pytest.mark.asyncio
async def test_calendar_next_empty(reg, osa):
    osa.replies = [(True, "")]
    res = await reg.get("calendar.next").run({})
    assert res.ok and res.summary == "nothing coming up"


@pytest.mark.asyncio
async def test_calendar_tcc_denied(reg, osa):
    osa.replies = [(False, _DENIED_MINUS_1743)]
    res = await reg.get("calendar.today").run({})
    assert not res.ok and res.data["setup"] == "automation"


# ── mail ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_mail_unread_parses_and_caps(reg, osa):
    fs, rs = apple._FIELD_SEP, apple._RECORD_SEP
    out = f"Ada <ada@x.com>{fs}Re: kettle{rs}Bob{fs}Lunch?{rs}"
    osa.replies = [(True, out)]
    res = await reg.get("mail.unread").run({})
    assert res.ok and res.summary == "2 unread"
    assert res.data["messages"] == [
        {"from": "Ada <ada@x.com>", "subject": "Re: kettle"},
        {"from": "Bob", "subject": "Lunch?"},
    ]
    assert "read status is false" in osa.scripts[0]
    # Cap is baked into the script so Mail stops early.
    assert "if c ≥ 8 then exit repeat" in osa.scripts[0]


@pytest.mark.asyncio
async def test_mail_unread_clear(reg, osa):
    osa.replies = [(True, "")]
    res = await reg.get("mail.unread").run({})
    assert res.ok and res.summary == "inbox clear"


@pytest.mark.asyncio
async def test_mail_unread_timeout_degrades(reg, osa):
    osa.replies = [(False, "osascript timeout")]
    res = await reg.get("mail.unread").run({})
    assert not res.ok and res.summary == "couldn't read Mail in time"


@pytest.mark.asyncio
async def test_mail_unread_tcc_denied(reg, osa):
    osa.replies = [(False, _DENIED_ASSISTIVE)]
    res = await reg.get("mail.unread").run({})
    assert not res.ok and res.data["setup"] == "automation"
    assert "Automation permission for Mail" in res.summary
