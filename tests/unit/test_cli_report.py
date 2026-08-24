from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from inferrail.cli.report import aggregate, format_summary, format_table, load_receipts, run_report
from inferrail.receipts.schema import InferenceReceipt, PricingSnapshot


def _pricing() -> PricingSnapshot:
    return PricingSnapshot(
        input_usd_per_million=Decimal("1.00"),
        output_usd_per_million=Decimal("2.00"),
        source="test-fixture",
        verified_date=date(2020, 1, 1),
    )


def _receipt(**overrides: object) -> InferenceReceipt:
    defaults: dict[str, object] = dict(
        receipt_id="ir_x",
        request_id="req_x",
        route="default",
        provider="openai",
        model="gpt-4o-mini",
        status="success",
        total_latency_ms=10.0,
    )
    defaults.update(overrides)
    return InferenceReceipt(**defaults)  # type: ignore[arg-type]


def test_load_receipts_parses_valid_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "receipts.jsonl"
    r1 = _receipt(receipt_id="ir_1")
    r2 = _receipt(receipt_id="ir_2")
    path.write_text(r1.model_dump_json() + "\n" + r2.model_dump_json() + "\n")

    receipts, skipped = load_receipts(path)

    assert [r.receipt_id for r in receipts] == ["ir_1", "ir_2"]
    assert skipped == 0


def test_load_receipts_skips_malformed_json_line(tmp_path: Path) -> None:
    path = tmp_path / "receipts.jsonl"
    good = _receipt(receipt_id="ir_1")
    path.write_text(good.model_dump_json() + "\nnot valid json at all\n")

    receipts, skipped = load_receipts(path)

    assert [r.receipt_id for r in receipts] == ["ir_1"]
    assert skipped == 1


def test_load_receipts_skips_rows_missing_required_fields(tmp_path: Path) -> None:
    # Simulates an older/incompatible schema version.
    path = tmp_path / "receipts.jsonl"
    good = _receipt(receipt_id="ir_1")
    old_schema_row = json.dumps({"request_id": "req_old", "some_other_shape": True})
    path.write_text(good.model_dump_json() + "\n" + old_schema_row + "\n")

    receipts, skipped = load_receipts(path)

    assert [r.receipt_id for r in receipts] == ["ir_1"]
    assert skipped == 1


def test_run_report_empty_existing_file_is_not_an_error(tmp_path: Path) -> None:
    # Distinct from a missing file: the file exists (e.g. created by
    # JSONLReceiptSink's mkdir-on-construct) but no receipt has been
    # written to it yet.
    path = tmp_path / "receipts.jsonl"
    path.write_text("")

    result = run_report(path, "customer")

    assert result == 0


def test_load_receipts_skips_blank_lines(tmp_path: Path) -> None:
    path = tmp_path / "receipts.jsonl"
    good = _receipt(receipt_id="ir_1")
    path.write_text(good.model_dump_json() + "\n\n\n")

    receipts, skipped = load_receipts(path)

    assert len(receipts) == 1
    assert skipped == 0


def test_aggregate_by_attribute_groups_and_sums_exactly() -> None:
    pricing = _pricing()
    receipts = [
        _receipt(
            prompt_tokens=1000, completion_tokens=100,
            pricing=pricing, estimated_cost_usd=Decimal("0.001200"),
            attributes={"customer": "acme"},
        ),
        _receipt(
            prompt_tokens=500, completion_tokens=50,
            pricing=pricing, estimated_cost_usd=Decimal("0.000600"),
            attributes={"customer": "acme"},
        ),
        _receipt(
            prompt_tokens=200, completion_tokens=20,
            pricing=pricing, estimated_cost_usd=Decimal("0.000240"),
            attributes={"customer": "globex"},
        ),
    ]

    groups = {g.key: g for g in aggregate(receipts, "customer")}

    assert groups["acme"].requests == 2
    assert groups["acme"].input_tokens == 1500
    assert groups["acme"].output_tokens == 150
    assert groups["acme"].known_cost_usd == Decimal("0.001800")
    assert groups["globex"].requests == 1
    assert groups["globex"].known_cost_usd == Decimal("0.000240")


def test_aggregate_by_provider_and_model_dimensions() -> None:
    receipts = [
        _receipt(provider="openai", model="gpt-4o-mini"),
        _receipt(provider="openai", model="gpt-4o"),
    ]

    by_provider = {g.key: g for g in aggregate(receipts, "provider")}
    assert by_provider["openai"].requests == 2

    by_model = {g.key: g for g in aggregate(receipts, "model")}
    assert by_model["gpt-4o-mini"].requests == 1
    assert by_model["gpt-4o"].requests == 1


def test_aggregate_missing_attribute_buckets_as_unattributed() -> None:
    receipts = [_receipt(attributes={})]

    groups = aggregate(receipts, "customer")

    assert groups[0].key == "(unattributed)"


def test_aggregate_unknown_cost_counted_only_for_successful_requests() -> None:
    receipts = [
        # Successful but no known price: counts as unknown cost.
        _receipt(status="success", prompt_tokens=10, completion_tokens=5),
        # Failed request: no usage occurred, not a pricing gap.
        _receipt(status="error", prompt_tokens=None, completion_tokens=None),
    ]

    groups = aggregate(receipts, "provider")

    assert groups[0].unknown_cost_requests == 1
    assert groups[0].requests == 2
    assert groups[0].successful_requests == 1


def test_aggregate_partial_receipt_counts_as_neither_success_nor_unknown_cost() -> None:
    # A stream interrupted partway through (status="partial", introduced
    # alongside real streaming support) never fabricates a cost — same as
    # "error" — and must not be double-counted as an unresolved pricing
    # gap: it genuinely never reached a measured, priced completion.
    receipts = [_receipt(status="partial", prompt_tokens=None, completion_tokens=None)]

    groups = aggregate(receipts, "provider")

    assert groups[0].requests == 1
    assert groups[0].successful_requests == 0
    assert groups[0].unknown_cost_requests == 0


def test_format_table_includes_total_row() -> None:
    pricing = _pricing()
    receipts = [
        _receipt(
            prompt_tokens=100, completion_tokens=10,
            pricing=pricing, estimated_cost_usd=Decimal("0.000120"),
            attributes={"customer": "acme"},
        )
    ]

    table = format_table(aggregate(receipts, "customer"), "customer")

    assert "TOTAL" in table
    assert "acme" in table
    assert "CUSTOMER" in table


def test_format_table_small_costs_are_not_rounded_to_zero() -> None:
    receipts = [
        _receipt(
            prompt_tokens=100, completion_tokens=10,
            pricing=_pricing(), estimated_cost_usd=Decimal("0.000120"),
        )
    ]

    table = format_table(aggregate(receipts, "provider"), "provider")

    assert "$0.00012" in table
    assert "$0.00 " not in table
    assert "$0.00\n" not in table


def test_format_table_empty_receipts() -> None:
    assert format_table([], "customer") == "No receipts to report on."


def test_format_summary_includes_all_up_economic_fields() -> None:
    receipts = [
        _receipt(
            prompt_tokens=100,
            completion_tokens=10,
            pricing=_pricing(),
            estimated_cost_usd=Decimal("0.000120"),
        ),
        _receipt(
            receipt_id="ir_error",
            status="error",
        ),
        _receipt(
            receipt_id="ir_unknown",
            prompt_tokens=50,
            completion_tokens=5,
        ),
    ]

    summary = format_summary(receipts)

    assert "Requests:        3" in summary
    assert "Failed requests: 1" in summary
    assert "Input tokens:    150" in summary
    assert "Output tokens:   15" in summary
    assert "Known cost (USD): $0.00012" in summary
    assert "Unknown cost:    1" in summary
    assert "INFRERRAIL" not in summary
    assert "INFERRAIL SPEND SUMMARY" in summary


def test_format_table_all_unknown_pricing_never_shows_fabricated_zero() -> None:
    # A group where every successful request has unresolvable pricing (e.g.
    # an unpriced self-hosted model) must never render as "$0.00" in the
    # COST column — that reads as "this cost nothing," which is exactly the
    # fabricated-zero failure mode this project refuses to produce.
    receipts = [
        _receipt(prompt_tokens=100, completion_tokens=10, attributes={"customer": "acme"})
        for _ in range(3)
    ]

    table = format_table(aggregate(receipts, "customer"), "customer")

    assert "$0.00" not in table
    assert "unknown" in table


def test_format_table_partial_known_cost_still_shows_the_known_sum() -> None:
    # Contrast case: when a group has *some* known-cost receipts, the known
    # sum should still be shown (not blanked out to "unknown"), with the
    # unknown-cost count as the adjacent, separate disclosure.
    receipts = [
        _receipt(
            prompt_tokens=100, completion_tokens=10,
            pricing=_pricing(), estimated_cost_usd=Decimal("0.000120"),
            attributes={"customer": "acme"},
        ),
        _receipt(
            prompt_tokens=50, completion_tokens=5, attributes={"customer": "acme"},
        ),
    ]

    table = format_table(aggregate(receipts, "customer"), "customer")

    assert "$0.00012" in table
    assert "unknown" not in table


def _cell(table: str, row_prefix: str, header_col: str, next_header_col: str) -> str:
    """Slice one fixed-width cell out of `format_table` output by column
    header position, since a blank cell (ljust-padded to width) disappears
    under a naive `.split()` and shifts every later token's index."""
    header_line = table.splitlines()[0]
    row_line = next(line for line in table.splitlines() if line.startswith(row_prefix))
    start = header_line.index(header_col)
    end = header_line.index(next_header_col)
    return row_line[start:end].strip()


def test_format_table_reveals_a_failed_request_within_a_group() -> None:
    # Reproduces the dogfooding defect (EVIDENCE_LOG.md 2026-08-21, finding
    # 4): a task with 3 successes and 1 real provider error must not render
    # identically to an all-success group — `inferrail transaction` already
    # surfaces the failure per-event; `report` must not conceal it when
    # grouping by the same dimension (e.g. task_id).
    receipts = [
        _receipt(status="success", attributes={"task_id": "bugfix"}),
        _receipt(status="success", attributes={"task_id": "bugfix"}),
        _receipt(status="error", attributes={"task_id": "bugfix"}),
        _receipt(status="success", attributes={"task_id": "bugfix"}),
    ]

    table = format_table(aggregate(receipts, "task_id"), "task_id")

    assert "FAILED" in table
    assert _cell(table, "bugfix", "FAILED", "INPUT TOKENS") == "1"


def test_format_table_all_success_group_shows_no_failed_marker() -> None:
    receipts = [_receipt(status="success", attributes={"task_id": "clean"}) for _ in range(3)]

    table = format_table(aggregate(receipts, "task_id"), "task_id")

    assert _cell(table, "clean", "FAILED", "INPUT TOKENS") == ""


def test_run_report_missing_file_is_not_an_error(tmp_path: Path) -> None:
    result = run_report(tmp_path / "does-not-exist.jsonl", "customer")

    assert result == 0


def test_run_report_prints_table_and_skip_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "receipts.jsonl"
    good = _receipt(attributes={"customer": "acme"})
    path.write_text(good.model_dump_json() + "\nmalformed\n")

    result = run_report(path, "customer")

    assert result == 0
    captured = capsys.readouterr()
    assert "acme" in captured.out
    assert "Skipped 1 malformed" in captured.err
