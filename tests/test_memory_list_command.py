"""`list_memories` answers with every memory on disk, archived ones too.

The renderer heard only of memories a turn retrieved or an edit changed,
so a memory archived in an earlier run never reached the sidebar and
could not be restored from the app.
"""

from __future__ import annotations

from bridge import freyja_bridge as fb
from bridge.knowledge import MemoryStore


async def test_list_memories_includes_archived_from_an_earlier_run(monkeypatch, tmp_path):
    events: list[dict] = []
    monkeypatch.setattr(fb, "emit", events.append)
    # An earlier run: two memories saved, one archived.
    earlier = MemoryStore(tmp_path)
    kept = earlier.record_preference("Prefer short answers.", "style")
    gone = earlier.record_preference("Use imperial units.", "style")
    earlier.delete_item(gone.id, actor="user")

    # A fresh bridge holds no session yet.
    state = fb._BridgeState(workspace=str(tmp_path), default_model="claude-sonnet-5-5")
    await fb._handle_command(state, {"type": "list_memories"})

    [snapshot] = [e for e in events if e["type"] == "memories_snapshot"]
    assert {m["id"]: m["archived"] for m in snapshot["memories"]} == {
        kept.id: False,
        gone.id: True,
    }
    # No session id: it would copy the whole list into that session's
    # events file.
    assert "sessionId" not in snapshot
