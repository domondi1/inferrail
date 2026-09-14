from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError

from inferrail.budgets.enforcement import (
    BudgetEnforcer,
    approx_char_count,
    estimate_upper_bound_usd,
    matching_budgets,
    spent_so_far_usd,
)
from inferrail.budgets.schema import Budget, new_budget_id
from inferrail.budgets.store import BudgetStore
from inferrail.config.models import PriceEntry, ProviderConfig
from inferrail.errors import BudgetExceededError
from inferrail.pricing.resolver import PricingResolver
from inferrail.receipts.builder import new_receipt_id
from inferrail.receipts.schema import InferenceReceipt
from inferrail.receipts.sqlite_store import ReceiptsStore

_PROVIDER_CONFIG = {
    "openai": ProviderConfig(type="openai", api_key_env="TEST_OPENAI_API_KEY"),
}


def _price(input_price: str = "1.00", output_price: str = "2.00") -> PriceEntry:
    return PriceEntry(
        input_usd_per_million=Decimal(input_price),
        output_usd_per_million=Decimal(output_price),
        source="test-fixture",
        verified_date=date(2020, 1, 1),
    )


def _resolver(overrides: dict[str, dict[str, PriceEntry]] | None = None) -> PricingResolver:
    return PricingResolver(_PROVIDER_CONFIG, overrides or {"openai": {"gpt-4o-mini": _price()}})


def _receipt(
    *,
    status: str = "success",
    prompt_tokens: int | None = 1_000_000,
    completion_tokens: int | None = 1_000_000,
    cost: Decimal | None = Decimal("3.000000"),
    attributes: dict[str, str] | None = None,
    timestamp: datetime | None = None,
    model: str = "gpt-4o-mini",
) -> InferenceReceipt:
    return InferenceReceipt(
        receipt_id=new_receipt_id(),
        request_id="req_x",
        timestamp=timestamp or datetime.now(UTC),
        route="default",
        provider="openai",
        model=model,
        status=status,  # type: ignore[arg-type]
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        pricing=_price() if cost is not None else None,
        estimated_cost_usd=cost,
        attributes=attributes or {},
        total_latency_ms=1.0,
        retry_count=0,
    )


# ---------------------------------------------------------------------------
# Budget schema
# ---------------------------------------------------------------------------


def test_global_budget_forbids_scope_value() -> None:
    with pytest.raises(ValidationError):
        Budget(
            budget_id=new_budget_id("global", "acme", "daily"),
            scope="global",
            scope_value="acme",
            window="daily",
            mode="block",
            limit_usd=Decimal("1"),
        )


def test_project_budget_requires_scope_value() -> None:
    with pytest.raises(ValidationError):
        Budget(
            budget_id=new_budget_id("project", None, "daily"),
            scope="project",
            scope_value=None,
            window="daily",
            mode="block",
            limit_usd=Decimal("1"),
        )


def test_per_work_window_requires_work_id_scope() -> None:
    with pytest.raises(ValidationError):
        Budget(
            budget_id=new_budget_id("project", "acme", "per_work"),
            scope="project",
            scope_value="acme",
            window="per_work",
            mode="block",
            limit_usd=Decimal("1"),
        )


def test_budget_id_must_match_deterministic_shape() -> None:
    with pytest.raises(ValidationError):
        Budget(
            budget_id="not-the-real-id",
            scope="global",
            window="daily",
            mode="block",
            limit_usd=Decimal("1"),
        )


def test_new_budget_id_is_deterministic() -> None:
    assert new_budget_id("global", None, "daily") == "global:_:daily"
    assert new_budget_id("project", "acme", "monthly") == "project:acme:monthly"


# ---------------------------------------------------------------------------
# BudgetStore
# ---------------------------------------------------------------------------


def test_store_set_is_an_upsert_keyed_on_budget_id(tmp_path: Path) -> None:
    store = BudgetStore(tmp_path / "budgets.db")
    budget_id = new_budget_id("global", None, "daily")
    store.set(
        Budget(
            budget_id=budget_id, scope="global", window="daily", mode="warn",
            limit_usd=Decimal("10"),
        )
    )
    store.set(
        Budget(
            budget_id=budget_id, scope="global", window="daily", mode="block",
            limit_usd=Decimal("20"),
        )
    )

    budgets = store.list()
    assert len(budgets) == 1
    assert budgets[0].mode == "block"
    assert budgets[0].limit_usd == Decimal("20")


def test_store_list_is_ordered_by_budget_id(tmp_path: Path) -> None:
    store = BudgetStore(tmp_path / "budgets.db")
    store.set(
        Budget(
            budget_id=new_budget_id("work_id", "wf_2", "per_work"), scope="work_id",
            scope_value="wf_2", window="per_work", mode="block", limit_usd=Decimal("1"),
        )
    )
    store.set(
        Budget(
            budget_id=new_budget_id("global", None, "daily"), scope="global",
            window="daily", mode="block", limit_usd=Decimal("1"),
        )
    )

    ids = [b.budget_id for b in store.list()]
    assert ids == sorted(ids)


def test_store_remove_is_idempotent(tmp_path: Path) -> None:
    store = BudgetStore(tmp_path / "budgets.db")
    budget_id = new_budget_id("global", None, "daily")
    store.set(
        Budget(
            budget_id=budget_id, scope="global", window="daily", mode="block",
            limit_usd=Decimal("1"),
        )
    )

    assert store.remove(budget_id) is True
    assert store.remove(budget_id) is False
    assert store.list() == []


def test_store_get_missing_returns_none(tmp_path: Path) -> None:
    store = BudgetStore(tmp_path / "budgets.db")
    assert store.get("global:_:daily") is None


# ---------------------------------------------------------------------------
# Pre-flight estimate
# ---------------------------------------------------------------------------


def test_approx_char_count_sums_nested_strings() -> None:
    value = [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}]
    assert approx_char_count(value) == len("hello") + len("hi") + len("user") + len("assistant")


def test_approx_char_count_handles_none_and_scalars() -> None:
    assert approx_char_count(None) == 0
    assert approx_char_count(42) == 0
    assert approx_char_count("") == 0


def test_estimate_upper_bound_usd_is_none_for_unknown_model() -> None:
    resolver = _resolver()
    estimate = estimate_upper_bound_usd(
        prompt_chars=3000, max_completion_tokens=100, provider="openai",
        model="not-in-any-catalog", pricing_resolver=resolver,
    )
    assert estimate is None


def test_estimate_upper_bound_usd_is_conservative_ceiling() -> None:
    resolver = _resolver(overrides={"openai": {"m": _price("3", "6")}})
    # 3000 chars / 3 chars-per-token == 1000 prompt tokens exactly.
    estimate = estimate_upper_bound_usd(
        prompt_chars=3000, max_completion_tokens=1_000_000, provider="openai",
        model="m", pricing_resolver=resolver,
    )
    # input: 1000 tokens * $3/M = $0.003; output: 1_000_000 * $6/M = $6.0
    assert estimate == Decimal("6.003000")


def test_estimate_upper_bound_usd_rounds_prompt_tokens_up() -> None:
    resolver = _resolver(overrides={"openai": {"m": _price("3", "6")}})
    # 3001 chars / 3 -> ceil to 1001 tokens, not 1000 (never underestimate).
    estimate = estimate_upper_bound_usd(
        prompt_chars=3001, max_completion_tokens=0, provider="openai",
        model="m", pricing_resolver=resolver,
    )
    assert estimate == Decimal("3") * Decimal("1001") / Decimal(1_000_000)


# ---------------------------------------------------------------------------
# matching_budgets
# ---------------------------------------------------------------------------


def _budget(scope: str, scope_value: str | None, window: str, mode: str, limit: str) -> Budget:
    return Budget(
        budget_id=new_budget_id(scope, scope_value, window),  # type: ignore[arg-type]
        scope=scope,  # type: ignore[arg-type]
        scope_value=scope_value,
        window=window,  # type: ignore[arg-type]
        mode=mode,  # type: ignore[arg-type]
        limit_usd=Decimal(limit),
    )


def test_global_budget_matches_every_request() -> None:
    budgets = [_budget("global", None, "daily", "block", "1")]
    assert matching_budgets(budgets, {}) == budgets
    assert matching_budgets(budgets, {"project": "acme"}) == budgets


def test_project_budget_matches_only_its_own_project() -> None:
    budget = _budget("project", "acme", "monthly", "block", "1")
    assert matching_budgets([budget], {"project": "acme"}) == [budget]
    assert matching_budgets([budget], {"project": "other"}) == []
    assert matching_budgets([budget], {}) == []


def test_work_id_budget_matches_only_its_own_work_id() -> None:
    budget = _budget("work_id", "wf_1", "per_work", "block", "1")
    assert matching_budgets([budget], {"work_id": "wf_1"}) == [budget]
    assert matching_budgets([budget], {"work_id": "wf_2"}) == []


def test_matching_budgets_is_sorted_by_budget_id() -> None:
    b1 = _budget("work_id", "wf_2", "per_work", "block", "1")
    b2 = _budget("global", None, "daily", "block", "1")
    result = matching_budgets([b1, b2], {"work_id": "wf_2"})
    assert [b.budget_id for b in result] == sorted([b1.budget_id, b2.budget_id])


# ---------------------------------------------------------------------------
# spent_so_far_usd
# ---------------------------------------------------------------------------


def test_spent_so_far_sums_only_success_and_partial(tmp_path: Path) -> None:
    store = ReceiptsStore(tmp_path / "receipts.db")
    store.emit(_receipt(status="success", cost=Decimal("1")))
    store.emit(_receipt(status="partial", cost=Decimal("2")))
    store.emit(_receipt(status="error", prompt_tokens=None, completion_tokens=None, cost=None))

    result = spent_so_far_usd(store, _budget("global", None, "monthly", "block", "1"))
    assert result.spent_usd == Decimal("3")
    assert result.has_unpriced_usage is False


def test_spent_so_far_flags_unpriced_usage_without_fabricating_cost(tmp_path: Path) -> None:
    store = ReceiptsStore(tmp_path / "receipts.db")
    store.emit(_receipt(status="success", cost=None))

    result = spent_so_far_usd(store, _budget("global", None, "monthly", "block", "1"))
    assert result.spent_usd == Decimal(0)
    assert result.has_unpriced_usage is True


def test_spent_so_far_respects_daily_window(tmp_path: Path) -> None:
    store = ReceiptsStore(tmp_path / "receipts.db")
    yesterday = datetime.now(UTC) - timedelta(days=1, hours=1)
    store.emit(_receipt(status="success", cost=Decimal("5"), timestamp=yesterday))
    store.emit(_receipt(status="success", cost=Decimal("2")))

    result = spent_so_far_usd(store, _budget("global", None, "daily", "block", "100"))
    assert result.spent_usd == Decimal("2")


def test_spent_so_far_scopes_by_project(tmp_path: Path) -> None:
    store = ReceiptsStore(tmp_path / "receipts.db")
    store.emit(_receipt(status="success", cost=Decimal("1"), attributes={"project": "acme"}))
    store.emit(_receipt(status="success", cost=Decimal("9"), attributes={"project": "other"}))

    result = spent_so_far_usd(store, _budget("project", "acme", "monthly", "block", "100"))
    assert result.spent_usd == Decimal("1")


# ---------------------------------------------------------------------------
# BudgetEnforcer.check
# ---------------------------------------------------------------------------


def test_check_is_noop_with_no_budgets(tmp_path: Path) -> None:
    store = BudgetStore(tmp_path / "budgets.db")
    receipts = ReceiptsStore(tmp_path / "receipts.db")
    enforcer = BudgetEnforcer(store, receipts, _resolver())

    enforcer.check(
        provider="openai", model="gpt-4o-mini", attributes={}, prompt_chars=10,
        max_completion_tokens=10,
    )  # must not raise


def test_check_blocks_when_projected_exceeds_limit(tmp_path: Path) -> None:
    store = BudgetStore(tmp_path / "budgets.db")
    receipts = ReceiptsStore(tmp_path / "receipts.db")
    store.set(_budget("global", None, "daily", "block", "0.01"))
    enforcer = BudgetEnforcer(store, receipts, _resolver())

    with pytest.raises(BudgetExceededError) as exc_info:
        enforcer.check(
            provider="openai", model="gpt-4o-mini", attributes={}, prompt_chars=100,
            max_completion_tokens=1_000_000,
        )
    exc = exc_info.value
    assert exc.budget_id == "global:_:daily"
    assert exc.mode == "block"
    assert exc.projected_total_usd > exc.limit_usd


def test_check_does_not_raise_in_warn_mode(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    store = BudgetStore(tmp_path / "budgets.db")
    receipts = ReceiptsStore(tmp_path / "receipts.db")
    store.set(_budget("global", None, "daily", "warn", "0.01"))
    enforcer = BudgetEnforcer(store, receipts, _resolver())

    enforcer.check(
        provider="openai", model="gpt-4o-mini", attributes={}, prompt_chars=100,
        max_completion_tokens=1_000_000,
    )  # must not raise despite exceeding the limit


def test_check_is_noop_when_price_unknown(tmp_path: Path) -> None:
    store = BudgetStore(tmp_path / "budgets.db")
    receipts = ReceiptsStore(tmp_path / "receipts.db")
    store.set(_budget("global", None, "daily", "block", "0.0000001"))
    enforcer = BudgetEnforcer(store, receipts, _resolver())

    enforcer.check(
        provider="openai", model="totally-unrecognized-model", attributes={},
        prompt_chars=1_000_000, max_completion_tokens=1_000_000,
    )  # unknown price -> nothing to project -> never blocks


def test_check_ignores_non_matching_scoped_budget(tmp_path: Path) -> None:
    store = BudgetStore(tmp_path / "budgets.db")
    receipts = ReceiptsStore(tmp_path / "receipts.db")
    store.set(_budget("project", "acme", "monthly", "block", "0.01"))
    enforcer = BudgetEnforcer(store, receipts, _resolver())

    enforcer.check(
        provider="openai", model="gpt-4o-mini", attributes={"project": "someone-else"},
        prompt_chars=100, max_completion_tokens=1_000_000,
    )  # must not raise: this request isn't scoped to the "acme" project


# ---------------------------------------------------------------------------
# BudgetEnforcer.augment_overrun
# ---------------------------------------------------------------------------


def test_augment_overrun_adds_field_when_actual_cost_exceeds_limit(tmp_path: Path) -> None:
    store = BudgetStore(tmp_path / "budgets.db")
    receipts = ReceiptsStore(tmp_path / "receipts.db")
    store.set(_budget("global", None, "daily", "warn", "1"))
    enforcer = BudgetEnforcer(store, receipts, _resolver())

    result = enforcer.augment_overrun(
        {"customer": "acme"}, provider="openai", model="gpt-4o-mini",
        prompt_tokens=1_000_000, completion_tokens=1_000_000,
    )

    assert result["customer"] == "acme"
    assert Decimal(result["budget_overrun_usd"]) == Decimal("2.000000")


def test_augment_overrun_is_noop_when_under_limit(tmp_path: Path) -> None:
    store = BudgetStore(tmp_path / "budgets.db")
    receipts = ReceiptsStore(tmp_path / "receipts.db")
    store.set(_budget("global", None, "daily", "warn", "1000"))
    enforcer = BudgetEnforcer(store, receipts, _resolver())

    attributes = {"customer": "acme"}
    result = enforcer.augment_overrun(
        attributes, provider="openai", model="gpt-4o-mini",
        prompt_tokens=1, completion_tokens=1,
    )

    assert result == attributes
    assert "budget_overrun_usd" not in result


def test_augment_overrun_is_noop_with_no_matching_budgets(tmp_path: Path) -> None:
    store = BudgetStore(tmp_path / "budgets.db")
    receipts = ReceiptsStore(tmp_path / "receipts.db")
    enforcer = BudgetEnforcer(store, receipts, _resolver())

    attributes = {"customer": "acme"}
    result = enforcer.augment_overrun(
        attributes, provider="openai", model="gpt-4o-mini",
        prompt_tokens=1_000_000, completion_tokens=1_000_000,
    )

    assert result is attributes


def test_augment_overrun_reports_the_worst_of_several_matching_budgets(tmp_path: Path) -> None:
    store = BudgetStore(tmp_path / "budgets.db")
    receipts = ReceiptsStore(tmp_path / "receipts.db")
    store.set(_budget("global", None, "daily", "warn", "1"))
    store.set(_budget("project", "acme", "monthly", "warn", "2.5"))
    enforcer = BudgetEnforcer(store, receipts, _resolver())

    result = enforcer.augment_overrun(
        {"project": "acme"}, provider="openai", model="gpt-4o-mini",
        prompt_tokens=1_000_000, completion_tokens=1_000_000,
    )

    # actual cost = $3.0; global overrun = 3 - 1 = 2; project overrun = 3 - 2.5 = 0.5
    assert Decimal(result["budget_overrun_usd"]) == Decimal("2.000000")
