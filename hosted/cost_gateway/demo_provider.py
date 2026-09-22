"""Zero-key demo mode for the hosted Cost Gateway: `POST
/v1/demo/chat/completions` (service.py) runs a real request through
Inferrail's real `InferenceEngine` -> `InferenceReceipt` pipeline using
this fake provider instead of a real network call -- no key needed, no
real money spent, always available regardless of a trial's real-key
state. Mirrors `inferrail.cli.demo`'s `_DemoProvider` in spirit (see that
module's docstring), reimplemented here rather than imported because this
service needs one-shot, per-request demo calls (a visitor clicking "Send
a test request" repeatedly) rather than a fixed six-scenario script.

Every price used here is a made-up round number, explicitly labeled
`DEMO` in its own `source` field -- never real provider pricing.
"""

from __future__ import annotations

import itertools
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from inferrail.config.models import PriceEntry
from inferrail.providers.base import NormalizedChatRequest, NormalizedChatResponse

DEMO_PROVIDER_NAME = "demo"
DEMO_MODEL_NAME = "demo-model"
DEMO_PRICE_SOURCE = "DEMO -- a made-up round number, not a real provider price"


@dataclass(frozen=True)
class _CannedResponse:
    content: str
    prompt_tokens: int
    completion_tokens: int


_CANNED_RESPONSES: list[_CannedResponse] = [
    _CannedResponse(
        "This is a demo response from Inferrail's Cost Gateway -- no real "
        "provider was called and no money was spent. The receipt below is "
        "real, though: real token counts, a real (made-up) DEMO price, and "
        "the exact same payload-free schema a real request produces.",
        128,
        64,
    ),
    _CannedResponse(
        "Another demo receipt. Notice there is no prompt or response text "
        "stored anywhere in Inferrail's own records -- only counts, cost, "
        "and whatever attribution you attach.",
        96,
        48,
    ),
    _CannedResponse(
        "Demo mode never touches a real API key. Add your own OpenAI or "
        "Anthropic key above to see a real receipt from a real call.",
        64,
        32,
    ),
]


def demo_pricing_overrides() -> dict[str, dict[str, PriceEntry]]:
    return {
        DEMO_PROVIDER_NAME: {
            DEMO_MODEL_NAME: PriceEntry(
                input_usd_per_million=Decimal("0.50"),
                output_usd_per_million=Decimal("1.50"),
                source=DEMO_PRICE_SOURCE,
                verified_date=date.today(),
            )
        }
    }


class DemoProvider:
    """A fake `Provider` (see `inferrail.providers.base.Provider`) that
    returns pre-scripted responses instead of calling a real API. Cycles
    through a short, fixed list of canned responses so repeated demo
    calls from one visitor aren't identical every time -- purely
    cosmetic, not meant to simulate real model variance."""

    name = DEMO_PROVIDER_NAME

    def __init__(self) -> None:
        self._responses = itertools.cycle(_CANNED_RESPONSES)

    async def complete(
        self, request: NormalizedChatRequest, *, timeout: float
    ) -> NormalizedChatResponse:
        del timeout  # unused: no real call is made, so nothing can time out
        canned = next(self._responses)
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
        del request, timeout  # unused: demo mode never streams
        raise NotImplementedError("DemoProvider does not support streaming")
        yield b""  # pragma: no cover - unreachable; makes this an async generator
