"""Router retry policy (audit F046).

_invoke_with_retry must NOT re-retry exceptions that carry an HTTP status (the
provider SDK already applied its own retry policy — 429/5xx with retry-after,
deterministic 4xx surfaced). It only retries uncategorized transport errors.
"""

from __future__ import annotations

import pytest
from kun.interface.llm.router import _invoke_with_retry, _should_retry_llm_error


class _StatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


class _ResponseStatusError(Exception):
    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.response = type("R", (), {"status_code": status_code})()


class _TransportError(Exception):
    """No HTTP status — e.g. a raw connection drop."""


class _CountingProvider:
    def __init__(self, exc: Exception | None) -> None:
        self.exc = exc
        self.calls = 0

    async def invoke(self, _request):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return "ok"


@pytest.mark.unit
@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422, 429, 500, 529])
def test_predicate_never_retries_status_bearing_errors(status: int) -> None:
    assert _should_retry_llm_error(_StatusError(status)) is False
    assert _should_retry_llm_error(_ResponseStatusError(status)) is False


@pytest.mark.unit
def test_predicate_retries_transport_errors() -> None:
    assert _should_retry_llm_error(_TransportError("conn reset")) is True


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invoke_does_not_retry_http_status_error() -> None:
    prov = _CountingProvider(_StatusError(400))
    with pytest.raises(_StatusError):  # reraise=True → real error, not RetryError
        await _invoke_with_retry(prov, None)
    assert prov.calls == 1  # no router-level retry


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invoke_does_not_retry_429_either() -> None:
    prov = _CountingProvider(_StatusError(429))
    with pytest.raises(_StatusError):
        await _invoke_with_retry(prov, None)
    assert prov.calls == 1  # delegated to the SDK, not amplified here


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invoke_retries_transport_error_up_to_three() -> None:
    prov = _CountingProvider(_TransportError("conn reset"))
    with pytest.raises(_TransportError):
        await _invoke_with_retry(prov, None)
    assert prov.calls == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_invoke_returns_result_on_success() -> None:
    prov = _CountingProvider(None)
    assert await _invoke_with_retry(prov, None) == "ok"
    assert prov.calls == 1
