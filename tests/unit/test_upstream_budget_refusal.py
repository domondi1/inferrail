"""A downstream gateway refusing a request because of its *own* budget is
not an ordinary rate limit or auth failure. It is reported as
`UpstreamBudgetExceededError` (HTTP 402, `INFERRAIL_E014`), which isn't
retried and isn't told to retry, with the downstream status and type kept
in the error details. See docs/adr/0022-per-run-budget-declaration.md.

Bodies below are real ones captured from LiteLLM 1.103.0 and otari 0.4.0,
plus the documented shapes for Vercel AI Gateway and OpenAI.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from _budget_harness import WORK_ID, make_harness
from fastapi.testclient import TestClient

from inferrail.config.models import InferrailConfig
from inferrail.errors import (
    AuthenticationError,
    RateLimitError,
    UpstreamBudgetExceededError,
)
from inferrail.errors.codes import code_for
from inferrail.gateway import app as app_module
from inferrail.providers.anthropic import AnthropicProvider
from inferrail.providers.base import ChatMessage, NormalizedChatRequest
from inferrail.providers.openai import OpenAIProvider

LITELLM_429 = (429, {"error": {"message": "Budget has been exceeded! Key=key (sk-...XdVw) "
                               "Current cost: 0.00045, Max budget: 1e-07",
                               "type": "budget_exceeded", "param": None, "code": "429"}})
OTARI_403 = (403, {"detail": "API key has exceeded budget limit"})
VERCEL_402 = (402, {"error": {"message": "Project budget exceeded. Current spend: $200.00, "
                              "limit: $200.00.", "type": "quota_for_entity_exceeded"}})
OPENAI_QUOTA_429 = (429, {"error": {"message": "You exceeded your current quota",
                                    "type": "insufficient_quota", "code": "insufficient_quota"}})
PLAIN_RATE_LIMIT = (429, {"error": {"message": "Rate limit reached for requests",
                                    "type": "requests", "code": "rate_limit_exceeded"}})
PLAIN_AUTH = (401, {"error": {"message": "Incorrect API key provided",
                              "type": "invalid_request_error"}})
FORBIDDEN_OTHER = (403, {"detail": "Model not allowed for this key"})


def _openai(status: int, body: dict[str, Any]) -> OpenAIProvider:
    return OpenAIProvider(
        name="gw", api_key="k", base_url="http://gw.invalid/v1",
        client=httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _r: httpx.Response(status, json=body))),
    )


def _request() -> NormalizedChatRequest:
    return NormalizedChatRequest(model="m", messages=[ChatMessage(role="user", content="hi")])


@pytest.mark.parametrize(
    ("case", "upstream_type"),
    [(LITELLM_429, "budget_exceeded"), (OTARI_403, "budget"),
     (VERCEL_402, "quota_for_entity_exceeded"), (OPENAI_QUOTA_429, "insufficient_quota")],
)
async def test_downstream_budget_refusals_are_classified(
    case: tuple[int, dict[str, Any]], upstream_type: str
) -> None:
    status, body = case
    with pytest.raises(UpstreamBudgetExceededError) as exc_info:
        await _openai(status, body).complete(_request(), timeout=5)

    exc = exc_info.value
    assert exc.retryable is False
    assert exc.status_code == status
    assert exc.upstream_type == upstream_type
    assert code_for(exc).code == "INFERRAIL_E014"
    # Telemetry-safe: categorical only, never the upstream's free text.
    assert "Current cost" not in exc.safe_summary and "API key has" not in exc.safe_summary


async def test_streaming_refusal_is_classified_too() -> None:
    status, body = LITELLM_429
    with pytest.raises(UpstreamBudgetExceededError):
        async for _ in _openai(status, body).stream(_request(), timeout=5):
            pass


async def test_ordinary_rate_limit_is_unchanged() -> None:
    status, body = PLAIN_RATE_LIMIT
    with pytest.raises(RateLimitError) as exc_info:
        await _openai(status, body).complete(_request(), timeout=5)
    assert not isinstance(exc_info.value, UpstreamBudgetExceededError)


@pytest.mark.parametrize("case", [PLAIN_AUTH, FORBIDDEN_OTHER])
async def test_real_auth_failures_are_unchanged(case: tuple[int, dict[str, Any]]) -> None:
    status, body = case
    with pytest.raises(AuthenticationError):
        await _openai(status, body).complete(_request(), timeout=5)


async def test_anthropic_shaped_gateway_refusal_is_classified() -> None:
    from inferrail.providers.anthropic_base import AnthropicNormalizedRequest

    status, body = LITELLM_429
    provider = AnthropicProvider(
        name="gw", api_key="k", base_url="http://gw.invalid",
        client=httpx.AsyncClient(transport=httpx.MockTransport(
            lambda _r: httpx.Response(status, json=body))),
    )
    request = AnthropicNormalizedRequest(
        model="m", max_tokens=5, messages=[{"role": "user", "content": "hi"}]
    )
    with pytest.raises(UpstreamBudgetExceededError):
        await provider.complete(request, timeout=5)


async def test_refusal_is_not_retried_and_releases_the_reservation(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("1.00")
    status, body = LITELLM_429
    provider = _openai(status, body)
    calls: list[int] = []
    original = provider.complete

    async def counting(request: NormalizedChatRequest, *, timeout: float) -> Any:
        calls.append(1)
        return await original(request, timeout=timeout)

    provider.complete = counting  # type: ignore[method-assign]
    provider.name = "openai"
    engine = h.openai_engine(provider, max_retries=2)
    from inferrail.gateway.schemas import ChatCompletionRequest

    with pytest.raises(UpstreamBudgetExceededError):
        await engine.execute(
            ChatCompletionRequest.model_validate(
                {"model": "default", "max_tokens": 10,
                 "messages": [{"role": "user", "content": "hi"}]}
            ),
            attributes={"work_id": WORK_ID},
        )

    assert len(calls) == 1  # not retried
    assert h.budgets.list_reservations() == []  # an HTTP refusal was not billed


def test_gateway_returns_402_with_downstream_details(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("INFERRAIL_GATEWAY_TOKEN", raising=False)
    status, body = LITELLM_429
    provider = _openai(status, body)
    provider.name = "openai"
    config = InferrailConfig.model_validate(
        {
            "providers": {"openai": {"type": "openai_compatible", "api_key_env": "K",
                                     "base_url": "http://gw.invalid/v1"}},
            "routes": {"default": {"provider": "openai", "model": "gpt-4o-mini"}},
            "telemetry": {"sink": "none"},
            "receipts": {"sink": "sqlite", "path": str(tmp_path / "r.db")},
        }
    )
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {"openai": provider})
    monkeypatch.setattr(app_module, "build_anthropic_providers", lambda cfg, **_kw: {})
    client = TestClient(app_module.create_app(config))

    response = client.post(
        "/v1/chat/completions",
        json={"model": "default", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 402
    error = response.json()["error"]
    assert error["code"] == "INFERRAIL_E014"
    assert error["details"] == {"upstream_status": "429", "upstream_type": "budget_exceeded"}
    assert "won't help" in error["remediation"]
