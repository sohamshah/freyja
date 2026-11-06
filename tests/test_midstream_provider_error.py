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


def _body(error_type: str, message: str) -> dict:
    return {"type": "error", "error": {"type": error_type, "message": message}}


@pytest.fixture()
def provider() -> AnthropicProvider:
    return AnthropicProvider(AnthropicConfig(api_key="test-key"))


def _sdk_error(provider: AnthropicProvider, status: int, body: dict | None):
    """Raise the error as the SDK does. Status 200 is the mid-stream case
    (_streaming.py: dict-repr message, the connected stream's response);
    any other status is an ordinary HTTP-level error."""
    response = httpx.Response(
        status,
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"),
    )
    return provider._client._make_status_error(
        f"{body}" if body else f"{status} error", body=body, response=response
    )


@pytest.mark.parametrize(
    "status,body,expected_type,retryable",
    [
        (200, _API_ERROR_BODY, ProviderError, True),
        (200, _body("overloaded_error", "Overloaded"), ProviderError, True),
        (200, _body("rate_limit_error", "Rate limited"), RateLimitError, True),
        (200, _body("invalid_request_error", "bad field"), ProviderError, False),
        (500, None, ProviderError, True),
        (401, None, AuthenticationError, False),
        (400, _body("invalid_request_error", "bad"), ProviderError, False),
    ],
    ids=[
        "midstream-api_error",
        "midstream-overloaded",
        "midstream-rate_limit",
        "midstream-invalid_request-stays-fatal",
        "http-500",
        "http-401",
        "http-400",
    ],
)
def test_convert_api_error_reads_the_body_type_when_the_status_lies(
    provider, status, body, expected_type, retryable
):
    """Mid-stream errors arrive with status 200, so retryability must come
    from the body type — without loosening genuinely fatal ones or
    changing the HTTP-level path."""
    err = _sdk_error(provider, status, body)
    assert err.status_code == status

    converted = provider._convert_api_error(err)

    assert isinstance(converted, expected_type)
    assert converted.retryable is retryable
    if body is _API_ERROR_BODY:
        assert converted.code == "api_error"


# str() of the converted ProviderError — what the runner actually classifies.
_DICT_REPR_MESSAGE = str(_API_ERROR_BODY)
_COMPACT_JSON_MESSAGE = (
    '{"type":"error","error":{"type":"api_error",'
    '"message":"Internal server error"}}'
)


def test_message_classifiers_read_dict_repr_like_compact_json():
    """Before the fix the dict repr classified as reason=None and not
    retryable — the other half of the runner's retry gate."""
    for message in (_DICT_REPR_MESSAGE, _COMPACT_JSON_MESSAGE):
        assert is_api_internal_error(message) is True, message
        assert classify_failover_reason(message) == "timeout", message
        assert is_retryable_error(message) is True, message
    for other in ("{'type': 'overloaded_error'}", "plain failure", ""):
        assert is_api_internal_error(other) is False, other
