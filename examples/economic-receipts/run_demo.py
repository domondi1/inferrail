"""DEMO DATA / NOT REAL PROVIDER BILLING.

Runs a handful of canned chat requests through Inferrail's *real*
`InferenceEngine` -> `InferenceReceipt` -> `inferrail report` pipeline,
using a fake `DemoProvider` (defined below) instead of a real network call
to OpenAI. It exists so you can see the economic-receipt magic moment —
attributed cost per customer, with honest "unknown cost" for a model with
no price on file — in a few seconds, with no API key, no signup, and no
money spent.

This script does not touch `inferrail.yaml`, does not start the gateway,
and is not reachable from `inferrail serve` — `DemoProvider` lives only
here, not in `src/inferrail`, so there's no way for demo data to leak into
a real deployment. Every price used below is a made-up round number
labeled as such in its own `source` field; none of it is OpenAI's actual
pricing (see src/inferrail/pricing/builtin.py for that).

Usage:
    python examples/economic-receipts/run_demo.py

Then, exactly as you would for real receipts:
    inferrail report --by customer --receipts examples/economic-receipts/demo-receipts.jsonl

To see the same flow against a real provider, follow the Quickstart in
the top-level README.md instead.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

from inferrail.cli.report import run_report
from inferrail.config.models import PriceEntry, RouteConfig
from inferrail.gateway.execution import InferenceEngine
from inferrail.gateway.schemas import ChatCompletionRequest
from inferrail.pricing.resolver import PricingResolver
from inferrail.providers.base import ChatMessage, NormalizedChatRequest, NormalizedChatResponse
from inferrail.receipts.sinks import JSONLReceiptSink
from inferrail.routing.router import Router
from inferrail.telemetry.sinks import NullTelemetrySink

_RECEIPTS_PATH = Path(__file__).parent / "demo-receipts.jsonl"

_DEMO_PRICE_SOURCE = "DEMO — a made-up round number, not a real provider price"
_DEMO_PRICE_DATE = date.today()


@dataclass(frozen=True)
class CannedResponse:
    """One scripted `DemoProvider` reply, in the order requests are sent."""

    content: str
    prompt_tokens: int
    completion_tokens: int


class DemoProvider:
    """A fake `Provider` (see `inferrail.providers.base.Provider`) that
    returns pre-scripted responses instead of calling a real API.

    Used only by this demo script — never imported by `src/inferrail`, and
    there is no `inferrail.yaml` provider `type` that constructs one (see
    `inferrail.providers.registry.build_providers`). Compare
    `inferrail.providers.openai.OpenAIProvider`, the real implementation
    the gateway actually uses.
    """

    name = "demo"

    def __init__(self, responses: list[CannedResponse]) -> None:
        self._responses = iter(responses)

    async def complete(
        self, request: NormalizedChatRequest, *, timeout: float
    ) -> NormalizedChatResponse:
        del timeout  # unused: no real call is made, so nothing can time out
        try:
            canned = next(self._responses)
        except StopIteration:  # pragma: no cover - demo script has a fixed script
            raise RuntimeError("DemoProvider ran out of canned responses") from None
        return NormalizedChatResponse(
            content=canned.content,
            finish_reason="stop",
            prompt_tokens=canned.prompt_tokens,
            completion_tokens=canned.completion_tokens,
            raw_model=request.model,
        )


# Six scripted requests: two customers, two workflows, one model with no
# price on file (demo-preview) to show honest "unknown cost" handling —
# exactly the same behavior a real unrecognized model gets in production.
_SCENARIOS: list[tuple[str, dict[str, str], CannedResponse]] = [
    (
        "default",
        {"customer": "acme", "workflow": "contract-review"},
        CannedResponse("Clause 4.2 caps liability at 12 months' fees.", 812, 143),
    ),
    (
        "default",
        {"customer": "acme", "workflow": "contract-review"},
        CannedResponse("The termination notice period is 30 days.", 640, 98),
    ),
    (
        "default",
        {"customer": "globex", "workflow": "support-ticket"},
        CannedResponse("Reset the device, then reapply the config profile.", 421, 76),
    ),
    (
        "large",
        {"customer": "globex", "workflow": "support-ticket"},
        CannedResponse(
            "Root cause: a race condition in the retry path under load; "
            "patch attached, rollout plan below.",
            1200,
            340,
        ),
    ),
    (
        "default",
        {},  # no X-Inferrail-Attribute-* headers -> shows up as (unattributed)
        CannedResponse("Hello! How can I help today?", 300, 50),
    ),
    (
        "preview",
        {"customer": "acme", "workflow": "contract-review"},
        CannedResponse(
            "This is a preview model Inferrail has no price on file for.", 500, 120
        ),
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
    provider = DemoProvider([canned for _, _, canned in _SCENARIOS])
    return InferenceEngine(
        router=Router(routes),
        providers={"demo": provider},
        telemetry=NullTelemetrySink(),
        pricing_resolver=pricing_resolver,
        receipts=JSONLReceiptSink(_RECEIPTS_PATH),
    )


async def _run() -> None:
    if _RECEIPTS_PATH.exists():
        _RECEIPTS_PATH.unlink()  # fresh, reproducible output on every run

    print("=" * 72)
    print("INFERRAIL DEMO — canned responses, not real provider billing")
    print("=" * 72)
    print(f"Writing receipts to {_RECEIPTS_PATH}\n")

    engine = _build_engine()
    for route, attributes, canned in _SCENARIOS:
        request = ChatCompletionRequest(
            model=route, messages=[ChatMessage(role="user", content="(demo prompt)")]
        )
        response = await engine.execute(request, attributes=attributes)
        cost = response.inferrail  # latency/route metadata only; cost lives on the receipt
        who = attributes.get("customer", "(unattributed)")
        print(f"  [{route:8}] {who:10} -> {canned.completion_tokens:>4} completion tokens "
              f"(latency {cost.total_latency_ms:.2f}ms)")

    print("\nDone. Every one of those requests just produced a real")
    print("InferenceReceipt (see receipts.path) via the exact same code path")
    print("`inferrail serve` uses — only DemoProvider is fake.\n")

    print("-" * 72)
    print("inferrail report --by customer   (run directly on the demo receipts)")
    print("-" * 72)
    run_report(_RECEIPTS_PATH, "customer")

    print("\nTry other dimensions yourself, e.g.:")
    print(f"  inferrail report --by workflow --receipts {_RECEIPTS_PATH}")
    print(f"  inferrail report --by model    --receipts {_RECEIPTS_PATH}")
    print(f"\nOpen {_RECEIPTS_PATH.name} directly to see one full receipt, including")
    print("the 'unknown' pricing on the demo-preview request and the DEMO source")
    print("label on every price used above.")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
