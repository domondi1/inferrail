"""Anthropic prompt-cache tokens are counted and priced, never dropped.

Anthropic reports cache tokens outside `input_tokens`
(`total input = input_tokens + cache_creation_input_tokens +
cache_read_input_tokens`) and bills 5-minute writes, 1-hour writes, and
reads at their own rates. Before this, receipts priced only
`input_tokens`, so a heavily cached request got a known-looking cost that
understated the real one by an order of magnitude.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from _fakes import AnthropicFakeProvider, StreamScript
from fastapi.testclient import TestClient

from inferrail.config.models import InferrailConfig, PriceEntry
from inferrail.gateway import app as app_module
from inferrail.providers.anthropic_base import AnthropicNormalizedResponse
from inferrail.receipts.calculator import (
    CacheTokens,
    calculate_cost_usd,
    calculate_cost_with_cache_usd,
    parse_anthropic_cache_usage,
)
from inferrail.receipts.schema import InferenceReceipt
from inferrail.receipts.sqlite_store import ReceiptsStore

# claude-sonnet-5, per the built-in catalog: $2 in, $10 out, $2.50 5m write,
# $4 1h write, $0.20 read (per million tokens).
_CACHED_USAGE: dict[str, object] = {
    "cache_creation_input_tokens": 2000,
    "cache_read_input_tokens": 20000,
    "cache_creation": {"ephemeral_5m_input_tokens": 1500, "ephemeral_1h_input_tokens": 500},
}
# 100*2 + 50*10 + 20000*0.20 + 1500*2.50 + 500*4 = 10450 -> $0.010450
_EXPECTED_COST = Decimal("0.010450")
_TOTAL_INPUT = 100 + 2000 + 20000


class _Receipts:
    def __init__(self) -> None:
        self.receipts: list[InferenceReceipt] = []

    def emit(self, receipt: InferenceReceipt) -> None:
        self.receipts.append(receipt)


@pytest.fixture(autouse=True)
def _no_gateway_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INFERRAIL_GATEWAY_TOKEN", raising=False)


@pytest.fixture
def config() -> InferrailConfig:
    return InferrailConfig.model_validate(
        {
            "providers": {
                "anthropic": {"type": "anthropic", "api_key_env": "TEST_ANTHROPIC_API_KEY"},
            },
            "routes": {"claude": {"provider": "anthropic", "model": "claude-sonnet-5"}},
            "telemetry": {"sink": "none"},
            "receipts": {"sink": "none"},
        }
    )


def _client(
    monkeypatch: pytest.MonkeyPatch,
    config: InferrailConfig,
    provider: AnthropicFakeProvider,
    receipts: _Receipts,
) -> TestClient:
    monkeypatch.setattr(
        app_module, "build_anthropic_providers", lambda cfg, **_kw: {"anthropic": provider}
    )
    monkeypatch.setattr(app_module, "build_receipt_sink", lambda cfg: receipts)
    return TestClient(app_module.create_app(config))


def _body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": "claude",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hello"}],
    }
    body.update(overrides)
    return body


def _price(**cache: str) -> PriceEntry:
    return PriceEntry(
        input_usd_per_million=Decimal("2"),
        output_usd_per_million=Decimal("10"),
        source="test",
        verified_date=date(2026, 9, 29),
        **{k: Decimal(v) for k, v in cache.items()},
    )


_FULL_CACHE_PRICE = {
    "cache_write_5m_usd_per_million": "2.50",
    "cache_write_1h_usd_per_million": "4",
    "cache_read_usd_per_million": "0.20",
}


# --- calculator -------------------------------------------------------------


def test_parse_returns_none_without_cache_fields() -> None:
    assert parse_anthropic_cache_usage({"input_tokens": 5, "output_tokens": 2}) is None


def test_parse_reads_breakdown() -> None:
    cache = parse_anthropic_cache_usage(_CACHED_USAGE)
    assert cache == CacheTokens(creation=2000, creation_5m=1500, creation_1h=500, read=20000)
    assert cache.total == 22000
    assert cache.creation_split_known


def test_cost_prices_each_cache_category_at_its_own_rate() -> None:
    cache = parse_anthropic_cache_usage(_CACHED_USAGE)
    cost = calculate_cost_with_cache_usd(_TOTAL_INPUT, 50, _price(**_FULL_CACHE_PRICE), cache)
    assert cost == _EXPECTED_COST


def test_cost_without_cache_matches_plain_calculation() -> None:
    price = _price()
    assert calculate_cost_with_cache_usd(100, 50, price, None) == calculate_cost_usd(
        100, 50, price
    )
    zero = parse_anthropic_cache_usage(
        {"cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    )
    assert calculate_cost_with_cache_usd(100, 50, price, zero) == calculate_cost_usd(
        100, 50, price
    )


def test_cache_writes_without_ttl_split_are_unknown_not_guessed() -> None:
    cache = parse_anthropic_cache_usage(
        {"cache_creation_input_tokens": 2000, "cache_read_input_tokens": 0}
    )
    assert cache is not None and not cache.creation_split_known
    assert calculate_cost_with_cache_usd(2100, 50, _price(**_FULL_CACHE_PRICE), cache) is None


def test_missing_cache_rate_is_unknown_not_base_rate() -> None:
    cache = parse_anthropic_cache_usage({"cache_read_input_tokens": 20000})
    assert calculate_cost_with_cache_usd(20100, 50, _price(), cache) is None


def test_builtin_catalog_carries_verified_cache_rates() -> None:
    from inferrail.pricing.builtin_anthropic import BUILTIN_ANTHROPIC_PRICING

    for model, entry in BUILTIN_ANTHROPIC_PRICING.items():
        assert entry.cache_write_5m_usd_per_million is not None, model
        assert entry.cache_write_1h_usd_per_million is not None, model
        assert entry.cache_read_usd_per_million is not None, model
    sonnet = BUILTIN_ANTHROPIC_PRICING["claude-sonnet-5"]
    assert sonnet.cache_write_5m_usd_per_million == Decimal("2.50")
    assert sonnet.cache_read_usd_per_million == Decimal("0.20")
    # Fable 5.1 cache reads are 0.025x base input, not 0.1x.
    assert BUILTIN_ANTHROPIC_PRICING["claude-fable-5-1"].cache_read_usd_per_million == (
        Decimal("0.25")
    )


# --- gateway ----------------------------------------------------------------


def test_non_streaming_receipt_prices_cache_tokens(
    monkeypatch: pytest.MonkeyPatch, config: InferrailConfig
) -> None:
    receipts = _Receipts()
    provider = AnthropicFakeProvider(
        outcomes=[
            AnthropicNormalizedResponse(
                content=[{"type": "text", "text": "ok"}],
                stop_reason="end_turn", stop_sequence=None,
                input_tokens=100, output_tokens=50, cache_usage=_CACHED_USAGE,
            )
        ]
    )
    response = _client(monkeypatch, config, provider, receipts).post("/v1/messages", json=_body())

    assert response.status_code == 200
    usage = response.json()["usage"]
    # Passed back to the client exactly as Anthropic reported it.
    assert usage["input_tokens"] == 100
    assert usage["cache_read_input_tokens"] == 20000
    assert usage["cache_creation_input_tokens"] == 2000
    assert usage["cache_creation"] == _CACHED_USAGE["cache_creation"]

    (receipt,) = receipts.receipts
    assert receipt.prompt_tokens == _TOTAL_INPUT
    assert receipt.cache_read_input_tokens == 20000
    assert receipt.cache_creation_input_tokens == 2000
    assert receipt.cache_creation_5m_input_tokens == 1500
    assert receipt.cache_creation_1h_input_tokens == 500
    assert receipt.estimated_cost_usd == _EXPECTED_COST
    assert receipt.pricing is not None


def test_streaming_receipt_prices_cache_tokens_from_message_start(
    monkeypatch: pytest.MonkeyPatch, config: InferrailConfig
) -> None:
    import json

    def event(kind: str, data: dict[str, Any]) -> bytes:
        return f"event: {kind}\ndata: {json.dumps(data)}\n\n".encode()

    chunks = [
        event("message_start", {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "content": [],
            "model": "claude-sonnet-5",
            "usage": {"input_tokens": 100, "output_tokens": 1, **_CACHED_USAGE},
        }}),
        event("content_block_delta", {"type": "content_block_delta", "index": 0,
                                      "delta": {"type": "text_delta", "text": "hi"}}),
        event("message_delta", {"type": "message_delta",
                                "delta": {"stop_reason": "end_turn"},
                                "usage": {"output_tokens": 50}}),
        event("message_stop", {"type": "message_stop"}),
    ]
    receipts = _Receipts()
    provider = AnthropicFakeProvider(stream_outcomes=[StreamScript(chunks=chunks)])
    client = _client(monkeypatch, config, provider, receipts)

    with client.stream("POST", "/v1/messages", json=_body(stream=True)) as response:
        assert response.status_code == 200
        b"".join(response.iter_bytes())

    (receipt,) = receipts.receipts
    assert receipt.prompt_tokens == _TOTAL_INPUT
    assert receipt.completion_tokens == 50
    assert receipt.cache_read_input_tokens == 20000
    assert receipt.estimated_cost_usd == _EXPECTED_COST


def test_unsplit_cache_writes_give_unknown_cost_not_understated_cost(
    monkeypatch: pytest.MonkeyPatch, config: InferrailConfig
) -> None:
    receipts = _Receipts()
    provider = AnthropicFakeProvider(
        outcomes=[
            AnthropicNormalizedResponse(
                content=[{"type": "text", "text": "ok"}],
                stop_reason="end_turn", stop_sequence=None,
                input_tokens=100, output_tokens=50,
                cache_usage={"cache_creation_input_tokens": 2000, "cache_read_input_tokens": 0},
            )
        ]
    )
    _client(monkeypatch, config, provider, receipts).post("/v1/messages", json=_body())

    (receipt,) = receipts.receipts
    assert receipt.prompt_tokens == 2100
    assert receipt.estimated_cost_usd is None
    assert receipt.pricing is None


def test_uncached_request_receipt_unchanged(
    monkeypatch: pytest.MonkeyPatch, config: InferrailConfig
) -> None:
    receipts = _Receipts()
    provider = AnthropicFakeProvider()  # input 3, output 2, no cache fields
    response = _client(monkeypatch, config, provider, receipts).post("/v1/messages", json=_body())

    # No cache fields reported -> none added to the client's response.
    assert response.json()["usage"] == {"input_tokens": 3, "output_tokens": 2}
    (receipt,) = receipts.receipts
    assert receipt.prompt_tokens == 3
    assert receipt.cache_read_input_tokens is None
    assert receipt.estimated_cost_usd == Decimal("0.000026")  # 3*2 + 2*10 per million


# --- storage ----------------------------------------------------------------


def test_sqlite_store_adds_cache_columns_to_an_existing_database(tmp_path: Path) -> None:
    db = tmp_path / "receipts.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE receipts (
            receipt_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, ts REAL NOT NULL,
            route TEXT NOT NULL, provider TEXT NOT NULL, model TEXT NOT NULL,
            status TEXT NOT NULL, prompt_tokens INTEGER, completion_tokens INTEGER,
            pricing_json TEXT, estimated_cost_usd TEXT, attributes_json TEXT NOT NULL,
            work_id TEXT, project TEXT, total_latency_ms REAL NOT NULL,
            retry_count INTEGER NOT NULL
        );
        INSERT INTO receipts VALUES ('ir_old', 'req_old', 1.0, 'r', 'p', 'm', 'success',
            5, 2, NULL, NULL, '{}', NULL, NULL, 1.0, 0);
        """
    )
    conn.commit()
    conn.close()

    store = ReceiptsStore(db)
    ReceiptsStore(db)  # opening twice is a no-op, not a duplicate-column error
    receipts, skipped = store.read_all()
    assert skipped == 0
    (old,) = receipts
    assert old.receipt_id == "ir_old"
    assert old.cache_read_input_tokens is None

    new = old.model_copy(
        update={"receipt_id": "ir_new", "cache_read_input_tokens": 7,
                "cache_creation_input_tokens": 3, "cache_creation_5m_input_tokens": 3,
                "cache_creation_1h_input_tokens": 0}
    )
    store.emit(new)
    by_id = {r.receipt_id: r for r in store.read_all()[0]}
    assert by_id["ir_new"].cache_read_input_tokens == 7
    assert by_id["ir_new"].cache_creation_5m_input_tokens == 3


# --- budgets ----------------------------------------------------------------


def test_budget_overrun_counts_cache_tokens(tmp_path: Path) -> None:
    from inferrail.budgets.enforcement import augment_attributes_with_overrun
    from inferrail.budgets.schema import Budget
    from inferrail.pricing.resolver import PricingResolver

    config = InferrailConfig.model_validate(
        {
            "providers": {
                "anthropic": {"type": "anthropic", "api_key_env": "TEST_ANTHROPIC_API_KEY"},
            },
            "routes": {"claude": {"provider": "anthropic", "model": "claude-sonnet-5"}},
        }
    )
    budget = Budget(
        budget_id="global:_:daily", scope="global", window="daily", mode="warn",
        limit_usd=Decimal("0.005"),
    )
    common: dict[str, Any] = {
        "attributes": {},
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "prompt_tokens": _TOTAL_INPUT,
        "completion_tokens": 50,
        "matching": [budget],
        "receipts": ReceiptsStore(tmp_path / "r.db"),
        "pricing_resolver": PricingResolver(config.providers, config.pricing),
    }

    # $0.010450 actual vs a $0.005 limit: an overrun of $0.005450.
    enriched = augment_attributes_with_overrun(
        **common, cache=parse_anthropic_cache_usage(_CACHED_USAGE)
    )
    assert enriched["budget_overrun_usd"] == "0.005450"
