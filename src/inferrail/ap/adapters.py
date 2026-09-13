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
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol
from uuid import uuid4

from .models import AttemptStatus, ExceptionCase, RetryAttemptResult


class RetryAdapter(Protocol):
    """The callback interface a customer implements to execute one
    retry. Called at most once per `ExceptionCase`, synchronously, by
    `engine.RecoveryEngine`. Must never be called more than once for the
    same checkpoint -- the engine enforces this via its idempotency
    store, not this protocol.
    """

    name: str

    def retry(self, case: ExceptionCase) -> RetryAttemptResult: ...


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

    def retry(self, case: ExceptionCase) -> RetryAttemptResult:
        result = self.results_by_work_id.get(case.work_id)
        if result is None:
            raise KeyError(
                f"FixtureRetryAdapter has no recorded result for work_id={case.work_id!r} "
                "-- add one to results_by_work_id; this adapter never fabricates a result"
            )
        return result


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

        from openai import OpenAI  # type: ignore[import-not-found]

        api_key = self._api_key or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError(
                "OpenAIRetryAdapter requires OPENAI_API_KEY (or an explicit api_key) -- "
                "this is live-provider execution, never silently substituted with a fixture"
            )
        return OpenAI(api_key=api_key)

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
    """Best-effort cost estimate from response usage; `None` (never a
    fabricated zero) if usage isn't reported or the model isn't priced
    here. This is deliberately not wired into Inferrail's own gateway
    pricing catalog (`src/inferrail/pricing`) -- this adapter may run
    against a model the catalog doesn't carry, and this module must not
    silently assume one."""
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    completion_tokens = getattr(usage, "completion_tokens", None)
    if prompt_tokens is None or completion_tokens is None:
        return None
    # Labeled placeholder rates for gpt-4o-mini only -- an assumption for
    # demo cost estimation, not a verified, sourced price. Any other model
    # returns None rather than an invented rate.
    if model != "gpt-4o-mini":
        return None
    input_rate = Decimal("0.15") / Decimal(1_000_000)
    output_rate = Decimal("0.60") / Decimal(1_000_000)
    return (Decimal(prompt_tokens) * input_rate) + (Decimal(completion_tokens) * output_rate)
