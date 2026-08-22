from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from inferrail.config.models import PriceEntry, ReceiptsConfig
from inferrail.pricing.resolver import PricingResolver
from inferrail.receipts.builder import build_receipt, new_receipt_id
from inferrail.receipts.calculator import calculate_cost_usd
from inferrail.receipts.schema import InferenceReceipt
from inferrail.receipts.sinks import (
    JSONLReceiptSink,
    NullReceiptSink,
    build_receipt_sink,
)


def _fixture_price(input_price: str = "1.00", output_price: str = "2.00") -> PriceEntry:
    return PriceEntry(
        input_usd_per_million=Decimal(input_price),
        output_usd_per_million=Decimal(output_price),
        source="test-fixture",
        verified_date=date(2020, 1, 1),
    )


# ---------------------------------------------------------------------------
# CostCalculator: deterministic Decimal arithmetic
# ---------------------------------------------------------------------------


def test_calculate_cost_usd_basic() -> None:
    price = _fixture_price("1.00", "2.00")

    cost = calculate_cost_usd(prompt_tokens=1_000_000, completion_tokens=1_000_000, price=price)

    assert cost == Decimal("3.000000")


def test_calculate_cost_usd_input_and_output_priced_independently() -> None:
    price = _fixture_price("1.00", "10.00")

    cost = calculate_cost_usd(prompt_tokens=1_000_000, completion_tokens=0, price=price)
    assert cost == Decimal("1.000000")

    cost = calculate_cost_usd(prompt_tokens=0, completion_tokens=1_000_000, price=price)
    assert cost == Decimal("10.000000")


def test_calculate_cost_usd_small_token_counts() -> None:
    # Realistic single-request scale: must not silently round to zero.
    price = _fixture_price("0.15", "0.60")

    cost = calculate_cost_usd(prompt_tokens=842, completion_tokens=191, price=price)

    expected = (Decimal(842) * Decimal("0.15") + Decimal(191) * Decimal("0.60")) / Decimal(
        1_000_000
    )
    assert cost == expected.quantize(Decimal("0.000001"))
    assert cost > 0


def test_calculate_cost_usd_zero_tokens_is_exactly_zero() -> None:
    price = _fixture_price()

    cost = calculate_cost_usd(prompt_tokens=0, completion_tokens=0, price=price)

    assert cost == Decimal("0.000000")


def test_calculate_cost_usd_uses_decimal_not_float() -> None:
    # A classic float trap: 0.1 + 0.2 != 0.3 in binary floating point.
    # Decimal arithmetic must not exhibit this.
    price = _fixture_price("0.1", "0.2")

    cost = calculate_cost_usd(prompt_tokens=1_000_000, completion_tokens=1_000_000, price=price)

    assert cost == Decimal("0.300000")
    assert isinstance(cost, Decimal)


# ---------------------------------------------------------------------------
# InferenceReceipt: privacy structure + provenance
# ---------------------------------------------------------------------------


def test_inference_receipt_has_no_payload_fields() -> None:
    # Structural guarantee mirroring test_inference_event_has_no_payload_fields.
    field_names = set(InferenceReceipt.model_fields)
    assert field_names.isdisjoint({"prompt", "messages", "content", "response"})


def test_receipt_json_serializes_cost_as_decimal_string_not_float() -> None:
    price = _fixture_price("0.15", "0.60")
    receipt = InferenceReceipt(
        receipt_id="ir_test",
        request_id="req_test",
        route="default",
        provider="openai",
        model="gpt-4o-mini",
        status="success",
        prompt_tokens=842,
        completion_tokens=191,
        pricing=price,
        estimated_cost_usd=calculate_cost_usd(842, 191, price),
        total_latency_ms=100.0,
    )

    raw = json.loads(receipt.model_dump_json())

    assert isinstance(raw["estimated_cost_usd"], str)
    assert raw["estimated_cost_usd"] == str(calculate_cost_usd(842, 191, price))


# ---------------------------------------------------------------------------
# build_receipt: usage -> price -> cost -> receipt assembly
# ---------------------------------------------------------------------------


def test_build_receipt_success_computes_cost_from_known_pricing() -> None:
    price = _fixture_price("0.15", "0.60")
    resolver = PricingResolver({}, overrides={"openai": {"gpt-4o-mini": price}})

    receipt = build_receipt(
        receipt_id=new_receipt_id(),
        request_id="req_1",
        route="default",
        provider="openai",
        model="gpt-4o-mini",
        status="success",
        prompt_tokens=842,
        completion_tokens=191,
        attributes={"customer": "acme"},
        total_latency_ms=1832.0,
        retry_count=0,
        pricing_resolver=resolver,
    )

    assert receipt.status == "success"
    assert receipt.pricing == price
    assert receipt.estimated_cost_usd == calculate_cost_usd(842, 191, price)
    assert receipt.attributes == {"customer": "acme"}
    assert receipt.receipt_id.startswith("ir_")


def test_build_receipt_unknown_pricing_leaves_cost_none_not_zero() -> None:
    resolver = PricingResolver({}, overrides={})

    receipt = build_receipt(
        receipt_id=new_receipt_id(),
        request_id="req_1",
        route="default",
        provider="openai",
        model="some-unpriced-model",
        status="success",
        prompt_tokens=100,
        completion_tokens=50,
        attributes={},
        total_latency_ms=10.0,
        retry_count=0,
        pricing_resolver=resolver,
    )

    assert receipt.pricing is None
    assert receipt.estimated_cost_usd is None


def test_build_receipt_error_status_has_no_tokens_or_cost() -> None:
    price = _fixture_price()
    resolver = PricingResolver({}, overrides={"openai": {"gpt-4o-mini": price}})

    receipt = build_receipt(
        receipt_id=new_receipt_id(),
        request_id="req_1",
        route="default",
        provider="openai",
        model="gpt-4o-mini",
        status="error",
        prompt_tokens=None,
        completion_tokens=None,
        attributes={},
        total_latency_ms=5.0,
        retry_count=1,
        pricing_resolver=resolver,
    )

    assert receipt.status == "error"
    assert receipt.prompt_tokens is None
    assert receipt.completion_tokens is None
    assert receipt.pricing is None
    assert receipt.estimated_cost_usd is None


def test_new_receipt_id_is_unique() -> None:
    assert new_receipt_id() != new_receipt_id()


# ---------------------------------------------------------------------------
# Sinks
# ---------------------------------------------------------------------------


def _receipt(**overrides: object) -> InferenceReceipt:
    defaults: dict[str, object] = dict(
        receipt_id="ir_test",
        request_id="req_test",
        route="default",
        provider="openai",
        model="gpt-4o-mini",
        status="success",
        total_latency_ms=100.0,
    )
    defaults.update(overrides)
    return InferenceReceipt(**defaults)  # type: ignore[arg-type]


def test_jsonl_receipt_sink_writes_one_line_per_receipt(tmp_path: Path) -> None:
    sink = JSONLReceiptSink(tmp_path / "nested" / "receipts.jsonl")

    sink.emit(_receipt(receipt_id="ir_1"))
    sink.emit(_receipt(receipt_id="ir_2"))

    lines = (tmp_path / "nested" / "receipts.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["receipt_id"] == "ir_1"
    assert json.loads(lines[1])["receipt_id"] == "ir_2"


def test_jsonl_receipt_sink_write_failure_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # receipts.sink defaults to "jsonl" — a write failure (read-only
    # filesystem, permissions, disk full) must never crash an in-flight
    # request. This simulates that failure independent of OS permission
    # semantics (which behave inconsistently when running as root).
    sink = JSONLReceiptSink(tmp_path / "receipts.jsonl")

    def _raise_open(*args: object, **kwargs: object) -> None:
        raise PermissionError("simulated permission denied")

    monkeypatch.setattr(os, "open", _raise_open)

    with caplog.at_level(logging.WARNING, logger="inferrail.receipts"):
        sink.emit(_receipt())  # must not raise

    assert "failed to write receipt" in caplog.text


def test_jsonl_receipt_sink_survives_concurrent_large_receipts(tmp_path: Path) -> None:
    # A receipt larger than the ~8 KiB text-IO buffer used to be split
    # across several syscalls, so concurrent writers interleaved mid-line
    # and *both* records were lost as unparseable rows. Ordinary
    # attribution (many attributes, or long values) reaches that size, and
    # this ledger is the product's accounting record — silently dropping
    # rows under concurrency is not an acceptable failure mode.
    path = tmp_path / "receipts.jsonl"
    sink = JSONLReceiptSink(path)
    big_attributes = {"customer": "acme", "pad": "z" * 20_000}
    writers, per_writer = 8, 40

    def _write() -> None:
        for _ in range(per_writer):
            sink.emit(_receipt(attributes=dict(big_attributes)))

    threads = [threading.Thread(target=_write) for _ in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    lines = [line for line in path.read_text().splitlines() if line.strip()]
    assert len(lines) == writers * per_writer
    for line in lines:
        assert json.loads(line)["attributes"]["pad"] == "z" * 20_000


def test_null_receipt_sink_discards_silently() -> None:
    NullReceiptSink().emit(_receipt())  # must not raise


def test_build_receipt_sink_dispatches_by_config(tmp_path: Path) -> None:
    assert isinstance(build_receipt_sink(ReceiptsConfig(sink="none")), NullReceiptSink)
    assert isinstance(
        build_receipt_sink(ReceiptsConfig(sink="jsonl", path=str(tmp_path / "r.jsonl"))),
        JSONLReceiptSink,
    )


def test_receipts_config_defaults_to_jsonl_with_sensible_path() -> None:
    config = ReceiptsConfig()

    assert config.sink == "jsonl"
    assert config.path == "./inferrail-receipts.jsonl"
