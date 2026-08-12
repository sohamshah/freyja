"""Adapter behavior: exact AppleScript strings, undo cycles, search, timers.

Nothing here touches the real Mac — `mac.run_osascript` / `mac.run_exec`
are the single subprocess seams and every test monkeypatches them. The
one exception is the FREYJA_VOICE_LIVE=1 smoke test at the bottom.
"""

import asyncio
import os
from types import SimpleNamespace

import pytest

from bridge.voice.adapters import mac, spotify, timers
from bridge.voice.verbs import build_default_registry


class ScriptRecorder:
    """Stands in for mac.run_osascript; replays canned (ok, out) replies."""

    def __init__(self, replies=None):
        self.scripts = []
        self.replies = list(replies or [])

    async def __call__(self, script, timeout=6.0):
        self.scripts.append(script)
        return self.replies.pop(0) if self.replies else (True, "")


class ExecRecorder:
    """Stands in for mac.run_exec."""

    def __init__(self, result=(True, "")):
        self.calls = []
        self.result = result

    async def __call__(self, argv, timeout=6.0):
        self.calls.append(list(argv))
        return self.result


@pytest.fixture
def osa(monkeypatch):
    rec = ScriptRecorder()
    # run_osascript_lines resolves run_osascript through module globals at
    # call time, so this one patch intercepts both entry points.
    monkeypatch.setattr(mac, "run_osascript", rec)
    return rec


@pytest.fixture
def run_exec(monkeypatch):
    rec = ExecRecorder()
    monkeypatch.setattr(mac, "run_exec", rec)
    return rec


@pytest.fixture
def reg():
    return build_default_registry()


@pytest.fixture(autouse=True)
def _reset_spotify_token_cache():
    spotify._token_cache.update({"token": None, "expires": 0.0})
    yield
    spotify._token_cache.update({"token": None, "expires": 0.0})


@pytest.fixture(autouse=True)
def _stub_installed_apps(monkeypatch):
    """Pin the app index so app.* resolution is deterministic and never
    touches the real machine's Spotlight / /Applications."""
    from bridge.voice.adapters import system

    index = {
        "safari": "Safari",
        "arc": "Arc",
        "google chrome": "Google Chrome",
        "google chrome canary": "Google Chrome Canary",
        "slack": "Slack",
        "visual studio code": "Visual Studio Code",
        "system settings": "System Settings",
    }

    async def fake_installed():
        return dict(index)

    monkeypatch.setattr(system, "_installed_apps", fake_installed)
    system._app_cache.update({"expires": 0.0, "by_name": {}})
    yield
    system._app_cache.update({"expires": 0.0, "by_name": {}})


@pytest.fixture
def no_spotify_creds(monkeypatch):
    monkeypatch.delenv("SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.delenv("SPOTIFY_CLIENT_SECRET", raising=False)


@pytest.fixture
def timer_events(osa):
    """Fake emitter + guaranteed cleanup of the process-lifetime manager."""
    events = []
    timers.set_emitter(events.append)
    yield events
    timers._manager.cancel_all()
    timers.set_emitter(None)


# ── spotify ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_spotify_transport_scripts(reg, osa):
    cases = [
        ("spotify.pause", 'tell application "Spotify" to pause', "⏸ paused"),
        ("spotify.resume", 'tell application "Spotify" to play', "▶ resumed"),
        ("spotify.next", 'tell application "Spotify" to next track', "⏭ next track"),
        ("spotify.previous", 'tell application "Spotify" to previous track', "⏮ previous track"),
    ]
    for name, script, summary in cases:
        res = await reg.get(name).run({})
        assert res.ok and res.summary == summary
        assert osa.scripts[-1] == script


@pytest.mark.asyncio
async def test_spotify_transport_failure_surfaces_error(reg, osa):
    """Raw AppleScript stderr rides in error= (for the model); the human
    summary stays terse — it lands verbatim in the HUD receipt row."""
    stderr = "execution error: Not authorized to send Apple events to Spotify. (-1743)"
    cases = [
        ("spotify.pause", "couldn't pause"),
        ("spotify.resume", "couldn't resume"),
        ("spotify.next", "couldn't skip"),
        ("spotify.previous", "couldn't go back"),
    ]
    for name, fail_summary in cases:
        osa.replies = [(False, stderr)]
        res = await reg.get(name).run({})
        assert not res.ok
        assert res.summary == fail_summary
        assert res.error == stderr


@pytest.mark.asyncio
async def test_spotify_play_explicit_uri(reg, osa):
    res = await reg.get("spotify.play").run({"uri": "spotify:track:abc123"})
    assert res.ok
    assert osa.scripts == ['tell application "Spotify" to play track "spotify:track:abc123"']
    assert res.data == {"uri": "spotify:track:abc123"}


@pytest.mark.asyncio
async def test_spotify_play_bare_resumes(reg, osa, no_spotify_creds):
    res = await reg.get("spotify.play").run({})
    assert res.ok and res.summary == "▶ resumed"
    assert osa.scripts == ['tell application "Spotify" to play']


@pytest.mark.asyncio
async def test_spotify_play_query_without_creds(reg, osa, no_spotify_creds):
    res = await reg.get("spotify.play").run({"query": "vienna billy joel"})
    assert not res.ok
    assert res.summary == "can't search — Spotify API creds not set"
    assert res.data == {"setup": "spotify_search"}
    assert osa.scripts == []  # no blind AppleScript attempt


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _FakeHttpx:
    """Hand-stub of the httpx surface spotify.py uses (AsyncClient ctx-mgr)."""

    def __init__(self, search_items):
        self.posts = []
        self.gets = []
        self._search_items = search_items
        outer = self

        class Client:
            def __init__(self, timeout=None):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, data=None, auth=None):
                outer.posts.append((url, data, auth))
                return _FakeResponse({"access_token": "tok-1", "expires_in": 3600})

            async def get(self, url, params=None, headers=None):
                outer.gets.append((url, params, headers))
                return _FakeResponse({"tracks": {"items": outer._search_items}})

        self.AsyncClient = Client


@pytest.fixture
def spotify_api(monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "shh")
    fake = _FakeHttpx(
        [
            {"uri": "spotify:track:VIENNA1", "name": "Vienna", "artists": [{"name": "Billy Joel"}]},
            {"uri": "spotify:track:OTHER", "name": "Vienna (Live)", "artists": []},
        ]
    )
    monkeypatch.setattr(spotify, "httpx", SimpleNamespace(AsyncClient=fake.AsyncClient))
    return fake


@pytest.mark.asyncio
async def test_spotify_play_search_path(reg, osa, spotify_api):
    res = await reg.get("spotify.play").run({"query": "vienna billy joel"})
    assert res.ok
    assert res.summary == "▶ Vienna — Billy Joel"
    # Token request: client-credentials against the accounts host.
    url, data, auth = spotify_api.posts[0]
    assert url == "https://accounts.spotify.com/api/token"
    assert data == {"grant_type": "client_credentials"}
    assert auth == ("cid", "shh")
    # Search request: exact params, bearer from the token call.
    url, params, headers = spotify_api.gets[0]
    assert url == "https://api.spotify.com/v1/search"
    assert params == {"q": "vienna billy joel", "type": "track", "limit": 3}
    assert headers == {"Authorization": "Bearer tok-1"}
    # Best (first) hit's uri is what gets played.
    assert osa.scripts == ['tell application "Spotify" to play track "spotify:track:VIENNA1"']


@pytest.mark.asyncio
async def test_spotify_play_track_artist_builds_query_and_caches_token(reg, osa, spotify_api):
    res = await reg.get("spotify.play").run({"track": "Vienna", "artist": "Billy Joel"})
    assert res.ok
    assert spotify_api.gets[0][1]["q"] == "Vienna Billy Joel"
    await reg.get("spotify.play").run({"query": "again"})
    assert len(spotify_api.posts) == 1  # token cached, no second mint
    assert len(spotify_api.gets) == 2


@pytest.mark.asyncio
async def test_spotify_play_search_no_results(reg, osa, monkeypatch):
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SPOTIFY_CLIENT_SECRET", "shh")
    fake = _FakeHttpx([])
    monkeypatch.setattr(spotify, "httpx", SimpleNamespace(AsyncClient=fake.AsyncClient))
    res = await reg.get("spotify.play").run({"query": "zzzz nothing"})
    assert not res.ok
    assert 'no Spotify results for "zzzz nothing"' == res.summary
    assert osa.scripts == []


@pytest.mark.asyncio
async def test_spotify_now_playing(reg, osa):
    osa.replies = [(True, "Vienna\nBilly Joel\nThe Stranger\n83\nplaying")]
    res = await reg.get("spotify.now_playing").run({})
    assert res.ok
    assert res.summary == "▶ Vienna — Billy Joel · The Stranger @ 1:23"
    assert res.data == {
        "track": "Vienna",
        "artist": "Billy Joel",
        "album": "The Stranger",
        "position_sec": 83,
        "state": "playing",
    }
    assert "current track" in osa.scripts[0]
    assert "player position" in osa.scripts[0]


@pytest.mark.asyncio
async def test_spotify_now_playing_paused_glyph_and_error(reg, osa):
    osa.replies = [(True, "Vienna\nBilly Joel\nThe Stranger\n0\npaused")]
    res = await reg.get("spotify.now_playing").run({})
    assert res.summary.startswith("⏸ ")
    osa.replies = [(False, "Spotify got an error: no current track")]
    res = await reg.get("spotify.now_playing").run({})
    assert not res.ok and res.summary == "nothing playing"


# ── system.volume ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_volume_absolute_level_with_undo(reg, osa):
    osa.replies = [(True, "55\nfalse"), (True, "")]
    res = await reg.get("system.volume").run({"level": 40})
    assert res.ok and res.summary == "volume 55% → 40%"
    assert "get volume settings" in osa.scripts[0]
    assert osa.scripts[1] == "set volume output volume 40"
    assert res.undo is not None
    undo_res = await res.undo()
    assert undo_res.ok and undo_res.summary == "volume restored to 55%"
    assert osa.scripts[2] == "set volume output volume 55\nset volume output muted false"


@pytest.mark.asyncio
async def test_volume_delta_and_clamp(reg, osa):
    osa.replies = [(True, "55\nfalse"), (True, "")]
    res = await reg.get("system.volume").run({"delta": -10})
    assert res.summary == "volume 55% → 45%"
    assert osa.scripts[1] == "set volume output volume 45"

    osa.replies = [(True, "95\nfalse"), (True, "")]
    res = await reg.get("system.volume").run({"delta": 10})
    assert res.summary == "volume 95% → 100%"
    assert osa.scripts[-1] == "set volume output volume 100"


@pytest.mark.asyncio
async def test_volume_mute_undo_restores_full_state(reg, osa):
    osa.replies = [(True, "55\nfalse"), (True, "")]
    res = await reg.get("system.volume").run({"mute": True})
    assert res.ok and res.summary == "muted"
    assert osa.scripts[1] == "set volume output muted true"
    undo_res = await res.undo()
    assert undo_res.ok
    # Undo restores BOTH the prior level and the prior mute flag.
    assert osa.scripts[2] == "set volume output volume 55\nset volume output muted false"


@pytest.mark.asyncio
async def test_volume_requires_some_change(reg, osa):
    res = await reg.get("system.volume").run({})
    assert not res.ok
    assert osa.scripts == []


@pytest.mark.asyncio
async def test_volume_delta_fails_without_prior_read(reg, osa):
    osa.replies = [(False, "no audio device")]
    res = await reg.get("system.volume").run({"delta": 10})
    assert not res.ok
    assert res.summary == "couldn't read current volume"


# ── app.* ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_app_open_via_open_a(reg, osa, run_exec):
    res = await reg.get("app.open").run({"name": "Safari"})
    assert res.ok and res.summary == "opened Safari"
    assert run_exec.calls == [["open", "-a", "Safari"]]
    assert osa.scripts == []


@pytest.mark.asyncio
async def test_app_open_falls_back_to_applescript(reg, osa, run_exec):
    run_exec.result = (False, "Unable to find application")
    res = await reg.get("app.open").run({"name": "Safari"})
    assert res.ok
    assert osa.scripts == ['tell application "Safari" to activate']


@pytest.mark.asyncio
async def test_app_focus_opens_and_activates(reg, osa, run_exec):
    # Focus is open-if-needed + activate; `open -a` brings a running app
    # forward, so the resolved name goes straight through it.
    res = await reg.get("app.focus").run({"name": "Safari"})
    assert res.ok and res.summary == "focused Safari"
    assert run_exec.calls == [["open", "-a", "Safari"]]


@pytest.mark.asyncio
async def test_app_quit_script_and_undo_reopens(reg, osa, run_exec):
    res = await reg.get("app.quit").run({"name": "Safari"})
    assert res.ok and res.summary == "quit Safari"
    assert osa.scripts == ['tell application "Safari" to quit']
    undo_res = await res.undo()
    assert undo_res.ok and undo_res.summary == "opened Safari"
    assert run_exec.calls == [["open", "-a", "Safari"]]


@pytest.mark.asyncio
async def test_app_open_resolves_misheard_name(reg, osa, run_exec):
    # The live gap: the model says "Arc Browser"; the bundle is "Arc".
    res = await reg.get("app.open").run({"name": "Arc Browser"})
    assert res.ok and res.summary == "opened Arc"
    assert run_exec.calls == [["open", "-a", "Arc"]]


@pytest.mark.asyncio
async def test_app_open_resolves_subword(reg, osa, run_exec):
    res = await reg.get("app.open").run({"name": "chrome"})
    assert res.ok and res.summary == "opened Google Chrome"
    assert run_exec.calls == [["open", "-a", "Google Chrome"]]


@pytest.mark.asyncio
async def test_app_open_prefers_plainest_when_close(reg, osa, run_exec):
    # "chrome" is a subword of both Chrome and Chrome Canary; the plainest
    # (shortest) name is the one meant — resolve, don't nag.
    res = await reg.get("app.open").run({"name": "chrome"})
    assert res.ok and res.summary == "opened Google Chrome"
    assert run_exec.calls == [["open", "-a", "Google Chrome"]]


@pytest.mark.asyncio
async def test_app_open_subword_resolves_canary(reg, osa, run_exec):
    # "canary" is a full spoken word inside exactly one app name → resolve.
    res = await reg.get("app.open").run({"name": "canary"})
    assert res.ok and res.summary == "opened Google Chrome Canary"


@pytest.mark.asyncio
async def test_app_open_weak_match_asks(reg, osa, run_exec):
    # "chrome browser" shares only "chrome" with two apps and isn't a
    # prefix of either — a weak overlap, so surface both and ask.
    res = await reg.get("app.open").run({"name": "chrome browser"})
    assert not res.ok
    assert "Google Chrome" in res.data["suggestions"]
    assert run_exec.calls == []  # never launched anything


@pytest.mark.asyncio
async def test_app_open_ambiguous_tie_asks(reg, monkeypatch, osa, run_exec):
    # A genuine tie — two equally short top matches — must ask, not guess.
    from bridge.voice.adapters import system as sysmod

    async def two_ties():
        return {"notes": "Notes", "nomad": "Nomad"}

    monkeypatch.setattr(sysmod, "_installed_apps", two_ties)
    res = await reg.get("app.open").run({"name": "no"})
    assert not res.ok
    assert set(res.data["suggestions"]) == {"Notes", "Nomad"}
    assert run_exec.calls == []


@pytest.mark.asyncio
async def test_app_open_unknown_fails_cleanly(reg, osa, run_exec):
    # No resolver opinion + launch fails both ways → clean failure.
    run_exec.result = (False, "not found")
    osa.replies = [(False, "no such app")]
    res = await reg.get("app.open").run({"name": "zzqwidget"})
    assert not res.ok
    assert res.summary == "couldn't open zzqwidget"


@pytest.mark.asyncio
async def test_app_quit_resolves_before_quitting(reg, osa, run_exec):
    res = await reg.get("app.quit").run({"name": "arc browser"})
    assert res.ok and res.summary == "quit Arc"
    assert osa.scripts == ['tell application "Arc" to quit']
    undo_res = await res.undo()
    assert undo_res.ok and undo_res.summary == "opened Arc"


@pytest.mark.asyncio
async def test_app_frontmost(reg, osa):
    osa.replies = [(True, "Safari")]
    res = await reg.get("app.frontmost").run({})
    assert res.ok and res.summary == "frontmost: Safari"
    assert res.data == {"app": "Safari"}
    assert osa.scripts == [
        'tell application "System Events" to get name of first process whose frontmost is true'
    ]


@pytest.mark.asyncio
async def test_app_verbs_require_name(reg, osa, run_exec):
    for verb in ("app.open", "app.focus", "app.quit"):
        res = await reg.get(verb).run({})
        assert not res.ok, verb
    assert osa.scripts == [] and run_exec.calls == []


@pytest.mark.asyncio
async def test_app_name_is_applescript_quoted(reg, osa):
    res = await reg.get("app.focus").run({"name": 'My "Weird" App'})
    assert res.ok
    assert osa.scripts == ['tell application "My \\"Weird\\" App" to activate']


# ── timers ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_timer_fires_emits_and_notifies(reg, osa, timer_events):
    res = await reg.get("timer.set").run({"seconds": 0.05, "label": "tea"})
    assert res.ok and res.summary.startswith("⏱ tea")
    await asyncio.sleep(0.15)
    assert timer_events == [{"type": "voice_timer_fired", "label": "tea", "seconds": 0.05}]
    note = osa.scripts[-1]
    assert note.startswith("display notification ")
    assert '"tea — done"' in note
    assert 'with title "Freyja" sound name "Glass"' in note


@pytest.mark.asyncio
async def test_timer_list_and_labelled_cancel(reg, osa, timer_events):
    await reg.get("timer.set").run({"seconds": 5, "label": "tea"})
    res = await reg.get("timer.list").run({})
    assert res.ok and "⏱ tea" in res.summary
    assert len(res.data["timers"]) == 1
    assert 0 < res.data["timers"][0]["remaining_sec"] <= 5

    res = await reg.get("timer.cancel").run({"label": "tea"})
    assert res.ok and res.summary == "cancelled ⏱ tea"
    await asyncio.sleep(0.05)
    assert timer_events == []  # cancelled task never fires

    res = await reg.get("timer.list").run({})
    assert res.summary == "no timers running"


@pytest.mark.asyncio
async def test_timer_cancel_without_label_picks_most_recent(reg, osa, timer_events):
    await reg.get("timer.set").run({"seconds": 5, "label": "first"})
    await reg.get("timer.set").run({"seconds": 5, "label": "second"})
    res = await reg.get("timer.cancel").run({})
    assert res.ok and res.summary == "cancelled ⏱ second"
    res = await reg.get("timer.list").run({})
    assert [t["label"] for t in res.data["timers"]] == ["first"]


@pytest.mark.asyncio
async def test_timer_undo_cancels(reg, osa, timer_events):
    res = await reg.get("timer.set").run({"seconds": 0.05, "label": "tea"})
    undo_res = await res.undo()
    assert undo_res.ok and undo_res.summary == "cancelled ⏱ tea"
    await asyncio.sleep(0.15)
    assert timer_events == []


@pytest.mark.asyncio
async def test_timer_minutes_and_default_label(reg, osa, timer_events):
    res = await reg.get("timer.set").run({"minutes": 1})
    assert res.ok
    assert res.data == {"label": "1m timer", "seconds": 60.0}
    assert res.summary == "⏱ 1m timer — 1m"


@pytest.mark.asyncio
async def test_timer_duplicate_labels_deduped(reg, osa, timer_events):
    await reg.get("timer.set").run({"seconds": 5, "label": "tea"})
    res = await reg.get("timer.set").run({"seconds": 5, "label": "tea"})
    assert res.data["label"] == "tea 2"


@pytest.mark.asyncio
async def test_timer_requires_duration_and_cancel_missing(reg, osa, timer_events):
    res = await reg.get("timer.set").run({})
    assert not res.ok
    res = await reg.get("timer.cancel").run({"label": "ghost"})
    assert not res.ok


# ── live smoke (opt-in) ──────────────────────────────────────────────────


@pytest.mark.skipif(
    os.environ.get("FREYJA_VOICE_LIVE") != "1",
    reason="touches the real Mac; set FREYJA_VOICE_LIVE=1 to run",
)
@pytest.mark.asyncio
async def test_live_now_playing_smoke():
    reg = build_default_registry()
    res = await reg.get("spotify.now_playing").run({})
    # Either a real track line or a clean "nothing playing" — never a crash.
    assert isinstance(res.summary, str) and res.summary
