"""`inferrail receipts import|export` (see docs/adr/0013-sqlite-receipts-store.md)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from inferrail.cli.main import main
from inferrail.cli.receipts_io import run_receipts_export, run_receipts_import
from inferrail.receipts.schema import InferenceReceipt
from inferrail.receipts.sqlite_store import ReceiptsStore


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


def test_run_receipts_import_populates_a_fresh_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    jsonl_path = tmp_path / "receipts.jsonl"
    r1, r2 = _receipt(receipt_id="ir_1"), _receipt(receipt_id="ir_2")
    jsonl_path.write_text(r1.model_dump_json() + "\n" + r2.model_dump_json() + "\n")
    db_path = tmp_path / "receipts.sqlite3"

    result = run_receipts_import(jsonl_path, db_path)

    assert result == 0
    assert "imported 2" in capsys.readouterr().out
    receipts, _skipped = ReceiptsStore(db_path).read_all()
    assert {r.receipt_id for r in receipts} == {"ir_1", "ir_2"}


def test_run_receipts_import_missing_jsonl_file_is_not_a_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = run_receipts_import(tmp_path / "does-not-exist.jsonl", tmp_path / "receipts.sqlite3")

    assert result == 1
    assert "No JSONL file found" in capsys.readouterr().out


def test_run_receipts_export_writes_every_stored_receipt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "receipts.sqlite3"
    store = ReceiptsStore(db_path)
    store.emit(_receipt(receipt_id="ir_1"))
    store.emit(_receipt(receipt_id="ir_2"))
    jsonl_path = tmp_path / "exported.jsonl"

    result = run_receipts_export(db_path, jsonl_path)

    assert result == 0
    assert "Exported 2" in capsys.readouterr().out
    lines = jsonl_path.read_text().splitlines()
    assert {json.loads(line)["receipt_id"] for line in lines} == {"ir_1", "ir_2"}


def test_run_receipts_export_missing_db_is_not_a_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = run_receipts_export(tmp_path / "does-not-exist.sqlite3", tmp_path / "out.jsonl")

    assert result == 1
    assert "No SQLite receipts store found" in capsys.readouterr().out


def test_cli_receipts_import_and_export_round_trip_via_main(tmp_path: Path) -> None:
    jsonl_in = tmp_path / "in.jsonl"
    jsonl_in.write_text(_receipt(receipt_id="ir_1").model_dump_json() + "\n")
    db_path = tmp_path / "receipts.sqlite3"
    jsonl_out = tmp_path / "out.jsonl"

    assert main(["receipts", "import", "--jsonl", str(jsonl_in), "--db", str(db_path)]) == 0
    assert main(["receipts", "export", "--db", str(db_path), "--jsonl", str(jsonl_out)]) == 0

    assert json.loads(jsonl_out.read_text().splitlines()[0])["receipt_id"] == "ir_1"


def test_cli_receipts_import_is_idempotent_when_run_twice(tmp_path: Path) -> None:
    jsonl_in = tmp_path / "in.jsonl"
    jsonl_in.write_text(_receipt(receipt_id="ir_1").model_dump_json() + "\n")
    db_path = tmp_path / "receipts.sqlite3"

    assert main(["receipts", "import", "--jsonl", str(jsonl_in), "--db", str(db_path)]) == 0
    assert main(["receipts", "import", "--jsonl", str(jsonl_in), "--db", str(db_path)]) == 0

    receipts, _skipped = ReceiptsStore(db_path).read_all()
    assert len(receipts) == 1
