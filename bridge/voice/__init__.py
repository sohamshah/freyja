"""Galdr — Freyja's voice agent, bridge side.

The renderer owns audio (WebRTC ⇄ OpenAI Realtime); this package owns
everything else: secret minting, verb dispatch, tiers + confirm tokens,
receipts + undo, the typed-command floor, and panic detection. See
docs/GALDR-BUILD.md for the interface contract.
"""

from bridge.voice.service import VoiceService

__all__ = ["VoiceService"]
