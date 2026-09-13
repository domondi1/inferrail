"""Retry adapters: the customer-supplied callback interface this release
uses to execute the one permitted machine retry.

`RetryAdapter` is a `Protocol`, not a network contract Inferrail owns --
implementations run inside the customer's own process, against whatever
extraction system and credentials the customer controls. Invoice
contents and provider credentials never leave that process; the engine
only ever reads back a `RetryAttemptResult` (status/cost/confidence),
never the re-extracted field values themselves (see `models.RetryAttemptResult`).

Two reference implementations ship with this release:

- `FixtureRetryAdapter` -- deterministic, offline, no network call. Used
  for reproducible tests and the fixture-labeled demo path.
- `OpenAIRetryAdapter` -- a working, real-provider adapter that re-runs
  structured-field extraction via one OpenAI chat-completions call (JSON
  mode), using a caller-supplied API key. Requires `OPENAI_API_KEY` (or
  an explicitly passed key) and network access; every call it makes is
  labeled live-provider execution, never presented as deterministic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol
from uuid import uuid4

from inferrail.pricing.builtin import BUILTIN_OPENAI_PRICING

from .models import AttemptStatus, CostEstimate, ExceptionCase, RetryAttemptResult

# The output-token ceiling `OpenAIRetryAdapter.estimate_cost` assumes when
# projecting a worst-case cost, and the actual `max_completion_tokens` the
# real API call is capped at -- these must stay equal, or the estimate
# would not be a real bound on what the call can cost.
_MAX_COMPLETION_TOKENS = 500


class RetryAdapter(Protocol):
    """The callback interface a customer implements to execute one
    retry. Called at most once per `ExceptionCase`, synchronously, by
    `engine.RecoveryEngine`. Must never be called more than once for the
    same checkpoint -- the engine enforces this via its idempotency
    store, not this protocol.

    `estimate_cost` (see below) is **not** part of this required
    interface -- an adapter implements it only if it can genuinely bound
    its next call's cost. `Protocol` cannot express "optional method" at
    runtime, so declaring it here would force every adapter to implement
    it (or be cast past the check); instead, `get_cost_estimate` detects
    support via `getattr`.
    """

    name: str

    def retry(self, case: ExceptionCase) -> RetryAttemptResult: ...


def get_cost_estimate(adapter: RetryAdapter, case: ExceptionCase) -> CostEstimate | None:
    """Calls `adapter.estimate_cost(case)` if the adapter implements it,
    else returns `None`. Used by `engine.RecoveryEngine._execute_retry`
    (via `policy.authorize_retry_cost`) to decide whether the next paid
    attempt may proceed at all. `None` is never treated as "free" -- it
    means "this adapter cannot bound the cost," which `authorize_retry_cost`
    always treats as not-authorized, routing to human review instead of
    invoking an unbounded action."""
    estimate_fn = getattr(adapter, "estimate_cost", None)
    if estimate_fn is None:
        return None
    result = estimate_fn(case)
    return result if isinstance(result, CostEstimate) else None


@dataclass(frozen=True)
class FixtureRetryAdapter:
    """Deterministic, offline reference adapter for reproducible tests
    and the fixture-labeled demo. Looks up a pre-recorded result by
    `work_id`; makes no network call and needs no credentials.

    **Fixture-based execution.** Every result this adapter returns is
    canned, not a real extraction re-attempt -- label any demo output it
    produces as such.
    """

    results_by_work_id: dict[str, RetryAttemptResult]
    name: str = "fixture_retry_adapter"
    estimated_costs_by_work_id: dict[str, Decimal] = field(default_factory=dict)
    """Declared pre-flight cost estimate per work_id, for exercising
    `policy.authorize_retry_cost` in tests/demos. A work_id with no entry
    here has no estimate (`estimate_cost` returns `None`) -- never a
    fabricated one."""

    def retry(self, case: ExceptionCase) -> RetryAttemptResult:
        result = self.results_by_work_id.get(case.work_id)
        if result is None:
            raise KeyError(
                f"FixtureRetryAdapter has no recorded result for work_id={case.work_id!r} "
                "-- add one to results_by_work_id; this adapter never fabricates a result"
            )
        return result

    def estimate_cost(self, case: ExceptionCase) -> CostEstimate | None:
        amount = self.estimated_costs_by_work_id.get(case.work_id)
        if amount is None:
            return None
        return CostEstimate(amount_usd=amount, basis="fixture_declared")


class OpenAIRetryAdapter:
    """Real-provider reference adapter: re-runs structured-field
    extraction via one OpenAI chat-completions call (JSON mode).

    **Live-provider execution.** Requires network access and a real
    `OPENAI_API_KEY` (from the caller's own environment/process -- never
    sent to or read by any Inferrail-operated service). The invoice text
    supplied to `retry()` is sent directly from the caller's process to
    OpenAI's API, exactly as a customer's own extraction pipeline already
    would -- Inferrail does not proxy, log, or store it.

    This adapter answers exactly one question per call: given the same
    invoice text the checkpoint attempt saw, re-extract the required
    fields and report a confidence estimate -- it does not decide
    retry-vs-review (that's `policy.recommend`) and does not touch a
    real vendor's production system.
    """

    name = "openai_retry_adapter"

    def __init__(
        self,
        *,
        invoice_text_by_work_id: dict[str, str],
        required_fields: tuple[str, ...],
        api_key: str | None = None,
        model: str = "gpt-4o-mini",
        client: object | None = None,
    ) -> None:
        self._invoice_text_by_work_id = invoice_text_by_work_id
        self._required_fields = required_fields
        self._model = model
        self._api_key = api_key
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        import os

        from openai import OpenAI

        api_key = self._api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OpenAIRetryAdapter requires OPENAI_API_KEY (or an explicit api_key) -- "
                "this is live-provider execution, never silently substituted with a fixture"
            )
        # max_retries=0: the openai SDK defaults to retrying transient
        # errors itself (commonly up to 2 extra HTTP requests per call),
        # which would silently violate this adapter's "called at most
        # once" promise -- Inferrail's own retry-vs-review decision, not
        # the HTTP client's, governs whether a second attempt happens.
        # An explicit timeout keeps one call from hanging past the
        # decision deadline instead of failing fast into the ambiguous
        # path (see engine.RecoveryEngine._execute_retry).
        return OpenAI(api_key=api_key, max_retries=0, timeout=30.0)

    def estimate_cost(self, case: ExceptionCase) -> CostEstimate | None:
        """A defensible upper bound, not a hard billing guarantee -- see
        `models.CostEstimate`. Projects input tokens from the invoice
        text's character length (~4 chars/token, a standard rough
        estimator) and assumes the worst case of `_MAX_COMPLETION_TOKENS`
        output tokens, the same ceiling the real call in `retry()` is
        capped at via `max_completion_tokens` -- without that cap, this
        estimate would not actually bound the call it authorizes."""
        invoice_text = self._invoice_text_by_work_id.get(case.work_id)
        if invoice_text is None:
            return None
        price = BUILTIN_OPENAI_PRICING.get(self._model)
        if price is None:
            return None
        projected_input_tokens = max(1, len(invoice_text) // 4)
        amount = (
            Decimal(projected_input_tokens) * price.input_usd_per_million / Decimal(1_000_000)
            + Decimal(_MAX_COMPLETION_TOKENS)
            * price.output_usd_per_million
            / Decimal(1_000_000)
        )
        return CostEstimate(
            amount_usd=amount,
            basis=(
                f"{self._model} builtin catalog rate ({price.source}, "
                f"verified {price.verified_date}), ~4 chars/token input "
                f"projection, {_MAX_COMPLETION_TOKENS}-token output ceiling"
            ),
        )

    def retry(self, case: ExceptionCase) -> RetryAttemptResult:
        invoice_text = self._invoice_text_by_work_id.get(case.work_id)
        if invoice_text is None:
            raise KeyError(
                f"no invoice_text recorded for work_id={case.work_id!r} -- "
                "OpenAIRetryAdapter never fabricates input"
            )

        client = self._get_client()
        attempt_id = f"ret_{uuid4().hex[:12]}"

        prompt = (
            "Re-extract the following required fields from this invoice text. "
            "Respond with strict JSON: {\"fields\": {<field>: <value or null>}, "
            "\"confidence\": <float 0-1, your estimate of extraction confidence>}. "
            f"Required fields: {list(self._required_fields)}.\n\nInvoice text:\n{invoice_text}"
        )
        response = client.chat.completions.create(
            model=self._model,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            max_completion_tokens=_MAX_COMPLETION_TOKENS,
        )
        usage = getattr(response, "usage", None)
        cost_usd = _estimate_cost_usd(self._model, usage)

        try:
            content = response.choices[0].message.content
            parsed = json.loads(content) if content else {}
        except (AttributeError, IndexError, json.JSONDecodeError):
            return RetryAttemptResult(
                attempt_id=attempt_id,
                status=AttemptStatus.AMBIGUOUS,
                cost_usd=cost_usd,
                confidence=None,
                provider="openai",
                raw_fields={},
            )

        fields = parsed.get("fields") or {}
        confidence = parsed.get("confidence")
        confidence = float(confidence) if isinstance(confidence, (int, float)) else None
        all_present = all(fields.get(f) not in (None, "") for f in self._required_fields)
        status = AttemptStatus.SUCCESS if all_present else AttemptStatus.PARTIAL

        return RetryAttemptResult(
            attempt_id=attempt_id,
            status=status,
            cost_usd=cost_usd,
            confidence=confidence,
            provider="openai",
            raw_fields={k: str(v) for k, v in fields.items()},
        )


def _estimate_cost_usd(model: str, usage: object) -> Decimal | None:
    """Actual cost from response usage, using the same sourced, dated
    `BUILTIN_OPENAI_PRICING` catalog `estimate_cost` uses for its
    pre-flight bound (`src/inferrail/pricing/builtin.py` -- each entry
    carries its own `source`/`verified_date`, never an unsourced guess).
    `None` (never a fabricated zero) if usage isn't reported or the
    model isn't in that catalog."""
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    if prompt_tokens is None or completion_tokens is None:
        return None
    price = BUILTIN_OPENAI_PRICING.get(model)
    if price is None:
        return None
    return (
        Decimal(prompt_tokens) * price.input_usd_per_million / Decimal(1_000_000)
        + Decimal(completion_tokens) * price.output_usd_per_million / Decimal(1_000_000)
    )
