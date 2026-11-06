"""Unit tests for bridge/mcp proxy naming, permissions, summaries,
result mapping, spawn-env allowlist and tool include/exclude filtering.
No subprocesses are spawned here (the connect-classification test fails
before exec). Injection-scan patterns are covered in
tests/test_mcp_lifecycle_v2.py; the unset-secret needs-auth signal in
tests/test_mcp_lifecycle.py."""

from __future__ import annotations

from types import SimpleNamespace

from bridge.mcp.config import McpServerSpec
from bridge.mcp.connection import (
    SPAWN_ENV_BASE,
    McpConnection,
    State,
    build_spawn_env,
)
from bridge.mcp.manager import McpManager
from bridge.mcp.proxy_tool import (
    MAX_TOOL_NAME_LEN,
    McpProxyTool,
    build_summary,
    first_sentence,
    proxy_tool_name,
    resolve_permission_level,
    sanitize_component,
    to_tool_result,
)
from bridge.tools.base import PermissionLevel

# ---------------------------------------------------------------------------
# Name sanitization + truncation
# ---------------------------------------------------------------------------

def test_proxy_tool_name_sanitizes_and_truncates_with_stable_hash():
    assert sanitize_component("Echo Text!") == "echo_text"
    assert sanitize_component("get-user.byID") == "get_user_byid"
    assert sanitize_component("___") == "x"
    assert proxy_tool_name("fixture", "echo") == "mcp__fixture__echo"

    server = "a-very-long-mcp-server-name-for-testing"
    tool = "an_extremely_long_remote_tool_name_that_will_not_fit_in_the_cap"
    name1 = proxy_tool_name(server, tool)
    name2 = proxy_tool_name(server, tool)
    assert name1 == name2, "truncated name must be stable across calls"
    assert len(name1) == MAX_TOOL_NAME_LEN
    # stable 6-char hash suffix, separated by underscore
    assert name1[-7] == "_"
    suffix = name1[-6:]
    assert all(c in "0123456789abcdef" for c in suffix)
    # A different long tool must not collide.
    assert proxy_tool_name(server, tool + "_v2") != name1
    # Short names are left untouched.
    assert proxy_tool_name("fixture", "slow") == "mcp__fixture__slow"


# ---------------------------------------------------------------------------
# Trust -> PermissionLevel mapping incl per-tool override
# ---------------------------------------------------------------------------

def _spec(trust="standard", tool_permissions=None) -> McpServerSpec:
    return McpServerSpec(
        name="srv",
        transport="stdio",
        command="cmd",
        trust=trust,
        tool_permissions=tool_permissions or {},
    )


def test_trust_table_and_per_tool_override():
    assert resolve_permission_level(_spec("trusted"), "t") is None
    assert resolve_permission_level(_spec("standard"), "t") is PermissionLevel.MEDIUM
    assert resolve_permission_level(_spec("untrusted"), "t") is PermissionLevel.HIGH

    spec = _spec("untrusted", {"read_thing": "low", "nuke": "high", "free": "none"})
    assert resolve_permission_level(spec, "read_thing") is PermissionLevel.LOW
    assert resolve_permission_level(spec, "nuke") is PermissionLevel.HIGH
    assert resolve_permission_level(spec, "free") is None
    # tools without an override fall back to the trust mapping
    assert resolve_permission_level(spec, "other") is PermissionLevel.HIGH
    # an unknown override value falls back too
    bogus = _spec("standard", {"t": "bogus"})
    assert resolve_permission_level(bogus, "t") is PermissionLevel.MEDIUM


async def test_permission_prompt_trusted_skips_and_standard_redacts_secrets():
    manager = McpManager()
    trusted = McpProxyTool(
        manager=manager, spec=_spec("trusted"), remote_name="echo",
        description="Echo.", input_schema=None, summary="[srv] Echo.",
    )
    assert await trusted.permission_prompt({"text": "x"}) is None

    standard = McpProxyTool(
        manager=manager, spec=_spec("standard"), remote_name="echo",
        description="Echo.", input_schema=None, summary="[srv] Echo.",
    )
    request = await standard.permission_prompt(
        {"text": "x", "api_token": "abcdefghijklmnop"}
    )
    assert request is not None
    assert request.level is PermissionLevel.MEDIUM
    assert "echo" in request.prompt and "srv" in request.prompt
    assert "abcdefghijklmnop" not in (request.details or "")


# ---------------------------------------------------------------------------
# Summary construction
# ---------------------------------------------------------------------------

def test_build_summary_first_sentence_prefix_and_cap():
    assert first_sentence("Does a thing. Then more. And more.") == "Does a thing."
    assert first_sentence("No period here") == "No period here"
    assert first_sentence("Line one\nLine two.") == "Line one"
    assert first_sentence("") == ""
    summary = build_summary("fixture", "Retrieves lore. Second sentence.")
    assert summary == "[fixture] Retrieves lore."
    capped = build_summary("fixture", "word " * 60 + ".")
    assert len(capped) <= 140
    assert capped.startswith("[fixture] ")


# ---------------------------------------------------------------------------
# Result mapping
# ---------------------------------------------------------------------------

def _mcp_result(content, is_error=False):
    return SimpleNamespace(content=content, is_error=is_error)


def test_to_tool_result_text_error_and_image_blocks():
    result = to_tool_result("c1", _mcp_result([
        SimpleNamespace(type="text", text="one"),
        SimpleNamespace(type="text", text="two"),
    ]))
    assert result.content == "one\ntwo"
    assert result.is_error is False
    assert result.call_id == "c1"

    result = to_tool_result("c1", _mcp_result(
        [SimpleNamespace(type="text", text="boom")], is_error=True
    ))
    assert result.is_error is True
    assert "boom" in result.content

    result = to_tool_result("c1", _mcp_result([
        SimpleNamespace(type="text", text="see image"),
        SimpleNamespace(type="image", data="aGVsbG8=", mime_type="image/jpeg"),
    ]))
    assert result.is_error is False
    assert isinstance(result.content, list)
    assert [getattr(b, "type", None) for b in result.content] == ["text", "image"]
    image = result.content[1]
    assert image.data == "aGVsbG8="
    assert image.media_type == "image/jpeg"


# ---------------------------------------------------------------------------
# Spawn env allowlist
# ---------------------------------------------------------------------------

def test_build_spawn_env_allowlist_only_and_declared_overrides_base(monkeypatch):
    monkeypatch.setenv("SUPER_SECRET_PROVIDER_KEY", "leakme")
    monkeypatch.setenv("PATH", "/usr/bin")
    env = build_spawn_env({"MY_SERVER_FLAG": "1"})
    assert env["MY_SERVER_FLAG"] == "1"
    assert "SUPER_SECRET_PROVIDER_KEY" not in env
    for key in env:
        assert key in set(SPAWN_ENV_BASE) | {"MY_SERVER_FLAG"}
    assert env.get("PATH") == "/usr/bin"
    assert build_spawn_env({"PATH": "/custom/bin"})["PATH"] == "/custom/bin"


# ---------------------------------------------------------------------------
# Connect classification: missing non-secret var is not needs-auth; disabled
# ---------------------------------------------------------------------------

async def test_unset_nonsecret_var_expands_empty_and_connect_proceeds(monkeypatch):
    # A non-secret missing var must NOT gate on needs-auth; the connect
    # proceeds (and fails on the bogus command, which is FAILED not
    # NEEDS_AUTH).
    monkeypatch.delenv("FIXTURE_MISSING_FLAG", raising=False)
    spec = McpServerSpec(
        name="flaggy",
        transport="stdio",
        command="freyja-definitely-not-a-real-binary",
        env={"SOME_FLAG": "${FIXTURE_MISSING_FLAG}"},
        connect_timeout_s=5,
    )
    conn = McpConnection(spec, max_reconnects=0)
    await conn.start()
    assert conn.state is State.FAILED
    await conn.stop()

    off = McpServerSpec(name="off", transport="stdio", command="x", enabled=False)
    disabled = McpConnection(off)
    await disabled.start()
    assert disabled.state is State.DISABLED


# ---------------------------------------------------------------------------
# Tool include/exclude filter
# ---------------------------------------------------------------------------

def test_manager_build_proxies_respects_include_exclude():
    spec = _spec("trusted")
    spec.tools_include = ["get_*"]
    spec.tools_exclude = ["get_secret"]
    manager = McpManager()
    fake_conn = SimpleNamespace(
        spec=spec,
        tools=[
            SimpleNamespace(name="get_user", title=None, description="Gets a user.",
                            input_schema={}),
            SimpleNamespace(name="get_secret", title=None, description="Gets a secret.",
                            input_schema={}),
            SimpleNamespace(name="delete_user", title=None, description="Deletes.",
                            input_schema={}),
        ],
    )
    names = [p.remote_name for p in manager._build_proxies(fake_conn)]  # noqa: SLF001
    assert names == ["get_user"]
