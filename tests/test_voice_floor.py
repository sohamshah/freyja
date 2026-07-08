"""Floor grammar tests (bridge/voice/floor.py).

The contract §5 table IS the spec — every row appears below, plus the
documented edge choices: whole-utterance matching ("play vienna" and
"stop the presses" are None), name-prefix transparency ("freyja pause"),
and the panic-scan extension (utterance *ending* in "freyja stop").
"""

from __future__ import annotations

import pytest

from bridge.voice.floor import FloorIntent, parse, scan_for_panic

# ── panic row ─────────────────────────────────────────────────────────────

PANIC_UTTERANCES = [
    "stop",
    "Stop!",
    "STOP.",
    "freyja stop",
    "Freyja, stop",
    "hey freyja stop",
    "cancel",
    "cancel that",
    "shut up",
    "never mind",
    "Never mind.",
    "nevermind",
    "stop it",
]


@pytest.mark.parametrize("utterance", PANIC_UTTERANCES)
def test_parse_panic(utterance):
    intent = parse(utterance)
    assert intent is not None, utterance
    assert intent.panic is True
    assert intent.verb == "__panic__"
    assert intent.args == {}


# ── verb rows (every contract row + synonym/punctuation variants) ─────────

TABLE = [
    # spotify.pause
    ("pause", "spotify.pause", {}),
    ("Pause.", "spotify.pause", {}),
    ("pause the music", "spotify.pause", {}),
    ("freyja pause", "spotify.pause", {}),
    # spotify.resume — bare "play" only
    ("resume", "spotify.resume", {}),
    ("play", "spotify.resume", {}),
    ("Play", "spotify.resume", {}),
    ("unpause", "spotify.resume", {}),
    # spotify.next
    ("next", "spotify.next", {}),
    ("skip", "spotify.next", {}),
    ("next track", "spotify.next", {}),
    ("next song", "spotify.next", {}),
    ("skip this song", "spotify.next", {}),
    # spotify.previous
    ("previous", "spotify.previous", {}),
    ("go back a track", "spotify.previous", {}),
    ("previous song", "spotify.previous", {}),
    # system.volume relative
    ("louder", "system.volume", {"delta": 10}),
    ("turn it up", "system.volume", {"delta": 10}),
    ("volume up", "system.volume", {"delta": 10}),
    ("quieter", "system.volume", {"delta": -10}),
    ("turn it down", "system.volume", {"delta": -10}),
    ("turn down the volume", "system.volume", {"delta": -10}),
    # system.volume mute
    ("mute", "system.volume", {"mute": True}),
    ("unmute", "system.volume", {"mute": False}),
    # system.volume absolute — "volume (to) 40 (percent)"
    ("volume 40", "system.volume", {"level": 40}),
    ("volume to 40", "system.volume", {"level": 40}),
    ("volume to 40 percent", "system.volume", {"level": 40}),
    ("Volume to 40%", "system.volume", {"level": 40}),
    ("set the volume to 15", "system.volume", {"level": 15}),
    ("volume 0", "system.volume", {"level": 0}),
    ("volume to 100", "system.volume", {"level": 100}),
    # mis-heard overshoot clamps instead of erroring
    ("volume to 400", "system.volume", {"level": 100}),
    # spotify.now_playing
    ("what's playing", "spotify.now_playing", {}),
    ("whats playing", "spotify.now_playing", {}),
    ("What is playing?", "spotify.now_playing", {}),
    ("what song is this", "spotify.now_playing", {}),
]


@pytest.mark.parametrize("utterance,verb,args", TABLE)
def test_parse_table(utterance, verb, args):
    intent = parse(utterance)
    assert intent is not None, utterance
    assert intent.panic is False
    assert intent.verb == verb
    assert intent.args == args


# ── negatives — "anything else" is None, never an error ──────────────────

NEGATIVES = [
    # a query means the brain lane, not the floor
    "play vienna",
    "play some jazz",
    # longer sentence starting with a panic word is conversation, not panic
    "stop the presses",
    "cancel my meeting tomorrow",
    "never mind the weather, what's on my calendar",
    # near-misses
    "pause everything I'm doing",
    "skip to the good part",
    "volume to eleven",  # no digits, no parse
    "volume",  # no level, no direction
    "what's happening",
    "shut down the computer",
    # junk
    "",
    "   ",
    "?!",
]


@pytest.mark.parametrize("utterance", NEGATIVES)
def test_parse_negatives(utterance):
    assert parse(utterance) is None, utterance


def test_floor_intent_shape():
    intent = parse("louder")
    assert isinstance(intent, FloorIntent)
    # args must be a fresh dict per parse — callers mutate them
    intent2 = parse("louder")
    assert intent.args is not intent2.args


# ── scan_for_panic — live transcript scanning (finals AND partials) ──────


@pytest.mark.parametrize(
    "text,expected",
    [
        ("stop", "stop"),
        ("Stop!", "stop"),
        ("freyja stop", "stop"),
        ("shut up", "shut up"),
        ("never mind", "never mind"),
        # name-addressed panic fires even mid-utterance (trailing)
        ("no no freyja stop", "stop"),
        ("okay okay freyja cancel", "cancel"),
    ],
)
def test_scan_for_panic_matches(text, expected):
    assert scan_for_panic(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        # sentence-starters are commands to the model, not panic
        "stop the presses",
        "cancel my meeting tomorrow",
        "can you stop by the store reminder",
        # partial that hasn't reached a panic word yet
        "sto",
        "please",
        "",
        # panic word buried mid-sentence without the name
        "I never mind waiting",
    ],
)
def test_scan_for_panic_negatives(text):
    assert scan_for_panic(text) is None
