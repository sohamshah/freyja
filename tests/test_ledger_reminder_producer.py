"""Validate the bridge's write-ledger reminder + forgetting-detector logic.

These are _BridgeSession methods; rather than build a whole bridge we bind the
methods to a lightweight fake holding the same attributes they read.
"""

from __future__ import annotations

from types import SimpleNamespace

from bridge.freyja_bridge import _BridgeSession
from bridge.session_ledger import SessionLedger


def _fake(tmp_path):
    led = SessionLedger(session_id="s1", project_dir=tmp_path)
    led.ensure()
    fake = SimpleNamespace(
        id="s1",
        session_ledger=led,
        session=SimpleNamespace(transcript=SimpleNamespace(entries=[]), compaction_count=0),
        project_output_dir=tmp_path,
        _ledger_last_digest=None,
        _ledger_turns_since_emit=0,
        _ledger_seen_compactions=0,
        _forgetting_flag_index=-1,
        _tool_call_index=0,
        _LEDGER_REMINDER_FLOOR=4,
        forgetting_calls=[],
    )
    # Bind the methods under test + their helpers.
    fake._build_write_ledger_reminder = _BridgeSession._build_write_ledger_reminder.__get__(fake)
    fake._build_forgetting_correction = _BridgeSession._build_forgetting_correction.__get__(fake)
    fake._session_memory_present = _BridgeSession._session_memory_present.__get__(fake)
    fake._ledger_ground_truth = _BridgeSession._ledger_ground_truth.__get__(fake)
    fake._last_assistant_text = _BridgeSession._last_assistant_text  # staticmethod
    fake._emit_forgetting_telemetry = lambda n: fake.forgetting_calls.append(n)
    return fake, led


def _add_write(led, path, lines):
    led.record_from_tool(
        tool_name="write_file", tool_args={"path": path},
        result_text=f"Created file: {path}\nWrote 1 characters ({lines} lines)",
        result_chars=40, is_error=False, tool_call_id="t",
    )


def test_reminder_none_when_empty(tmp_path):
    fake, _ = _fake(tmp_path)
    assert fake._build_write_ledger_reminder() is None


def test_reminder_emits_then_debounces(tmp_path):
    fake, led = _fake(tmp_path)
    _add_write(led, "/repo/widget_tools.py", 680)
    first = fake._build_write_ledger_reminder()
    assert first is not None
    assert "widget_tools.py (680 lines)" in first
    # Same digest, within floor → suppressed on the next few turns.
    assert fake._build_write_ledger_reminder() is None
    assert fake._build_write_ledger_reminder() is None


def test_reminder_reemits_after_floor(tmp_path):
    fake, led = _fake(tmp_path)
    _add_write(led, "/repo/a.py", 3)
    assert fake._build_write_ledger_reminder() is not None
    # Floor is 4: after enough suppressed turns it re-emits even if unchanged.
    outs = [fake._build_write_ledger_reminder() for _ in range(5)]
    assert any(o is not None for o in outs)


def test_reminder_changes_on_new_effect(tmp_path):
    fake, led = _fake(tmp_path)
    _add_write(led, "/repo/a.py", 3)
    assert fake._build_write_ledger_reminder() is not None
    _add_write(led, "/repo/b.py", 9)  # digest changes
    out = fake._build_write_ledger_reminder()
    assert out is not None and "b.py" in out


def test_just_compacted_framing(tmp_path):
    fake, led = _fake(tmp_path)
    _add_write(led, "/repo/a.py", 3)
    fake._build_write_ledger_reminder()  # initial emit
    fake.session.compaction_count = 1   # a compaction just fired
    out = fake._build_write_ledger_reminder()
    assert out is not None
    assert "just compacted" in out.lower()


def test_forgetting_correction_fires_once(tmp_path):
    fake, led = _fake(tmp_path)
    _add_write(led, "/repo/widget_tools.py", 680)
    # Last assistant message disowns the work.
    fake.session.transcript.entries = [
        SimpleNamespace(message=SimpleNamespace(
            role="assistant",
            content="I have no recollection of making changes; everything was read-only.",
        )),
    ]
    out = fake._build_forgetting_correction()
    assert out is not None
    assert "ledger records" in out
    assert fake.forgetting_calls == [1]
    # Same tool-call index → flagged only once.
    assert fake._build_forgetting_correction() is None


def test_no_correction_without_negative_claim(tmp_path):
    fake, led = _fake(tmp_path)
    _add_write(led, "/repo/a.py", 3)
    fake.session.transcript.entries = [
        SimpleNamespace(message=SimpleNamespace(
            role="assistant", content="I created a.py and will continue.",
        )),
    ]
    assert fake._build_forgetting_correction() is None


def test_no_correction_when_ledger_empty(tmp_path):
    fake, led = _fake(tmp_path)
    fake.session.transcript.entries = [
        SimpleNamespace(message=SimpleNamespace(
            role="assistant", content="No changes were made.",
        )),
    ]
    assert fake._build_forgetting_correction() is None


def test_ledger_ground_truth_render(tmp_path):
    fake, led = _fake(tmp_path)
    _add_write(led, "/repo/widget_tools.py", 680)
    led.record_pinned_fact("backend is DONE")
    gt = fake._ledger_ground_truth()
    assert "widget_tools.py (680 lines)" in gt
    assert "(pinned) backend is DONE" in gt
