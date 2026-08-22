"""End-to-end (through the real FastAPI app, via TestClient) tests for the
economic receipt feature: attribution capture, cost calculation, unknown
pricing, and — critically — the same payload-privacy adversarial tests
`test_gateway.py` applies to `InferenceEvent`, applied here to
`InferenceReceipt`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from inferrail.config.models import InferrailConfig
from inferrail.errors import AuthenticationError, InvalidRequestError
from inferrail.gateway import app as app_module
from inferrail.providers.base import NormalizedChatRequest, NormalizedChatResponse
from inferrail.receipts.schema import InferenceReceipt


class FakeProvider:
    name = "openai"

    def __init__(self, outcomes: list[Any] | None = None) -> None:
        self._outcomes = outcomes or [
            NormalizedChatResponse(
                content="ok", finish_reason="stop", prompt_tokens=842, completion_tokens=191
            )
        ]
        self.calls: list[NormalizedChatRequest] = []

    async def complete(
        self, request: NormalizedChatRequest, *, timeout: float
    ) -> NormalizedChatResponse:
        self.calls.append(request)
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class InMemoryReceiptSink:
    def __init__(self) -> None:
        self.receipts: list[InferenceReceipt] = []

    def emit(self, receipt: InferenceReceipt) -> None:
        self.receipts.append(receipt)


@pytest.fixture(autouse=True)
def _no_gateway_token_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INFERRAIL_GATEWAY_TOKEN", raising=False)


def _make_client(
    monkeypatch: pytest.MonkeyPatch,
    config: InferrailConfig,
    provider: FakeProvider,
    receipts: InMemoryReceiptSink,
) -> TestClient:
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {"openai": provider})
    monkeypatch.setattr(app_module, "build_receipt_sink", lambda cfg: receipts)
    app = app_module.create_app(config)
    return TestClient(app)


def _chat_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "default",
        "messages": [{"role": "user", "content": "hello"}],
    }
    body.update(overrides)
    return body


def test_receipt_emitted_with_computed_cost_for_known_pricing(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    # base_config's route -> provider "openai" (type: openai, default
    # base_url) -> model "gpt-4o-mini", which is in the built-in catalog.
    receipts = InMemoryReceiptSink()
    client = _make_client(monkeypatch, base_config, FakeProvider(), receipts)

    client.post("/v1/chat/completions", json=_chat_body())

    assert len(receipts.receipts) == 1
    receipt = receipts.receipts[0]
    assert receipt.status == "success"
    assert receipt.provider == "openai"
    assert receipt.model == "gpt-4o-mini"
    assert receipt.prompt_tokens == 842
    assert receipt.completion_tokens == 191
    assert receipt.pricing is not None
    assert receipt.estimated_cost_usd is not None
    assert receipt.estimated_cost_usd > 0


def test_receipt_unknown_model_pricing_is_none_not_zero(
    monkeypatch: pytest.MonkeyPatch, base_config_dict: dict[str, Any]
) -> None:
    base_config_dict["routes"]["default"]["model"] = "some-model-not-in-any-catalog"
    config = InferrailConfig.model_validate(base_config_dict)
    receipts = InMemoryReceiptSink()
    client = _make_client(monkeypatch, config, FakeProvider(), receipts)

    client.post("/v1/chat/completions", json=_chat_body())

    receipt = receipts.receipts[0]
    assert receipt.status == "success"
    assert receipt.pricing is None
    assert receipt.estimated_cost_usd is None


def test_receipt_on_error_has_no_tokens_or_cost(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    receipts = InMemoryReceiptSink()
    provider = FakeProvider(outcomes=[AuthenticationError("bad key", provider="openai")])
    client = _make_client(monkeypatch, base_config, provider, receipts)

    client.post("/v1/chat/completions", json=_chat_body())

    assert len(receipts.receipts) == 1
    receipt = receipts.receipts[0]
    assert receipt.status == "error"
    assert receipt.prompt_tokens is None
    assert receipt.completion_tokens is None
    assert receipt.pricing is None
    assert receipt.estimated_cost_usd is None


def test_receipt_captures_attribution_headers(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    receipts = InMemoryReceiptSink()
    client = _make_client(monkeypatch, base_config, FakeProvider(), receipts)

    client.post(
        "/v1/chat/completions",
        json=_chat_body(),
        headers={
            "X-Inferrail-Attribute-Customer": "acme",
            "X-Inferrail-Attribute-Workflow": "contract-review",
        },
    )

    receipt = receipts.receipts[0]
    assert receipt.attributes == {"customer": "acme", "workflow": "contract-review"}


def test_receipt_without_attribution_headers_has_empty_attributes(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    receipts = InMemoryReceiptSink()
    client = _make_client(monkeypatch, base_config, FakeProvider(), receipts)

    client.post("/v1/chat/completions", json=_chat_body())

    assert receipts.receipts[0].attributes == {}


def test_attribution_headers_never_reach_the_provider(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    receipts = InMemoryReceiptSink()
    provider = FakeProvider()
    client = _make_client(monkeypatch, base_config, provider, receipts)

    client.post(
        "/v1/chat/completions",
        json=_chat_body(),
        headers={"X-Inferrail-Attribute-Customer": "acme"},
    )

    assert len(provider.calls) == 1
    # NormalizedChatRequest has no field capable of carrying attribution —
    # structurally impossible for it to leak upstream (see providers/base.py).
    assert "attributes" not in NormalizedChatRequest.model_fields
    assert "acme" not in provider.calls[0].model_dump_json()


def test_receipts_never_persist_prompt_content(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig, tmp_path: Path
) -> None:
    secret = "TOP-SECRET-PROMPT-CONTENT-receipts"
    receipts_path = tmp_path / "receipts.jsonl"
    config_dict = base_config.model_dump()
    config_dict["receipts"] = {"sink": "jsonl", "path": str(receipts_path)}
    config = InferrailConfig.model_validate(config_dict)

    monkeypatch.setattr(
        app_module, "build_providers", lambda cfg, **_kw: {"openai": FakeProvider()}
    )
    app = app_module.create_app(config)
    client = TestClient(app)

    client.post(
        "/v1/chat/completions",
        json=_chat_body(messages=[{"role": "user", "content": secret}]),
    )

    raw = receipts_path.read_text()
    assert secret not in raw
    receipt = json.loads(raw.strip())
    assert receipt["provider"] == "openai"


def test_receipts_never_persist_response_content(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig, tmp_path: Path
) -> None:
    secret = "TOP-SECRET-RESPONSE-CONTENT-receipts"
    receipts_path = tmp_path / "receipts.jsonl"
    config_dict = base_config.model_dump()
    config_dict["receipts"] = {"sink": "jsonl", "path": str(receipts_path)}
    config = InferrailConfig.model_validate(config_dict)
    provider = FakeProvider(
        outcomes=[
            NormalizedChatResponse(
                content=secret, finish_reason="stop", prompt_tokens=1, completion_tokens=1
            )
        ]
    )
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {"openai": provider})
    app = app_module.create_app(config)
    client = TestClient(app)

    client.post("/v1/chat/completions", json=_chat_body())

    raw = receipts_path.read_text()
    assert secret not in raw


def test_receipts_never_persist_provider_echoed_error_text(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    canary = "TOP-SECRET-PROMPT-CONTENT-flagged-by-moderation-receipts"
    receipts = InMemoryReceiptSink()
    provider = FakeProvider(
        outcomes=[
            InvalidRequestError(
                f"provider 'openai' returned HTTP 400: content policy violation "
                f"for input '{canary}'",
                provider="openai",
                status_code=400,
                safe_summary="provider 'openai' returned HTTP 400 (content_policy_violation)",
            )
        ]
    )
    client = _make_client(monkeypatch, base_config, provider, receipts)

    client.post("/v1/chat/completions", json=_chat_body())

    assert len(receipts.receipts) == 1
    assert canary not in receipts.receipts[0].model_dump_json()


def test_user_field_never_becomes_receipt_attribution(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    # `user` is OpenAI's end-user identifier (forwarded to the provider —
    # see test_gateway.test_chat_completions_forwards_user_field_to_provider)
    # and is a completely separate mechanism from business attribution
    # (X-Inferrail-Attribute-<Name> headers -> receipt.attributes). Sending
    # `user` must never populate, key, or otherwise appear in
    # receipt.attributes — the two are not the same concept and must not be
    # conflated.
    marker = "end-user-do-not-attribute-me"
    receipts = InMemoryReceiptSink()
    client = _make_client(monkeypatch, base_config, FakeProvider(), receipts)

    client.post("/v1/chat/completions", json=_chat_body(user=marker))

    receipt = receipts.receipts[0]
    assert receipt.attributes == {}
    assert marker not in receipt.model_dump_json()


def test_attribution_values_do_persist_in_receipts_by_design(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    # Contrast case for the privacy tests above: attribution is caller-
    # supplied *business* metadata, not payload content, and is meant to
    # be persisted — see gateway/attribution.py.
    marker = "INTENTIONALLY-PERSISTED-BUSINESS-CONTEXT"
    receipts = InMemoryReceiptSink()
    client = _make_client(monkeypatch, base_config, FakeProvider(), receipts)

    client.post(
        "/v1/chat/completions",
        json=_chat_body(),
        headers={"X-Inferrail-Attribute-Customer": marker},
    )

    assert marker in receipts.receipts[0].model_dump_json()


def test_response_still_succeeds_when_receipt_sink_write_fails(
    monkeypatch: pytest.MonkeyPatch, base_config_dict: dict[str, Any], tmp_path: Path
) -> None:
    # receipts.sink defaults to "jsonl" (on by default), so an unwritable
    # path (read-only filesystem, permissions, disk full) must never turn
    # an otherwise-successful inference call into a 500 — the receipt is a
    # side-channel accounting record, not part of the response path. Uses
    # the real JSONLReceiptSink (not a fake) so this exercises the actual
    # production code path.
    config_dict = dict(base_config_dict)
    config_dict["receipts"] = {"sink": "jsonl", "path": str(tmp_path / "receipts.jsonl")}
    config = InferrailConfig.model_validate(config_dict)

    def _raise_open(*args: object, **kwargs: object) -> None:
        raise PermissionError("simulated permission denied")

    monkeypatch.setattr(Path, "open", _raise_open)
    monkeypatch.setattr(
        app_module, "build_providers", lambda cfg, **_kw: {"openai": FakeProvider()}
    )
    app = app_module.create_app(config)
    client = TestClient(app)

    response = client.post("/v1/chat/completions", json=_chat_body())

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "ok"
