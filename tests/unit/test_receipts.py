from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import UTC, date, datetime
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
from inferrail.receipts.sqlite_store import ReceiptsStore, import_jsonl, looks_like_sqlite


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
    assert isinstance(
        build_receipt_sink(ReceiptsConfig(sink="sqlite", path=str(tmp_path / "r.sqlite3"))),
        ReceiptsStore,
    )


def test_receipts_config_defaults_to_jsonl_with_sensible_path() -> None:
    config = ReceiptsConfig()

    assert config.sink == "jsonl"
    assert config.path == "./inferrail-receipts.jsonl"


# ---------------------------------------------------------------------------
# ReceiptsStore: WAL-mode SQLite sink (docs/adr/0013)
# ---------------------------------------------------------------------------


def test_sqlite_store_round_trips_a_full_receipt(tmp_path: Path) -> None:
    price = _fixture_price("0.15", "0.60")
    store = ReceiptsStore(tmp_path / "receipts.sqlite3")
    original = _receipt(
        receipt_id="ir_full",
        # Explicit, not the field's now()-default -- a float round-trip
        # through SQLite's REAL column must reproduce this exact
        # microsecond value, not merely "close enough".
        timestamp=datetime(2026, 1, 1, 12, 30, 45, 123456, tzinfo=UTC),
        prompt_tokens=842,
        completion_tokens=191,
        pricing=price,
        estimated_cost_usd=calculate_cost_usd(842, 191, price),
        attributes={"customer": "acme", "work_id": "WORK-1", "project": "proj-a"},
        retry_count=2,
    )

    store.emit(original)
    receipts, skipped = store.read_all()

    assert skipped == 0
    assert len(receipts) == 1
    round_tripped = receipts[0]
    assert round_tripped == original
    assert isinstance(round_tripped.estimated_cost_usd, Decimal)


def test_sqlite_store_round_trips_unknown_pricing_as_none_not_zero(tmp_path: Path) -> None:
    store = ReceiptsStore(tmp_path / "receipts.sqlite3")
    store.emit(_receipt(receipt_id="ir_unpriced", pricing=None, estimated_cost_usd=None))

    receipts, _skipped = store.read_all()

    assert receipts[0].pricing is None
    assert receipts[0].estimated_cost_usd is None


def test_sqlite_store_emit_is_idempotent_on_receipt_id(tmp_path: Path) -> None:
    store = ReceiptsStore(tmp_path / "receipts.sqlite3")
    receipt = _receipt(receipt_id="ir_dup")

    store.emit(receipt)
    store.emit(receipt)  # replayed -- must not duplicate or raise

    receipts, _skipped = store.read_all()
    assert len(receipts) == 1


def test_sqlite_store_query_filters_by_work_id_project_and_model(tmp_path: Path) -> None:
    store = ReceiptsStore(tmp_path / "receipts.sqlite3")
    store.emit(
        _receipt(
            receipt_id="ir_1", model="gpt-4o-mini", attributes={"work_id": "W1", "project": "P1"}
        )
    )
    store.emit(
        _receipt(receipt_id="ir_2", model="gpt-4o", attributes={"work_id": "W2", "project": "P1"})
    )
    store.emit(_receipt(receipt_id="ir_3", model="gpt-4o-mini", attributes={}))

    assert [r.receipt_id for r in store.query(work_id="W1")] == ["ir_1"]
    assert {r.receipt_id for r in store.query(project="P1")} == {"ir_1", "ir_2"}
    assert {r.receipt_id for r in store.query(model="gpt-4o-mini")} == {"ir_1", "ir_3"}
    assert [r.receipt_id for r in store.query(work_id="W1", model="gpt-4o-mini")] == ["ir_1"]
    assert store.query(work_id="does-not-exist") == []


def test_sqlite_store_query_limit_and_offset_page_in_ts_order(tmp_path: Path) -> None:
    store = ReceiptsStore(tmp_path / "receipts.sqlite3")
    for i in range(5):
        ts = datetime(2026, 1, 1, 0, 0, i, tzinfo=UTC)
        store.emit(_receipt(receipt_id=f"ir_{i}", timestamp=ts))

    page1 = store.query(limit=2, offset=0)
    page2 = store.query(limit=2, offset=2)
    page3 = store.query(limit=2, offset=4)

    assert [r.receipt_id for r in page1] == ["ir_0", "ir_1"]
    assert [r.receipt_id for r in page2] == ["ir_2", "ir_3"]
    assert [r.receipt_id for r in page3] == ["ir_4"]


def test_sqlite_store_query_since_is_exclusive_lower_bound(tmp_path: Path) -> None:
    store = ReceiptsStore(tmp_path / "receipts.sqlite3")
    t0 = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC)
    store.emit(_receipt(receipt_id="ir_old", timestamp=t0))
    store.emit(_receipt(receipt_id="ir_new", timestamp=t1))

    assert [r.receipt_id for r in store.query(since=t0.timestamp())] == ["ir_new"]
    assert {r.receipt_id for r in store.query(since=None)} == {"ir_old", "ir_new"}


def test_sqlite_store_count_matches_filters_without_limit(tmp_path: Path) -> None:
    store = ReceiptsStore(tmp_path / "receipts.sqlite3")
    store.emit(_receipt(receipt_id="ir_1", attributes={"project": "P1"}))
    store.emit(_receipt(receipt_id="ir_2", attributes={"project": "P1"}))
    store.emit(_receipt(receipt_id="ir_3", attributes={"project": "P2"}))

    assert store.count() == 3
    assert store.count(project="P1") == 2
    assert store.count(project="does-not-exist") == 0


def test_sqlite_store_survives_concurrent_writers(tmp_path: Path) -> None:
    # Same concurrency bar as JSONLReceiptSink's own test: WAL mode plus a
    # busy_timeout should serialize concurrent writers rather than losing
    # or corrupting rows.
    store = ReceiptsStore(tmp_path / "receipts.sqlite3")
    writers, per_writer = 8, 25

    def _write(writer_index: int) -> None:
        for i in range(per_writer):
            store.emit(_receipt(receipt_id=f"ir_{writer_index}_{i}"))

    threads = [threading.Thread(target=_write, args=(w,)) for w in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    receipts, skipped = store.read_all()
    assert skipped == 0
    assert len(receipts) == writers * per_writer
    assert len({r.receipt_id for r in receipts}) == writers * per_writer


def test_sqlite_store_read_all_skips_a_row_with_corrupted_attributes_json(tmp_path: Path) -> None:
    db_path = tmp_path / "receipts.sqlite3"
    store = ReceiptsStore(db_path)
    store.emit(_receipt(receipt_id="ir_good"))
    store.emit(_receipt(receipt_id="ir_bad"))

    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "UPDATE receipts SET attributes_json = ? WHERE receipt_id = ?",
        ("{not valid json", "ir_bad"),
    )
    conn.commit()
    conn.close()

    receipts, skipped = store.read_all()

    assert skipped == 1
    assert [r.receipt_id for r in receipts] == ["ir_good"]


def test_sqlite_store_export_jsonl_writes_every_receipt(tmp_path: Path) -> None:
    store = ReceiptsStore(tmp_path / "receipts.sqlite3")
    store.emit(_receipt(receipt_id="ir_1"))
    store.emit(_receipt(receipt_id="ir_2"))
    jsonl_path = tmp_path / "exported.jsonl"

    count = store.export_jsonl(jsonl_path)

    assert count == 2
    lines = jsonl_path.read_text().splitlines()
    assert len(lines) == 2
    assert {json.loads(line)["receipt_id"] for line in lines} == {"ir_1", "ir_2"}


def test_import_jsonl_populates_store_and_is_idempotent(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "receipts.jsonl"
    r1, r2 = _receipt(receipt_id="ir_1"), _receipt(receipt_id="ir_2")
    jsonl_path.write_text(r1.model_dump_json() + "\n" + r2.model_dump_json() + "\n")
    store = ReceiptsStore(tmp_path / "receipts.sqlite3")

    total, skipped = import_jsonl(store, jsonl_path)
    assert (total, skipped) == (2, 0)
    receipts, _ = store.read_all()
    assert len(receipts) == 2

    # Re-importing the same file must not duplicate rows.
    import_jsonl(store, jsonl_path)
    receipts, _ = store.read_all()
    assert len(receipts) == 2


def test_import_jsonl_reports_skipped_malformed_rows(tmp_path: Path) -> None:
    jsonl_path = tmp_path / "receipts.jsonl"
    good = _receipt(receipt_id="ir_1")
    jsonl_path.write_text(good.model_dump_json() + "\n" + "{not valid json\n")
    store = ReceiptsStore(tmp_path / "receipts.sqlite3")

    total, skipped = import_jsonl(store, jsonl_path)

    assert total == 1
    assert skipped == 1


def test_looks_like_sqlite_detects_by_magic_bytes_not_extension(tmp_path: Path) -> None:
    sqlite_path = tmp_path / "receipts.sqlite3"
    ReceiptsStore(sqlite_path)  # creates the file with a real SQLite header
    jsonl_path = tmp_path / "receipts.jsonl"
    jsonl_path.write_text(_receipt().model_dump_json() + "\n")
    misnamed_path = tmp_path / "receipts.jsonl.but.actually.sqlite"
    misnamed_path.write_bytes(sqlite_path.read_bytes())

    assert looks_like_sqlite(sqlite_path) is True
    assert looks_like_sqlite(jsonl_path) is False
    assert looks_like_sqlite(misnamed_path) is True
    assert looks_like_sqlite(tmp_path / "does-not-exist") is False
