from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from inferrail.cli.main import main
from inferrail.cli.work import format_work_aggregate
from inferrail.receipts.schema import InferenceReceipt
from inferrail.work.builder import load_outcomes
from inferrail.work.schema import WorkSummary

_T0 = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)


def _receipt(**overrides: object) -> InferenceReceipt:
    defaults: dict[str, object] = dict(
        receipt_id="ir_x",
        request_id="req_x",
        route="default",
        provider="openai",
        model="gpt-4o-mini",
        status="success",
        total_latency_ms=10.0,
        timestamp=_T0,
    )
    defaults.update(overrides)
    return InferenceReceipt(**defaults)  # type: ignore[arg-type]


def _write_receipts(path: Path, receipts: list[InferenceReceipt]) -> None:
    path.write_text("".join(receipt.model_dump_json() + "\n" for receipt in receipts))


def test_work_outcome_command_appends_history(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    outcomes = tmp_path / "outcomes.jsonl"

    assert (
        main(["work", "outcome", "work-42", "--status", "escalated", "--outcomes", str(outcomes)])
        == 0
    )
    assert (
        main(["work", "outcome", "work-42", "--status", "resolved", "--outcomes", str(outcomes)])
        == 0
    )

    loaded, skipped = load_outcomes(outcomes)
    assert skipped == 0
    assert [outcome.outcome_status for outcome in loaded] == ["escalated", "resolved"]
    assert "Recorded outcome" in capsys.readouterr().out


def test_work_inspection_loads_receipts_and_persisted_outcome(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    receipts = tmp_path / "receipts.jsonl"
    outcomes = tmp_path / "outcomes.jsonl"
    _write_receipts(
        receipts,
        [_receipt(attributes={"work_id": "work-a"}, estimated_cost_usd=Decimal("0.120000"))],
    )
    main(["work", "outcome", "work-a", "--status", "resolved", "--outcomes", str(outcomes)])
    capsys.readouterr()

    assert main(["work", "work-a", "--receipts", str(receipts), "--outcomes", str(outcomes)]) == 0
    output = capsys.readouterr().out
    assert "resolved" in output
    assert "Known attributed inference cost:    $0.12" in output


def test_work_aggregate_groups_outcomes_without_interpreting_business_success(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    receipts = tmp_path / "receipts.jsonl"
    outcomes = tmp_path / "outcomes.jsonl"
    _write_receipts(
        receipts,
        [
            _receipt(
                receipt_id="ir_a",
                attributes={"work_id": "work-a"},
                estimated_cost_usd=Decimal("0.100000"),
            ),
            _receipt(receipt_id="ir_b", attributes={"work_id": "work-b"}),
            _receipt(
                receipt_id="ir_c",
                attributes={"work_id": "work-c"},
                estimated_cost_usd=Decimal("0.300000"),
            ),
            _receipt(
                receipt_id="ir_d",
                attributes={"work_id": "work-d"},
                estimated_cost_usd=Decimal("0.050000"),
            ),
        ],
    )
    for work_id, status in [
        ("work-a", "resolved"),
        ("work-b", "resolved"),
        ("work-c", "resolved"),
        ("work-e", "failed"),
    ]:
        main(["work", "outcome", work_id, "--status", status, "--outcomes", str(outcomes)])
    capsys.readouterr()

    assert main(["work", "--all", "--receipts", str(receipts), "--outcomes", str(outcomes)]) == 0
    output = capsys.readouterr().out
    assert "work-d" in output and "undeclared" in output
    assert "work-e" in output and "unavailable" in output
    assert "WORK ECONOMICS BY CUSTOMER-DECLARED OUTCOME" in output
    assert "Successful work" not in output
    assert "resolved" in output
    assert "2" in output  # resolved works with fully-known cost
    assert "1" in output  # resolved work containing unknown cost
    assert "$0.20 (known-only)" in output


def _outcome_section(output: str) -> str:
    return output.split("WORK ECONOMICS BY CUSTOMER-DECLARED OUTCOME\n", maxsplit=1)[1]


def test_outcome_only_group_reports_unavailable_known_cost() -> None:
    output = format_work_aggregate(
        [WorkSummary(work_id="outcome-only", outcome_status="escalated")]
    )

    outcome_section = _outcome_section(output)
    assert "NO RECEIPT EVIDENCE" in outcome_section
    assert "escalated" in outcome_section
    assert "unavailable" in outcome_section
    assert "$0.00" not in outcome_section


def test_mixed_group_retains_known_cost_and_counts_no_receipt_evidence() -> None:
    output = format_work_aggregate(
        [
            WorkSummary(
                work_id="known",
                outcome_status="resolved",
                receipt_count=1,
                known_attributed_inference_cost_usd=Decimal("0.200000"),
                inference_status="success",
            ),
            WorkSummary(
                work_id="unknown",
                outcome_status="resolved",
                receipt_count=1,
                known_attributed_inference_cost_usd=Decimal("0"),
                unknown_cost_count=1,
                inference_status="success",
            ),
            WorkSummary(work_id="no-evidence", outcome_status="resolved"),
        ]
    )

    resolved_row = next(
        line for line in _outcome_section(output).splitlines() if line.startswith("resolved")
    )
    assert "$0.20" in resolved_row
    assert "1" in resolved_row  # one unknown-cost work and one no-receipt-evidence work


def test_observed_zero_cost_remains_distinct_from_no_receipt_evidence() -> None:
    output = format_work_aggregate(
        [
            WorkSummary(
                work_id="known-zero",
                outcome_status="accepted",
                receipt_count=1,
                known_attributed_inference_cost_usd=Decimal("0"),
                inference_status="success",
            )
        ]
    )

    accepted_row = next(
        line for line in _outcome_section(output).splitlines() if line.startswith("accepted")
    )
    assert "$0.00" in accepted_row
    assert "unavailable" not in accepted_row


def test_unknown_priced_observed_receipt_remains_separate_from_no_evidence() -> None:
    output = format_work_aggregate(
        [
            WorkSummary(
                work_id="unknown-price",
                outcome_status="reviewed",
                receipt_count=1,
                known_attributed_inference_cost_usd=Decimal("0"),
                unknown_cost_count=1,
                inference_status="success",
            )
        ]
    )

    reviewed_row = next(
        line for line in _outcome_section(output).splitlines() if line.startswith("reviewed")
    )
    assert "$0.00" in reviewed_row
    assert "unavailable" in reviewed_row


def test_work_missing_id_is_clear_and_json_output_is_valid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    receipts = tmp_path / "receipts.jsonl"
    outcomes = tmp_path / "outcomes.jsonl"

    assert main(["work", "missing", "--receipts", str(receipts), "--outcomes", str(outcomes)]) == 0
    assert "No work evidence found for 'missing'." in capsys.readouterr().out

    _write_receipts(receipts, [_receipt(attributes={"work_id": "present"})])
    assert (
        main(
            ["work", "present", "--receipts", str(receipts), "--outcomes", str(outcomes), "--json"]
        )
        == 0
    )
    parsed = json.loads(capsys.readouterr().out)
    assert parsed["work_id"] == "present"
    assert parsed["outcome_status"] is None
