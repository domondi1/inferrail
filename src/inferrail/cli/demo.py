"""`inferrail demo`: DEMO DATA / NOT REAL PROVIDER BILLING.

Runs a handful of canned chat requests through Inferrail's *real*
`InferenceEngine` -> `InferenceReceipt` -> `inferrail report` pipeline,
using a fake `DemoProvider` (defined below) instead of a real network call
to OpenAI. It exists so a stranger can see the economic-receipt magic
moment — attributed cost per customer, with honest "unknown cost" for a
model with no price on file — in a few seconds, with no API key, no
network access, and no money spent.

This module does not touch `inferrail.yaml`, does not start the gateway,
and `DemoProvider` is not reachable from `inferrail serve` — it lives only
here, not anywhere `inferrail.providers.registry.build_providers` can
construct one, so there is no way for demo data to leak into a real
deployment. Every price used below is a made-up round number labeled as
such in its own `source` field; none of it is OpenAI's actual pricing (see
`src/inferrail/pricing/builtin.py` for that).

To see the same flow against a real provider: `inferrail try`.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from inferrail.cli.report import run_report
from inferrail.cli.work import DEFAULT_OUTCOMES_PATH, run_work
from inferrail.config.models import PriceEntry, RouteConfig
from inferrail.gateway.execution import InferenceEngine
from inferrail.gateway.schemas import ChatCompletionRequest
from inferrail.pricing.resolver import PricingResolver
from inferrail.providers.base import ChatMessage, NormalizedChatRequest, NormalizedChatResponse
from inferrail.receipts.sinks import JSONLReceiptSink
from inferrail.routing.router import Router
from inferrail.telemetry.sinks import NullTelemetrySink
from inferrail.work.builder import append_outcome
from inferrail.work.schema import WorkOutcomeRecord

RECEIPTS_PATH = Path("./inferrail-demo-receipts.jsonl")

_DEMO_PRICE_SOURCE = "DEMO — a made-up round number, not a real provider price"
_DEMO_PRICE_DATE = date.today()


@dataclass(frozen=True)
class _CannedResponse:
    """One scripted `DemoProvider` reply, in the order requests are sent."""

    content: str
    prompt_tokens: int
    completion_tokens: int


class _DemoProvider:
    """A fake `Provider` (see `inferrail.providers.base.Provider`) that
    returns pre-scripted responses instead of calling a real API.

    Used only by `inferrail demo` — never imported anywhere else in
    `src/inferrail`, and there is no `inferrail.yaml` provider `type` that
    constructs one (see `inferrail.providers.registry.build_providers`).
    Compare `inferrail.providers.openai.OpenAIProvider`, the real
    implementation the gateway and `inferrail try` actually use.
    """

    name = "demo"

    def __init__(self, responses: list[_CannedResponse]) -> None:
        self._responses = iter(responses)

    async def complete(
        self, request: NormalizedChatRequest, *, timeout: float
    ) -> NormalizedChatResponse:
        del timeout  # unused: no real call is made, so nothing can time out
        try:
            canned = next(self._responses)
        except StopIteration:  # pragma: no cover - demo script has a fixed script
            raise RuntimeError("_DemoProvider ran out of canned responses") from None
        return NormalizedChatResponse(
            content=canned.content,
            finish_reason="stop",
            prompt_tokens=canned.prompt_tokens,
            completion_tokens=canned.completion_tokens,
            raw_model=request.model,
        )

    async def stream(
        self, request: NormalizedChatRequest, *, timeout: float
    ) -> AsyncGenerator[bytes, None]:
        del request, timeout  # unused: the demo script never streams
        raise NotImplementedError("_DemoProvider does not support streaming")
        yield b""  # pragma: no cover - unreachable; makes this an async generator


# Six scripted requests, each attributed to a generic customer-defined work
# id. The preview model has no price on file, demonstrating the same honest
# unknown-cost behavior a real unrecognized model gets in production.
_SCENARIOS: list[tuple[str, dict[str, str], _CannedResponse]] = [
    (
        "default",
        {"customer": "acme", "workflow": "contract-review", "work_id": "work-contract-1"},
        _CannedResponse("Clause 4.2 caps liability at 12 months' fees.", 812, 143),
    ),
    (
        "default",
        {"customer": "acme", "workflow": "contract-review", "work_id": "work-contract-1"},
        _CannedResponse("The termination notice period is 30 days.", 640, 98),
    ),
    (
        "default",
        {"customer": "globex", "workflow": "support-ticket", "work_id": "work-support-1"},
        _CannedResponse("Reset the device, then reapply the config profile.", 421, 76),
    ),
    (
        "large",
        {"customer": "globex", "workflow": "support-ticket", "work_id": "work-support-1"},
        _CannedResponse(
            "Root cause: a race condition in the retry path under load; "
            "patch attached, rollout plan below.",
            1200,
            340,
        ),
    ),
    (
        "default",
        {"work_id": "work-pending-1"},  # receipt evidence, no declared outcome
        _CannedResponse("Hello! How can I help today?", 300, 50),
    ),
    (
        "preview",
        {"customer": "acme", "workflow": "contract-review", "work_id": "work-unknown-1"},
        _CannedResponse("This is a preview model Inferrail has no price on file for.", 500, 120),
    ),
]


def _build_engine() -> InferenceEngine:
    routes = {
        "default": RouteConfig(provider="demo", model="demo-small"),
        "large": RouteConfig(provider="demo", model="demo-large"),
        # No pricing override exists for demo-preview below — deliberately,
        # to demonstrate that Inferrail leaves cost `null` rather than
        # guessing, exactly as it would for any real unrecognized model.
        "preview": RouteConfig(provider="demo", model="demo-preview"),
    }
    pricing_resolver = PricingResolver(
        providers={},
        overrides={
            "demo": {
                "demo-small": PriceEntry(
                    input_usd_per_million=Decimal("0.20"),
                    output_usd_per_million=Decimal("0.80"),
                    source=_DEMO_PRICE_SOURCE,
                    verified_date=_DEMO_PRICE_DATE,
                ),
                "demo-large": PriceEntry(
                    input_usd_per_million=Decimal("3.00"),
                    output_usd_per_million=Decimal("12.00"),
                    source=_DEMO_PRICE_SOURCE,
                    verified_date=_DEMO_PRICE_DATE,
                ),
            }
        },
    )
    provider = _DemoProvider([canned for _, _, canned in _SCENARIOS])
    return InferenceEngine(
        router=Router(routes),
        providers={"demo": provider},
        telemetry=NullTelemetrySink(),
        pricing_resolver=pricing_resolver,
        receipts=JSONLReceiptSink(RECEIPTS_PATH),
    )


async def _run() -> None:
    if RECEIPTS_PATH.exists():
        RECEIPTS_PATH.unlink()  # fresh, reproducible output on every run
    if DEFAULT_OUTCOMES_PATH.exists():
        DEFAULT_OUTCOMES_PATH.unlink()

    print("=" * 72)
    print("INFERRAIL DEMO — canned responses, not real provider billing")
    print("=" * 72)
    print(f"Writing receipts to {RECEIPTS_PATH}\n")

    engine = _build_engine()
    for route, attributes, canned in _SCENARIOS:
        request = ChatCompletionRequest(
            model=route, messages=[ChatMessage(role="user", content="(demo prompt)")]
        )
        response = await engine.execute(request, attributes=attributes)
        meta = response.inferrail  # latency/route metadata only; cost lives on the receipt
        who = attributes.get("customer", "(unattributed)")
        print(
            f"  [{route:8}] {who:10} -> {canned.completion_tokens:>4} completion tokens "
            f"(latency {meta.total_latency_ms:.2f}ms)"
        )

    print("\nDone. Every one of those requests just produced a real")
    print("InferenceReceipt via the exact same InferenceEngine code path")
    print("`inferrail serve` and `inferrail try` use — only the provider is fake.\n")

    print("-" * 72)
    print("inferrail report --by customer   (run directly on the demo receipts)")
    print("-" * 72)
    run_report(RECEIPTS_PATH, "customer")

    print("\nTry other dimensions yourself, e.g.:")
    print(f"  inferrail report --by workflow --receipts {RECEIPTS_PATH}")
    print(f"  inferrail report --by model    --receipts {RECEIPTS_PATH}")
    print(f"\nOpen {RECEIPTS_PATH.name} directly to see one full receipt, including")
    print("the 'unknown' pricing on the demo-preview request and the DEMO source")
    print("label on every price used above.")

    # Outcome records are independent evidence. The Work command below
    # reloads them from JSONL instead of receiving them in-memory.
    for work_id, status in [
        ("work-contract-1", "resolved"),
        ("work-support-1", "failed"),
        ("work-unknown-1", "resolved"),
        ("work-outcome-only-1", "escalated"),
    ]:
        append_outcome(
            DEFAULT_OUTCOMES_PATH,
            WorkOutcomeRecord(
                work_id=work_id, outcome_status=status, recorded_at=datetime.now(UTC)
            ),
        )

    print("\n" + "-" * 72)
    print("WORK ECONOMICS — receipts + shared work_id + declared outcome")
    print("-" * 72)
    run_work(RECEIPTS_PATH, DEFAULT_OUTCOMES_PATH, None, all_work=True, as_json=False)
    print("\nSYNTHETIC SUPPORT EXAMPLE")
    print("In this synthetic support example, the application defines 'resolved' as success.")
    print("\nReady to see a real cost number instead of a demo one?")
    print('  inferrail try "Reply with one word: ready" --customer acme')


def run_demo() -> int:
    asyncio.run(_run())
    return 0
