import sys

import pytest

# Paths under ~ that these modules compute once, when first imported. Most are
# imported at collection, before any test sets HOME, so they keep the real
# home: each is moved under the test's home here. A module first imported
# inside a test reads the test's HOME by itself.
_IMPORT_TIME_HOME_PATHS = {
    "bridge.freyja_bridge": {
        "_DEBUG_LOG_PATH": ".freyja/bridge-events.jsonl",
        "_DEBUG_LOG_PREV_PATH": ".freyja/bridge-events.prev.jsonl",
    },
    "bridge.transcript_persistence": {"SESSIONS_DIR": ".freyja/sessions"},
    "bridge.kanban_journal": {"SESSIONS_DIR": ".freyja/sessions"},
    "bridge.task_journal": {"SESSIONS_DIR": ".freyja/sessions"},
    "bridge.compaction_telemetry": {
        "TELEMETRY_DIR": ".freyja/telemetry",
        "TELEMETRY_FILE": ".freyja/telemetry/compaction.jsonl",
        "_ROTATED": ".freyja/telemetry/compaction.jsonl.old",
    },
    "bridge.tools.computer_tools": {"_FRAME_DUMP_DIR": ".freyja/last-frames"},
    "bridge.voice.adapters.computer": {"_FRAMES_DIR": ".freyja/voice/frames"},
    "bridge.tools.jev_operator.loop": {"RUN_DIR": ".freyja/jev-operator/runs"},
    "bridge.plugins.loader": {
        "DEFAULT_PLUGINS_ROOT": ".freyja/plugins",
        "DEFAULT_MCP_PATH": ".freyja/mcp.json",
    },
    "bridge.tools.image_generation_tool": {"DEFAULT_IMAGE_OUTPUT_DIR": "Pictures/Freyja"},
    "bridge.tools.sound_generation_tool": {"DEFAULT_AUDIO_DIR": "Music/Freyja"},
}


@pytest.fixture(autouse=True)
def _isolated_freyja_home(tmp_path_factory, monkeypatch):
    # Freyja persists operator state (sessions, telemetry, the bridge's event
    # log, MCP OAuth tokens, scheduler data) under FREYJA_HOME or ~/.freyja.
    # Without this guard a test that forgets to redirect a path writes into
    # the real ~/.freyja, which happened with an OAuth client registration
    # and, on every run, with bridge-events.jsonl. Both are set for every
    # test, even when FREYJA_HOME is already set: the gateway and the
    # scheduler daemon export the real one to the commands their agents run.
    # Subprocesses the tests spawn inherit both.
    home = tmp_path_factory.mktemp("home")
    (home / ".freyja").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FREYJA_HOME", str(home / ".freyja"))
    for name, paths in _IMPORT_TIME_HOME_PATHS.items():
        module = sys.modules.get(name)
        if module is None:
            continue
        for attr, relative in paths.items():
            path = home / relative
            if isinstance(getattr(module, attr), str):
                path = str(path)
            monkeypatch.setattr(module, attr, path)
    # launchd has one job registry per macOS user, so a test that installs
    # a LaunchAgent replaces the real Freyja job (bridge/launchd_guard.py).
    monkeypatch.setenv("FREYJA_NO_LAUNCHD", "1")
    # emit() asks the state a process sets as it starts. A test that starts
    # a gateway sets it, and later tests would ask that one.
    bridge = sys.modules.get("bridge.freyja_bridge")
    if bridge is not None:
        monkeypatch.setattr(bridge, "_EVENT_LOG_STATE", None)
    yield
