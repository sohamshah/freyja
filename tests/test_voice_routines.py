"""Voice routines — store + routine.* verbs (contract §13, pinned).

Follows the test_voice_service seams: FakeRegistry/FakeVerb injected via
the constructor, `_register_service_verbs` invoked manually (the existing
convention when the registry is injected), an emit collector, tmp_path
storage. Fake verbs are re-registered AFTER the service verbs so a fake
`computer.click` wins over the real gated one.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import yaml

import bridge.voice.service as voice_service_module
from bridge.voice.routines import (
    INFO_VERBS,
    Routine,
    RoutineStep,
    RoutineStore,
    slugify,
)
from bridge.voice.service import VoiceService

# ── fakes (test_voice_service idiom) ──────────────────────────────────────


class FakeResult:
    def __init__(
        self,
        ok=True,
        summary="",
        say=None,
        data=None,
        undo=None,
        error=None,
        image_b64=None,
        image_w=None,
        image_h=None,
    ):
        self.ok = ok
        self.summary = summary
        self.say = say
        self.data = data or {}
        self.undo = undo
        self.error = error
        self.image_b64 = image_b64
        self.image_w = image_w
        self.image_h = image_h


class FakeVerb:
    def __init__(self, name, run, tier="auto"):
        self.name = name
        self.description = f"fake {name}"
        self.params = {}
        self.required = []
        self.tier = tier
        self.run = run


class FakeRegistry:
    def __init__(self, verbs=()):
        self._verbs = {v.name: v for v in verbs}

    def register(self, verb):
        self._verbs[verb.name] = verb

    def get(self, name):
        return self._verbs.get(name)

    def all(self):
        return list(self._verbs.values())

    def catalog_markdown(self):
        return "\n".join(f"- {name}()" for name in self._verbs)

    def openai_tool_schema(self):
        return {"type": "function", "name": "act"}


def make_service(tmp_path, verbs=()):
    """Service with the routine verbs registered for real (manual
    `_register_service_verbs` call — the injected-registry convention);
    the caller's fakes are registered last so they shadow same-named
    service registrations (e.g. the gated computer.*)."""
    events = []
    svc = VoiceService(
        SimpleNamespace(default_model="test-model", computer_enabled=False),
        base_dir=tmp_path / "voice",
        registry=FakeRegistry(()),
        emit_fn=events.append,
    )
    svc._register_service_verbs(svc._registry)
    for verb in verbs:
        svc._registry.register(verb)
    return svc, events


def events_of(events, ev_type):
    return [e for e in events if e.get("type") == ev_type]


def _ok_run(summary, **kw):
    async def run(args):
        return FakeResult(ok=True, summary=summary, **kw)

    return run


def _act_cmd(vsid, call_id, verb, args=None, confirm_token=None, heard=""):
    payload = {"verb": verb}
    if args is not None:
        payload["args"] = args
    if confirm_token is not None:
        payload["confirm_token"] = confirm_token
    return {
        "voiceSessionId": vsid,
        "callId": call_id,
        "name": "act",
        "argumentsJson": json.dumps(payload),
        "heard": heard,
    }


async def _save_routine(svc, events, args, vsid="v1"):
    """Drive routine.save through its confirm gate. Returns
    (refusal_event, final_event) — the CONFIRM REQUIRED result and the
    post-confirmation result."""
    before = len(events)
    await svc.handle_tool_call(_act_cmd(vsid, "c-save-1", "routine.save", args=args))
    refusal = events_of(events[before:], "voice_tool_result")[-1]
    token = refusal["needsConfirm"]["token"]
    before = len(events)
    await svc.handle_tool_call(
        _act_cmd(vsid, "c-save-2", "routine.save", args=args, confirm_token=token)
    )
    final = events_of(events[before:], "voice_tool_result")[-1]
    return refusal, final


def _routine(name, steps, description="", stats=None):
    r = Routine(
        name=name,
        description=description,
        created_ts=1_780_000_000_000,
        updated_ts=1_780_000_000_000,
        steps=steps,
    )
    if stats:
        r.stats.update(stats)
    return r


# ── slug + store ──────────────────────────────────────────────────────────


def test_slugify_normalization():
    assert slugify("morning") == "morning"
    assert slugify("Morning Routine") == "morning-routine"
    assert slugify("  Hey!!  There  ") == "hey-there"
    assert slugify("a--b") == "a-b"
    assert slugify("***") == ""
    assert slugify("") == ""


def test_store_round_trip(tmp_path):
    store = RoutineStore(tmp_path / "routines")
    routine = _routine(
        "morning",
        [
            RoutineStep(verb="app.focus", args={"name": "cmux"}),
            RoutineStep(verb="computer.press", args={"key": "cmd+n"}, wait_ms=500),
        ],
        description="open cmux and start a session",
    )
    store.save(routine)
    loaded = store.get("morning")
    assert loaded is not None
    assert loaded.name == "morning"
    assert loaded.description == "open cmux and start a session"
    assert loaded.created_ts == 1_780_000_000_000
    assert [s.to_dict() for s in loaded.steps] == [
        {"verb": "app.focus", "args": {"name": "cmux"}},
        {"verb": "computer.press", "args": {"key": "cmd+n"}, "wait_ms": 500},
    ]
    assert loaded.stats == {"runs": 0, "ok": 0, "fail": 0, "last_run_ts": None}
    assert [r.name for r in store.load_all()] == ["morning"]


def test_store_atomic_write_no_tmp_and_schema_shape(tmp_path):
    store = RoutineStore(tmp_path / "routines")
    store.save(_routine("morning", [RoutineStep(verb="app.focus", args={"name": "cmux"})]))
    files = sorted(p.name for p in (tmp_path / "routines").iterdir())
    assert files == ["morning.yaml"]  # no .tmp left behind
    raw = yaml.safe_load((tmp_path / "routines" / "morning.yaml").read_text())
    assert list(raw) == ["name", "description", "created_ts", "updated_ts", "steps", "stats"]
    assert raw["steps"] == [{"verb": "app.focus", "args": {"name": "cmux"}}]


def test_store_get_is_slug_normalized(tmp_path):
    store = RoutineStore(tmp_path / "routines")
    store.save(_routine("Morning Routine", [RoutineStep(verb="app.focus")]))
    assert (tmp_path / "routines" / "morning-routine.yaml").exists()
    for query in ("Morning Routine", "morning routine", "MORNING   ROUTINE", "morning-routine"):
        got = store.get(query)
        assert got is not None and got.name == "Morning Routine", query
    assert store.get("evening") is None
    assert store.get("") is None


def test_store_corrupt_file_skipped_never_fatal(tmp_path):
    store = RoutineStore(tmp_path / "routines")
    store.save(_routine("good", [RoutineStep(verb="app.focus")]))
    (tmp_path / "routines" / "torn.yaml").write_text("{unbalanced: [", encoding="utf-8")
    (tmp_path / "routines" / "notadict.yaml").write_text("- 1\n- 2\n", encoding="utf-8")
    (tmp_path / "routines" / "badsteps.yaml").write_text(
        "name: badsteps\nsteps: nope\n", encoding="utf-8"
    )
    assert [r.name for r in store.load_all()] == ["good"]
    assert store.get("torn") is None  # tolerant single-get too
    assert store.get("good") is not None


def test_store_delete_returns_routine_for_undo(tmp_path):
    store = RoutineStore(tmp_path / "routines")
    store.save(_routine("morning", [RoutineStep(verb="app.focus")]))
    deleted = store.delete("Morning")  # normalized lookup
    assert deleted is not None and deleted.name == "morning"
    assert not (tmp_path / "routines" / "morning.yaml").exists()
    assert store.delete("morning") is None  # second delete: nothing there
    # the returned routine restores cleanly
    store.save(deleted)
    assert store.get("morning") is not None


def test_store_names_md(tmp_path):
    store = RoutineStore(tmp_path / "routines")
    assert store.names_md() == ""
    store.save(
        _routine(
            "morning",
            [RoutineStep(verb="app.focus"), RoutineStep(verb="computer.press")],
            description="open cmux",
        )
    )
    store.save(_routine("standup", [RoutineStep(verb="app.open")]))
    assert store.names_md() == "- morning (2 steps) — open cmux\n- standup (1 step)"


# ── routine.save: validation matrix ───────────────────────────────────────


async def test_save_explicit_steps_persists(tmp_path):
    svc, events = make_service(tmp_path, verbs=[FakeVerb("demo.a", run=_ok_run("a"))])
    refusal, final = await _save_routine(
        svc,
        events,
        {
            "name": "kickoff",
            "description": "one step",
            "steps": [{"verb": "demo.a", "args": {"x": 1}, "wait_ms": 250}],
        },
    )
    # confirm-tier: the first call never writes
    assert refusal["ok"] is False
    assert refusal["needsConfirm"]["summary"] == "Save routine 'kickoff': demo.a (1 step)"
    assert final["ok"] is True
    body = json.loads(final["output"])
    assert body["summary"] == "saved routine 'kickoff' (1 step: demo.a)"
    assert body["data"]["derived"] is False
    assert body["data"]["replaced"] is False
    saved = svc.routines.get("kickoff")
    assert saved is not None and saved.description == "one step"
    assert [s.to_dict() for s in saved.steps] == [
        {"verb": "demo.a", "args": {"x": 1}, "wait_ms": 250}
    ]


async def test_save_empty_name_refused(tmp_path):
    svc, events = make_service(tmp_path, verbs=[FakeVerb("demo.a", run=_ok_run("a"))])
    for bad_name in ("", "   ", "!!!"):
        _, final = await _save_routine(
            svc, events, {"name": bad_name, "steps": [{"verb": "demo.a"}]}
        )
        assert final["ok"] is False
        assert json.loads(final["output"])["error"] == "missing_name"
    assert svc.routines.load_all() == []


async def test_save_unknown_step_verb_refused(tmp_path):
    svc, events = make_service(tmp_path, verbs=[FakeVerb("demo.a", run=_ok_run("a"))])
    _, final = await _save_routine(
        svc,
        events,
        {"name": "x", "steps": [{"verb": "demo.a"}, {"verb": "email.send"}]},
    )
    assert final["ok"] is False
    body = json.loads(final["output"])
    assert body["error"] == "unknown_step_verb"
    assert body["summary"] == "step 2 uses unknown verb email.send"
    assert svc.routines.get("x") is None  # nothing was written


async def test_save_confirm_tier_step_refused_naming_offender(tmp_path):
    svc, events = make_service(
        tmp_path, verbs=[FakeVerb("app.quit", run=_ok_run("quit"), tier="confirm")]
    )
    _, final = await _save_routine(svc, events, {"name": "x", "steps": [{"verb": "app.quit"}]})
    assert final["ok"] is False
    body = json.loads(final["output"])
    assert body["error"] == "confirm_step"
    assert body["summary"] == (
        "app.quit needs a spoken yes each time, so it can't go in a routine"
    )


async def test_save_recursive_routine_step_refused(tmp_path):
    svc, events = make_service(tmp_path)
    for nested in ("routine.run", "routine.save", "routine.bogus"):
        _, final = await _save_routine(svc, events, {"name": "x", "steps": [{"verb": nested}]})
        assert final["ok"] is False
        assert json.loads(final["output"])["error"] == "recursive_step", nested


async def test_save_malformed_steps_refused(tmp_path):
    svc, events = make_service(tmp_path, verbs=[FakeVerb("demo.a", run=_ok_run("a"))])
    cases = [
        ({"name": "x", "steps": "demo.a"}, "bad_steps"),  # not a list
        ({"name": "x", "steps": ["demo.a"]}, "bad_steps"),  # step not an object
        ({"name": "x", "steps": [{"verb": "demo.a", "args": ["y"]}]}, "bad_step_args"),
        ({"name": "x", "steps": [{"verb": "demo.a", "wait_ms": -5}]}, "bad_step_wait"),
        ({"name": "x", "steps": [{"verb": "demo.a", "wait_ms": True}]}, "bad_step_wait"),
    ]
    for args, expected_error in cases:
        _, final = await _save_routine(svc, events, args)
        assert final["ok"] is False, args
        assert json.loads(final["output"])["error"] == expected_error, args


async def test_save_overwrite_says_replacing_and_keeps_created_ts(tmp_path):
    svc, events = make_service(
        tmp_path,
        verbs=[FakeVerb("demo.a", run=_ok_run("a")), FakeVerb("demo.b", run=_ok_run("b"))],
    )
    _, first = await _save_routine(svc, events, {"name": "morning", "steps": [{"verb": "demo.a"}]})
    assert first["ok"] is True
    created_ts = svc.routines.get("morning").created_ts
    # simulate history so the overwrite's stats reset is observable
    seasoned = svc.routines.get("morning")
    seasoned.stats.update({"runs": 4, "ok": 3, "fail": 1})
    svc.routines.save(seasoned)

    refusal, final = await _save_routine(
        svc, events, {"name": "Morning", "steps": [{"verb": "demo.b"}]}
    )
    assert "(replacing)" in refusal["needsConfirm"]["summary"]
    assert final["ok"] is True
    body = json.loads(final["output"])
    assert body["summary"].startswith("replaced routine 'Morning'")
    assert body["data"]["replaced"] is True
    replaced = svc.routines.get("morning")
    assert [s.verb for s in replaced.steps] == ["demo.b"]
    assert replaced.created_ts == created_ts  # original creation stands
    assert replaced.stats == {"runs": 0, "ok": 0, "fail": 0, "last_run_ts": None}


# ── routine.save: receipts-derived steps ──────────────────────────────────


def _seed(svc, vsid, verb, *, ok=True, lane="brain", args=None):
    svc._record(
        voice_session_id=vsid,
        heard="",
        lane=lane,
        verb=verb,
        args=args or {},
        ok=ok,
        summary=verb,
        undoable=False,
    )


async def test_save_derives_filtered_chronological_steps(tmp_path):
    svc, events = make_service(
        tmp_path,
        verbs=[
            FakeVerb("spotify.play", run=_ok_run("play")),
            FakeVerb("spotify.pause", run=_ok_run("pause")),
            FakeVerb("app.focus", run=_ok_run("focus")),
            FakeVerb("spotify.now_playing", run=_ok_run("np")),
            FakeVerb("mission.spawn", run=_ok_run("spawn")),
        ],
    )
    svc._active_session_id = "v1"
    _seed(svc, "v1", "spotify.play", args={"query": "vienna"})  # kept (1)
    _seed(svc, "v1", "spotify.now_playing")  # info verb → excluded
    _seed(svc, "v1", "app.focus", ok=False)  # failed → excluded
    _seed(svc, "v2", "app.focus")  # other session → excluded
    _seed(svc, "v1", "mission.spawn", lane="mission")  # mission lane → excluded
    _seed(svc, "v1", "spotify.pause", lane="floor")  # floor lane kept (2)
    _seed(svc, "v1", "app.focus", args={"name": "cmux"})  # kept (3)
    events.clear()

    refusal, final = await _save_routine(svc, events, {"name": "morning"})
    # the confirm line names the REAL derived steps, in order
    assert refusal["needsConfirm"]["summary"] == (
        "Save routine 'morning': spotify.play → spotify.pause → app.focus (3 steps)"
    )
    assert final["ok"] is True
    body = json.loads(final["output"])
    assert body["data"]["derived"] is True
    saved = svc.routines.get("morning")
    assert [s.to_dict() for s in saved.steps] == [
        {"verb": "spotify.play", "args": {"query": "vienna"}},
        {"verb": "spotify.pause", "args": {}},
        {"verb": "app.focus", "args": {"name": "cmux"}},
    ]


async def test_save_derive_empty_exchange_refused(tmp_path):
    svc, events = make_service(tmp_path)
    svc._active_session_id = "v1"
    _seed(svc, "v1", "spotify.now_playing", ok=True)  # info-only exchange
    _, final = await _save_routine(svc, events, {"name": "morning"})
    assert final["ok"] is False
    body = json.loads(final["output"])
    assert body["summary"] == "nothing to save — no actions in this exchange"
    assert svc.routines.get("morning") is None
    # its own awaiting-confirmation receipt never becomes a step
    assert svc._derive_routine_steps() == []


async def test_save_derive_without_active_session_refused(tmp_path):
    svc, events = make_service(tmp_path)
    _seed(svc, "v1", "spotify.play")
    svc._active_session_id = None
    _, final = await _save_routine(svc, events, {"name": "x"}, vsid="")
    assert final["ok"] is False
    assert json.loads(final["output"])["error"] == "no_steps"


async def test_save_confirm_summary_caps_at_90_chars(tmp_path):
    long_verbs = [FakeVerb(f"demo.verylongverbname{i}", run=_ok_run("x")) for i in range(8)]
    svc, events = make_service(tmp_path, verbs=long_verbs)
    steps = [{"verb": v.name} for v in long_verbs]
    refusal, _ = await _save_routine(svc, events, {"name": "everything", "steps": steps})
    line = refusal["needsConfirm"]["summary"]
    assert len(line) <= 90
    assert line.endswith("…")


# ── routine.run ───────────────────────────────────────────────────────────


def _tracking_verb(name, calls, ok=True, undo_log=None, error=None, **result_kw):
    async def run(args):
        calls.append((name, args))
        undo = None
        if undo_log is not None:

            async def _undo():
                undo_log.append(name)
                return FakeResult(ok=True, summary=f"undid {name}")

            undo = _undo
        return FakeResult(
            ok=ok, summary=f"did {name}", undo=undo, error=error, **result_kw
        )

    return FakeVerb(name, run=run)


async def test_run_happy_path_order_receipts_single_result(tmp_path):
    calls = []
    svc, events = make_service(
        tmp_path,
        verbs=[_tracking_verb("demo.a", calls), _tracking_verb("demo.b", calls)],
    )
    svc._active_session_id = "v1"
    svc.routines.save(
        _routine("morning", [RoutineStep("demo.a", {"x": 1}), RoutineStep("demo.b", {})])
    )
    await svc.handle_tool_call(_act_cmd("v1", "c1", "routine.run", args={"name": "morning"}))
    assert calls == [("demo.a", {"x": 1}), ("demo.b", {})]
    # ONE tool result for the whole run
    (result,) = events_of(events, "voice_tool_result")
    assert result["ok"] is True
    body = json.loads(result["output"])
    assert body["summary"] == "▷ ran 'morning' (2 steps)"
    assert body["data"]["steps"] == [
        {"verb": "demo.a", "ok": True, "summary": "did demo.a"},
        {"verb": "demo.b", "ok": True, "summary": "did demo.b"},
    ]
    # per-step receipts + the routine.run receipt itself, all brain lane
    receipts = [e["receipt"] for e in events_of(events, "voice_receipt")]
    assert [(r["verb"], r["heard"]) for r in receipts] == [
        ("demo.a", "(routine morning · step 1)"),
        ("demo.b", "(routine morning · step 2)"),
        ("routine.run", ""),
    ]
    assert all(r["lane"] == "brain" and r["ok"] for r in receipts)


async def test_run_normalized_name_match(tmp_path):
    calls = []
    svc, events = make_service(tmp_path, verbs=[_tracking_verb("demo.a", calls)])
    svc.routines.save(_routine("Morning Routine", [RoutineStep("demo.a")]))
    await svc.handle_tool_call(
        _act_cmd("v1", "c1", "routine.run", args={"name": "morning   routine"})
    )
    (result,) = events_of(events, "voice_tool_result")
    assert result["ok"] is True
    assert calls == [("demo.a", {})]


async def test_run_unknown_name_suggests_close_match(tmp_path):
    svc, events = make_service(tmp_path)
    svc.routines.save(_routine("morning", [RoutineStep("demo.a")]))
    await svc.handle_tool_call(_act_cmd("v1", "c1", "routine.run", args={"name": "mornin"}))
    (result,) = events_of(events, "voice_tool_result")
    assert result["ok"] is False
    body = json.loads(result["output"])
    assert body["error"] == "unknown_routine"
    assert body["summary"] == "no routine named 'mornin' — did you mean 'morning'?"


async def test_run_stops_on_first_failure(tmp_path):
    calls = []
    svc, events = make_service(
        tmp_path,
        verbs=[
            _tracking_verb("demo.a", calls),
            _tracking_verb("demo.b", calls, ok=False, error="boom"),
            _tracking_verb("demo.c", calls),
        ],
    )
    svc.routines.save(
        _routine("x", [RoutineStep("demo.a"), RoutineStep("demo.b"), RoutineStep("demo.c")])
    )
    await svc.handle_tool_call(_act_cmd("v1", "c1", "routine.run", args={"name": "x"}))
    assert [c[0] for c in calls] == ["demo.a", "demo.b"]  # demo.c never ran
    (result,) = events_of(events, "voice_tool_result")
    assert result["ok"] is False
    body = json.loads(result["output"])
    assert body["summary"] == "routine 'x' failed at step 2/3 (demo.b): boom"
    assert [s["ok"] for s in body["data"]["steps"]] == [True, False]


async def test_run_step_exception_becomes_failure(tmp_path):
    async def explode(args):
        raise RuntimeError("osascript exploded")

    svc, events = make_service(tmp_path, verbs=[FakeVerb("demo.a", run=explode)])
    svc.routines.save(_routine("x", [RoutineStep("demo.a")]))
    await svc.handle_tool_call(_act_cmd("v1", "c1", "routine.run", args={"name": "x"}))
    (result,) = events_of(events, "voice_tool_result")
    assert result["ok"] is False
    body = json.loads(result["output"])
    assert body["summary"] == (
        "routine 'x' failed at step 1/1 (demo.a): demo.a failed: osascript exploded"
    )


async def test_run_missing_verb_fails_with_step_message(tmp_path):
    calls = []
    svc, events = make_service(tmp_path, verbs=[_tracking_verb("demo.a", calls)])
    # gone.verb was valid at save time, then vanished from the registry
    svc.routines.save(_routine("x", [RoutineStep("demo.a"), RoutineStep("gone.verb")]))
    await svc.handle_tool_call(_act_cmd("v1", "c1", "routine.run", args={"name": "x"}))
    (result,) = events_of(events, "voice_tool_result")
    body = json.loads(result["output"])
    assert result["ok"] is False
    assert "step 2 uses verb gone.verb which no longer exists" in body["summary"]
    assert body["summary"].startswith("routine 'x' failed at step 2/2 (gone.verb)")


async def test_run_rechecks_tier_at_run_time(tmp_path):
    calls = []
    svc, events = make_service(tmp_path, verbs=[_tracking_verb("demo.a", calls)])
    svc.routines.save(_routine("x", [RoutineStep("demo.a")]))
    svc._registry.get("demo.a").tier = "confirm"  # promoted since the save
    await svc.handle_tool_call(_act_cmd("v1", "c1", "routine.run", args={"name": "x"}))
    assert calls == []  # never executed
    (result,) = events_of(events, "voice_tool_result")
    assert result["ok"] is False
    assert "needs a spoken yes" in json.loads(result["output"])["summary"]


async def test_run_settle_default_and_wait_ms_override(tmp_path, monkeypatch):
    settles = []

    async def fake_settle(ms):
        settles.append(ms)

    monkeypatch.setattr(voice_service_module, "_routine_settle", fake_settle)
    calls = []
    svc, events = make_service(
        tmp_path,
        verbs=[
            _tracking_verb("computer.click", calls),
            _tracking_verb("app.focus", calls),
            _tracking_verb("spotify.play", calls),
        ],
    )
    svc.routines.save(
        _routine(
            "x",
            [
                RoutineStep("computer.click", {"x": 1, "y": 2}),  # default 400
                RoutineStep("app.focus", {"name": "cmux"}, wait_ms=500),  # override
                RoutineStep("spotify.play", {}),  # non-GUI verb: no settle
            ],
        )
    )
    await svc.handle_tool_call(_act_cmd("v1", "c1", "routine.run", args={"name": "x"}))
    (result,) = events_of(events, "voice_tool_result")
    assert result["ok"] is True
    assert settles == [400, 500]


async def test_run_propagates_last_step_image(tmp_path):
    calls = []
    svc, events = make_service(
        tmp_path,
        verbs=[
            _tracking_verb("demo.a", calls),
            _tracking_verb(
                "computer.click", calls, image_b64="Zm9v", image_w=1280, image_h=800
            ),
        ],
    )
    svc.routines.save(_routine("x", [RoutineStep("demo.a"), RoutineStep("computer.click")]))
    await svc.handle_tool_call(_act_cmd("v1", "c1", "routine.run", args={"name": "x"}))
    (result,) = events_of(events, "voice_tool_result")
    assert result["imageB64"] == "Zm9v"
    assert result["imageW"] == 1280 and result["imageH"] == 800


async def test_run_no_stale_image_when_last_step_has_none(tmp_path):
    calls = []
    svc, events = make_service(
        tmp_path,
        verbs=[
            _tracking_verb("computer.click", calls, image_b64="Zm9v", image_w=1, image_h=1),
            _tracking_verb("demo.a", calls),
        ],
    )
    svc.routines.save(_routine("x", [RoutineStep("computer.click"), RoutineStep("demo.a")]))
    await svc.handle_tool_call(_act_cmd("v1", "c1", "routine.run", args={"name": "x"}))
    (result,) = events_of(events, "voice_tool_result")
    # an earlier screenshot is NOT the end state — never propagate it
    assert "imageB64" not in result


async def test_run_updates_stats_ok_and_fail(tmp_path):
    calls = []
    ok_verb = _tracking_verb("demo.a", calls)
    svc, events = make_service(tmp_path, verbs=[ok_verb])
    svc.routines.save(_routine("x", [RoutineStep("demo.a")]))
    before_ms = int(time.time() * 1000)
    await svc.handle_tool_call(_act_cmd("v1", "c1", "routine.run", args={"name": "x"}))
    stats = svc.routines.get("x").stats
    assert stats["runs"] == 1 and stats["ok"] == 1 and stats["fail"] == 0
    assert stats["last_run_ts"] >= before_ms

    svc._registry.register(_tracking_verb("demo.a", calls, ok=False, error="boom"))
    await svc.handle_tool_call(_act_cmd("v1", "c2", "routine.run", args={"name": "x"}))
    stats = svc.routines.get("x").stats
    assert stats["runs"] == 2 and stats["ok"] == 1 and stats["fail"] == 1


async def test_run_undo_reverses_step_undos(tmp_path):
    calls, undo_log = [], []
    svc, events = make_service(
        tmp_path,
        verbs=[
            _tracking_verb("demo.a", calls, undo_log=undo_log),
            _tracking_verb("demo.b", calls, undo_log=undo_log),
        ],
    )
    svc.routines.save(_routine("morning", [RoutineStep("demo.a"), RoutineStep("demo.b")]))
    await svc.handle_tool_call(_act_cmd("v1", "c1", "routine.run", args={"name": "morning"}))
    receipts = [e["receipt"] for e in events_of(events, "voice_receipt")]
    step_ids = [r["id"] for r in receipts if r["verb"] != "routine.run"]
    (run_receipt,) = [r for r in receipts if r["verb"] == "routine.run"]
    assert run_receipt["undoable"] is True
    assert all(r["undoable"] for r in receipts if r["verb"] != "routine.run")
    events.clear()

    await svc.handle_undo({"receiptId": run_receipt["id"]})
    assert undo_log == ["demo.b", "demo.a"]  # reverse order
    (result,) = events_of(events, "voice_tool_result")
    assert result["ok"] is True
    assert json.loads(result["output"])["summary"] == "undid 2/2 steps of 'morning'"
    # the step receipts flipped to undone in the persisted store
    stored = {r.id: r for r in svc.receipts.recent(limit=10)}
    assert all(stored[sid].undone for sid in step_ids)


async def test_run_undo_skips_individually_undone_step(tmp_path):
    calls, undo_log = [], []
    svc, events = make_service(
        tmp_path,
        verbs=[
            _tracking_verb("demo.a", calls, undo_log=undo_log),
            _tracking_verb("demo.b", calls, undo_log=undo_log),
        ],
    )
    svc.routines.save(_routine("morning", [RoutineStep("demo.a"), RoutineStep("demo.b")]))
    await svc.handle_tool_call(_act_cmd("v1", "c1", "routine.run", args={"name": "morning"}))
    receipts = [e["receipt"] for e in events_of(events, "voice_receipt")]
    step_b = [r for r in receipts if r["verb"] == "demo.b"][0]
    (run_receipt,) = [r for r in receipts if r["verb"] == "routine.run"]
    # undo step b individually first — the ledger pop is single-use
    await svc.handle_undo({"receiptId": step_b["id"]})
    assert undo_log == ["demo.b"]
    events.clear()

    await svc.handle_undo({"receiptId": run_receipt["id"]})
    assert undo_log == ["demo.b", "demo.a"]  # b was NOT double-undone
    (result,) = events_of(events, "voice_tool_result")
    assert json.loads(result["output"])["summary"] == "undid 1/2 steps of 'morning'"


async def test_run_panic_aborts_before_next_step(tmp_path):
    calls = []
    svc, events = make_service(tmp_path, verbs=[_tracking_verb("demo.a", calls)])
    svc._active_session_id = "v1"
    svc._panicked_sessions.add("v1")
    svc.routines.save(_routine("morning", [RoutineStep("demo.a")]))
    await svc.handle_tool_call(_act_cmd("v1", "c1", "routine.run", args={"name": "morning"}))
    assert calls == []  # nothing ran
    (result,) = events_of(events, "voice_tool_result")
    assert result["ok"] is False
    body = json.loads(result["output"])
    assert body["summary"] == "routine 'morning' stopped"
    assert body["error"] == "stopped"
    stats = svc.routines.get("morning").stats
    assert stats["runs"] == 1 and stats["fail"] == 1


# ── routine.list / routine.forget ─────────────────────────────────────────


async def test_list_reports_rows_and_count(tmp_path):
    svc, events = make_service(tmp_path)
    await svc.handle_tool_call(_act_cmd("v1", "c0", "routine.list"))
    assert json.loads(events_of(events, "voice_tool_result")[0]["output"])["summary"] == (
        "no routines yet"
    )
    events.clear()

    svc.routines.save(
        _routine(
            "morning",
            [RoutineStep("demo.a"), RoutineStep("demo.b")],
            description="open cmux",
            stats={"runs": 3, "ok": 2, "fail": 1},
        )
    )
    svc.routines.save(_routine("standup", [RoutineStep("demo.a")]))
    await svc.handle_tool_call(_act_cmd("v1", "c1", "routine.list"))
    (result,) = events_of(events, "voice_tool_result")
    body = json.loads(result["output"])
    assert body["summary"] == "2 routines"
    assert body["data"]["routines"] == [
        {"name": "morning", "description": "open cmux", "steps": 2, "runs": 3, "ok": 2},
        {"name": "standup", "description": "", "steps": 1, "runs": 0, "ok": 0},
    ]


async def test_forget_and_undo_restores(tmp_path):
    svc, events = make_service(tmp_path)
    svc.routines.save(_routine("morning", [RoutineStep("demo.a", {"x": 1})]))
    await svc.handle_tool_call(_act_cmd("v1", "c1", "routine.forget", args={"name": "Morning"}))
    (result,) = events_of(events, "voice_tool_result")
    assert result["ok"] is True
    assert json.loads(result["output"])["summary"] == "forgot routine 'morning'"
    assert svc.routines.get("morning") is None
    (receipt_ev,) = events_of(events, "voice_receipt")
    assert receipt_ev["receipt"]["undoable"] is True
    events.clear()

    await svc.handle_undo({"receiptId": receipt_ev["receipt"]["id"]})
    (undo_result,) = events_of(events, "voice_tool_result")
    assert undo_result["ok"] is True
    restored = svc.routines.get("morning")
    assert restored is not None
    assert [s.to_dict() for s in restored.steps] == [{"verb": "demo.a", "args": {"x": 1}}]


async def test_forget_unknown_refused(tmp_path):
    svc, events = make_service(tmp_path)
    await svc.handle_tool_call(_act_cmd("v1", "c1", "routine.forget", args={"name": "nope"}))
    (result,) = events_of(events, "voice_tool_result")
    assert result["ok"] is False
    assert json.loads(result["output"])["error"] == "unknown_routine"


# ── prompt + mint ─────────────────────────────────────────────────────────


def test_build_instructions_routines_section_and_default():
    from bridge.voice.prompts import build_instructions

    # the old 1-arg call keeps working — every pre-routines callsite/test
    baseline = build_instructions("- x() — y")
    assert "# Routines" in baseline
    assert "- (none saved yet)" in baseline
    assert "routine.save" in baseline
    with_names = build_instructions("- x() — y", routines_md="- morning (2 steps)")
    assert "- morning (2 steps)" in with_names
    assert "(none saved yet)" not in with_names


def test_mint_config_bakes_routine_names(tmp_path):
    svc, _ = make_service(tmp_path)
    svc.routines.save(_routine("morning", [RoutineStep("demo.a")], description="open cmux"))
    session = svc._build_session_config(svc._registry)
    assert "- morning (1 step) — open cmux" in session["instructions"]


# ── INFO_VERBS drift guard ────────────────────────────────────────────────


def test_info_verbs_all_exist_in_full_registry(tmp_path):
    """Every pinned INFO_VERBS name must exist in the FULL verb surface:
    build_default_registry() plus the service-registered verbs
    (mission.* / computer.* / freyja.ask / routine.* live service-side).
    A rename that orphans an entry here silently weakens the derive
    filter — this guard makes that loud."""
    from bridge.voice.verbs import build_default_registry

    registry = build_default_registry()
    events = []
    svc = VoiceService(
        SimpleNamespace(default_model="test-model", computer_enabled=False),
        base_dir=tmp_path / "voice",
        registry=registry,
        emit_fn=events.append,
    )
    svc._register_service_verbs(registry)
    names = {v.name for v in registry.all()}
    missing = INFO_VERBS - names
    assert not missing, f"INFO_VERBS drifted from the registry: {sorted(missing)}"


async def test_save_folds_top_level_step_keys_into_args(tmp_path):
    """The model reliably flattens verb args to the step's top level
    ({"verb": "demo.a", "x": 1}) — observed in the live gate. Same lesson
    as confirm_token placement: fold them into args instead of saving a
    step that runs empty. Explicit args win collisions."""
    svc, events = make_service(tmp_path, verbs=[FakeVerb("demo.a", run=_ok_run("a"))])
    refusal, final = await _save_routine(
        svc,
        events,
        {
            "name": "folded",
            "steps": [{"verb": "demo.a", "x": 1, "label": "hi", "args": {"x": 99}}],
        },
    )
    assert final["ok"] is True
    saved = svc.routines.get("folded")
    # explicit args["x"]=99 wins; top-level "label" folded in.
    assert saved is not None
    assert saved.steps[0].args == {"x": 99, "label": "hi"}
