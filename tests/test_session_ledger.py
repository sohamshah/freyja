"""Unit tests for the session action ledger (bridge/session_ledger.py)."""

from __future__ import annotations

import json

from bridge.session_ledger import (
    SessionLedger,
    classify_bash_command,
    classify_tool,
    detect_negative_self_claim,
    git_status_delta,
    render_ledger_reminder,
)

# ── classification ────────────────────────────────────────────────────────

def test_classify_tool_effect_vs_observation():
    assert classify_tool("write_file") == "effect"
    assert classify_tool("edit_file") == "effect"
    assert classify_tool("edit_json") == "effect"
    assert classify_tool("generate_image") == "effect"
    assert classify_tool("read_file") == "observation"
    assert classify_tool("grep") == "observation"
    assert classify_tool("web_search") == "observation"
    # bash is decided by command; classify_tool defers it.
    assert classify_tool("bash") == "neither"
    assert classify_tool("some_random_tool") == "neither"


def test_classify_bash_command_effects():
    for cmd in [
        "git commit -m 'x'",
        "git checkout -b feat/widget-tools",
        "gh pr create --fill",
        "echo hi > out.txt",
        "cat a b >> log.txt",
        "mkdir -p foo/bar",
        "mv a.py b.py",
        "rm -rf build",
        "npm install",
        "uv pip install pymupdf",
        "sed -i 's/a/b/' f.py",
        "alembic upgrade head",
    ]:
        assert classify_bash_command(cmd) == "effect", cmd


def test_classify_bash_command_observations():
    for cmd in [
        "git status",
        "git diff HEAD",
        "ls -la",
        "cat file.py",
        "grep -rn foo .",
        "echo hello",            # no redirect
        "python script.py 2>&1",  # stderr redirect only, not a file
        "rg pattern",
        "",
    ]:
        assert classify_bash_command(cmd) == "observation", cmd


# ── negative self-claim detection ─────────────────────────────────────────

def test_detect_negative_self_claim_positive():
    for txt in [
        "I have no recollection of making changes to agent-harness this session.",
        "Everything in our conversation so far has been read-only exploration.",
        "No changes were made during this session.",
        "I haven't made any edits.",
        "nothing was written or created",
    ]:
        assert detect_negative_self_claim(txt) is True, txt


def test_detect_negative_self_claim_negative():
    for txt in [
        "I created widget_tools.py with the show_widget tool.",
        "Let me make some changes to the file.",
        "I'll explore the codebase first, then implement.",
        "I edited three files and opened a PR.",
        "",
    ]:
        assert detect_negative_self_claim(txt) is False, txt


# ── ledger recording + dedup ──────────────────────────────────────────────

def _mk(tmp_path):
    led = SessionLedger(session_id="s1", project_dir=tmp_path)
    led.ensure()
    return led


def test_record_file_effect_parses_lines(tmp_path):
    led = _mk(tmp_path)
    row = led.record_from_tool(
        tool_name="write_file",
        tool_args={"path": "/repo/agent_harness/cli/tools/widget_tools.py"},
        result_text="Created file: /repo/.../widget_tools.py\nWrote 24000 characters (680 lines)",
        result_chars=70,
        is_error=False,
        tool_call_id="t1",
    )
    assert row is not None
    assert row["class"] == "effect"
    assert row["operation"] == "create"
    assert "widget_tools.py" in row["summary"]
    assert "680 lines" in row["summary"]


def test_error_results_not_recorded(tmp_path):
    led = _mk(tmp_path)
    row = led.record_from_tool(
        tool_name="write_file",
        tool_args={"path": "/repo/x.py"},
        result_text="Error writing file: permission denied",
        result_chars=40,
        is_error=True,
        tool_call_id="t1",
    )
    assert row is None
    assert led.has_effects() is False


def test_observations_not_in_effects(tmp_path):
    led = _mk(tmp_path)
    led.record_from_tool(
        tool_name="read_file", tool_args={"path": "/repo/a.py"},
        result_text="x" * 5000, result_chars=5000, is_error=False, tool_call_id="r1",
    )
    led.record_from_tool(
        tool_name="web_search", tool_args={"query": "mcp apps spec"},
        result_text="results", result_chars=900, is_error=False, tool_call_id="r2",
    )
    assert led.effects() == []
    assert led.has_effects() is False


def test_effects_dedup_keeps_created_verb(tmp_path):
    led = _mk(tmp_path)
    p = "/repo/foo.py"
    led.record_from_tool(
        tool_name="write_file", tool_args={"path": p},
        result_text="Created file: foo.py\nWrote 10 characters (3 lines)",
        result_chars=40, is_error=False, tool_call_id="t1",
    )
    led.record_from_tool(
        tool_name="edit_file", tool_args={"path": p},
        result_text="Edited file: foo.py\nReplaced lines 1-2 (5 lines)",
        result_chars=40, is_error=False, tool_call_id="t2",
    )
    effs = led.effects()
    assert len(effs) == 1  # collapsed to one row per path
    assert effs[0]["operation"] == "create"  # creation verb preserved


def test_bash_effect_vs_observation_recording(tmp_path):
    led = _mk(tmp_path)
    led.record_from_tool(
        tool_name="bash", tool_args={"command": "git commit -m 'widget tools'"},
        result_text="[feat/widget-tools abc] widget tools", result_chars=40,
        is_error=False, tool_call_id="b1",
    )
    led.record_from_tool(
        tool_name="bash", tool_args={"command": "git status"},
        result_text="On branch feat/widget-tools", result_chars=40,
        is_error=False, tool_call_id="b2",
    )
    effs = led.effects()
    assert len(effs) == 1
    assert effs[0]["kind"] == "shell_effect"
    assert led.shell_effect_count == 1


def test_pinned_facts(tmp_path):
    led = _mk(tmp_path)
    led.record_pinned_fact("agent-harness backend is DONE on branch feat/widget-tools")
    led.record_pinned_fact("agent-harness backend is DONE on branch feat/widget-tools")  # dup
    assert led.pinned_facts() == ["agent-harness backend is DONE on branch feat/widget-tools"]
    # pinned facts are not in effects()
    assert led.effects() == []


def test_digest_changes_on_new_effect(tmp_path):
    led = _mk(tmp_path)
    d0 = led.digest()
    led.record_from_tool(
        tool_name="write_file", tool_args={"path": "/repo/a.py"},
        result_text="Created file: a.py\nWrote 1 characters (1 lines)",
        result_chars=40, is_error=False, tool_call_id="t1",
    )
    d1 = led.digest()
    assert d0 != d1


def test_persistence_roundtrip(tmp_path):
    led = _mk(tmp_path)
    led.record_from_tool(
        tool_name="write_file", tool_args={"path": "/repo/a.py"},
        result_text="Created file: a.py\nWrote 1 characters (1 lines)",
        result_chars=40, is_error=False, tool_call_id="t1",
    )
    lines = (tmp_path / "action_ledger.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["class"] == "effect"
    assert row["sessionId"] == "s1"


# ── reminder rendering ────────────────────────────────────────────────────

def test_render_reminder_none_when_empty():
    assert render_ledger_reminder([], [], memory_present=False) is None


def test_render_reminder_lists_effects_no_shouting_caps():
    effects = [
        {"summary": "created widget_tools.py (680 lines)", "repo": "agent_harness/cli/tools"},
        {"summary": "edited runner_factory.py", "repo": "agent_harness/server"},
    ]
    block = render_ledger_reminder(effects, ["backend is DONE"], shell_note=True)
    assert block is not None
    assert "created widget_tools.py (680 lines)" in block
    assert "backend is DONE" in block
    assert "git status" in block
    assert "<system-reminder>" in block and "</system-reminder>" in block
    # Style guard: no gratuitous all-caps words like GROUND TRUTH / YOU.
    assert "GROUND TRUTH" not in block
    assert "YOU " not in block


def test_render_reminder_just_compacted_framing():
    effects = [{"summary": "created a.py (3 lines)"}]
    block = render_ledger_reminder(effects, [], just_compacted=True)
    assert "just compacted" in block.lower()


def test_render_reminder_caps_long_lists():
    effects = [{"summary": f"edited file_{i}.py"} for i in range(20)]
    block = render_ledger_reminder(effects, [], cap=12)
    assert "and 8 more" in block


# ── resume / hydration (C1) ────────────────────────────────────────────────

def test_ledger_hydrates_from_disk_on_resume(tmp_path):
    led1 = _mk(tmp_path)
    led1.record_from_tool(
        tool_name="write_file", tool_args={"path": "/repo/widget_tools.py"},
        result_text="Created file: widget_tools.py\nWrote 1 characters (680 lines)",
        result_chars=40, is_error=False, tool_call_id="t1",
    )
    led1.record_from_tool(
        tool_name="bash", tool_args={"command": "git commit -m x"},
        result_text="committed", result_chars=10, is_error=False, tool_call_id="b1",
    )
    # Simulate resume: a brand-new ledger over the same project dir must see
    # the prior run's effects (the forgetting incident happened on resume).
    led2 = SessionLedger(session_id="s1", project_dir=tmp_path)
    led2.ensure()
    effs = led2.effects()
    assert any("widget_tools.py" in (e.get("summary") or "") for e in effs)
    assert led2.has_effects() is True
    assert led2.shell_effect_count == 1


# ── creator attribution (H1) ───────────────────────────────────────────────

def test_effects_filtered_by_creator(tmp_path):
    led = _mk(tmp_path)  # ledger session_id = "s1"
    # Parent (s1) writes one file; a sub-agent (sub-7) writes another into the
    # same shared ledger.
    led.record_from_tool(
        tool_name="write_file", tool_args={"path": "/repo/parent.py"},
        result_text="Created file: parent.py\nWrote 1 characters (5 lines)",
        result_chars=40, is_error=False, tool_call_id="t1", creator_id="s1",
    )
    led.record_from_tool(
        tool_name="write_file", tool_args={"path": "/repo/child.py"},
        result_text="Created file: child.py\nWrote 1 characters (9 lines)",
        result_chars=40, is_error=False, tool_call_id="t2", creator_id="sub-7",
    )
    # The parent's reminder/seed filters to its own id — no sub-agent flooding.
    parent_effs = led.effects(creator_id="s1")
    assert len(parent_effs) == 1
    assert "parent.py" in parent_effs[0]["summary"]
    assert led.has_effects(creator_id="s1") is True
    # Unfiltered still sees everything (e.g. for a global view).
    assert len(led.effects()) == 2
    assert led.digest(creator_id="s1") != led.digest(creator_id="sub-7")


def test_detect_negative_claim_excludes_nothing_about():
    # M1: "nothing written about X" is documentation talk, not a self-claim.
    assert detect_negative_self_claim("There is nothing written about this in the docs.") is False
    # But the real self-claim still fires.
    assert detect_negative_self_claim("nothing was written or created") is True


# ── diff stats on file effects (chunk 2 / diff-aware artifact) ─────────────

def test_record_from_tool_lands_diff_stats_on_effect_row(tmp_path):
    led = _mk(tmp_path)
    row = led.record_from_tool(
        tool_name="edit_file",
        tool_args={"path": "/repo/widget_tools.py"},
        result_text="Edited file: widget_tools.py\nReplaced lines 1-2 (5 lines)",
        result_chars=40,
        is_error=False,
        tool_call_id="t1",
        extra={
            "additions": 12,
            "deletions": 3,
            "diff": "@@ -1,2 +1,2 @@\n-old\n+new\n",
            "diffTruncated": False,
        },
    )
    assert row is not None
    assert row["additions"] == 12
    assert row["deletions"] == 3
    assert "+new" in row["diff"]
    assert row["diffTruncated"] is False
    # Diff never leaks into the standing-reminder summary.
    assert "@@" not in row["summary"]


def test_record_from_tool_diff_stats_persist(tmp_path):
    led = _mk(tmp_path)
    led.record_from_tool(
        tool_name="write_file",
        tool_args={"path": "/repo/a.py"},
        result_text="Created file: a.py\nWrote 1 characters (9 lines)",
        result_chars=40,
        is_error=False,
        tool_call_id="t1",
        extra={"additions": 9, "deletions": 0, "diff": "+x", "diffTruncated": False},
    )
    row = json.loads((tmp_path / "action_ledger.jsonl").read_text().strip())
    assert row["additions"] == 9
    assert row["diff"] == "+x"


def test_record_from_tool_without_extra_is_unaffected(tmp_path):
    led = _mk(tmp_path)
    row = led.record_from_tool(
        tool_name="write_file",
        tool_args={"path": "/repo/a.py"},
        result_text="Created file: a.py\nWrote 1 characters (1 lines)",
        result_chars=40,
        is_error=False,
        tool_call_id="t1",
    )
    assert row is not None
    assert "additions" not in row
    assert "diff" not in row


def test_observation_ignores_diff_extra(tmp_path):
    # A web_search observation must not carry diff stats even if extra is passed.
    led = _mk(tmp_path)
    led.record_from_tool(
        tool_name="web_search",
        tool_args={"query": "mcp apps spec"},
        result_text="results",
        result_chars=900,
        is_error=False,
        tool_call_id="r1",
        extra={"additions": 5, "deletions": 5, "diff": "+x", "diffTruncated": False},
    )
    obs = [r for r in led._snapshot() if r.get("class") == "observation"]
    assert len(obs) == 1
    assert "additions" not in obs[0]
    assert "diff" not in obs[0]


# ── git-status-delta bash capture (C4) ─────────────────────────────────────

def test_git_status_delta_new_and_modified():
    before = " M existing.py\n"
    after = " M existing.py\n?? new.txt\n M another.py\n"
    delta = git_status_delta(before, after)
    paths = {d["path"]: d["op"] for d in delta}
    assert paths == {"new.txt": "created", "another.py": "modified"}


def test_git_status_delta_commit_clears_dirty_set():
    before = "M  staged_a.py\nA  staged_b.py\n"
    after = ""  # after `git commit`, the staged files are clean
    delta = git_status_delta(before, after)
    ops = {d["path"]: d["op"] for d in delta}
    assert ops == {"staged_a.py": "committed", "staged_b.py": "committed"}


def test_git_status_delta_empty_when_unchanged():
    s = " M a.py\n?? b.txt\n"
    assert git_status_delta(s, s) == []


def test_git_status_delta_rename():
    delta = git_status_delta("", 'R  old.py -> new.py\n')
    assert delta == [{"op": "renamed", "path": "new.py"}]


def test_record_shell_git_effect(tmp_path):
    led = _mk(tmp_path)
    row = led.record_shell_git_effect(
        command="git commit -m 'widget tools'",
        delta=[{"op": "committed", "path": "/repo/widget_tools.py"},
               {"op": "committed", "path": "/repo/__init__.py"}],
        repo="/repo", creator_id="s1", tool_call_id="b1",
    )
    assert row is not None
    assert row["kind"] == "shell_effect"
    assert "widget_tools.py" in row["summary"]
    assert "__init__.py" in row["summary"]
    assert row["gitDelta"][0]["path"] == "/repo/widget_tools.py"
    assert led.shell_effect_count == 1


def test_record_shell_git_effect_empty_delta_is_noop(tmp_path):
    led = _mk(tmp_path)
    assert led.record_shell_git_effect(command="git status", delta=[]) is None
    assert led.has_effects() is False


def test_git_capture_inflight_guard(tmp_path):
    led = _mk(tmp_path)
    assert led.begin_git_capture("/repo") is True   # first claim wins
    assert led.begin_git_capture("/repo") is False  # concurrent claim blocked
    assert led.begin_git_capture("/other") is True  # different repo ok
    led.end_git_capture("/repo")
    assert led.begin_git_capture("/repo") is True   # reclaimable after release


# ── manifest backfill (B3) ─────────────────────────────────────────────────

def test_backfill_from_manifest(tmp_path):
    # Simulate an old session: a manifest exists but no ledger file yet.
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text(
        json.dumps({"path": "/repo/widget_tools.py", "operation": "write",
                    "lines": 680, "creatorId": "s1", "createdAt": 1000}) + "\n"
        + json.dumps({"path": "/repo/foo.py", "operation": "edit",
                      "creatorId": "s1", "createdAt": 1001}) + "\n"
        + json.dumps({"path": "/x/legacy.md", "operation": "subagent_artifact",
                      "creatorId": "legacy", "createdAt": 999}) + "\n"
    )
    led = SessionLedger(session_id="s1", project_dir=tmp_path)
    led.ensure()  # hydrate (no ledger file) + backfill from manifest
    effs = led.effects(creator_id="s1")
    summaries = " ".join(e["summary"] for e in effs)
    assert "widget_tools.py (680 lines)" in summaries
    assert "foo.py" in summaries
    # subagent_artifact / legacy imports are skipped.
    assert "legacy.md" not in summaries


def test_backfill_does_not_duplicate_existing_ledger_rows(tmp_path):
    led = _mk(tmp_path)
    led.record_from_tool(
        tool_name="write_file", tool_args={"path": "/repo/a.py"},
        result_text="Created file: a.py\nWrote 1 characters (5 lines)",
        result_chars=40, is_error=False, tool_call_id="t1", creator_id="s1",
    )
    # A manifest row for the SAME path must not double-count after re-ensure.
    (tmp_path / "manifest.jsonl").write_text(
        json.dumps({"path": "/repo/a.py", "operation": "write",
                    "lines": 5, "creatorId": "s1", "createdAt": 1}) + "\n"
    )
    led2 = SessionLedger(session_id="s1", project_dir=tmp_path)
    led2.ensure()
    assert len([e for e in led2.effects() if e["path"] == "/repo/a.py"]) == 1


# ── pinned-fact creator filter (C6) ────────────────────────────────────────

def test_pinned_facts_creator_filter(tmp_path):
    led = _mk(tmp_path)
    led.record_pinned_fact("parent note", creator_id="s1")
    led.record_pinned_fact("child note", creator_id="sub-9")
    assert led.pinned_facts(creator_id="s1") == ["parent note"]
    assert led.pinned_facts(creator_id="sub-9") == ["child note"]
    assert len(led.pinned_facts()) == 2
