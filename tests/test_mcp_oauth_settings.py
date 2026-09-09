"""OAuthSettings parsing: env expansion, inline-secret rejection, validation,
plus the ContextVar interactivity gates."""

from __future__ import annotations

import asyncio
import threading

import pytest

from bridge.mcp.config import McpConfigError, McpServerSpec
from bridge.mcp.oauth import gates
from bridge.mcp.oauth.gates import (
    OAuthCallbackTimeoutError,
    OAuthNeedsAuthError,
    OAuthNonInteractiveError,
    OAuthUserSkippedError,
    force_interactive_oauth,
    is_interactive,
    login_hint,
    raise_if_non_interactive,
    suppress_interactive_oauth,
)
from bridge.mcp.oauth.settings import (
    DEFAULT_CLIENT_NAME,
    DEFAULT_TIMEOUT_S,
    DEFAULT_TOKEN_USER_AGENT,
    OAuthSettings,
    parse_oauth_settings,
    validate_oauth_block,
)


def _spec(oauth=None, **kw) -> McpServerSpec:
    base = dict(name="acme", transport="http", url="https://mcp.example/mcp", auth="oauth")
    base.update(kw)
    return McpServerSpec(oauth=oauth, **base)


# ---------------------------------------------------------------------------
# parse_oauth_settings
# ---------------------------------------------------------------------------


def test_defaults_when_block_missing():
    s = parse_oauth_settings(_spec(None))
    assert s == OAuthSettings()
    assert s.effective_client_name == DEFAULT_CLIENT_NAME == "Freyja"
    assert s.effective_user_agent == DEFAULT_TOKEN_USER_AGENT
    assert s.timeout_s == DEFAULT_TIMEOUT_S == 300.0
    assert s.redirect_host == "127.0.0.1"
    assert s.cimd is None
    assert not s.preregistered


def test_full_block_parses_and_expands_env():
    env = {"ACME_ID": "id-123", "ACME_SECRET": "s3cret"}
    s = parse_oauth_settings(_spec({
        "client_id": "${ACME_ID}",
        "client_secret": "${ACME_SECRET}",
        "scope": " read write ",
        "redirect_port": "4242",
        "redirect_host": "localhost",
        "redirect_uri": "https://proxy.example/cb",
        "client_name": "My App",
        "client_metadata_url": "https://me.example/cimd.json",
        "cimd": False,
        "timeout_s": 42,
        "token_endpoint_auth_method": "client_secret_basic",
        "user_agent": "Custom/1.0",
        "application_type": "web",
        "future_key": {"x": 1},
    }), environ=env)
    assert s.client_id == "id-123"
    assert s.client_secret == "s3cret"
    assert s.preregistered
    assert s.scope == "read write"
    assert s.redirect_port == 4242
    assert s.redirect_host == "localhost"
    assert s.redirect_uri == "https://proxy.example/cb"
    assert s.client_name == "My App"
    assert s.client_metadata_url == "https://me.example/cimd.json"
    assert s.cimd is False
    assert s.timeout_s == 42.0
    assert s.token_endpoint_auth_method == "client_secret_basic"
    assert s.user_agent == "Custom/1.0"
    assert s.application_type == "web"
    assert s.extra == {"future_key": {"x": 1}}


def test_legacy_timeout_key_accepted():
    assert parse_oauth_settings(_spec({"timeout": 10})).timeout_s == 10.0


def test_inline_client_secret_is_rejected():
    with pytest.raises(McpConfigError) as ei:
        parse_oauth_settings(_spec({"client_secret": "sk-live-abcdef"}))
    assert "inline secret" in str(ei.value)
    assert ".env" in str(ei.value)
    assert "sk-live-abcdef" not in str(ei.value)


def test_missing_secret_env_var_is_needs_auth_not_crash():
    with pytest.raises(OAuthNeedsAuthError) as ei:
        parse_oauth_settings(_spec({"client_secret": "${NOPE_SECRET}"}), environ={})
    msg = str(ei.value)
    assert "NOPE_SECRET" in msg
    assert "/mcp login acme" in msg
    assert ei.value.server_name == "acme"


def test_secret_with_default_is_allowed():
    s = parse_oauth_settings(_spec({"client_secret": "${X_SECRET:-}"}), environ={})
    assert s.client_secret is None


def test_client_id_reference_missing_is_left_empty():
    s = parse_oauth_settings(_spec({"client_id": "${MISSING_ID}"}), environ={})
    assert s.client_id is None


@pytest.mark.parametrize("block,needle", [
    ("not-a-dict", "must be an object"),
    ({"client_secret": 5}, "must be a string"),
    ({"redirect_port": "abc"}, "integer"),
    ({"redirect_port": 70000}, "range"),
    ({"token_endpoint_auth_method": "private_key_jwt"}, "token_endpoint_auth_method"),
    ({"client_metadata_url": "http://insecure/cimd.json"}, "https"),
    ({"timeout_s": 0}, "positive"),
    ({"timeout": -1}, "positive"),
])
def test_validate_oauth_block_errors(block, needle):
    with pytest.raises(McpConfigError) as ei:
        validate_oauth_block("acme", block)
    assert needle in str(ei.value)
    assert "acme" in str(ei.value)


def test_validate_oauth_block_accepts_none_and_valid():
    validate_oauth_block("acme", None)
    validate_oauth_block("acme", {"client_secret": "${A_SECRET}", "redirect_port": 0})


def test_to_redacted_dict_hides_secret():
    s = parse_oauth_settings(_spec({"client_secret": "${S}"}), environ={"S": "topsecret"})
    d = s.to_redacted_dict()
    assert "topsecret" not in repr(d)
    assert d["client_secret"].startswith("<redacted")
    # The dataclass repr excludes the secret too.
    assert "topsecret" not in repr(s)
    assert s.client_secret == "topsecret"


# ---------------------------------------------------------------------------
# gates
# ---------------------------------------------------------------------------


def test_error_hierarchy():
    assert issubclass(OAuthNonInteractiveError, OAuthNeedsAuthError)
    assert issubclass(OAuthCallbackTimeoutError, OAuthNonInteractiveError)
    assert issubclass(OAuthUserSkippedError, OAuthNonInteractiveError)
    err = OAuthNeedsAuthError("x", server_name="acme")
    assert err.server_name == "acme"
    assert isinstance(err, RuntimeError)


def test_login_hint_wording():
    assert "/mcp login acme" in login_hint("acme")
    assert "/mcp login <server>" in login_hint(None)


def test_is_interactive_precedence(monkeypatch):
    # explicit flag decides when no gate is active
    assert is_interactive(True) is True
    assert is_interactive(False) is False
    # suppression wins over everything
    with suppress_interactive_oauth():
        assert is_interactive(True) is False
        with force_interactive_oauth():
            assert is_interactive(True) is False
    # force wins over explicit False
    with force_interactive_oauth():
        assert is_interactive(False) is True
    # default heuristic: stdin tty
    class _Stdin:
        def isatty(self):
            return True

    monkeypatch.setattr(gates.sys, "stdin", _Stdin())
    assert is_interactive() is True

    class _NoTTY:
        def isatty(self):
            raise ValueError("closed")

    monkeypatch.setattr(gates.sys, "stdin", _NoTTY())
    assert is_interactive() is False


def test_raise_if_non_interactive_message_has_hint():
    raise_if_non_interactive("lead.", server_name="acme", explicit=True)  # no raise
    with pytest.raises(OAuthNonInteractiveError) as ei:
        raise_if_non_interactive("Browser needed.", server_name="acme", explicit=False)
    msg = str(ei.value)
    assert msg.startswith("Browser needed.")
    assert "/mcp login acme" in msg
    assert ei.value.server_name == "acme"


def test_gates_restore_on_exit_even_after_exception():
    with pytest.raises(RuntimeError):
        with suppress_interactive_oauth():
            raise RuntimeError("boom")
    assert is_interactive(True) is True


async def test_suppression_propagates_across_run_coroutine_threadsafe():
    """The #35927 scenario: a background thread sets suppression then schedules
    the connect coroutine onto the loop. A ContextVar crosses that boundary."""
    loop = asyncio.get_running_loop()
    seen: dict[str, bool] = {}

    async def probe():
        seen["interactive"] = is_interactive(True)

    def worker():
        with suppress_interactive_oauth():
            fut = asyncio.run_coroutine_threadsafe(probe(), loop)
            fut.result(timeout=5)

    t = threading.Thread(target=worker)
    t.start()
    while t.is_alive():
        await asyncio.sleep(0.01)
    assert seen == {"interactive": False}
    assert is_interactive(True) is True  # the loop's own context is untouched
