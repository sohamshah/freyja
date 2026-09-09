"""FreyjaTokenStorage: 0600 atomic writes, absolute expires_at, metadata cache,
poison recovery, client-change invalidation, redaction. All under tmp roots."""

from __future__ import annotations

import json
import logging
import os
import stat
import time
from pathlib import Path

import pytest
from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken

from bridge.mcp.oauth import storage as st
from bridge.mcp.oauth.storage import (
    FreyjaTokenStorage,
    clear,
    default_token_root,
    iter_secret_values,
    list_servers,
    read_json,
    redact_payload,
    redact_secret,
    safe_filename,
    write_json,
)


@pytest.fixture(autouse=True)
def _never_touch_real_home(monkeypatch, tmp_path):
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path / "fake-home"))


@pytest.fixture
def root(tmp_path) -> Path:
    return tmp_path / "mcp-tokens"


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


# ---------------------------------------------------------------------------
# paths / roots
# ---------------------------------------------------------------------------


def test_default_root_honours_freyja_home(monkeypatch, tmp_path):
    monkeypatch.setenv("FREYJA_HOME", str(tmp_path / "h"))
    assert default_token_root() == tmp_path / "h" / "mcp-tokens"
    # No eager mkdir on the real home.
    assert not (tmp_path / "h").exists()


def test_default_root_falls_back_to_home_dot_freyja(monkeypatch, tmp_path):
    monkeypatch.delenv("FREYJA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_token_root() == tmp_path / ".freyja" / "mcp-tokens"


def test_safe_filename_strips_path_separators():
    assert safe_filename("../../etc/passwd") == "etc_passwd"
    assert safe_filename("my server/prod") == "my_server_prod"
    assert safe_filename("ok-name_1") == "ok-name_1"
    assert safe_filename("") == "default"
    assert len(safe_filename("x" * 500)) == 128


def test_storage_layout_is_per_server_dir(root):
    s = FreyjaTokenStorage("acme/prod", root=root)
    assert s.server_dir == root / "acme_prod"
    assert s.tokens_path().name == "tokens.json"
    assert s.client_info_path().name == "client.json"
    assert s.meta_path().name == "meta.json"
    assert s.cimd_rejected_path().name == "cimd-off"


# ---------------------------------------------------------------------------
# write_json / read_json
# ---------------------------------------------------------------------------


def test_write_json_creates_0600_file_and_0700_dirs(root):
    path = root / "srv" / "tokens.json"
    write_json(path, {"a": 1})
    assert _mode(path) == 0o600
    assert _mode(path.parent) == 0o700
    assert _mode(root) == 0o700
    assert read_json(path) == {"a": 1}


def test_write_json_is_atomic_and_leaves_no_tmp_files(root):
    path = root / "srv" / "tokens.json"
    write_json(path, {"v": 1})
    write_json(path, {"v": 2})
    assert read_json(path) == {"v": 2}
    leftovers = [p for p in path.parent.iterdir() if ".tmp." in p.name]
    assert leftovers == []


def test_write_json_never_inherits_umask(root, monkeypatch):
    old = os.umask(0o000)
    try:
        path = root / "srv" / "client.json"
        write_json(path, {"client_id": "x"})
        assert _mode(path) == 0o600
    finally:
        os.umask(old)


def test_write_json_cleans_tmp_on_failure(root, monkeypatch):
    path = root / "srv" / "tokens.json"

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        write_json(path, {"x": 1})
    assert not path.exists()
    assert [p for p in path.parent.iterdir()] == []


def test_read_json_rejects_corrupt_and_non_object(root, caplog):
    path = root / "srv" / "tokens.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    assert read_json(path) is None
    path.write_text("[1,2,3]")
    assert read_json(path) is None
    assert read_json(root / "missing.json") is None


def test_secure_dir_refuses_root_and_shallow_paths(tmp_path):
    # Must not raise and must not chmod "/" or a top-level dir.
    st.secure_dir(Path("/"))
    st.secure_dir(Path("/tmp"))
    target = tmp_path / "deep" / "dir"
    target.mkdir(parents=True)
    st.secure_dir(target)
    assert _mode(target) == 0o700


# ---------------------------------------------------------------------------
# tokens
# ---------------------------------------------------------------------------


async def test_token_roundtrip_persists_absolute_expires_at(root):
    s = FreyjaTokenStorage("acme", root=root)
    assert await s.get_tokens() is None
    assert not s.has_cached_tokens()
    before = time.time()
    await s.set_tokens(
        OAuthToken(access_token="AT", refresh_token="RT", expires_in=3600, scope="a b")
    )
    raw = json.loads(s.tokens_path().read_text())
    assert raw["access_token"] == "AT"
    assert before + 3590 <= raw["expires_at"] <= time.time() + 3600
    assert _mode(s.tokens_path()) == 0o600
    assert s.has_cached_tokens()
    assert s.expires_at() == raw["expires_at"]

    loaded = await s.get_tokens()
    assert loaded is not None
    assert loaded.access_token == "AT"
    assert loaded.refresh_token == "RT"
    assert loaded.scope == "a b"
    assert 3590 <= loaded.expires_in <= 3600


async def test_get_tokens_rewrites_expires_in_from_expires_at(root):
    s = FreyjaTokenStorage("acme", root=root)
    # Simulate a token written 2 hours ago with a 1 hour lifetime: the stored
    # expires_in still says 3600 but expires_at is in the past.
    write_json(s.tokens_path(), {
        "access_token": "AT", "token_type": "Bearer", "expires_in": 3600,
        "refresh_token": "RT", "expires_at": time.time() - 3600,
    })
    loaded = await s.get_tokens()
    assert loaded is not None
    assert loaded.expires_in == 0  # expired-by-expires_at wins over expires_in


async def test_get_tokens_partial_remaining(root):
    s = FreyjaTokenStorage("acme", root=root)
    write_json(s.tokens_path(), {
        "access_token": "AT", "expires_in": 3600, "expires_at": time.time() + 100,
    })
    loaded = await s.get_tokens()
    assert 95 <= loaded.expires_in <= 100


async def test_legacy_tokens_without_expires_at_use_file_mtime(root):
    s = FreyjaTokenStorage("acme", root=root)
    write_json(s.tokens_path(), {"access_token": "AT", "expires_in": 60})
    # Pretend the file was written 10 minutes ago.
    old = time.time() - 600
    os.utime(s.tokens_path(), (old, old))
    loaded = await s.get_tokens()
    assert loaded.expires_in == 0
    # Self-heals: the next set_tokens writes expires_at.
    await s.set_tokens(loaded)
    assert "expires_at" in json.loads(s.tokens_path().read_text())


async def test_tokens_without_expiry_are_left_alone(root):
    s = FreyjaTokenStorage("acme", root=root)
    await s.set_tokens(OAuthToken(access_token="AT"))
    raw = json.loads(s.tokens_path().read_text())
    assert "expires_at" not in raw
    assert s.expires_at() is None
    loaded = await s.get_tokens()
    assert loaded.expires_in is None


async def test_corrupt_tokens_return_none_without_logging_values(root, caplog):
    s = FreyjaTokenStorage("acme", root=root)
    write_json(s.tokens_path(), {"access_token": 12345, "token_type": "weird"})
    with caplog.at_level(logging.DEBUG):
        assert await s.get_tokens() is None
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "corrupt tokens" in joined
    assert "12345" not in joined  # pydantic's error text (which echoes values) is not logged


async def test_get_tokens_tolerates_bad_expires_at(root):
    s = FreyjaTokenStorage("acme", root=root)
    write_json(s.tokens_path(), {"access_token": "AT", "expires_in": 100, "expires_at": "garbage"})
    loaded = await s.get_tokens()
    assert loaded is not None and loaded.access_token == "AT"


# ---------------------------------------------------------------------------
# client info
# ---------------------------------------------------------------------------


async def test_client_info_roundtrip_and_permissions(root):
    s = FreyjaTokenStorage("acme", root=root)
    assert await s.get_client_info() is None
    assert not s.has_cached_client_info()
    info = OAuthClientInformationFull(
        client_id="cid", redirect_uris=["http://127.0.0.1:4242/callback"],
        token_endpoint_auth_method="none",
    )
    await s.set_client_info(info)
    assert _mode(s.client_info_path()) == 0o600
    loaded = await s.get_client_info()
    assert loaded.client_id == "cid"
    assert loaded.token_endpoint_auth_method == "none"
    assert loaded.client_secret is None
    assert s.has_cached_client_info()
    assert s.read_client_info_raw()["client_id"] == "cid"


async def test_client_info_with_secret_is_coerced_to_client_secret_post(root):
    s = FreyjaTokenStorage("acme", root=root)
    # Supabase-style registration: secret present, method omitted (SDK default "none").
    info = OAuthClientInformationFull(
        client_id="cid", client_secret="shh", redirect_uris=["http://127.0.0.1:4242/callback"],
    )
    await s.set_client_info(info)
    on_disk = json.loads(s.client_info_path().read_text())
    assert on_disk["token_endpoint_auth_method"] == "client_secret_post"
    loaded = await s.get_client_info()
    assert loaded.token_endpoint_auth_method == "client_secret_post"


async def test_client_info_on_disk_none_method_with_secret_is_healed_on_read(root):
    s = FreyjaTokenStorage("acme", root=root)
    write_json(s.client_info_path(), {
        "client_id": "cid", "client_secret": "shh",
        "redirect_uris": ["http://127.0.0.1:4242/callback"], "token_endpoint_auth_method": "none",
    })
    loaded = await s.get_client_info()
    assert loaded.token_endpoint_auth_method == "client_secret_post"
    on_disk = json.loads(s.client_info_path().read_text())
    assert on_disk["token_endpoint_auth_method"] == "client_secret_post"


async def test_client_info_explicit_basic_method_is_preserved(root):
    s = FreyjaTokenStorage("acme", root=root)
    info = OAuthClientInformationFull(
        client_id="cid", client_secret="shh", redirect_uris=["http://127.0.0.1:4242/callback"],
        token_endpoint_auth_method="client_secret_basic",
    )
    await s.set_client_info(info)
    loaded = await s.get_client_info()
    assert loaded.token_endpoint_auth_method == "client_secret_basic"


async def test_corrupt_client_info_returns_none(root):
    s = FreyjaTokenStorage("acme", root=root)
    write_json(s.client_info_path(), {"redirect_uris": "not-a-list"})
    assert await s.get_client_info() is None


# ---------------------------------------------------------------------------
# AS metadata
# ---------------------------------------------------------------------------


def _meta(base="https://as.example") -> OAuthMetadata:
    return OAuthMetadata(
        issuer=base, authorization_endpoint=f"{base}/authorize",
        token_endpoint=f"{base}/token", registration_endpoint=f"{base}/register",
        response_types_supported=["code"],
    )


def test_oauth_metadata_roundtrip(root):
    s = FreyjaTokenStorage("acme", root=root)
    assert s.load_oauth_metadata() is None
    s.save_oauth_metadata(_meta())
    assert _mode(s.meta_path()) == 0o600
    loaded = s.load_oauth_metadata()
    assert str(loaded.token_endpoint) == "https://as.example/token"


def test_corrupt_metadata_returns_none(root):
    s = FreyjaTokenStorage("acme", root=root)
    write_json(s.meta_path(), {"issuer": "nope"})
    assert s.load_oauth_metadata() is None


# ---------------------------------------------------------------------------
# CIMD marker
# ---------------------------------------------------------------------------


def test_cimd_rejected_marker(root):
    s = FreyjaTokenStorage("acme", root=root)
    assert not s.cimd_rejected()
    s.mark_cimd_rejected()
    assert s.cimd_rejected()
    s.remove()
    assert not s.cimd_rejected()


# ---------------------------------------------------------------------------
# poison / invalidate / remove / snapshot
# ---------------------------------------------------------------------------


async def test_poison_client_registration_backs_up_and_removes_client_and_meta(root, caplog):
    s = FreyjaTokenStorage("acme", root=root)
    assert s.poison_client_registration() is False  # nothing to poison
    await s.set_client_info(OAuthClientInformationFull(
        client_id="dead", redirect_uris=["http://127.0.0.1:1/callback"], client_secret="s3cret",
    ))
    await s.set_tokens(OAuthToken(access_token="AT", refresh_token="RT"))
    s.save_oauth_metadata(_meta())
    with caplog.at_level(logging.DEBUG):
        assert s.poison_client_registration() is True
    assert not s.client_info_path().exists()
    assert not s.meta_path().exists()
    assert s.tokens_path().exists()  # tokens intentionally kept
    backup = s.client_info_path().with_name("client.json.bak")
    assert backup.exists()
    assert _mode(backup) == 0o600
    assert json.loads(backup.read_text())["client_id"] == "dead"
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "invalid_client" in joined
    assert "s3cret" not in joined


async def test_invalidate_on_client_change(root, caplog):
    s = FreyjaTokenStorage("acme", root=root)
    # No client on disk -> no-op
    assert s.invalidate_on_client_change("new", None) is False
    await s.set_client_info(OAuthClientInformationFull(
        client_id="old", redirect_uris=["http://127.0.0.1:1/callback"],
        token_endpoint_auth_method="none",
    ))
    await s.set_tokens(OAuthToken(access_token="AT"))
    s.save_oauth_metadata(_meta())
    # Same identity -> keep everything
    assert s.invalidate_on_client_change("old", None) is False
    assert s.tokens_path().exists()
    # Changed client_id -> drop tokens + meta, keep client.json (caller rewrites it)
    with caplog.at_level(logging.WARNING):
        assert s.invalidate_on_client_change("new", None) is True
    assert not s.tokens_path().exists()
    assert not s.meta_path().exists()
    assert s.client_info_path().exists()
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert "'old' -> 'new'" in joined
    assert "/mcp login acme" in joined


async def test_invalidate_on_secret_change_only(root):
    s = FreyjaTokenStorage("acme", root=root)
    write_json(s.client_info_path(), {
        "client_id": "same", "client_secret": "old-secret",
        "redirect_uris": ["http://127.0.0.1:1/callback"],
    })
    await s.set_tokens(OAuthToken(access_token="AT"))
    assert s.invalidate_on_client_change("same", "old-secret") is False
    assert s.tokens_path().exists()
    assert s.invalidate_on_client_change("same", "new-secret") is True
    assert not s.tokens_path().exists()


async def test_remove_list_servers_and_clear(root):
    a = FreyjaTokenStorage("alpha", root=root)
    b = FreyjaTokenStorage("beta", root=root)
    await a.set_tokens(OAuthToken(access_token="AT"))
    b.mark_cimd_rejected()
    (root / "empty-dir").mkdir()
    (root / "stray-file").write_text("x")
    assert list_servers(root) == ["alpha", "beta"]
    a.remove()
    assert list_servers(root) == ["beta"]
    assert not a.server_dir.exists()
    clear("beta", root)
    assert list_servers(root) == []
    assert list_servers(root / "does-not-exist") == []


async def test_snapshot_and_restore(root):
    s = FreyjaTokenStorage("acme", root=root)
    assert s.snapshot() == {}
    await s.set_tokens(OAuthToken(access_token="AT"))
    s.save_oauth_metadata(_meta())
    snap = s.snapshot()
    assert set(snap) == {"tokens.json", "meta.json"}
    s.remove()
    assert not s.has_cached_tokens()
    s.restore(snap)
    assert (await s.get_tokens()).access_token == "AT"
    assert _mode(s.tokens_path()) == 0o600
    assert s.load_oauth_metadata() is not None


async def test_restore_only_if_absent_skips_when_newer_state_exists(root):
    s = FreyjaTokenStorage("acme", root=root)
    await s.set_tokens(OAuthToken(access_token="OLD"))
    snap = s.snapshot()
    s.remove()
    await s.set_tokens(OAuthToken(access_token="NEW"))
    s.restore(snap, only_if_absent=True)
    assert (await s.get_tokens()).access_token == "NEW"
    s.restore(snap, only_if_absent=False)
    assert (await s.get_tokens()).access_token == "OLD"


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------


def test_redact_secret_never_contains_the_value():
    for value in ("hunter2", "at-abcdefghijklmnop", "x"):
        out = redact_secret(value)
        assert value not in out
        assert out.startswith("<redacted sha256:")
    assert redact_secret(None) == "<none>"
    assert redact_secret("") == "<empty>"
    # Deterministic so two log lines can be correlated.
    assert redact_secret("same") == redact_secret("same")
    assert redact_secret("a") != redact_secret("b")


def test_redact_payload_walks_nested_structures():
    payload = {
        "access_token": "AT-1", "refresh_token": "RT-1", "client_secret": "CS-1",
        "code": "CODE-1", "expires_in": 3600, "scope": "a b",
        "nested": {"id_token": "ID-1", "list": [{"Authorization": "Bearer AT-2"}, "plain"]},
    }
    out = redact_payload(payload)
    text = json.dumps(out)
    for secret in ("AT-1", "RT-1", "CS-1", "CODE-1", "ID-1", "AT-2"):
        assert secret not in text
    assert out["expires_in"] == 3600
    assert out["scope"] == "a b"
    assert out["nested"]["list"][1] == "plain"
    # Original untouched
    assert payload["access_token"] == "AT-1"


def test_iter_secret_values_collects_secret_shaped_strings():
    vals = set(iter_secret_values(
        {"access_token": "A", "expires_in": 1, "client_secret": "C"},
        None,
        {"refresh_token": "R", "scope": "s"},
    ))
    assert vals == {"A", "C", "R"}
