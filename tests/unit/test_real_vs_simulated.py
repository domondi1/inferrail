"""Simulated (demo) receipts never mix with real spend.

The scenario from a live trial: one work id held six demo calls, one failed real
call and one successful real OpenAI call. Before this split every rollup reported
the demo's illustrative prices as "known cost" alongside the real $0.000109.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from inferrail.budgets.enforcement import spent_so_far_usd
from inferrail.budgets.schema import Budget, new_budget_id
from inferrail.cli.report import aggregate, format_summary, format_table
from inferrail.cli.transaction import format_transaction
from inferrail.cli.work import format_work_aggregate, format_work_summary
from inferrail.config.models import PriceEntry
from inferrail.receipts.aggregation import summarize_receipts
from inferrail.receipts.builder import new_receipt_id
from inferrail.receipts.classification import is_simulated
from inferrail.receipts.schema import InferenceReceipt
from inferrail.receipts.sqlite_store import ReceiptsStore
from inferrail.transactions.builder import build_transaction
from inferrail.work.builder import build_work_summary

_T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
_DEMO_PRICE = PriceEntry(
    input_usd_per_million=Decimal("0.50"),
    output_usd_per_million=Decimal("1.50"),
    source="DEMO -- a made-up round number, not a real provider price",
    verified_date=date(2026, 11, 2),
)
_REAL_PRICE = PriceEntry(
    input_usd_per_million=Decimal("0.05"),
    output_usd_per_million=Decimal("0.40"),
    source="https://openai.com/api/pricing/",
    verified_date=date(2026, 11, 2),
)
WORK = {"work_id": "support-ticket-42", "task_id": "triage"}


def _receipt(n: int, *, provider: str, price: PriceEntry | None, cost: str | None,
             status: str = "success", tokens: tuple[int, int] | None = (12, 272),
             ) -> InferenceReceipt:  # fmt: skip
    return InferenceReceipt(
        receipt_id=new_receipt_id(),
        request_id=f"req_{n}",
        timestamp=_T0 + timedelta(seconds=n),
        route="default",
        provider=provider,
        model="demo-model" if provider == "demo" else "gpt-5-nano",
        status=status,  # type: ignore[arg-type]
        prompt_tokens=tokens[0] if tokens else None,
        completion_tokens=tokens[1] if tokens else None,
        pricing=price,
        estimated_cost_usd=Decimal(cost) if cost is not None else None,
        attributes=dict(WORK),
        total_latency_ms=10.0,
    )


def _ticket_42() -> list[InferenceReceipt]:
    demo = [
        _receipt(i, provider="demo", price=_DEMO_PRICE, cost="0.000160", tokens=(128, 64))
        for i in range(6)
    ]
    failed = _receipt(6, provider="openai", price=None, cost=None, status="error", tokens=None)
    real = _receipt(7, provider="openai", price=_REAL_PRICE, cost="0.000109")
    return [*demo, failed, real]


def test_a_receipt_is_simulated_only_when_provider_and_price_both_say_demo() -> None:
    assert is_simulated(_receipt(0, provider="demo", price=_DEMO_PRICE, cost="0.0001"))
    assert is_simulated(_receipt(0, provider="demo", price=None, cost=None))
    # A real price on a provider called "demo" is real spend: never excluded.
    assert not is_simulated(_receipt(0, provider="demo", price=_REAL_PRICE, cost="0.0001"))
    # A demo-labelled price on a real provider is still real usage.
    assert not is_simulated(_receipt(0, provider="openai", price=_DEMO_PRICE, cost="0.0001"))
    assert not is_simulated(_receipt(0, provider="openai", price=_REAL_PRICE, cost="0.0001"))


def test_shared_economics_keep_real_and_simulated_apart() -> None:
    e = summarize_receipts(_ticket_42())
    assert e.known_cost_usd == Decimal("0.000109")  # real only
    assert e.simulated_cost_usd == Decimal("0.000960")
    assert (e.real_receipt_count, e.simulated_receipt_count) == (2, 6)
    assert (e.prompt_tokens, e.completion_tokens) == (12, 272)  # real tokens only
    assert e.unknown_cost_count == 0  # the failed real call is an error, not unpriced usage
    assert e.status == "partial"  # it still shows that one call failed


def test_work_summary_reports_real_cost_and_demo_cost_separately() -> None:
    s = build_work_summary("support-ticket-42", _ticket_42(), [])
    assert s is not None
    assert s.receipt_count == 8
    assert s.known_attributed_inference_cost_usd == Decimal("0.000109")
    assert s.simulated_cost_usd == Decimal("0.000960")
    assert (s.real_receipt_count, s.simulated_receipt_count) == (2, 6)
    text = format_work_summary(s)
    assert "$0.000109" in text and "6, $0.00096 illustrative" in text
    assert "0.001069" not in text  # the old mixed total never appears


def test_demo_only_work_has_no_real_known_cost() -> None:
    demo = [_receipt(i, provider="demo", price=_DEMO_PRICE, cost="0.000160") for i in range(2)]
    s = build_work_summary("support-ticket-42", demo, [])
    assert s is not None
    assert s.known_attributed_inference_cost_usd is None
    assert s.simulated_cost_usd == Decimal("0.000320")
    assert "unavailable" in format_work_aggregate([s])


def test_transaction_marks_demo_events_and_totals_real_only() -> None:
    t = build_transaction("triage", _ticket_42())
    assert t is not None
    assert t.known_total_cost_usd == Decimal("0.000109")
    assert (t.simulated_event_count, t.simulated_total_cost_usd) == (6, Decimal("0.000960"))
    assert [e.simulated for e in t.events] == [True] * 6 + [False, False]
    text = format_transaction(t)
    assert "Known total cost: $0.000109" in text and "(simulated)" in text


def test_report_shows_demo_calls_in_their_own_column() -> None:
    groups = aggregate(_ticket_42(), "work_id")
    (g,) = groups
    assert g.requests == 8 and g.known_cost_usd == Decimal("0.000109")
    assert (g.simulated_requests, g.simulated_cost_usd) == (6, Decimal("0.000960"))
    table = format_table(groups, "work_id")
    assert "SIMULATED (ILLUSTRATIVE, NOT SPEND)" in table and "6 ($0.00096)" in table
    assert "Simulated:" in format_summary(_ticket_42())


def test_report_on_real_usage_alone_is_unchanged() -> None:
    real = [_receipt(1, provider="openai", price=_REAL_PRICE, cost="0.000109")]
    assert "SIMULATED" not in format_table(aggregate(real, "work_id"), "work_id")
    assert "Simulated" not in format_summary(real)


def test_demo_calls_never_consume_a_real_budget(tmp_path: Path) -> None:
    store = ReceiptsStore(tmp_path / "receipts.db")
    for receipt in _ticket_42():
        store.emit(receipt)
    budget = Budget(
        budget_id=new_budget_id("work_id", "support-ticket-42", "per_work"),
        scope="work_id", scope_value="support-ticket-42", window="per_work",
        mode="block", limit_usd=Decimal("1"),
    )  # fmt: skip
    assert spent_so_far_usd(store, budget).spent_usd == Decimal("0.000109")
