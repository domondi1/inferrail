from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from inferrail.cli.transaction import format_transaction, run_transaction
from inferrail.receipts.schema import InferenceReceipt
from inferrail.transactions.builder import build_transaction

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


def test_format_transaction_includes_task_and_status() -> None:
    receipts = [
        _receipt(
            attributes={"task_id": "bug_9281"},
            estimated_cost_usd=Decimal("0.450000"),
        )
    ]
    tx = build_transaction("bug_9281", receipts)
    assert tx is not None

    text = format_transaction(tx)

    assert "bug_9281" in text
    assert "success" in text
    assert "$0.45" in text


def test_format_transaction_unknown_total_never_shows_fabricated_zero() -> None:
    receipts = [_receipt(attributes={"task_id": "t"})]  # no pricing -> unknown cost
    tx = build_transaction("t", receipts)
    assert tx is not None

    text = format_transaction(tx)

    assert "$0.00" not in text
    assert "unknown" in text


def test_run_transaction_missing_file_is_not_an_error(tmp_path: Path) -> None:
    result = run_transaction(
        tmp_path / "does-not-exist.jsonl", "bug_9281", "task_id", as_json=False
    )

    assert result == 0


def test_run_transaction_no_match_prints_informative_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "receipts.jsonl"
    good = _receipt(attributes={"task_id": "other_task"})
    path.write_text(good.model_dump_json() + "\n")

    result = run_transaction(path, "bug_9281", "task_id", as_json=False)

    assert result == 0
    out = capsys.readouterr().out
    assert "No transaction found" in out
    assert "bug_9281" in out


def test_run_transaction_prints_formatted_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "receipts.jsonl"
    good = _receipt(attributes={"task_id": "bug_9281"}, estimated_cost_usd=Decimal("0.450000"))
    path.write_text(good.model_dump_json() + "\n")

    result = run_transaction(path, "bug_9281", "task_id", as_json=False)

    assert result == 0
    out = capsys.readouterr().out
    assert "bug_9281" in out
    assert "$0.45" in out


def test_run_transaction_json_output_is_valid_and_matches_schema(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "receipts.jsonl"
    good = _receipt(attributes={"task_id": "bug_9281"}, estimated_cost_usd=Decimal("0.450000"))
    path.write_text(good.model_dump_json() + "\n")

    result = run_transaction(path, "bug_9281", "task_id", as_json=True)

    assert result == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["task_id"] == "bug_9281"
    assert parsed["known_total_cost_usd"] == "0.450000"


def test_run_transaction_reports_skipped_malformed_rows(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "receipts.jsonl"
    good = _receipt(attributes={"task_id": "bug_9281"})
    path.write_text(good.model_dump_json() + "\nnot valid json at all\n")

    result = run_transaction(path, "bug_9281", "task_id", as_json=False)

    assert result == 0
    captured = capsys.readouterr()
    assert "bug_9281" in captured.out
    assert "Skipped 1 malformed" in captured.err


def test_run_transaction_custom_attribute_name(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "receipts.jsonl"
    good = _receipt(attributes={"run_id": "run_42"})
    path.write_text(good.model_dump_json() + "\n")

    result = run_transaction(path, "run_42", "run_id", as_json=False)

    assert result == 0
    assert "run_42" in capsys.readouterr().out
