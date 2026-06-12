"""Regression tests for mid-stream SSE error retryability.

A streaming /v1/messages call connects with HTTP 200, then the API may
deliver an ``error`` SSE event mid-stream (e.g. ``api_error`` — the
500-equivalent, transient). The anthropic SDK raises these via
``_make_status_error(..., response=self.response)`` where the response
is the already-connected stream — so the exception carries
``status_code=200`` and a Python-dict-repr message (single quotes).

Before the fix this laundered the error past every retry gate:
``_convert_api_error`` decided retryability solely from the status code
(200 → retryable=False), and the message classifiers in engine.errors
expected compact JSON (``"type":"api_error"``) or a leading 3-digit
status — so a transient server fault killed the turn on attempt 1
(session-mq8omxjv, req_011Cbx9JGPovKmKXyz9781pR).

These tests pin the body-type dispatch in the provider and the
quote-agnostic classifiers.
"""

from __future__ import annotations

import httpx
import pytest

from engine.anthropic_provider import AnthropicConfig, AnthropicProvider
from engine.errors import (
    classify_failover_reason,
    is_api_internal_error,
    is_retryable_error,
)
from engine.providers import (
    AuthenticationError,
    ProviderError,
    RateLimitError,
)


# The exact body shape from the observed failure.
_API_ERROR_BODY = {
    "type": "error",
    "error": {
        "details": None,
        "type": "api_error",
        "message": "Internal server error",
    },
    "request_id": "req_011Cbx9JGPovKmKXyz9781pR",
}


@pytest.fixture()
def provider() -> AnthropicProvider:
    return AnthropicProvider(AnthropicConfig(api_key="test-key"))


def _midstream_error(provider: AnthropicProvider, body: dict):
    """Raise the error exactly as the SDK's _streaming.py does for an
    SSE ``error`` event: dict-repr message, original 200 stream response."""
    err_msg = f"{body}"  # f-string of a dict → single-quoted Python repr
    response = httpx.Response(
        200,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )
    return provider._client._make_status_error(
        err_msg, body=body, response=response
    )


def _http_error(provider: AnthropicProvider, status: int, body: dict | None = None):
    """An ordinary HTTP-level error (status known at connect time)."""
    response = httpx.Response(
        status,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )
    return provider._client._make_status_error(
        f"{body}" if body else f"{status} error", body=body, response=response
    )


# ============================================================================
# Provider conversion: _convert_api_error
# ============================================================================

def test_midstream_api_error_is_retryable(provider):
    """The observed failure: api_error over a 200 stream must be retryable."""
    err = _midstream_error(provider, _API_ERROR_BODY)
    assert err.status_code == 200  # precondition: status laundered by SDK

    converted = provider._convert_api_error(err)
    assert isinstance(converted, ProviderError)
    assert converted.retryable is True
    assert converted.code == "api_error"


def test_midstream_overloaded_error_is_retryable(provider):
    body = {
        "type": "error",
        "error": {"type": "overloaded_error", "message": "Overloaded"},
    }
    converted = provider._convert_api_error(_midstream_error(provider, body))
    assert converted.retryable is True


def test_midstream_rate_limit_error_maps_to_rate_limit(provider):
    body = {
        "type": "error",
        "error": {"type": "rate_limit_error", "message": "Rate limited"},
    }
    converted = provider._convert_api_error(_midstream_error(provider, body))
    assert isinstance(converted, RateLimitError)
    assert converted.retryable is True


def test_midstream_invalid_request_stays_non_retryable(provider):
    """Body-type dispatch must not loosen genuinely fatal errors."""
    body = {
        "type": "error",
        "error": {"type": "invalid_request_error", "message": "bad field"},
    }
    converted = provider._convert_api_error(_midstream_error(provider, body))
    assert converted.retryable is False


def test_http_500_still_retryable(provider):
    """Regression guard: the HTTP-level path is unchanged."""
    converted = provider._convert_api_error(_http_error(provider, 500))
    assert converted.retryable is True


def test_http_401_still_authentication_error(provider):
    converted = provider._convert_api_error(_http_error(provider, 401))
    assert isinstance(converted, AuthenticationError)


def test_http_400_still_non_retryable(provider):
    converted = provider._convert_api_error(
        _http_error(provider, 400, {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}})
    )
    assert converted.retryable is False


# ============================================================================
# Message classifiers: engine.errors
# ============================================================================

# str() of the converted ProviderError — what the runner actually classifies.
_DICT_REPR_MESSAGE = str(_API_ERROR_BODY)
_COMPACT_JSON_MESSAGE = (
    '{"type":"error","error":{"type":"api_error",'
    '"message":"Internal server error"}}'
)


def test_is_api_internal_error_matches_dict_repr():
    assert is_api_internal_error(_DICT_REPR_MESSAGE) is True


def test_is_api_internal_error_matches_compact_json():
    assert is_api_internal_error(_COMPACT_JSON_MESSAGE) is True


def test_is_api_internal_error_ignores_other_types():
    assert is_api_internal_error("{'type': 'overloaded_error'}") is False
    assert is_api_internal_error("plain failure") is False
    assert is_api_internal_error("") is False


def test_classify_failover_reason_dict_repr_is_timeout():
    """Was None before the fix — the cause of the ``reason=None`` log."""
    assert classify_failover_reason(_DICT_REPR_MESSAGE) == "timeout"


def test_classify_failover_reason_compact_json_is_timeout():
    assert classify_failover_reason(_COMPACT_JSON_MESSAGE) == "timeout"


def test_is_retryable_error_dict_repr():
    """Was False before the fix — half of the runner's retry gate."""
    assert is_retryable_error(_DICT_REPR_MESSAGE) is True


# ============================================================================
# End to end: the runner's retry gate
# ============================================================================

def test_runner_retry_gate_passes_for_midstream_api_error(provider):
    """The exact gate from runner._handle_provider_error:
    ``if not is_retryable_error(str(error)) and not error.retryable``
    must NOT fire (i.e. the error must be retried)."""
    converted = provider._convert_api_error(
        _midstream_error(provider, _API_ERROR_BODY)
    )
    non_retryable = (
        not is_retryable_error(str(converted)) and not converted.retryable
    )
    assert non_retryable is False
