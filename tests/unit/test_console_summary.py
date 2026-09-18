"""`ConsoleSummaryReceiptSink` — the quickstart path's "immediately
visible in the terminal" half of the first-run promise. See
`inferrail.receipts.console_summary`.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from inferrail.config.models import PriceEntry
from inferrail.receipts.console_summary import ConsoleSummaryReceiptSink
from inferrail.receipts.schema import InferenceReceipt


class _RecordingSink:
    def __init__(self) -> None:
        self.received: list[InferenceReceipt] = []

    def emit(self, receipt: InferenceReceipt) -> None:
        self.received.append(receipt)


def _receipt(**overrides: object) -> InferenceReceipt:
    defaults: dict[str, object] = dict(
        receipt_id="ir_test",
        request_id="req_test",
        route="passthrough",
        provider="openai",
        model="gpt-4o-mini",
        status="success",
        prompt_tokens=100,
        completion_tokens=20,
        pricing=PriceEntry(
            input_usd_per_million=Decimal("1.00"),
            output_usd_per_million=Decimal("2.00"),
            source="test-fixture",
            verified_date=date(2020, 1, 1),
        ),
        estimated_cost_usd=Decimal("0.00014"),
        attributes={},
        total_latency_ms=12.5,
    )
    defaults.update(overrides)
    return InferenceReceipt.model_validate(defaults)


def test_console_summary_forwards_every_receipt_unchanged() -> None:
    inner = _RecordingSink()
    sink = ConsoleSummaryReceiptSink(inner)
    receipt = _receipt()

    sink.emit(receipt)

    assert inner.received == [receipt]


def test_console_summary_prints_model_tokens_and_cost(
    capsys: pytest.CaptureFixture[str],
) -> None:
    sink = ConsoleSummaryReceiptSink(_RecordingSink())

    sink.emit(_receipt())

    out = capsys.readouterr().out
    assert "openai/gpt-4o-mini" in out
    assert "100+20 tok" in out
    assert "cost=$0.00014" in out
    assert "no prompt/response ever recorded" in out


def test_console_summary_prints_unknown_for_unpriced_success() -> None:
    sink = ConsoleSummaryReceiptSink(_RecordingSink())
    receipt = _receipt(pricing=None, estimated_cost_usd=None)

    line = _capture_one_line(sink, receipt)

    assert "cost=unknown" in line


def test_console_summary_prints_work_id_when_present() -> None:
    sink = ConsoleSummaryReceiptSink(_RecordingSink())
    receipt = _receipt(attributes={"work_id": "wid-123"})

    line = _capture_one_line(sink, receipt)

    assert "work_id=wid-123" in line


def test_console_summary_never_fabricates_a_cost_for_a_failed_request() -> None:
    sink = ConsoleSummaryReceiptSink(_RecordingSink())
    receipt = _receipt(
        status="error",
        pricing=None,
        estimated_cost_usd=None,
        prompt_tokens=None,
        completion_tokens=None,
    )

    line = _capture_one_line(sink, receipt)

    assert "cost=n/a (error)" in line
    assert "$0" not in line


def _capture_one_line(
    sink: ConsoleSummaryReceiptSink, receipt: InferenceReceipt
) -> str:
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        sink.emit(receipt)
    return buf.getvalue()
