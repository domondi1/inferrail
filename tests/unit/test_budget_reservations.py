"""Atomic budget reservations — see docs/adr/0021-atomic-budget-reservations.md.

available = limit − committed spend − outstanding reservations; admission
reserves atomically before the provider call; settlement releases (usage
known and priced, or the provider definitely rejected the attempt) or
holds (the provider may have billed but cost is unknown).
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from _budget_harness import (
    ENGINE_HELLO_1000_RESERVATION,
    HELLO_1000_RESERVATION,
    WORK_ID,
    GatedAnthropicProvider,
    GatedProvider,
    make_harness,
    ok_response,
    settle_tasks,
)
from _fakes import FakeProvider

from inferrail.errors import (
    BudgetExceededError,
    BudgetUnpricedModelError,
    InvalidRequestError,
    ProviderError,
    ProviderTimeoutError,
)
from inferrail.errors.codes import code_for
from inferrail.gateway import execution as execution_module
from inferrail.gateway.anthropic_schemas import MessagesRequest
from inferrail.gateway.schemas import ChatCompletionRequest

ATTRS = {"work_id": WORK_ID}


def _chat(model: str = "default", **overrides: object) -> ChatCompletionRequest:
    body: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
        "max_tokens": 1000,
    }
    body.update(overrides)
    return ChatCompletionRequest.model_validate(body)


def _reserve_kwargs(**overrides: object) -> dict[str, object]:
    kwargs: dict[str, object] = {
        "request_id": "req_test",
        "provider": "openai",
        "model": "gpt-4o-mini",
        "attributes": ATTRS,
        "prompt_chars": 5,
        "max_completion_tokens": 1000,
    }
    kwargs.update(overrides)
    return kwargs


@pytest.fixture(autouse=True)
def _no_retry_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(execution_module, "_RETRY_BACKOFF_BASE_SECONDS", 0)


# ---------------------------------------------------------------------------
# BudgetEnforcer.reserve / release / hold
# ---------------------------------------------------------------------------


def test_reserve_records_an_outstanding_reservation(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("0.005")

    reservation = h.enforcer.reserve(**_reserve_kwargs())

    assert reservation is not None
    assert reservation.amount_usd == HELLO_1000_RESERVATION
    assert reservation.work_id == WORK_ID
    assert reservation.state == "active"
    assert [r.reservation_id for r in h.budgets.list_reservations()] == [
        reservation.reservation_id
    ]


def test_outstanding_reservations_count_against_the_limit_with_no_receipts(
    tmp_path: Path,
) -> None:
    h = make_harness(tmp_path)
    h.add_budget("0.005")  # room for exactly two $0.002002 reservations

    assert h.enforcer.reserve(**_reserve_kwargs()) is not None
    assert h.enforcer.reserve(**_reserve_kwargs()) is not None
    with pytest.raises(BudgetExceededError) as exc_info:
        h.enforcer.reserve(**_reserve_kwargs())

    # The refusal reports committed spend and reservations separately —
    # a reservation is never presented as spend.
    assert exc_info.value.spent_so_far_usd == Decimal(0)
    assert exc_info.value.reserved_usd == 2 * HELLO_1000_RESERVATION
    assert len(h.budgets.list_reservations()) == 2


def test_release_frees_the_reserved_amount(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("0.003")
    first = h.enforcer.reserve(**_reserve_kwargs())
    assert first is not None
    with pytest.raises(BudgetExceededError):
        h.enforcer.reserve(**_reserve_kwargs())

    h.enforcer.release(first)

    assert h.budgets.list_reservations() == []
    assert h.enforcer.reserve(**_reserve_kwargs()) is not None


def test_hold_keeps_the_reserved_amount_counted(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("0.003")
    first = h.enforcer.reserve(**_reserve_kwargs())
    assert first is not None

    h.enforcer.hold(first)

    [held] = h.budgets.list_reservations()
    assert held.state == "held"
    with pytest.raises(BudgetExceededError):
        h.enforcer.reserve(**_reserve_kwargs())


def test_no_matching_budget_reserves_nothing(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("0.005", scope_value="some-other-run")

    assert h.enforcer.reserve(**_reserve_kwargs()) is None
    assert h.budgets.list_reservations() == []


def test_reservation_counts_against_every_matching_scope(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("0.003", scope="global", window="daily")
    h.add_budget("1.00")  # the run's own budget has plenty of room

    assert h.enforcer.reserve(**_reserve_kwargs()) is not None
    # A different run is still bound by the shared global budget.
    with pytest.raises(BudgetExceededError) as exc_info:
        h.enforcer.reserve(**_reserve_kwargs(attributes={"work_id": "run-2"}))
    assert exc_info.value.budget_id == "global:_:daily"


def test_reservation_for_one_work_id_does_not_count_against_another(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("0.003")
    h.add_budget("0.003", scope_value="run-2")

    assert h.enforcer.reserve(**_reserve_kwargs()) is not None
    assert h.enforcer.reserve(**_reserve_kwargs(attributes={"work_id": "run-2"})) is not None


def test_held_reservation_from_a_previous_window_no_longer_counts(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("0.003", scope="global", window="daily")
    old = h.enforcer.reserve(**_reserve_kwargs())
    assert old is not None
    h.enforcer.hold(old)
    yesterday = datetime.now(UTC) - timedelta(days=1, hours=1)
    with sqlite3.connect(h.budgets.db_path) as conn:
        conn.execute(
            "UPDATE budget_reservations SET created_at = ? WHERE reservation_id = ?",
            (yesterday.timestamp(), old.reservation_id),
        )

    assert h.enforcer.reserve(**_reserve_kwargs()) is not None


def test_warn_mode_budget_never_refuses_a_reservation(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("0.000001", mode="warn")

    reservation = h.enforcer.reserve(**_reserve_kwargs())

    assert reservation is not None  # still reserved, so warn projections see in-flight spend


# ---------------------------------------------------------------------------
# Unknown pricing
# ---------------------------------------------------------------------------


def test_unpriced_model_is_refused_under_a_block_budget(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("100")

    with pytest.raises(BudgetUnpricedModelError) as exc_info:
        h.enforcer.reserve(**_reserve_kwargs(model="mystery-model"))

    assert exc_info.value.budget_id == f"work_id:{WORK_ID}:per_work"
    assert exc_info.value.model == "mystery-model"
    assert code_for(exc_info.value).code == "INFERRAIL_E012"
    assert h.budgets.list_reservations() == []


def test_unpriced_model_under_only_a_warn_budget_is_admitted_without_a_reservation(
    tmp_path: Path,
) -> None:
    h = make_harness(tmp_path)
    h.add_budget("100", mode="warn")

    assert h.enforcer.reserve(**_reserve_kwargs(model="mystery-model")) is None


async def test_unpriced_model_never_reaches_the_provider(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("100")
    provider = FakeProvider()
    engine = h.openai_engine(provider)

    with pytest.raises(BudgetUnpricedModelError):
        await engine.execute(_chat("unpriced"), attributes=ATTRS)

    assert provider.calls == []
    [receipt] = h.receipts.read_all()[0]
    assert receipt.status == "error"
    assert receipt.attributes["budget_id"] == f"work_id:{WORK_ID}:per_work"


# ---------------------------------------------------------------------------
# Crash / restart and multi-process
# ---------------------------------------------------------------------------


def test_active_reservation_survives_a_restart(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("0.003")
    assert h.enforcer.reserve(**_reserve_kwargs()) is not None
    # The process dies here without settling.

    restarted = h.fresh_enforcer()

    with pytest.raises(BudgetExceededError):
        restarted.reserve(**_reserve_kwargs())


def test_concurrent_admission_across_processes_never_over_reserves(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("0.0101")  # room for exactly five $0.002002 reservations
    workers = 16
    barrier = threading.Barrier(workers)

    def attempt(i: int) -> bool:
        enforcer = h.fresh_enforcer()  # its own connections, like a separate process
        barrier.wait()
        try:
            enforcer.reserve(**_reserve_kwargs(request_id=f"req_{i}"))
        except BudgetExceededError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=workers) as pool:
        admitted = sum(pool.map(attempt, range(workers)))

    assert admitted == 5
    assert len(h.budgets.list_reservations()) == 5


# ---------------------------------------------------------------------------
# Through the engine: concurrency, parallel fan-out, settlement
# ---------------------------------------------------------------------------


async def test_concurrent_calls_sharing_a_work_id_cannot_spend_the_same_budget(
    tmp_path: Path,
) -> None:
    h = make_harness(tmp_path)
    h.add_budget("0.005")  # room for two in-flight calls
    provider = GatedProvider()
    engine = h.openai_engine(provider)

    # An agent step fanning out into six parallel tool-driven model calls.
    tasks = [
        asyncio.create_task(engine.execute(_chat(), attributes=ATTRS)) for _ in range(6)
    ]
    await settle_tasks()
    provider.gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    succeeded = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, BudgetExceededError)]
    assert len(succeeded) == 2
    assert len(refused) == 4
    assert len(provider.calls) == 2  # refused calls never reached the provider
    assert h.budgets.list_reservations() == []  # both settled and released


async def test_concurrent_anthropic_calls_are_admitted_atomically(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("0.005")
    provider = GatedAnthropicProvider()
    engine = h.anthropic_engine(provider)
    request = MessagesRequest.model_validate(
        {"model": "claude", "max_tokens": 1000, "messages": [{"role": "user", "content": "hello"}]}
    )

    tasks = [
        asyncio.create_task(engine.execute(request, attributes=ATTRS)) for _ in range(5)
    ]
    await settle_tasks()
    provider.gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)

    assert sum(not isinstance(r, BaseException) for r in results) == 2
    assert len(provider.calls) == 2
    assert h.budgets.list_reservations() == []


async def test_success_releases_the_reservation_after_the_receipt_is_committed(
    tmp_path: Path,
) -> None:
    h = make_harness(tmp_path)
    h.add_budget("1.00")
    engine = h.openai_engine(FakeProvider())

    await engine.execute(_chat(), attributes=ATTRS)

    assert h.budgets.list_reservations() == []
    [receipt] = h.receipts.read_all()[0]
    assert receipt.estimated_cost_usd is not None
    assert "budget_held_usd" not in receipt.attributes


async def test_provider_http_error_releases_the_reservation(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("1.00")
    provider = FakeProvider(
        outcomes=[InvalidRequestError("bad", provider="openai", status_code=400)]
    )
    engine = h.openai_engine(provider)

    with pytest.raises(InvalidRequestError):
        await engine.execute(_chat(), attributes=ATTRS)

    assert h.budgets.list_reservations() == []


async def test_timeout_holds_the_reservation_and_says_so_on_the_receipt(
    tmp_path: Path,
) -> None:
    h = make_harness(tmp_path)
    h.add_budget("1.00")
    provider = FakeProvider(outcomes=[ProviderTimeoutError("slow", provider="openai")])
    engine = h.openai_engine(provider)

    with pytest.raises(ProviderTimeoutError):
        await engine.execute(_chat(), attributes=ATTRS)

    [held] = h.budgets.list_reservations()
    assert held.state == "held"
    [receipt] = h.receipts.read_all()[0]
    assert receipt.estimated_cost_usd is None  # never presented as a cost
    assert Decimal(receipt.attributes["budget_held_usd"]) == ENGINE_HELLO_1000_RESERVATION


async def test_transport_failure_without_http_status_holds(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("1.00")
    provider = FakeProvider(outcomes=[ProviderError("reset", provider="openai")])
    engine = h.openai_engine(provider)

    with pytest.raises(ProviderError):
        await engine.execute(_chat(), attributes=ATTRS)

    [held] = h.budgets.list_reservations()
    assert held.state == "held"


async def test_retry_after_http_5xx_reserves_again_and_releases_both(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("1.00")
    provider = FakeProvider(
        outcomes=[
            ProviderError("boom", provider="openai", status_code=500, retryable=True),
            ok_response(),
        ]
    )
    engine = h.openai_engine(provider, max_retries=1)

    await engine.execute(_chat(), attributes=ATTRS)

    assert len(provider.calls) == 2
    assert h.budgets.list_reservations() == []


async def test_retry_after_timeout_keeps_the_first_attempt_held(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("1.00")
    provider = FakeProvider(
        outcomes=[ProviderTimeoutError("slow", provider="openai"), ok_response()]
    )
    engine = h.openai_engine(provider, max_retries=1)

    await engine.execute(_chat(), attributes=ATTRS)

    [held] = h.budgets.list_reservations()
    assert held.state == "held"
    [receipt] = h.receipts.read_all()[0]
    assert receipt.status == "success"
    assert Decimal(receipt.attributes["budget_held_usd"]) == ENGINE_HELLO_1000_RESERVATION


async def test_retry_is_refused_when_the_held_attempt_used_up_the_budget(
    tmp_path: Path,
) -> None:
    h = make_harness(tmp_path)
    h.add_budget("0.003")  # room for exactly one reservation
    provider = FakeProvider(
        outcomes=[ProviderTimeoutError("slow", provider="openai"), ok_response()]
    )
    engine = h.openai_engine(provider, max_retries=1)

    with pytest.raises(BudgetExceededError):
        await engine.execute(_chat(), attributes=ATTRS)

    assert len(provider.calls) == 1  # the retry never reached the provider
    [receipt] = h.receipts.read_all()[0]
    assert receipt.attributes["budget_id"] == f"work_id:{WORK_ID}:per_work"
    assert Decimal(receipt.attributes["budget_held_usd"]) == ENGINE_HELLO_1000_RESERVATION


async def test_actual_cost_above_the_reservation_is_recorded_as_overrun(
    tmp_path: Path,
) -> None:
    h = make_harness(tmp_path)
    h.add_budget("0.01")
    provider = FakeProvider(outcomes=[ok_response(prompt_tokens=10, completion_tokens=100_000)])
    engine = h.openai_engine(provider)

    await engine.execute(_chat(), attributes=ATTRS)  # reserved $0.002, actually cost ~$0.2

    [receipt] = h.receipts.read_all()[0]
    assert Decimal(receipt.attributes["budget_overrun_usd"]) > 0
    assert h.budgets.list_reservations() == []
    with pytest.raises(BudgetExceededError):
        await engine.execute(_chat(), attributes=ATTRS)


async def test_reservation_estimate_counts_tool_definitions(tmp_path: Path) -> None:
    h = make_harness(tmp_path)
    h.add_budget("0.0021")  # fits "hello" + 1000 output tokens, not a large tool schema
    engine = h.openai_engine(FakeProvider())
    big_tool = {
        "type": "function",
        "function": {"name": "lookup", "description": "x" * 600_000, "parameters": {}},
    }

    with pytest.raises(BudgetExceededError):
        await engine.execute(_chat(tools=[big_tool]), attributes=ATTRS)


async def test_cancelled_attempt_holds_instead_of_leaking_an_active_reservation(
    tmp_path: Path,
) -> None:
    h = make_harness(tmp_path)
    h.add_budget("1.00")
    provider = GatedProvider()  # never released: the call stays in flight
    engine = h.openai_engine(provider)

    task = asyncio.create_task(engine.execute(_chat(), attributes=ATTRS))
    await settle_tasks()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    [held] = h.budgets.list_reservations()
    assert held.state == "held"
