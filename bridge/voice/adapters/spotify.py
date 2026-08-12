"""Spotify verbs (contract §4).

Transport verbs drive the desktop app over AppleScript — `tell
application "Spotify"` auto-launches it, so there is no separate
"is it running" probe. The AppleScript surface can only play explicit
URIs, so `spotify.play` with a free-text query resolves the track via
the Web API client-credentials flow first (search needs no user OAuth)
when SPOTIFY_CLIENT_ID/SECRET are in the environment. Without creds a
query returns ok=False with data.setup="spotify_search" so the model
can explain what to configure.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

import httpx

from bridge.voice.adapters import mac
from bridge.voice.adapters.mac import as_quoted
from bridge.voice.verbs import Verb, VerbRegistry, VerbResult

_TOKEN_URL = "https://accounts.spotify.com/api/token"
_SEARCH_URL = "https://api.spotify.com/v1/search"

# Client-credentials token cache — module-level because tokens are app-scoped
# (~1 h TTL), not session-scoped. Refreshed 60 s before expiry.
_token_cache: dict[str, Any] = {"token": None, "expires": 0.0}


def _creds() -> Optional[tuple[str, str]]:
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
    if client_id and secret:
        return client_id, secret
    return None


async def _get_token(client_id: str, secret: str) -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires"] - 60:
        return _token_cache["token"]
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            _TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(client_id, secret),
        )
        resp.raise_for_status()
        body = resp.json()
    _token_cache["token"] = body["access_token"]
    _token_cache["expires"] = now + float(body.get("expires_in", 3600))
    return _token_cache["token"]


async def _search_track(query: str) -> tuple[Optional[dict[str, str]], Optional[str]]:
    """Resolve a query to the best track hit.

    Returns ({uri, name, artist}, None) on a hit, (None, None) on an
    empty result, (None, error) on any transport failure — a flaky
    network must degrade to a spoken apology, never crash the service.
    """
    creds = _creds()
    if creds is None:
        return None, "Spotify API creds not set"
    try:
        token = await _get_token(*creds)
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                _SEARCH_URL,
                params={"q": query, "type": "track", "limit": 3},
                headers={"Authorization": f"Bearer {token}"},
            )
            resp.raise_for_status()
            items = resp.json().get("tracks", {}).get("items", [])
    except Exception as exc:
        return None, f"Spotify search failed: {exc}"
    if not items:
        return None, None
    hit = items[0]
    artists = hit.get("artists") or []
    return {
        "uri": str(hit.get("uri", "")),
        "name": str(hit.get("name", "")),
        "artist": str(artists[0].get("name", "")) if artists else "",
    }, None


async def _play_uri(uri: str) -> tuple[bool, str]:
    return await mac.run_osascript(f'tell application "Spotify" to play track {as_quoted(uri)}')


async def _play(args: dict[str, Any]) -> VerbResult:
    uri = str(args.get("uri") or "")
    track = str(args.get("track") or "")
    artist = str(args.get("artist") or "")
    query = str(args.get("query") or "") or " ".join(p for p in (track, artist) if p)

    if uri:
        ok, out = await _play_uri(uri)
        if not ok:
            return VerbResult(ok=False, summary="couldn't play that", error=out)
        summary = f"▶ {track} — {artist}" if track and artist else "▶ playing"
        return VerbResult(ok=True, summary=summary, data={"uri": uri})

    if query:
        if _creds() is None:
            return VerbResult(
                ok=False,
                summary="can't search — Spotify API creds not set",
                data={"setup": "spotify_search"},
            )
        hit, err = await _search_track(query)
        if err:
            return VerbResult(ok=False, summary=err, error=err)
        if hit is None:
            return VerbResult(ok=False, summary=f'no Spotify results for "{query}"')
        ok, out = await _play_uri(hit["uri"])
        if not ok:
            return VerbResult(ok=False, summary="couldn't play that", error=out)
        return VerbResult(ok=True, summary=f"▶ {hit['name']} — {hit['artist']}", data=dict(hit))

    # No uri, no query: bare "play" means resume whatever is queued.
    ok, out = await mac.run_osascript('tell application "Spotify" to play')
    if not ok:
        return VerbResult(ok=False, summary="couldn't resume", error=out)
    return VerbResult(ok=True, summary="▶ resumed")


# The trailing bare expression is the script result osascript prints.
_NOW_PLAYING_LINES = [
    'tell application "Spotify"',
    "set t to current track",
    "(name of t) & linefeed & (artist of t) & linefeed & (album of t) & linefeed"
    " & ((player position as integer) as string) & linefeed & (player state as string)",
    "end tell",
]


async def _now_playing(args: dict[str, Any]) -> VerbResult:
    ok, out = await mac.run_osascript_lines(_NOW_PLAYING_LINES)
    if not ok:
        # `current track` errors when nothing is queued — that IS the answer.
        return VerbResult(ok=False, summary="nothing playing", error=out)
    parts = out.split("\n")
    if len(parts) < 5:
        return VerbResult(ok=False, summary="nothing playing", error=out)
    name, artist, album, pos_raw, state = (p.strip() for p in parts[:5])
    try:
        pos = int(pos_raw)
    except ValueError:
        pos = 0
    glyph = "▶" if state == "playing" else "⏸"
    summary = f"{glyph} {name} — {artist} · {album} @ {pos // 60}:{pos % 60:02d}"
    return VerbResult(
        ok=True,
        summary=summary,
        data={"track": name, "artist": artist, "album": album, "position_sec": pos, "state": state},
    )


def _transport(script: str, summary: str, fail_summary: str) -> Any:
    # On failure the human summary stays terse (it lands in the HUD row
    # and the spoken outcome); the raw osascript stderr — AppleScript
    # permission/not-installed prose — rides in error= for the model,
    # matching the _play/_volume pattern.
    async def run(args: dict[str, Any]) -> VerbResult:
        ok, out = await mac.run_osascript(script)
        if not ok:
            return VerbResult(ok=False, summary=fail_summary, error=out)
        return VerbResult(ok=True, summary=summary)

    return run


def register(registry: VerbRegistry) -> None:
    registry.register(
        Verb(
            name="spotify.play",
            description=(
                "Play music: a track by query/track+artist (Web API search), "
                "an explicit spotify:track: uri, or resume when no args"
            ),
            params={
                "query": {"type": "string", "description": "free-text search"},
                "track": {"type": "string"},
                "artist": {"type": "string"},
                "uri": {"type": "string", "description": "spotify:track:… plays directly"},
            },
            required=[],
            tier="auto",
            run=_play,
        )
    )
    registry.register(
        Verb(
            name="spotify.pause",
            description="Pause Spotify playback",
            params={},
            required=[],
            tier="auto",
            run=_transport('tell application "Spotify" to pause', "⏸ paused", "couldn't pause"),
        )
    )
    registry.register(
        Verb(
            name="spotify.resume",
            description="Resume Spotify playback",
            params={},
            required=[],
            tier="auto",
            run=_transport('tell application "Spotify" to play', "▶ resumed", "couldn't resume"),
        )
    )
    registry.register(
        Verb(
            name="spotify.next",
            description="Skip to the next track",
            params={},
            required=[],
            tier="auto",
            run=_transport(
                'tell application "Spotify" to next track', "⏭ next track", "couldn't skip"
            ),
        )
    )
    registry.register(
        Verb(
            name="spotify.previous",
            description="Go back to the previous track",
            params={},
            required=[],
            tier="auto",
            run=_transport(
                'tell application "Spotify" to previous track',
                "⏮ previous track",
                "couldn't go back",
            ),
        )
    )
    registry.register(
        Verb(
            name="spotify.now_playing",
            description="What's playing now: track, artist, album, position",
            params={},
            required=[],
            tier="auto",
            run=_now_playing,
        )
    )
