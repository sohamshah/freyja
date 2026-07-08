"""The floor grammar — deterministic voice commands that never touch a model.

``parse()`` maps a handful of transport/volume utterances (contract §5
table) straight to verbs, and recognizes the panic phrases that end a
session. It is intentionally an *exact-phrase* grammar, not NLU:

  · The whole normalized utterance must equal a known phrase. "play"
    resumes; "play vienna" is None (that's the model's job). "stop" is
    panic; "stop the presses" is None — a longer sentence means the
    operator is talking TO the assistant, not slamming the brakes.
  · Addressing by name is transparent: a leading "freyja" / "hey freyja" /
    "ok freyja" is stripped before matching, so "freyja stop" ≡ "stop"
    and "hey freyja pause" ≡ "pause".

``scan_for_panic()`` is the live-transcript variant used on user finals
AND partials. Same whole-utterance rule, plus one extension: an utterance
that *ends* with "freyja <panic-phrase>" fires ("no no freyja stop") —
name-addressed panic is unambiguous even mid-sentence. A bare partial
"stop" fires immediately by design: barge-in latency beats waiting for
the final, at the cost of a rare false stop when the utterance continues.
Common command-starters like "cancel my meeting…" do NOT fire because the
extra words break the whole-utterance match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class FloorIntent:
    verb: str
    args: dict[str, Any] = field(default_factory=dict)
    panic: bool = False


# Bare panic phrases (post name-strip). "freyja stop" from the contract
# table is covered by the name-strip + "stop".
_PANIC_PHRASES: tuple[str, ...] = (
    "stop",
    "stop it",
    "stop stop",
    "cancel",
    "cancel that",
    "shut up",
    "never mind",
    "nevermind",
)

# Whole-utterance phrase → (verb, args). Small synonym fans around each
# contract row; anything not here falls through to None (the brain lane).
_COMMANDS: dict[str, tuple[str, dict[str, Any]]] = {
    # spotify.pause
    "pause": ("spotify.pause", {}),
    "pause it": ("spotify.pause", {}),
    "pause that": ("spotify.pause", {}),
    "pause music": ("spotify.pause", {}),
    "pause the music": ("spotify.pause", {}),
    # spotify.resume — bare "play" only; "play <anything>" is brain-lane
    "play": ("spotify.resume", {}),
    "resume": ("spotify.resume", {}),
    "unpause": ("spotify.resume", {}),
    "resume music": ("spotify.resume", {}),
    "resume the music": ("spotify.resume", {}),
    # spotify.next
    "next": ("spotify.next", {}),
    "skip": ("spotify.next", {}),
    "skip it": ("spotify.next", {}),
    "skip this": ("spotify.next", {}),
    "next track": ("spotify.next", {}),
    "next song": ("spotify.next", {}),
    "skip track": ("spotify.next", {}),
    "skip song": ("spotify.next", {}),
    "skip this song": ("spotify.next", {}),
    # spotify.previous
    "previous": ("spotify.previous", {}),
    "previous track": ("spotify.previous", {}),
    "previous song": ("spotify.previous", {}),
    "go back a track": ("spotify.previous", {}),
    "go back a song": ("spotify.previous", {}),
    "back a track": ("spotify.previous", {}),
    "back a song": ("spotify.previous", {}),
    # system.volume — relative
    "louder": ("system.volume", {"delta": 10}),
    "turn it up": ("system.volume", {"delta": 10}),
    "volume up": ("system.volume", {"delta": 10}),
    "turn the volume up": ("system.volume", {"delta": 10}),
    "turn up the volume": ("system.volume", {"delta": 10}),
    "quieter": ("system.volume", {"delta": -10}),
    "softer": ("system.volume", {"delta": -10}),
    "turn it down": ("system.volume", {"delta": -10}),
    "volume down": ("system.volume", {"delta": -10}),
    "turn the volume down": ("system.volume", {"delta": -10}),
    "turn down the volume": ("system.volume", {"delta": -10}),
    # system.volume — mute
    "mute": ("system.volume", {"mute": True}),
    "mute it": ("system.volume", {"mute": True}),
    "unmute": ("system.volume", {"mute": False}),
    # spotify.now_playing
    "whats playing": ("spotify.now_playing", {}),
    "what is playing": ("spotify.now_playing", {}),
    "whats this song": ("spotify.now_playing", {}),
    "what song is this": ("spotify.now_playing", {}),
    "whats this track": ("spotify.now_playing", {}),
    "now playing": ("spotify.now_playing", {}),
}

# "volume (to) 40 (percent)" — absolute level. Clamped to 0–100 so a
# mis-heard "volume to 400" lands at max instead of erroring.
_VOLUME_RE = re.compile(r"^(?:set )?(?:the )?volume(?: to| at)? (\d{1,3})(?: percent)?$")

_NAME_PREFIXES: tuple[str, ...] = ("hey freyja ", "ok freyja ", "okay freyja ", "freyja ")

_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]+")
_WS_RE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Lowercase, drop apostrophes ("what's"→"whats"), map remaining
    punctuation to spaces, collapse whitespace."""
    t = (text or "").lower().replace("'", "").replace("’", "")
    t = _NON_ALNUM_RE.sub(" ", t)
    return _WS_RE.sub(" ", t).strip()


def _strip_name(norm: str) -> str:
    for prefix in _NAME_PREFIXES:
        if norm.startswith(prefix):
            return norm[len(prefix):]
    return norm


def parse(text: str) -> Optional[FloorIntent]:
    """Pure, deterministic, case/punctuation-insensitive floor parse.

    Returns a ``FloorIntent`` for the contract §5 table (panic first),
    None for everything else — None means "not floor business", never
    an error.
    """
    norm = _strip_name(_normalize(text))
    if not norm:
        return None
    if norm in _PANIC_PHRASES:
        return FloorIntent(verb="__panic__", args={}, panic=True)
    hit = _COMMANDS.get(norm)
    if hit is not None:
        verb, args = hit
        return FloorIntent(verb=verb, args=dict(args))
    m = _VOLUME_RE.match(norm)
    if m is not None:
        level = max(0, min(100, int(m.group(1))))
        return FloorIntent(verb="system.volume", args={"level": level})
    return None


def scan_for_panic(text: str) -> Optional[str]:
    """Panic scan for live transcripts (finals AND partials).

    Fires when the whole (name-stripped) utterance is a panic phrase, or
    when the utterance ends with "freyja <panic-phrase>". Returns the
    matched phrase, else None.
    """
    norm = _normalize(text)
    if not norm:
        return None
    stripped = _strip_name(norm)
    if stripped in _PANIC_PHRASES:
        return stripped
    for phrase in _PANIC_PHRASES:
        if norm.endswith(f"freyja {phrase}"):
            return phrase
    return None
