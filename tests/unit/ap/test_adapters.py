"""Tests for `adapters.py`'s pre-flight cost estimation (defect #3 fix)
and the "called at most once" hardening of `OpenAIRetryAdapter`."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from inferrail.ap.adapters import FixtureRetryAdapter, OpenAIRetryAdapter, get_cost_estimate
from inferrail.ap.models import AttemptStatus, CostEstimate, ExceptionCase, RetryAttemptResult

NOW_CASE = ExceptionCase(
    work_id="W1", checkpoint_attempt_id="A1", failure_type="low_confidence", confidence=0.6,
)


def test_fixture_adapter_estimate_cost_known_and_unknown() -> None:
    adapter = FixtureRetryAdapter(
        results_by_work_id={}, estimated_costs_by_work_id={"W1": Decimal("0.10")}
    )
    known = adapter.estimate_cost(NOW_CASE)
    assert known is not None
    assert known.amount_usd == Decimal("0.10")

    unknown_case = ExceptionCase(
        work_id="NO-ESTIMATE", checkpoint_attempt_id="A2", failure_type="low_confidence",
    )
    assert adapter.estimate_cost(unknown_case) is None


def test_get_cost_estimate_returns_none_for_adapter_without_the_method() -> None:
    class Bare:
        name = "bare"

        def retry(self, case: ExceptionCase) -> RetryAttemptResult:
            raise NotImplementedError

    assert get_cost_estimate(Bare(), NOW_CASE) is None


def test_get_cost_estimate_delegates_to_adapter_estimate_cost() -> None:
    adapter = FixtureRetryAdapter(
        results_by_work_id={}, estimated_costs_by_work_id={"W1": Decimal("0.25")}
    )
    estimate = get_cost_estimate(adapter, NOW_CASE)
    assert estimate == CostEstimate(amount_usd=Decimal("0.25"), basis="fixture_declared")


def _openai_adapter(**kwargs: object) -> OpenAIRetryAdapter:
    return OpenAIRetryAdapter(
        invoice_text_by_work_id={"W1": "invoice text " * 20},
        required_fields=("invoice_number",),
        client=MagicMock(),
        **kwargs,  # type: ignore[arg-type]
    )


def test_openai_adapter_estimate_cost_uses_builtin_catalog() -> None:
    adapter = _openai_adapter(model="gpt-4o-mini")
    estimate = adapter.estimate_cost(NOW_CASE)
    assert estimate is not None
    assert estimate.amount_usd > 0
    assert "gpt-4o-mini" in estimate.basis
    assert "verified" in estimate.basis


def test_openai_adapter_estimate_cost_unknown_model_returns_none() -> None:
    adapter = _openai_adapter(model="some-future-model-not-in-catalog")
    assert adapter.estimate_cost(NOW_CASE) is None


def test_openai_adapter_estimate_cost_unknown_invoice_text_returns_none() -> None:
    adapter = _openai_adapter()
    other_case = ExceptionCase(
        work_id="NOT-RECORDED", checkpoint_attempt_id="A2", failure_type="low_confidence",
    )
    assert adapter.estimate_cost(other_case) is None


def test_openai_client_constructed_with_max_retries_zero_and_explicit_timeout(
    monkeypatch,
) -> None:
    """The openai SDK defaults to retrying transient errors itself --
    left unset, one logical `.retry()` call could silently cause
    multiple real HTTP requests, violating the "called at most once"
    promise. Verifies the client this adapter builds pins max_retries=0."""
    captured: dict[str, object] = {}

    class FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    import inferrail.ap.adapters as adapters_module

    monkeypatch.setitem(
        __import__("sys").modules, "openai", type("m", (), {"OpenAI": FakeOpenAI})()
    )
    adapter = OpenAIRetryAdapter(
        invoice_text_by_work_id={}, required_fields=(), api_key="sk-test",
    )
    adapter._get_client()
    assert captured["max_retries"] == 0
    assert captured["timeout"] == 30.0
    del adapters_module  # imported only to document where _get_client lives


def test_chat_completion_call_sets_max_completion_tokens() -> None:
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content='{"fields": {}, "confidence": 0.5}'))],
        usage=None,
    )
    adapter = OpenAIRetryAdapter(
        invoice_text_by_work_id={"W1": "text"}, required_fields=("x",), client=client,
    )
    adapter.retry(NOW_CASE)
    _args, kwargs = client.chat.completions.create.call_args
    assert kwargs["max_completion_tokens"] == 500


def test_retry_result_status_partial_when_openai_json_parse_fails() -> None:
    client = MagicMock()
    client.chat.completions.create.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="not json"))], usage=None,
    )
    adapter = OpenAIRetryAdapter(
        invoice_text_by_work_id={"W1": "text"}, required_fields=("x",), client=client,
    )
    result = adapter.retry(NOW_CASE)
    assert result.status == AttemptStatus.AMBIGUOUS
