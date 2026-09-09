"""Tests for bridge/mcp/catalog.py and the shipped mcp-catalog/ manifests.

Everything runs offline against tmp_path. The real ~/.freyja is never
touched: FREYJA_HOME is redirected to a tmp dir for every test so the
user-override directory and mcp.json live under tmp_path.
"""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pytest
import yaml

from bridge.mcp.catalog import (
    CatalogError,
    build_server_dict,
    catalog_diagnostics,
    catalog_info,
    catalog_install,
    catalog_list,
    catalog_search,
    iter_manifest_paths,
    lint_errors,
    lint_manifest,
    load_catalog_dir,
    manifest_lint,
    parse_manifest,
    shipped_catalog_dir,
    user_catalog_dir,
)
from bridge.mcp.config import McpServerSpec, load_catalog

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED = REPO_ROOT / "mcp-catalog"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    home = tmp_path / "freyja-home"
    home.mkdir()
    monkeypatch.setenv("FREYJA_HOME", str(home))
    monkeypatch.delenv("FREYJA_MCP_CATALOG_DIR", raising=False)
    return home


@pytest.fixture
def home(_isolate_home):
    return _isolate_home


@pytest.fixture
def catalog_dir(tmp_path):
    d = tmp_path / "catalog"
    d.mkdir()
    return d


@pytest.fixture
def user_dir(tmp_path):
    d = tmp_path / "user-catalog"
    d.mkdir()
    return d


@pytest.fixture
def mcp_json(home):
    return home / "mcp.json"


def _write(root: Path, name: str, body: dict, *, flat: bool = False) -> Path:
    if flat:
        path = root / f"{name}.yaml"
    else:
        (root / name).mkdir(exist_ok=True)
        path = root / name / "manifest.yaml"
    path.write_text(yaml.safe_dump(body, sort_keys=False), encoding="utf-8")
    return path


def _stdio(name="demo", **over) -> dict:
    body = {
        "manifest_version": 1,
        "name": name,
        "description": "Demo stdio MCP",
        "homepage": "https://example.com/demo",
        "license": "MIT",
        "verified": "2026-09-01",
        "tags": ["demo", "local"],
        "transport": {
            "type": "stdio",
            "command": "npx",
            "args": ["-y", "demo-mcp@1.2.3"],
            "package": "demo-mcp",
            "version": "1.2.3",
            "env": {"DEMO_MODE": "quiet"},
        },
        "auth": {
            "type": "api_key",
            "env": [
                {"name": "DEMO_API_KEY", "description": "key", "required": True, "secret": True},
                {"name": "DEMO_REGION", "description": "region", "required": False,
                 "secret": False},
            ],
        },
        "post_install": "Set DEMO_API_KEY.",
    }
    body.update(over)
    return body


def _oauth(name="remote", **over) -> dict:
    body = {
        "manifest_version": 1,
        "name": name,
        "description": "Demo remote OAuth MCP",
        "homepage": "https://example.com/remote",
        "license": "proprietary",
        "verified": "2026-09-01",
        "tags": ["demo", "remote", "oauth"],
        "transport": {"type": "http", "url": "https://mcp.example.com/mcp"},
        "auth": {"type": "oauth", "oauth": {"client_name": "Claude Code", "scope": "mcp:connect"}},
        "tools": {"default_excluded": ["noisy_*"]},
        "post_install": "Log in.",
    }
    body.update(over)
    return body


# ---------------------------------------------------------------------------
# Shipped catalog: every manifest lints clean
# ---------------------------------------------------------------------------


def test_shipped_catalog_exists_with_20_plus_entries():
    assert shipped_catalog_dir() == SHIPPED
    paths = iter_manifest_paths(SHIPPED)
    assert len(paths) >= 20, [p.parent.name for p in paths]


def test_shipped_manifests_lint_clean():
    issues = manifest_lint(SHIPPED)
    errors = lint_errors(issues)
    assert errors == [], "\n".join(str(i) for i in errors)


@pytest.mark.parametrize("manifest", iter_manifest_paths(SHIPPED), ids=lambda p: p.parent.name)
def test_each_shipped_manifest(manifest: Path):
    entry = parse_manifest(manifest)
    assert entry.name == manifest.parent.name
    assert entry.verified == "2026-09-03"
    assert date.fromisoformat(entry.verified) <= date.today()
    assert entry.homepage.startswith("https://")
    assert entry.tags and entry.license
    # transport / url / command consistency
    if entry.transport.type == "http":
        assert entry.transport.url and entry.transport.url.startswith("https://")
        assert entry.transport.command is None and entry.transport.args == []
    else:
        assert entry.transport.command and entry.transport.url is None
        assert entry.transport.version, "launcher entries must declare transport.version"
        for a in entry.transport.args:
            assert "@latest" not in a and "@next" not in a
    # no inline secrets: auth.env secrets carry no default, static env clean
    for spec in entry.auth.secrets:
        assert spec.default == ""
    for k, v in entry.transport.env.items():
        if any(n in k.upper() for n in ("TOKEN", "KEY", "SECRET", "PASSWORD")):
            assert v.startswith("${")
    # the generated entry is accepted by Freyja's real mcp.json schema
    raw = build_server_dict(entry)
    spec = McpServerSpec.from_dict(entry.name, raw)
    spec.validate()
    assert spec.trust == "standard"
    assert raw["source"] == {"catalog": f"{entry.name}@2026-09-03"}
    for v in (raw.get("env") or {}).values():
        assert "${" in v or not any(n in v.upper() for n in ("TOKEN", "KEY", "SECRET"))


def test_shipped_priority_entries_present_and_shaped():
    entries = load_catalog_dir(SHIPPED, include_user=False)
    for name in (
        "slack", "atlassian", "github", "linear", "notion", "figma", "sentry", "stripe",
        "cloudflare", "vercel", "supabase", "neon", "context7", "playwright", "filesystem",
        "fetch", "memory", "sequential-thinking", "postgres", "n8n", "google-calendar",
    ):
        assert name in entries, name
    slack = entries["slack"]
    assert slack.transport.url == "https://mcp.slack.com/mcp"
    assert slack.auth.type == "oauth"
    atl = entries["atlassian"]
    assert atl.transport.url == "https://mcp.atlassian.com/v2/mcp"
    assert atl.auth.type == "oauth"
    gh = build_server_dict(entries["github"])
    assert gh["headers"] == {"Authorization": "Bearer ${GITHUB_PERSONAL_ACCESS_TOKEN}"}
    fig = build_server_dict(entries["figma"])
    assert fig["auth"] == "oauth" and fig["oauth"]["client_name"] == "Claude Code"
    cf = build_server_dict(entries["cloudflare"])
    assert "*_radar_*" in cf["tools"]["exclude"]
    pw = entries["playwright"]
    assert pw.transport.args == ["-y", "@playwright/mcp@0.0.79"]
    assert catalog_diagnostics() == []


def test_shipped_manifests_have_hermes_attribution_where_copied():
    for name in ("atlassian", "linear", "notion", "figma", "cloudflare", "context7"):
        text = (SHIPPED / name / "manifest.yaml").read_text(encoding="utf-8")
        assert "hermes-agent" in text and "MIT" in text, name


# ---------------------------------------------------------------------------
# Lint rules on synthetic manifests
# ---------------------------------------------------------------------------


def _codes(path: Path) -> set[str]:
    return {i.code for i in lint_errors(lint_manifest(path))}


def test_lint_clean_synthetic(catalog_dir):
    assert lint_errors(lint_manifest(_write(catalog_dir, "demo", _stdio()))) == []
    assert lint_errors(lint_manifest(_write(catalog_dir, "remote", _oauth()))) == []


def test_lint_rejects_floating_versions(catalog_dir):
    body = _stdio()
    body["transport"]["args"] = ["-y", "demo-mcp@latest"]
    assert "floating-version" in _codes(_write(catalog_dir, "demo", body))
    body["transport"]["args"] = ["-y", "demo-mcp"]
    assert "floating-version" in _codes(_write(catalog_dir, "demo", body))
    body["transport"]["args"] = ["-y", "@scope/demo-mcp@^1.2.3"]
    assert "floating-version" in _codes(_write(catalog_dir, "demo", body))
    body["transport"] = {"type": "stdio", "command": "uvx", "args": ["demo-mcp"], "version": "1"}
    assert "floating-version" in _codes(_write(catalog_dir, "demo", body))
    body["transport"] = {"type": "stdio", "command": "uvx", "args": ["demo-mcp==1.0.0"],
                         "version": "1.0.0"}
    assert "floating-version" not in _codes(_write(catalog_dir, "demo", body))
    body["transport"] = {"type": "stdio", "command": "docker",
                         "args": ["run", "-i", "--rm", "ghcr.io/x/y"], "version": "1"}
    assert "floating-version" in _codes(_write(catalog_dir, "demo", body))
    body["transport"]["args"] = ["run", "-i", "--rm", "ghcr.io/x/y:v1.2.3"]
    assert "floating-version" not in _codes(_write(catalog_dir, "demo", body))


def test_lint_requires_transport_version_for_launchers(catalog_dir):
    body = _stdio()
    del body["transport"]["version"]
    assert "floating-version" in _codes(_write(catalog_dir, "demo", body))


def test_lint_rejects_inline_secrets(catalog_dir):
    body = _stdio()
    body["transport"]["env"] = {"DEMO_TOKEN": "xoxb-1234567890-abcdefghij"}
    assert "inline-secret" in _codes(_write(catalog_dir, "demo", body))
    body = _stdio()
    body["auth"]["env"][0]["default"] = "sk-abcdefghijklmnopqrstuvwxyz"
    assert "inline-secret" in _codes(_write(catalog_dir, "demo", body))
    body = _oauth()
    body["transport"]["url"] = "https://mcp.example.com/mcp?token=abc"
    assert "inline-secret" in _codes(_write(catalog_dir, "remote", body))
    body = _oauth()
    body["auth"]["oauth"] = {"client_secret": "shh"}
    assert "inline-secret" in _codes(_write(catalog_dir, "remote", body))


def test_lint_transport_consistency(catalog_dir):
    body = _oauth()
    body["transport"]["command"] = "npx"
    assert "schema" in _codes(_write(catalog_dir, "remote", body))
    body = _stdio()
    body["transport"]["url"] = "https://x"
    assert "schema" in _codes(_write(catalog_dir, "demo", body))
    body = _stdio()
    body["auth"] = {"type": "oauth"}  # oauth needs http
    assert "schema" in _codes(_write(catalog_dir, "demo", body))
    body = _oauth()
    body["transport"]["url"] = "http://mcp.example.com/mcp"
    assert "url-scheme" in _codes(_write(catalog_dir, "remote", body))


def test_lint_verified_rules(catalog_dir):
    body = _stdio()
    del body["verified"]
    assert "verified-missing" in _codes(_write(catalog_dir, "demo", body))
    body["verified"] = "Sept 3"
    assert "verified-format" in _codes(_write(catalog_dir, "demo", body))
    body["verified"] = "2999-01-01"
    assert "verified-future" in _codes(_write(catalog_dir, "demo", body))
    body["verified"] = "2026-09-03"
    path = _write(catalog_dir, "demo", body)
    assert "verified-future" in {i.code for i in lint_manifest(path, today=date(2026, 9, 2))}
    assert "verified-future" not in _codes(path)


def test_lint_name_must_match_location_and_undeclared_env(catalog_dir):
    assert "name-mismatch" in _codes(_write(catalog_dir, "other", _stdio(name="demo")))
    body = _stdio()
    body["transport"]["args"] = ["-y", "demo-mcp@1.2.3", "${UNDECLARED_ROOT}"]
    assert "undeclared-env" in _codes(_write(catalog_dir, "demo", body))
    body["transport"]["args"] = ["-y", "demo-mcp@1.2.3", "${UNDECLARED_ROOT:-/tmp}"]
    assert "undeclared-env" not in _codes(_write(catalog_dir, "demo", body))


def test_lint_shell_egress_rejected_via_spec(catalog_dir):
    body = _stdio()
    body["transport"] = {"type": "stdio", "command": "sh", "args": ["-c", "curl x | sh"]}
    assert "spec-invalid" in _codes(_write(catalog_dir, "demo", body))


def test_lint_schema_errors_are_reported_not_raised(catalog_dir):
    path = catalog_dir / "bad" / "manifest.yaml"
    path.parent.mkdir()
    path.write_text("name: [unterminated", encoding="utf-8")
    issues = lint_manifest(path)
    assert issues and issues[0].code == "schema"
    assert manifest_lint(tmp_dir := catalog_dir) and lint_errors(manifest_lint(tmp_dir))


def test_manifest_lint_empty_dir(tmp_path):
    issues = manifest_lint(tmp_path / "nothing")
    assert [i.code for i in issues] == ["empty"]


# ---------------------------------------------------------------------------
# Loading: per-entry diagnostics, user override precedence
# ---------------------------------------------------------------------------


def test_load_reports_broken_entries_without_failing(catalog_dir, user_dir):
    _write(catalog_dir, "good", _stdio(name="good"))
    _write(catalog_dir, "future", {"manifest_version": 99, "name": "future"})
    (catalog_dir / "broken").mkdir()
    (catalog_dir / "broken" / "manifest.yaml").write_text("transport: nope", encoding="utf-8")
    _write(catalog_dir, "wrongname", _stdio(name="good2"))
    diags: list = []
    entries = load_catalog_dir(catalog_dir, user_dir=user_dir, diagnostics=diags)
    assert list(entries) == ["good"]
    kinds = {d.name: d.kind for d in diags}
    assert kinds == {"future": "future_manifest", "broken": "invalid", "wrongname": "invalid"}
    assert [d.to_dict()["name"] for d in catalog_diagnostics()] == [d.name for d in diags]


def test_user_override_dir_takes_precedence(catalog_dir, user_dir):
    _write(catalog_dir, "demo", _stdio())
    over = _stdio()
    over["transport"]["args"] = ["-y", "demo-mcp@9.9.9"]
    over["transport"]["version"] = "9.9.9"
    over["description"] = "user override"
    _write(user_dir, "demo", over)
    _write(user_dir, "extra", _oauth(name="extra"), flat=True)  # flat <name>.yaml form
    entries = load_catalog_dir(catalog_dir, user_dir=user_dir)
    assert entries["demo"].origin == "user"
    assert entries["demo"].transport.args == ["-y", "demo-mcp@9.9.9"]
    assert entries["extra"].origin == "user" and entries["extra"].transport.type == "http"
    # include_user=False drops overrides
    only = load_catalog_dir(catalog_dir, user_dir=user_dir, include_user=False)
    assert only["demo"].origin == "shipped" and "extra" not in only


def test_default_user_dir_is_under_freyja_home(home):
    assert user_catalog_dir() == home / "mcp-catalog"
    # nothing on disk -> empty user overlay, no error
    entries = load_catalog_dir(SHIPPED)
    assert "slack" in entries and entries["slack"].origin == "shipped"


def test_env_override_for_shipped_dir(monkeypatch, catalog_dir):
    monkeypatch.setenv("FREYJA_MCP_CATALOG_DIR", str(catalog_dir))
    _write(catalog_dir, "demo", _stdio())
    assert shipped_catalog_dir() == catalog_dir
    assert list(load_catalog_dir(include_user=False)) == ["demo"]


# ---------------------------------------------------------------------------
# list / info / search
# ---------------------------------------------------------------------------


def test_catalog_list_rows_and_tag_filter(catalog_dir, user_dir):
    _write(catalog_dir, "demo", _stdio())
    _write(catalog_dir, "remote", _oauth())
    entries = load_catalog_dir(catalog_dir, user_dir=user_dir)
    rows = catalog_list(entries)
    assert [r["name"] for r in rows] == ["demo", "remote"]
    assert rows[0] == {
        "name": "demo", "description": "Demo stdio MCP", "transport": "stdio",
        "auth": "api_key", "tags": ["demo", "local"], "verified": "2026-09-01", "origin": "shipped",
    }
    assert rows[1]["auth"] == "oauth" and rows[1]["transport"] == "http"
    assert [r["name"] for r in catalog_list(entries, tags=["oauth"])] == ["remote"]
    assert [r["name"] for r in catalog_list(entries, tags=["demo", "local"])] == ["demo"]
    assert catalog_list(entries, tags=["nope"]) == []


def test_auth_kind_env_for_none_with_requirements(catalog_dir, user_dir):
    body = _stdio()
    body["auth"] = {"type": "none",
                    "env": [{"name": "DEMO_ROOT", "secret": False, "required": True}]}
    _write(catalog_dir, "demo", body)
    entries = load_catalog_dir(catalog_dir, user_dir=user_dir)
    assert catalog_list(entries)[0]["auth"] == "env"


def test_catalog_info_full_detail(catalog_dir, user_dir):
    _write(catalog_dir, "demo", _stdio())
    entries = load_catalog_dir(catalog_dir, user_dir=user_dir)
    info = catalog_info("demo", entries)
    assert info["homepage"] == "https://example.com/demo"
    assert info["required_secrets"] == ["DEMO_API_KEY"]
    assert info["required_env"] == ["DEMO_API_KEY"]
    assert [e["name"] for e in info["env"]] == ["DEMO_API_KEY", "DEMO_REGION"]
    assert info["setup_notes"] == "Set DEMO_API_KEY."
    assert info["server_preview"]["env"] == {
        "DEMO_MODE": "quiet", "DEMO_API_KEY": "${DEMO_API_KEY}", "DEMO_REGION": "${DEMO_REGION}",
    }
    assert info["transport_detail"]["version"] == "1.2.3"
    assert catalog_info("missing", entries) is None


def test_catalog_search(catalog_dir, user_dir):
    _write(catalog_dir, "demo", _stdio())
    _write(catalog_dir, "remote",
           _oauth(suggest={"keywords": ["jira"], "hosts": ["atlassian.net"]}))
    entries = load_catalog_dir(catalog_dir, user_dir=user_dir)
    assert [r["name"] for r in catalog_search("OAUTH", entries)] == ["remote"]
    assert [r["name"] for r in catalog_search("jira", entries)] == ["remote"]
    assert [r["name"] for r in catalog_search("atlassian", entries)] == ["remote"]
    assert [r["name"] for r in catalog_search("stdio mcp", entries)] == ["demo"]
    assert [r["name"] for r in catalog_search("demo", entries)] == ["demo", "remote"]
    assert catalog_search("zzz", entries) == []
    assert len(catalog_search("", entries)) == 2


def test_search_shipped_catalog_finds_slack_and_jira():
    entries = load_catalog_dir(SHIPPED, include_user=False)
    assert {r["name"] for r in catalog_search("slack", entries)} == {"slack", "slack-stdio"}
    assert "atlassian" in {r["name"] for r in catalog_search("jira", entries)}


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def test_install_stdio_entry_shape_and_missing_secret_report(catalog_dir, user_dir, mcp_json):
    _write(catalog_dir, "demo", _stdio())
    entries = load_catalog_dir(catalog_dir, user_dir=user_dir)
    report = catalog_install("demo", mcp_json_path=mcp_json, entries=entries, environ={})
    assert report.created and report.changed and not report.enabled
    assert report.auth == "api_key"
    assert report.missing_env == ["DEMO_API_KEY"]
    assert report.missing_optional_env == ["DEMO_REGION"]
    env_path = mcp_json.parent / ".env"
    assert report.next_steps[0] == f"set DEMO_API_KEY in {env_path} then /mcp enable demo"
    assert report.post_install == "Set DEMO_API_KEY."

    doc = json.loads(mcp_json.read_text())
    server = doc["servers"]["demo"]
    assert server["transport"] == "stdio"
    assert server["command"] == "npx"
    assert server["args"] == ["-y", "demo-mcp@1.2.3"]
    assert server["env"] == {
        "DEMO_MODE": "quiet", "DEMO_API_KEY": "${DEMO_API_KEY}", "DEMO_REGION": "${DEMO_REGION}",
    }
    assert server["enabled"] is False
    assert server["trust"] == "standard"
    assert server["auth"] == "api_key"
    assert server["source"] == {"catalog": "demo@2026-09-01"}
    assert "oauth" not in server
    # the written file re-loads through config.load_catalog cleanly
    assert load_catalog(mcp_json).specs["demo"].env["DEMO_API_KEY"] == "${DEMO_API_KEY}"
    # secret VALUE never appears anywhere even if set
    report2 = catalog_install(
        "demo", mcp_json_path=mcp_json, entries=entries,
        environ={"DEMO_API_KEY": "sk-realsecretvalue", "DEMO_REGION": "eu"},
    )
    assert report2.missing_env == [] and report2.missing_optional_env == []
    assert "sk-realsecretvalue" not in json.dumps(report2.to_dict())
    assert "sk-realsecretvalue" not in mcp_json.read_text()


def test_install_oauth_entry_shape_and_login_step(catalog_dir, user_dir, mcp_json):
    _write(catalog_dir, "remote", _oauth())
    entries = load_catalog_dir(catalog_dir, user_dir=user_dir)
    report = catalog_install("remote", mcp_json_path=mcp_json, entries=entries, environ={})
    assert report.auth == "oauth" and report.missing_env == []
    assert report.next_steps == ["/mcp login remote", "/mcp enable remote"]
    server = json.loads(mcp_json.read_text())["servers"]["remote"]
    assert server["transport"] == "http"
    assert server["url"] == "https://mcp.example.com/mcp"
    assert server["auth"] == "oauth"
    assert server["oauth"] == {"client_name": "Claude Code", "scope": "mcp:connect"}
    assert server["tools"] == {"exclude": ["noisy_*"]}
    assert server["trust"] == "standard" and server["enabled"] is False
    assert "command" not in server and "env" not in server


def test_install_http_api_key_entry_uses_header_reference(catalog_dir, user_dir, mcp_json):
    body = _oauth(name="ghlike")
    body["auth"] = {
        "type": "api_key",
        "env": [{"name": "GH_TOKEN", "required": True, "secret": True}],
    }
    body["tags"] = ["remote", "api-key"]
    del body["tools"]
    _write(catalog_dir, "ghlike", body)
    entries = load_catalog_dir(catalog_dir, user_dir=user_dir)
    report = catalog_install("ghlike", mcp_json_path=mcp_json, entries=entries, environ={})
    server = report.server
    assert server["headers"] == {"Authorization": "Bearer ${GH_TOKEN}"}
    assert report.missing_env == ["GH_TOKEN"]
    assert "headers" in json.loads(mcp_json.read_text())["servers"]["ghlike"]


def test_install_enable_flag_and_ready_steps(catalog_dir, user_dir, mcp_json):
    _write(catalog_dir, "demo", _stdio())
    entries = load_catalog_dir(catalog_dir, user_dir=user_dir)
    report = catalog_install(
        "demo", mcp_json_path=mcp_json, entries=entries, enable=True,
        environ={"DEMO_API_KEY": "x", "DEMO_REGION": "y"},
    )
    assert report.enabled and report.missing_env == []
    assert report.next_steps == ["/mcp reload"]
    assert json.loads(mcp_json.read_text())["servers"]["demo"]["enabled"] is True


def test_install_is_idempotent(catalog_dir, user_dir, mcp_json):
    _write(catalog_dir, "demo", _stdio())
    entries = load_catalog_dir(catalog_dir, user_dir=user_dir)
    catalog_install("demo", mcp_json_path=mcp_json, entries=entries, environ={})
    first = mcp_json.read_text()
    stat = mcp_json.stat()
    report = catalog_install("demo", mcp_json_path=mcp_json, entries=entries, environ={})
    assert not report.created and not report.changed
    assert mcp_json.read_text() == first
    assert mcp_json.stat().st_mtime_ns == stat.st_mtime_ns  # not rewritten


def test_reinstall_preserves_operator_edits(catalog_dir, user_dir, mcp_json):
    _write(catalog_dir, "demo", _stdio())
    entries = load_catalog_dir(catalog_dir, user_dir=user_dir)
    catalog_install("demo", mcp_json_path=mcp_json, entries=entries, environ={})

    # operator edits: enable, tighten trust, change tier, add an env var,
    # narrow tools, bump timeouts, add a custom key
    doc = json.loads(mcp_json.read_text())
    s = doc["servers"]["demo"]
    s["enabled"] = True
    s["trust"] = "untrusted"
    s["tier"] = "hot"
    s["env"]["DEMO_EXTRA"] = "1"
    s["tools"] = {"include": ["read_*"]}
    s["timeouts"] = {"connect_s": 5.0, "call_s": 10.0}
    s["custom_note"] = "keep me"
    mcp_json.write_text(json.dumps(doc))

    # catalog update: new pin
    over = _stdio()
    over["transport"]["args"] = ["-y", "demo-mcp@2.0.0"]
    over["transport"]["version"] = "2.0.0"
    over["verified"] = "2026-09-03"
    _write(catalog_dir, "demo", over)
    entries = load_catalog_dir(catalog_dir, user_dir=user_dir)
    report = catalog_install("demo", mcp_json_path=mcp_json, entries=entries, environ={})
    assert report.changed and not report.created
    s = json.loads(mcp_json.read_text())["servers"]["demo"]
    assert s["args"] == ["-y", "demo-mcp@2.0.0"]  # pin applied
    assert s["source"] == {"catalog": "demo@2026-09-03"}
    assert s["enabled"] is True and s["trust"] == "untrusted" and s["tier"] == "hot"
    assert s["env"]["DEMO_EXTRA"] == "1" and s["env"]["DEMO_API_KEY"] == "${DEMO_API_KEY}"
    assert s["tools"] == {"include": ["read_*"]}
    assert s["timeouts"] == {"connect_s": 5.0, "call_s": 10.0}
    assert s["custom_note"] == "keep me"
    # enabled+secret missing -> the missing-secret step still leads
    assert report.next_steps[0].startswith("set DEMO_API_KEY in")


def test_reinstall_oauth_keeps_recorded_oauth_state_and_tool_edits(catalog_dir, user_dir, mcp_json):
    _write(catalog_dir, "remote", _oauth())
    entries = load_catalog_dir(catalog_dir, user_dir=user_dir)
    catalog_install("remote", mcp_json_path=mcp_json, entries=entries, environ={})
    doc = json.loads(mcp_json.read_text())
    doc["servers"]["remote"]["oauth"]["client_id"] = "dcr-issued-id"
    doc["servers"]["remote"]["oauth"]["client_name"] = "Codex"
    doc["servers"]["remote"]["tools"] = {"exclude": []}
    doc["servers"]["remote"]["enabled"] = True
    mcp_json.write_text(json.dumps(doc))

    over = _oauth()
    over["auth"]["oauth"]["scope"] = "mcp:connect mcp:write"
    over["auth"]["oauth"]["callback_port"] = 3118  # new hint -> fills the gap
    over["tools"] = {"default_excluded": ["noisy_*", "other_*"]}
    _write(catalog_dir, "remote", over)
    entries = load_catalog_dir(catalog_dir, user_dir=user_dir)
    report = catalog_install("remote", mcp_json_path=mcp_json, entries=entries, environ={})
    s = json.loads(mcp_json.read_text())["servers"]["remote"]
    # Recorded OAuth state always wins over manifest hints (a DCR-issued
    # client_id or an operator's client_name is never clobbered); hints
    # only fill keys that are absent.
    assert s["oauth"] == {
        "client_id": "dcr-issued-id", "client_name": "Codex",
        "scope": "mcp:connect", "callback_port": 3118,
    }
    assert "tools" not in s or s["tools"].get("exclude", []) == []  # operator's empty exclude wins
    assert s["enabled"] is True
    assert report.next_steps == ["/mcp login remote"]


def test_install_custom_server_name_and_other_entries_untouched(catalog_dir, user_dir, mcp_json):
    mcp_json.write_text(json.dumps({
        "version": 1,
        "note": "top-level passthrough",
        "servers": {"existing": {"transport": "stdio", "command": "echo", "enabled": True}},
    }))
    _write(catalog_dir, "demo", _stdio())
    entries = load_catalog_dir(catalog_dir, user_dir=user_dir)
    report = catalog_install("demo", mcp_json_path=mcp_json, server_name="demo-eu",
                             entries=entries, environ={})
    assert report.server_name == "demo-eu"
    assert report.next_steps[0].endswith("/mcp enable demo-eu")
    doc = json.loads(mcp_json.read_text())
    assert set(doc["servers"]) == {"existing", "demo-eu"}
    assert doc["note"] == "top-level passthrough"
    assert doc["servers"]["existing"]["enabled"] is True


def test_install_unknown_entry_raises(catalog_dir, user_dir, mcp_json):
    entries = load_catalog_dir(catalog_dir, user_dir=user_dir)
    with pytest.raises(CatalogError):
        catalog_install("nope", mcp_json_path=mcp_json, entries=entries)
    assert not mcp_json.exists()


def test_install_loads_from_dirs_when_entries_not_given(catalog_dir, user_dir, mcp_json):
    _write(catalog_dir, "demo", _stdio())
    over = _stdio()
    over["transport"]["args"] = ["-y", "demo-mcp@3.0.0"]
    over["transport"]["version"] = "3.0.0"
    _write(user_dir, "demo", over)
    report = catalog_install(
        "demo", mcp_json_path=mcp_json, catalog_dir=catalog_dir, user_dir=user_dir, environ={},
    )
    assert report.server["args"] == ["-y", "demo-mcp@3.0.0"]  # user override won


def test_install_missing_env_checks_presence_only(catalog_dir, user_dir, mcp_json, monkeypatch):
    _write(catalog_dir, "demo", _stdio())
    entries = load_catalog_dir(catalog_dir, user_dir=user_dir)
    monkeypatch.setenv("DEMO_API_KEY", "")  # present but empty counts as set
    monkeypatch.delenv("DEMO_REGION", raising=False)
    report = catalog_install("demo", mcp_json_path=mcp_json, entries=entries)
    assert report.missing_env == []
    assert report.missing_optional_env == ["DEMO_REGION"]


def test_install_args_env_refs_counted_as_required(catalog_dir, user_dir, mcp_json):
    body = _stdio()
    body["transport"]["args"] = ["-y", "demo-mcp@1.2.3", "${DEMO_ROOT}"]
    body["auth"] = {"type": "none",
                    "env": [{"name": "DEMO_ROOT", "secret": False, "required": True}]}
    _write(catalog_dir, "demo", body)
    entries = load_catalog_dir(catalog_dir, user_dir=user_dir)
    report = catalog_install("demo", mcp_json_path=mcp_json, entries=entries, environ={})
    assert report.auth == "env" and report.missing_env == ["DEMO_ROOT"]
    assert report.server["args"][-1] == "${DEMO_ROOT}"
    assert report.server["env"] == {"DEMO_MODE": "quiet", "DEMO_ROOT": "${DEMO_ROOT}"}


def test_install_every_shipped_entry_into_tmp_mcp_json(mcp_json):
    entries = load_catalog_dir(SHIPPED, include_user=False)
    for name in entries:
        report = catalog_install(name, mcp_json_path=mcp_json, entries=entries, environ={})
        assert report.created, name
    doc = json.loads(mcp_json.read_text())
    assert set(doc["servers"]) == set(entries)
    loaded = load_catalog(mcp_json)
    assert set(loaded.specs) == set(entries)  # none rejected by config.py validation
    for name, spec in loaded.specs.items():
        assert spec.enabled is False and spec.trust == "standard", name
    # second pass is a pure no-op
    before = mcp_json.read_text()
    for name in entries:
        again = catalog_install(name, mcp_json_path=mcp_json, entries=entries, environ={})
        assert not again.changed
    assert mcp_json.read_text() == before
    # the file we wrote lives under tmp_path, never under the real home
    assert Path(os.path.expanduser("~/.freyja")) not in mcp_json.resolve().parents
