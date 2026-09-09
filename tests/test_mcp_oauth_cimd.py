"""CIMD eligibility (default OFF), URL validation, and the shipped document's
consistency with the pinned-port pool."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.shared.auth import OAuthMetadata

from bridge.mcp.oauth import callback as cb
from bridge.mcp.oauth import cimd
from bridge.mcp.oauth.callback import CIMD_PORTS
from bridge.mcp.oauth.cimd import (
    CIMD_DOCUMENT_RELPATH,
    cimd_provider_kwargs,
    effective_cimd_url,
    expected_document_redirect_uris,
    is_valid_cimd_url,
    maybe_use_cimd,
    server_declined_cimd,
)
from bridge.mcp.oauth.settings import OAuthSettings
from bridge.mcp.oauth.storage import FreyjaTokenStorage, write_json

REPO_ROOT = Path(__file__).resolve().parents[1]
URL = "https://me.example/freyja/oauth/client-metadata.json"


@pytest.fixture(autouse=True)
def _reset_ports(monkeypatch):
    cb.reset_port_state_for_tests()
    # Never actually bind the pinned ports in unit tests — the dev box may use them.
    monkeypatch.setattr(cb, "reserve_fixed_port", lambda port: True)
    yield
    cb.reset_port_state_for_tests()


# ---------------------------------------------------------------------------
# Shipped document
# ---------------------------------------------------------------------------


def test_document_exists_and_matches_pinned_ports():
    doc_path = REPO_ROOT / CIMD_DOCUMENT_RELPATH
    assert doc_path.exists(), doc_path
    doc = json.loads(doc_path.read_text())
    assert doc["client_name"] == "Freyja"
    assert doc["application_type"] == "native"
    assert doc["token_endpoint_auth_method"] == "none"
    assert doc["grant_types"] == ["authorization_code", "refresh_token"]
    assert doc["response_types"] == ["code"]
    assert sorted(doc["redirect_uris"]) == sorted(expected_document_redirect_uris())
    ports = {int(u.rsplit(":", 1)[1].split("/")[0]) for u in doc["redirect_uris"]}
    assert ports == set(CIMD_PORTS)
    # client_id must be an https URL with a path (draft section 3)
    assert is_valid_cimd_url(doc["client_id"])


def test_cimd_is_off_by_default():
    assert cimd.FREYJA_CLIENT_METADATA_URL is None
    assert effective_cimd_url(OAuthSettings()) is None
    assert maybe_use_cimd(OAuthSettings()) is None
    assert cimd_provider_kwargs(OAuthSettings()) == {}


def test_cimd_document_url_constant_when_set_is_valid(monkeypatch):
    # When the operator flips the constant, the default path turns on.
    monkeypatch.setattr(cimd, "FREYJA_CLIENT_METADATA_URL", URL)
    assert effective_cimd_url(OAuthSettings()) == URL
    assert effective_cimd_url(OAuthSettings(cimd=False)) is None
    # explicit per-server URL overrides the constant
    assert effective_cimd_url(OAuthSettings(client_metadata_url="https://x.example/a.json")) == (
        "https://x.example/a.json"
    )


# ---------------------------------------------------------------------------
# URL validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url,ok", [
    ("https://me.example/freyja/cimd.json", True),
    ("https://me.example/", False),            # root path rejected by SDK
    ("https://me.example", False),
    ("http://me.example/cimd.json", False),    # https required
    ("https://user:pw@me.example/cimd.json", False),
    ("https://me.example/cimd.json#frag", False),
    ("https://me.example/../cimd.json", False),
    ("https://me.example/./x.json", False),
    ("", False),
    (None, False),
])
def test_is_valid_cimd_url(url, ok):
    assert is_valid_cimd_url(url) is ok


# ---------------------------------------------------------------------------
# Eligibility gating
# ---------------------------------------------------------------------------


def test_explicit_client_metadata_url_enables_cimd_and_pins_a_port():
    result = maybe_use_cimd(OAuthSettings(client_metadata_url=URL))
    assert result is not None
    url, port = result
    assert url == URL
    assert port in CIMD_PORTS
    settings = OAuthSettings(client_metadata_url=URL)
    settings.cimd_url = url
    assert cimd_provider_kwargs(settings) == {"client_metadata_url": URL}


def test_two_servers_get_distinct_pinned_ports():
    _, p1 = maybe_use_cimd(OAuthSettings(client_metadata_url=URL))
    _, p2 = maybe_use_cimd(OAuthSettings(client_metadata_url=URL))
    assert p1 != p2


@pytest.mark.parametrize("settings", [
    OAuthSettings(client_metadata_url=URL, cimd=False),
    OAuthSettings(client_metadata_url=URL, client_id="pre"),
    OAuthSettings(client_metadata_url=URL, client_secret="s"),
    OAuthSettings(client_metadata_url=URL, client_name="Claude Code"),
    OAuthSettings(client_metadata_url=URL, token_endpoint_auth_method="client_secret_post"),
    OAuthSettings(client_metadata_url=URL, redirect_uri="https://proxy/cb"),
    OAuthSettings(client_metadata_url=URL, redirect_port=5555),
    OAuthSettings(client_metadata_url=URL, redirect_host="myhost.local"),
    OAuthSettings(client_metadata_url="http://insecure/x.json"),
])
def test_ineligible_settings_fall_back_to_dcr(settings):
    assert maybe_use_cimd(settings) is None
    assert cb.assigned_cimd_ports() == ()


def test_localhost_redirect_host_is_allowed():
    settings = OAuthSettings(client_metadata_url=URL, redirect_host="localhost")
    assert maybe_use_cimd(settings) is not None


def test_cached_registration_blocks_cimd(tmp_path):
    s = FreyjaTokenStorage("acme", root=tmp_path)
    write_json(s.client_info_path(), {"client_id": "dcr-1", "redirect_uris": ["http://127.0.0.1:1/callback"]})
    assert maybe_use_cimd(OAuthSettings(client_metadata_url=URL), s) is None


def test_rejection_marker_blocks_cimd(tmp_path):
    s = FreyjaTokenStorage("acme", root=tmp_path)
    s.mark_cimd_rejected()
    assert maybe_use_cimd(OAuthSettings(client_metadata_url=URL), s) is None
    s.remove()
    assert maybe_use_cimd(OAuthSettings(client_metadata_url=URL), s) is not None


def _meta(supported: bool | None) -> OAuthMetadata:
    kwargs = dict(
        issuer="https://as.example", authorization_endpoint="https://as.example/a",
        token_endpoint="https://as.example/t", response_types_supported=["code"],
    )
    if supported is not None:
        kwargs["client_id_metadata_document_supported"] = supported
    return OAuthMetadata(**kwargs)


def test_server_declined_cimd_uses_cached_metadata(tmp_path):
    s = FreyjaTokenStorage("acme", root=tmp_path)
    assert server_declined_cimd(None) is False
    assert server_declined_cimd(s) is False  # unknown server -> optimistic pin
    s.save_oauth_metadata(_meta(False))
    assert server_declined_cimd(s) is True
    assert maybe_use_cimd(OAuthSettings(client_metadata_url=URL), s) is None
    s.save_oauth_metadata(_meta(None))
    assert server_declined_cimd(s) is True  # absent flag == not advertised
    s.save_oauth_metadata(_meta(True))
    assert server_declined_cimd(s) is False
    assert maybe_use_cimd(OAuthSettings(client_metadata_url=URL), s) is not None


def test_no_pinned_port_available_falls_back(monkeypatch):
    monkeypatch.setattr(cb, "reserve_fixed_port", lambda port: False)
    assert maybe_use_cimd(OAuthSettings(client_metadata_url=URL)) is None
