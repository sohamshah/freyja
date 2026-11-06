"""Unit tests for the session action ledger (bridge/session_ledger.py)."""

from __future__ import annotations

import json

import pytest

from bridge.session_ledger import (
    SessionLedger,
    classify_bash_command,
    classify_tool,
    detect_negative_self_claim,
    find_negative_self_claim,
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


def test_classify_bash_command_tells_writes_from_reads():
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
        # A real redirect into a file still counts, even next to a harmless one.
        "pytest -q > out.txt 2>&1",
        "grep -rn foo . 2>/dev/null > hits.txt",
        "date >> log 2>/dev/null",
        "jq '.x' in.json > out.json",
    ]:
        assert classify_bash_command(cmd) == "effect", cmd
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
        # Discarding or merging output writes no file — these were most of one
        # session's shell "actions".
        "ls missing 2>/dev/null",
        "find . -name '*.py' 2> /dev/null | head",
        "curl -s localhost:8080 >/dev/null",
        "pgrep -f freyja &>/dev/null",
        "echo 'bad input' >&2",
        "curl -s x 1>/dev/null 2>&1",
        "python check.py 2>&1 | tail -5",
        # Arrows and comparisons tripped the redirect pattern and turned
        # read-only commands into "actions", which is what still made two
        # denials look like forgetting.
        "gcloud logging read 'timestamp>=\"2026-09-01\"' --limit 5",
        "jq '.items[] | select(.n >= 3)' data.json",
        "echo 'a -> b'",
        "rg 'x => y' src",
    ]:
        assert classify_bash_command(cmd) == "observation", cmd


# ── negative self-claim detection ─────────────────────────────────────────


def test_negative_self_claim_detection_and_its_false_alarms():
    for txt in [
        "I have no recollection of making changes to agent-harness this session.",
        "Everything in our conversation so far has been read-only exploration.",
        "No changes were made during this session.",
        "I haven't made any edits.",
        "I have not written any files so far.",
        "Nothing has been written so far.",
        # Scoped to the session, a report on "nothing written" does fire.
        "nothing was written or created this session",
    ]:
        assert detect_negative_self_claim(txt) is True, txt
    for txt in [
        "I created widget_tools.py with the show_widget tool.",
        "Let me make some changes to the file.",
        "I'll explore the codebase first, then implement.",
        "I edited three files and opened a PR.",
        "",
        # Accurate reports on a single run or click, not the whole session
        # (the two desktop false alarms, plus near misses).
        "Nothing was written. `>05<` occurs three times on the page, so that "
        "replacement has to be limited to the index tile.",
        "Nothing changed. Next I'll read the Automation page's accessibility structure.",
        "The script didn't make any changes.",
        "no changes were made to the file because the pattern didn't match",
        # Unscoped "nothing was written" is how the agent reports on one
        # command or run.
        "nothing was written or created",
        # "nothing written about X" is documentation talk, not a self-claim.
        "There is nothing written about this in the docs.",
        # First-person, but narrowed to one place rather than the session.
        "I haven't changed anything in prod.",
    ]:
        assert detect_negative_self_claim(txt) is False, txt
    # The triggering sentence is what the correction quotes back.
    txt = (
        "I checked the build. Everything in our conversation so far has been "
        "read-only exploration. Want me to start?"
    )
    assert find_negative_self_claim(txt) == (
        "Everything in our conversation so far has been read-only exploration."
    )
    assert find_negative_self_claim("I created a.py.") is None


# ── ledger recording + dedup ──────────────────────────────────────────────


def _mk(tmp_path):
    led = SessionLedger(session_id="s1", project_dir=tmp_path)
    led.ensure()
    return led


def _create(led, path, *, lines, call_id="t1", **kw):
    return led.record_from_tool(
        tool_name="write_file", tool_args={"path": path},
        result_text=f"Created file: {path.rsplit('/', 1)[-1]}\nWrote 10 characters ({lines} lines)",
        result_chars=40, is_error=False, tool_call_id=call_id, **kw,
    )


def test_record_from_tool_keeps_successful_effects_only(tmp_path):
    led = _mk(tmp_path)
    # A failed write is not an action.
    assert led.record_from_tool(
        tool_name="write_file", tool_args={"path": "/repo/x.py"},
        result_text="Error writing file: permission denied",
        result_chars=40, is_error=True, tool_call_id="t0",
    ) is None
    # Observations are recorded but are not effects.
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

    row = led.record_from_tool(
        tool_name="write_file",
        tool_args={"path": "/repo/agent_harness/cli/tools/widget_tools.py"},
        result_text="Created file: /repo/.../widget_tools.py\nWrote 24000 characters (680 lines)",
        result_chars=70, is_error=False, tool_call_id="t1",
    )
    assert row is not None
    assert row["class"] == "effect" and row["operation"] == "create"
    assert "widget_tools.py" in row["summary"] and "680 lines" in row["summary"]
    assert led.has_effects() is True


def test_effects_collapse_to_one_row_per_path(tmp_path):
    led = _mk(tmp_path)
    p = "/repo/foo.py"
    _create(led, p, lines=3, call_id="t1")
    led.record_from_tool(
        tool_name="edit_file", tool_args={"path": p},
        result_text="Edited file: foo.py\nReplaced lines 1-2 (5 lines)",
        result_chars=40, is_error=False, tool_call_id="t2",
    )
    [row] = led.effects()
    assert row["operation"] == "create"  # the creation verb is preserved

    # 23 edits to one file collapse to ONE row carrying the edit count and the
    # summed diff, not 23 rows and not a misleading "(1 lines)".
    q = "/repo/v2/index.html"
    for i in range(3):
        led.record_from_tool(
            tool_name="edit_file", tool_args={"path": q},
            result_text="Edited file: index.html\nReplaced lines 1-1 (1 lines)",
            result_chars=40, is_error=False, tool_call_id=f"e{i}", creator_id="s1",
            extra={"additions": 10, "deletions": 4},
        )
    [edited] = [e for e in led.effects(creator_id="s1") if e["path"] == q]
    assert edited["editCount"] == 3
    assert (edited["additions"], edited["deletions"]) == (30, 12)
    assert "×3" in edited["summary"] and "(1 lines)" not in edited["summary"]
    assert "+30" in edited["summary"] and "−12" in edited["summary"]

    # A lone create with no diff keeps its meaningful "(N lines)" file size.
    _create(led, "/repo/new.py", lines=680, call_id="t3", creator_id="s1")
    [created] = [e for e in led.effects(creator_id="s1") if e["path"] == "/repo/new.py"]
    assert "(680 lines)" in created["summary"] and "×" not in created["summary"]


def test_bash_effects_are_told_from_observations_by_their_command(tmp_path):
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
    [row] = led.effects()
    assert row["kind"] == "shell_effect"
    assert led.shell_effect_count == 1


def test_pinned_facts_dedup_and_filter_by_creator(tmp_path):
    led = _mk(tmp_path)
    fact = "agent-harness backend is DONE on branch feat/widget-tools"
    led.record_pinned_fact(fact)
    led.record_pinned_fact(fact)  # dup
    assert led.pinned_facts() == [fact]
    assert led.effects() == []  # pinned facts are not effects
    led.record_pinned_fact("parent note", creator_id="s1")
    led.record_pinned_fact("child note", creator_id="sub-9")
    assert led.pinned_facts(creator_id="sub-9") == ["child note"]
    assert "parent note" in led.pinned_facts(creator_id="s1")
    assert "child note" not in led.pinned_facts(creator_id="s1")
    assert len(led.pinned_facts()) == 3


def test_digest_changes_on_a_new_effect_or_coincident_row(tmp_path):
    led = _mk(tmp_path)
    d0 = led.digest()
    _create(led, "/repo/a.py", lines=1)
    d1 = led.digest()
    assert d0 != d1
    led.record_shell_git_effect(
        command="sleep 30", delta=[{"op": "modified", "path": "b.py"}], repo="/r",
    )
    assert led.digest() != d1


def test_ledger_persists_rows_and_hydrates_them_on_resume(tmp_path):
    led1 = _mk(tmp_path)
    _create(led1, "/repo/widget_tools.py", lines=680)
    lines = (tmp_path / "action_ledger.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["class"] == "effect" and row["sessionId"] == "s1"
    led1.record_from_tool(
        tool_name="bash", tool_args={"command": "git commit -m x"},
        result_text="committed", result_chars=10, is_error=False, tool_call_id="b1",
    )
    # Simulate resume: a brand-new ledger over the same project dir must see
    # the prior run's effects (the forgetting incident happened on resume).
    led2 = SessionLedger(session_id="s1", project_dir=tmp_path)
    led2.ensure()
    assert any("widget_tools.py" in (e.get("summary") or "") for e in led2.effects())
    assert led2.has_effects() is True
    assert led2.shell_effect_count == 1


# ── reminder rendering ────────────────────────────────────────────────────


def test_render_reminder_lists_effects_and_pinned_facts_without_shouting():
    assert render_ledger_reminder([], [], memory_present=False) is None
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
    # Long lists are capped.
    many = [{"summary": f"edited file_{i}.py"} for i in range(20)]
    assert "and 8 more" in render_ledger_reminder(many, [], cap=12)


# ── creator attribution (H1) ───────────────────────────────────────────────


def test_effects_filtered_by_creator(tmp_path):
    led = _mk(tmp_path)  # ledger session_id = "s1"
    # Parent (s1) writes one file; a sub-agent (sub-7) writes another into the
    # same shared ledger.
    _create(led, "/repo/parent.py", lines=5, call_id="t1", creator_id="s1")
    _create(led, "/repo/child.py", lines=9, call_id="t2", creator_id="sub-7")
    # The parent's reminder/seed filters to its own id — no sub-agent flooding.
    [parent_eff] = led.effects(creator_id="s1")
    assert "parent.py" in parent_eff["summary"]
    assert led.has_effects(creator_id="s1") is True
    # Unfiltered still sees everything (e.g. for a global view).
    assert len(led.effects()) == 2
    assert led.digest(creator_id="s1") != led.digest(creator_id="sub-7")


# ── diff stats on file effects (chunk 2 / diff-aware artifact) ─────────────


def test_diff_stats_land_on_effect_rows_only_and_persist(tmp_path):
    led = _mk(tmp_path)
    row = led.record_from_tool(
        tool_name="edit_file",
        tool_args={"path": "/repo/widget_tools.py"},
        result_text="Edited file: widget_tools.py\nReplaced lines 1-2 (5 lines)",
        result_chars=40, is_error=False, tool_call_id="t1",
        extra={"additions": 12, "deletions": 3, "diff": "@@ -1,2 +1,2 @@\n-old\n+new\n", "diffTruncated": False},
    )
    assert row is not None
    assert (row["additions"], row["deletions"]) == (12, 3)
    assert "+new" in row["diff"] and row["diffTruncated"] is False
    # The diff never leaks into the standing-reminder summary.
    assert "@@" not in row["summary"]
    saved = json.loads((tmp_path / "action_ledger.jsonl").read_text().strip())
    assert saved["additions"] == 12 and "+new" in saved["diff"]

    # Without extra there are no diff fields.
    plain = _create(led, "/repo/a.py", lines=1, call_id="t2")
    assert plain is not None
    assert "additions" not in plain and "diff" not in plain
    # An observation must not carry diff stats even if extra is passed.
    led.record_from_tool(
        tool_name="web_search", tool_args={"query": "mcp apps spec"},
        result_text="results", result_chars=900, is_error=False, tool_call_id="r1",
        extra={"additions": 5, "deletions": 5, "diff": "+x", "diffTruncated": False},
    )
    [obs] = [r for r in led._snapshot() if r.get("class") == "observation"]
    assert "additions" not in obs and "diff" not in obs


# ── git-status-delta bash capture (C4) ─────────────────────────────────────


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (
            " M existing.py\n",
            " M existing.py\n?? new.txt\n M another.py\n",
            [{"op": "created", "path": "new.txt"}, {"op": "modified", "path": "another.py"}],
        ),
        # After `git commit`, the staged files are clean.
        (
            "M  staged_a.py\nA  staged_b.py\n",
            "",
            [{"op": "committed", "path": "staged_a.py"}, {"op": "committed", "path": "staged_b.py"}],
        ),
        (" M a.py\n?? b.txt\n", " M a.py\n?? b.txt\n", []),
        ("", "R  old.py -> new.py\n", [{"op": "renamed", "path": "new.py"}]),
    ],
    ids=["new_and_modified", "commit_clears_dirty_set", "unchanged", "rename"],
)
def test_git_status_delta(before, after, expected):
    by_path = sorted(git_status_delta(before, after), key=lambda d: d["path"])
    assert by_path == sorted(expected, key=lambda d: d["path"])


def test_record_shell_git_effect_summarizes_the_delta_and_skips_empty_ones(tmp_path):
    led = _mk(tmp_path)
    assert led.record_shell_git_effect(command="git status", delta=[]) is None
    assert led.has_effects() is False
    row = led.record_shell_git_effect(
        command="git commit -m 'widget tools'",
        delta=[{"op": "committed", "path": "/repo/widget_tools.py"},
               {"op": "committed", "path": "/repo/__init__.py"}],
        repo="/repo", creator_id="s1", tool_call_id="b1",
    )
    assert row is not None
    assert row["kind"] == "shell_effect"
    assert "widget_tools.py" in row["summary"] and "__init__.py" in row["summary"]
    assert row["gitDelta"][0]["path"] == "/repo/widget_tools.py"
    assert led.shell_effect_count == 1


def test_git_capture_inflight_guard(tmp_path):
    led = _mk(tmp_path)
    assert led.begin_git_capture("/repo") is True   # first claim wins
    assert led.begin_git_capture("/repo") is False  # concurrent claim blocked
    assert led.begin_git_capture("/other") is True  # different repo ok
    led.end_git_capture("/repo")
    assert led.begin_git_capture("/repo") is True   # reclaimable after release


# ── manifest backfill (B3) ─────────────────────────────────────────────────


def test_backfill_from_manifest_skips_legacy_rows_and_does_not_duplicate(tmp_path):
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
    summaries = " ".join(e["summary"] for e in led.effects(creator_id="s1"))
    assert "widget_tools.py (680 lines)" in summaries
    assert "foo.py" in summaries
    # subagent_artifact / legacy imports are skipped.
    assert "legacy.md" not in summaries

    # A manifest row for a path the ledger already holds must not double-count
    # after re-ensure.
    _create(led, "/repo/a.py", lines=5, creator_id="s1")
    manifest.write_text(
        manifest.read_text()
        + json.dumps({"path": "/repo/a.py", "operation": "write",
                      "lines": 5, "creatorId": "s1", "createdAt": 1}) + "\n"
    )
    led2 = SessionLedger(session_id="s1", project_dir=tmp_path)
    led2.ensure()
    assert len([e for e in led2.effects() if e["path"] == "/repo/a.py"]) == 1
    assert len([e for e in led2.effects() if e["path"] == "/repo/widget_tools.py"]) == 1


# ── coincident changes (passive commands) + fork marker ───────────────────


def test_passive_command_delta_is_coincident_not_an_effect(tmp_path):
    # The incident: a fork's agent read "ran `sleep 100…` → changed
    # transcript_persistence.py" in its reminder. The file was edited by
    # someone else during the sleep; the command has no mutating verb, so it
    # cannot be the author of the delta.
    led = _mk(tmp_path)
    row = led.record_shell_git_effect(
        command="cd ~/personal/freyja && sleep 100; tail -1 evals/x.log; ls evals/*.jsonl",
        delta=[{"op": "modified", "path": "bridge/transcript_persistence.py"}],
        repo="/Users/sohamshah/personal/freyja", creator_id="s1", tool_call_id="b9",
    )
    assert row is not None
    assert row["class"] == "observation" and row["kind"] == "shell_coincident"
    assert "not by this command" in row["summary"]
    assert "transcript_persistence.py" in row["summary"]
    # Never counted as the agent's own work.
    assert led.has_effects() is False
    assert led.effects(creator_id="s1") == []
    assert led.shell_effect_count == 0
    assert [r["kind"] for r in led.coincident(creator_id="s1")] == ["shell_coincident"]
    # Survives a reload from disk with the same classification.
    led2 = _mk(tmp_path)
    assert led2.has_effects() is False
    assert len(led2.coincident()) == 1

    # A command with a mutating verb owns its delta.
    row = led2.record_shell_git_effect(
        command="sed -i '' 's/a/b/' bridge/x.py && sleep 5",
        delta=[{"op": "modified", "path": "bridge/x.py"}],
        repo="/r", creator_id="s1",
    )
    assert row["class"] == "effect" and row["kind"] == "shell_effect"
    assert led2.shell_effect_count == 1
    assert len(led2.coincident()) == 1


def test_render_reminder_lists_coincident_changes_as_not_yours():
    effects = [{"kind": "file_edit", "summary": "edited foo.py", "path": "/r/foo.py"}]
    coincident = [{
        "kind": "shell_coincident",
        "command": "cd ~/personal/freyja && sleep 100; tail -1 evals/x.log",
        "gitDelta": [{"op": "modified", "path": "bridge/transcript_persistence.py"}],
    }]
    block = render_ledger_reminder(effects, [], coincident=coincident)
    assert block is not None
    assert "NOT by them" in block
    assert "transcript_persistence.py — during `cd ~/personal/freyja && sleep 100; tail" in block
    # The coincident file never appears under the "changed" list.
    changed = block.split("Files and actions you've created or changed:")[1].split("Changed on disk")[0]
    assert "transcript_persistence" not in changed


def test_render_reminder_names_the_fork_source():
    effects = [{"kind": "file_edit", "summary": "edited foo.py", "path": "/r/foo.py"}]
    block = render_ledger_reminder(effects, [], forked_from="session-abc")
    assert block is not None
    assert "fork of `session-abc`" in block
    assert block.index("fork of") < block.index("Files and actions")
    assert "fork of" not in (render_ledger_reminder(effects, []) or "")


def test_fork_marker_is_a_note_not_an_effect(tmp_path):
    led = _mk(tmp_path)
    led.record_effect(kind="file_edit", operation="edit", summary="edited a.py", path="/r/a.py")
    row = led.record_fork_marker("session-src", 1790000000000)
    assert row["class"] == "note" and row["kind"] == "fork"
    assert row["sourceSessionId"] == "session-src" and row["createdAt"] == 1790000000000
    assert [r["summary"] for r in led.effects()] == ["edited a.py"]
    # Written to disk, reloads, still not an effect.
    led2 = _mk(tmp_path)
    assert any(r.get("kind") == "fork" for r in led2._snapshot())
    assert len(led2.effects()) == 1
