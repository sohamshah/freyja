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
    assert len(set(CIMD_PORTS)) == len(CIMD_PORTS) == 5
    assert all(1024 < p < 32768 for p in CIMD_PORTS)  # pinned, below the ephemeral floor
    # client_id must be an https URL with a path (draft section 3)
    assert is_valid_cimd_url(doc["client_id"])


def test_cimd_is_off_by_default_until_the_constant_or_a_server_url_is_set(monkeypatch):
    assert cimd.FREYJA_CLIENT_METADATA_URL is None
    assert effective_cimd_url(OAuthSettings()) is None
    assert maybe_use_cimd(OAuthSettings()) is None
    assert cimd_provider_kwargs(OAuthSettings()) == {}
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


def test_is_valid_cimd_url():
    for url, ok in (
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
    ):
        assert is_valid_cimd_url(url) is ok, url


# ---------------------------------------------------------------------------
# Eligibility gating
# ---------------------------------------------------------------------------


def test_explicit_client_metadata_url_enables_cimd_with_distinct_pinned_ports():
    result = maybe_use_cimd(OAuthSettings(client_metadata_url=URL))
    assert result is not None
    url, port = result
    assert url == URL
    assert port in CIMD_PORTS
    settings = OAuthSettings(client_metadata_url=URL)
    settings.cimd_url = url
    assert cimd_provider_kwargs(settings) == {"client_metadata_url": URL}
    _, p2 = maybe_use_cimd(OAuthSettings(client_metadata_url=URL))
    assert p2 != port
    # localhost is an allowed redirect host for CIMD.
    on_localhost = OAuthSettings(client_metadata_url=URL, redirect_host="localhost")
    assert maybe_use_cimd(on_localhost) is not None


def test_ineligible_settings_fall_back_to_dcr():
    for settings in (
        OAuthSettings(client_metadata_url=URL, cimd=False),
        OAuthSettings(client_metadata_url=URL, client_id="pre"),
        OAuthSettings(client_metadata_url=URL, client_secret="s"),
        OAuthSettings(client_metadata_url=URL, client_name="Claude Code"),
        OAuthSettings(client_metadata_url=URL, token_endpoint_auth_method="client_secret_post"),
        OAuthSettings(client_metadata_url=URL, redirect_uri="https://proxy/cb"),
        OAuthSettings(client_metadata_url=URL, redirect_port=5555),
        OAuthSettings(client_metadata_url=URL, redirect_host="myhost.local"),
        OAuthSettings(client_metadata_url="http://insecure/x.json"),
    ):
        assert maybe_use_cimd(settings) is None, settings
        assert cb.assigned_cimd_ports() == ()


def _meta(supported: bool | None) -> OAuthMetadata:
    kwargs = dict(
        issuer="https://as.example", authorization_endpoint="https://as.example/a",
        token_endpoint="https://as.example/t", response_types_supported=["code"],
    )
    if supported is not None:
        kwargs["client_id_metadata_document_supported"] = supported
    return OAuthMetadata(**kwargs)


def test_stored_state_blocks_cimd(tmp_path, monkeypatch):
    # maybe_use_cimd records its decision on the settings object, so each
    # check gets a fresh one.
    def settings() -> OAuthSettings:
        return OAuthSettings(client_metadata_url=URL)

    # A cached DCR registration keeps the DCR identity.
    s = FreyjaTokenStorage("acme", root=tmp_path)
    write_json(s.client_info_path(), {"client_id": "dcr-1", "redirect_uris": ["http://127.0.0.1:1/callback"]})
    assert maybe_use_cimd(settings(), s) is None
    # A rejection marker blocks until the state is removed.
    s2 = FreyjaTokenStorage("beta", root=tmp_path)
    s2.mark_cimd_rejected()
    assert maybe_use_cimd(settings(), s2) is None
    s2.remove()
    assert maybe_use_cimd(settings(), s2) is not None
    # Cached AS metadata that does not advertise support declines CIMD.
    s3 = FreyjaTokenStorage("gamma", root=tmp_path)
    assert server_declined_cimd(None) is False
    assert server_declined_cimd(s3) is False  # unknown server -> optimistic pin
    s3.save_oauth_metadata(_meta(False))
    assert server_declined_cimd(s3) is True
    assert maybe_use_cimd(settings(), s3) is None
    s3.save_oauth_metadata(_meta(None))
    assert server_declined_cimd(s3) is True  # absent flag == not advertised
    s3.save_oauth_metadata(_meta(True))
    assert server_declined_cimd(s3) is False
    assert maybe_use_cimd(settings(), s3) is not None
    # No pinned port bindable (and none already assigned to this process) -> DCR.
    cb.reset_port_state_for_tests()
    monkeypatch.setattr(cb, "reserve_fixed_port", lambda port: False)
    assert maybe_use_cimd(settings()) is None
