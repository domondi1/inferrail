"""Shared engine-level harness for the reservation tests
(test_budget_reservations.py, test_streaming_budgets.py,
test_field_policy.py). Not a test module itself — see _fakes.py for the
same convention.

Wires a real `BudgetEnforcer` (real SQLite `BudgetStore` and
`ReceiptsStore` in `tmp_path`) into a real `InferenceEngine` /
`AnthropicInferenceEngine`, so these tests exercise the same admission
and settlement code the gateway runs — only the provider is faked.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from inferrail.budgets.enforcement import BudgetEnforcer
from inferrail.budgets.schema import Budget, BudgetMode, BudgetScope, BudgetWindow, new_budget_id
from inferrail.budgets.store import BudgetStore
from inferrail.config.models import PriceEntry, RouteConfig
from inferrail.gateway.anthropic_execution import AnthropicInferenceEngine
from inferrail.gateway.execution import InferenceEngine
from inferrail.pricing.resolver import PricingResolver
from inferrail.providers.anthropic_base import (
    AnthropicNormalizedRequest,
    AnthropicNormalizedResponse,
)
from inferrail.providers.base import NormalizedChatRequest, NormalizedChatResponse
from inferrail.receipts.sqlite_store import ReceiptsStore
from inferrail.routing.router import Router
from inferrail.telemetry.events import InferenceEvent
from inferrail.telemetry.sinks import TelemetrySink

# $1.00 / $2.00 per million tokens. `BudgetEnforcer.reserve` with
# prompt_chars=5 (2 estimated prompt tokens) and max_completion_tokens=1000
# reserves exactly 2 * 1e-6 + 1000 * 2e-6 = $0.002002.
PRICE = PriceEntry(
    input_usd_per_million=Decimal("1.00"),
    output_usd_per_million=Decimal("2.00"),
    source="test-fixture",
    verified_date=date(2020, 1, 1),
)
HELLO_1000_RESERVATION = Decimal("0.002002")
# Through an engine, the estimate counts every string in the message, so
# {"role": "user", "content": "hello"} is 9 chars -> 3 tokens: $0.002003.
ENGINE_HELLO_1000_RESERVATION = Decimal("0.002003")

WORK_ID = "run-1"


class NullTelemetry(TelemetrySink):
    def __init__(self) -> None:
        self.events: list[InferenceEvent] = []

    def emit(self, event: InferenceEvent) -> None:
        self.events.append(event)


def ok_response(prompt_tokens: int = 3, completion_tokens: int = 2) -> NormalizedChatResponse:
    return NormalizedChatResponse(
        content="ok",
        finish_reason="stop",
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )


def ok_anthropic_response() -> AnthropicNormalizedResponse:
    return AnthropicNormalizedResponse(
        content=[{"type": "text", "text": "ok"}],
        stop_reason="end_turn",
        stop_sequence=None,
        input_tokens=3,
        output_tokens=2,
    )


class GatedProvider:
    """An OpenAI-shaped provider whose `complete` parks every call on one
    shared `asyncio.Event`, so a test can get N requests genuinely in
    flight at once (past admission, inside the provider call) before any
    of them finishes and writes a receipt — the exact window the
    check-then-act race lives in."""

    name = "openai"

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.calls: list[NormalizedChatRequest] = []

    async def complete(
        self, request: NormalizedChatRequest, *, timeout: float
    ) -> NormalizedChatResponse:
        self.calls.append(request)
        await self.gate.wait()
        return ok_response()

    async def stream(
        self, request: NormalizedChatRequest, *, timeout: float
    ) -> AsyncGenerator[bytes, None]:
        self.calls.append(request)
        await self.gate.wait()
        yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        yield (
            b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":2}}\n\n'
        )
        yield b"data: [DONE]\n\n"


class GatedAnthropicProvider:
    name = "anthropic"

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.calls: list[AnthropicNormalizedRequest] = []

    async def complete(
        self, request: AnthropicNormalizedRequest, *, timeout: float
    ) -> AnthropicNormalizedResponse:
        self.calls.append(request)
        await self.gate.wait()
        return ok_anthropic_response()

    async def stream(
        self, request: AnthropicNormalizedRequest, *, timeout: float
    ) -> AsyncGenerator[bytes, None]:  # pragma: no cover - not used
        raise NotImplementedError
        yield b""


@dataclass
class Harness:
    tmp_path: Path
    budgets: BudgetStore
    receipts: ReceiptsStore
    pricing: PricingResolver
    enforcer: BudgetEnforcer
    telemetry: NullTelemetry

    def add_budget(
        self,
        limit_usd: str,
        *,
        scope: BudgetScope = "work_id",
        scope_value: str | None = WORK_ID,
        window: BudgetWindow = "per_work",
        mode: BudgetMode = "block",
    ) -> Budget:
        if scope == "global":
            scope_value = None
        budget = Budget(
            budget_id=new_budget_id(scope, scope_value, window),
            scope=scope,
            scope_value=scope_value,
            window=window,
            mode=mode,
            limit_usd=Decimal(limit_usd),
        )
        self.budgets.set(budget)
        return budget

    def openai_engine(self, provider: object, *, max_retries: int = 0) -> InferenceEngine:
        route = RouteConfig(provider="openai", model="gpt-4o-mini", max_retries=max_retries)
        unpriced = RouteConfig(provider="openai", model="mystery-model", max_retries=max_retries)
        router = Router({"default": route, "unpriced": unpriced})
        return InferenceEngine(
            router, {"openai": provider},  # type: ignore[dict-item]
            self.telemetry, self.pricing, self.receipts, budgets=self.enforcer,
        )

    def anthropic_engine(
        self, provider: object, *, max_retries: int = 0
    ) -> AnthropicInferenceEngine:
        route = RouteConfig(provider="anthropic", model="claude-test", max_retries=max_retries)
        router = Router({"claude": route})
        return AnthropicInferenceEngine(
            router, {"anthropic": provider},  # type: ignore[dict-item]
            self.telemetry, self.pricing, self.receipts, budgets=self.enforcer,
        )

    def fresh_enforcer(self) -> BudgetEnforcer:
        """A second enforcer over the same files — what a restarted (or a
        second, concurrent) gateway process would construct."""
        return BudgetEnforcer(
            BudgetStore(self.budgets.db_path), ReceiptsStore(self.receipts.db_path), self.pricing
        )


def make_harness(tmp_path: Path) -> Harness:
    budgets = BudgetStore(tmp_path / "budgets.db")
    receipts = ReceiptsStore(tmp_path / "receipts.db")
    pricing = PricingResolver(
        {},
        overrides={"openai": {"gpt-4o-mini": PRICE}, "anthropic": {"claude-test": PRICE}},
    )
    return Harness(
        tmp_path=tmp_path,
        budgets=budgets,
        receipts=receipts,
        pricing=pricing,
        enforcer=BudgetEnforcer(budgets, receipts, pricing),
        telemetry=NullTelemetry(),
    )


async def settle_tasks() -> None:
    """Let every already-scheduled task run up to its next real await."""
    for _ in range(20):
        await asyncio.sleep(0)
