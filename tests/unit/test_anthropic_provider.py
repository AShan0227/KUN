"""LT.OAUTH — AnthropicProvider auth-path 单测.

Verifies the three auth modes documented in ``anthropic_provider.py``:

  1. ofox proxy (KUN_OFOX_API_KEY set) — uses x-api-key + base_url override
  2. direct API key (sk-ant-api...) — uses x-api-key, no Bearer
  3. OAuth subscription token (sk-ant-oat...) — uses Authorization: Bearer +
     ``anthropic-beta: oauth-2025-04-20`` header, and api_key is nulled to
     prevent the SDK from also sending X-Api-Key from the env var.

The tests don't hit the network — they inspect the AsyncAnthropic client's
attributes (api_key, auth_token, default_headers) to confirm what would be
sent on the wire.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from kun.interface.llm.anthropic_provider import AnthropicProvider


class _FakeSettings:
    """Stand-in for ``kun.core.config.Settings`` covering only the fields the
    provider reads. We toggle ``ofox_api_key`` to simulate proxy-vs-direct.
    """

    def __init__(self, *, ofox_api_key: str = "", ofox_proxy_url: str = "https://api.ofox.ai"):
        self.ofox_api_key = ofox_api_key
        self.ofox_proxy_url = ofox_proxy_url


@pytest.fixture(autouse=True)
def _clear_anthropic_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test sets its own env vars; default state is unset."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)


def test_ofox_proxy_wins_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    """If KUN_OFOX_API_KEY is set, that path is used regardless of
    ANTHROPIC_API_KEY — ofox already proxies subscription auth."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-direct-should-be-ignored")
    fake_cfg = _FakeSettings(ofox_api_key="ofox-secret", ofox_proxy_url="https://ofox.test")

    with patch("kun.interface.llm.anthropic_provider.settings", return_value=fake_cfg):
        provider = AnthropicProvider(model_id="claude-opus-4-7", tier="top")

    client = provider._client
    # ofox path uses api_key (x-api-key header), not auth_token
    assert client.api_key == "ofox-secret"
    assert client.auth_token is None
    # base_url should reflect the ofox endpoint
    assert "ofox.test" in str(client.base_url)


def test_direct_api_key_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regular ``sk-ant-api...`` key uses standard x-api-key auth, no Bearer."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-regular-key")
    fake_cfg = _FakeSettings(ofox_api_key="")

    with patch("kun.interface.llm.anthropic_provider.settings", return_value=fake_cfg):
        provider = AnthropicProvider(model_id="claude-sonnet-4-6", tier="strong")

    client = provider._client
    assert client.api_key == "sk-ant-api03-regular-key"
    assert client.auth_token is None
    # No oauth-beta header on the regular path
    assert "anthropic-beta" not in (client.default_headers or {})


def test_oauth_subscription_token_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """OAuth tokens (``sk-ant-oat...``) flip to Bearer auth + oauth beta header,
    and api_key is nulled so the SDK doesn't also send X-Api-Key from env var.

    This is the regression that the curl test caught: with both X-Api-Key and
    Authorization headers present, the subscription endpoint returns 401.
    """
    oauth_token = "sk-ant-oat01-test-token-abc123"
    monkeypatch.setenv("ANTHROPIC_API_KEY", oauth_token)
    fake_cfg = _FakeSettings(ofox_api_key="")

    with patch("kun.interface.llm.anthropic_provider.settings", return_value=fake_cfg):
        provider = AnthropicProvider(model_id="claude-opus-4-7", tier="top")

    client = provider._client
    # The critical assertion: api_key MUST be None on OAuth path so the SDK's
    # ``_api_key_auth`` returns {} and only Bearer auth goes out.
    assert client.api_key is None
    assert client.auth_token == oauth_token
    # Beta header must be present so subscription endpoint accepts the token
    assert client.default_headers.get("anthropic-beta") == "oauth-2025-04-20"


def test_oauth_token_sdk_emits_bearer_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """Concrete header-build check: with the provider constructed in OAuth mode,
    the SDK's auth_headers property must emit ONLY Authorization (no X-Api-Key).

    Catches future regressions where someone re-introduces an api_key fallback
    on the OAuth branch.
    """
    oauth_token = "sk-ant-oat01-bearer-only-please"
    monkeypatch.setenv("ANTHROPIC_API_KEY", oauth_token)
    fake_cfg = _FakeSettings(ofox_api_key="")

    with patch("kun.interface.llm.anthropic_provider.settings", return_value=fake_cfg):
        provider = AnthropicProvider(model_id="claude-opus-4-7", tier="top")

    auth_headers = provider._client.auth_headers
    assert "Authorization" in auth_headers
    assert auth_headers["Authorization"] == f"Bearer {oauth_token}"
    # X-Api-Key must NOT be present — the dual-header bug.
    assert "X-Api-Key" not in auth_headers
    assert "x-api-key" not in {k.lower() for k in auth_headers}


def test_no_credentials_warning_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """When neither ofox nor ANTHROPIC_API_KEY is set, provider builds with
    placeholder api_key so call-time fails fast rather than crashing at import.
    """
    fake_cfg = _FakeSettings(ofox_api_key="")

    with patch("kun.interface.llm.anthropic_provider.settings", return_value=fake_cfg):
        provider = AnthropicProvider(model_id="claude-haiku-4-5-20251001", tier="cheap")

    client = provider._client
    assert client.api_key == "missing"
    assert client.auth_token is None
