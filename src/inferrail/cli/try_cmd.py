"""`inferrail try`: one real request through Inferrail's actual
`InferenceEngine`, using `OPENAI_API_KEY` from the environment.

This is the "magic moment" command — no `inferrail.yaml`, no server, no
curl, no HTTP headers. It builds the same `InferenceEngine` that
`inferrail serve` builds (see `gateway/app.py:create_app`), from an
in-memory quickstart config (`inferrail.config.quickstart`), and calls it
directly. The HTTP server is an adapter around the engine, not the other
way around — this command never starts one.

Attribution (`--customer`/`--workflow`/`-a`) is parsed by
`inferrail.cli.attributes` into the same generic `dict[str, str]` the
gateway extracts from `X-Inferrail-Attribute-*` headers — one attribution
model, two entry points.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from inferrail.cli.attributes import parse_cli_attributes
from inferrail.cli.report import format_usd
from inferrail.config.quickstart import (
    QUICKSTART_API_KEY_ENV,
    QUICKSTART_ROUTE,
    build_quickstart_config,
)
from inferrail.errors import (
    AuthenticationError,
    ConfigurationError,
    InferrailError,
    InvalidRequestError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
)
from inferrail.gateway.execution import InferenceEngine
from inferrail.gateway.schemas import ChatCompletionRequest, ChatCompletionResponse
from inferrail.pricing.resolver import PricingResolver
from inferrail.providers.base import ChatMessage, Provider
from inferrail.providers.registry import build_providers
from inferrail.receipts.schema import InferenceReceipt
from inferrail.receipts.sinks import ReceiptSink, build_receipt_sink
from inferrail.routing.router import Router
from inferrail.telemetry.sinks import build_telemetry_sink

_MISSING_KEY_MESSAGE = f"""\
{QUICKSTART_API_KEY_ENV} is not set.

To try Inferrail without a key:
    inferrail demo

To use a real provider:
    set {QUICKSTART_API_KEY_ENV} in your environment"""


class _CapturingReceiptSink:
    """Wraps a real `ReceiptSink`, remembering the last receipt emitted.

    `InferenceEngine.execute` builds and persists the receipt internally
    but doesn't return it — this lets the CLI show the caller exactly what
    was written, without a second, duplicate `build_receipt` call (which
    would mint a second, never-persisted receipt id and risk drifting from
    what's actually on disk).
    """

    def __init__(self, inner: ReceiptSink) -> None:
        self._inner = inner
        self.last_receipt: InferenceReceipt | None = None

    def emit(self, receipt: InferenceReceipt) -> None:
        self.last_receipt = receipt
        self._inner.emit(receipt)


def _explain_provider_error(exc: InferrailError, *, model: str) -> str:
    """A short, actionable explanation for an adversarial `try` failure.

    Built only from `exc.safe_summary` (never `str(exc)`) — some providers
    echo request content back in error text (see `ProviderError.safe_summary`
    and `providers/openai.py`), and this is printed straight to the
    terminal, so it gets the same fail-closed treatment as Inferrail's own
    operator logs.
    """
    if isinstance(exc, AuthenticationError):
        return (
            f"OpenAI rejected the API key ({exc.safe_summary}).\n"
            f"Check that {QUICKSTART_API_KEY_ENV} is a valid key with access to the "
            "Chat Completions API."
        )
    if isinstance(exc, RateLimitError):
        return (
            f"OpenAI rate-limited or quota-exhausted this request ({exc.safe_summary}).\n"
            "Check your OpenAI account's billing and usage limits, then try again."
        )
    if isinstance(exc, ProviderTimeoutError):
        return f"{exc.safe_summary}. Check your network connection and try again."
    if isinstance(exc, InvalidRequestError):
        return (
            f"OpenAI rejected the request ({exc.safe_summary}).\n"
            f"Check that model '{model}' is available to your account."
        )
    if isinstance(exc, ProviderError):
        return (
            f"Could not complete the request ({exc.safe_summary}). "
            "Check your network connection."
        )
    return exc.safe_summary


def _row(label: str, value: str) -> str:
    return f"  {label:<17} {value}"


def _print_success(
    response: ChatCompletionResponse,
    receipt: InferenceReceipt,
    attributes: dict[str, str],
    receipts_path: str,
) -> None:
    print(response.choices[0].message.content)
    print()
    print(f"Receipt {receipt.receipt_id}")
    print(_row("Provider", receipt.provider))
    print(_row("Model", receipt.model))
    print(_row("Input tokens", str(receipt.prompt_tokens)))
    print(_row("Output tokens", str(receipt.completion_tokens)))
    cost = (
        format_usd(receipt.estimated_cost_usd)
        if receipt.estimated_cost_usd is not None
        else "unknown (no verified price on file for this model)"
    )
    print(_row("Cost", cost))
    for key, value in attributes.items():
        print(_row(key.capitalize(), value))
    print(_row("Prompt stored", "no"))
    print(_row("Response stored", "no"))
    print()
    print(f"Saved to {receipts_path}")
    print()
    next_dimension = next(iter(attributes), "provider")
    print("Next:")
    print(f"  inferrail report --by {next_dimension}")


async def _execute(
    engine: InferenceEngine,
    request: ChatCompletionRequest,
    attributes: dict[str, str],
    providers: dict[str, Provider],
) -> ChatCompletionResponse:
    try:
        return await engine.execute(request, attributes=attributes)
    finally:
        for provider in providers.values():
            aclose = getattr(provider, "aclose", None)
            if aclose is not None:
                await aclose()


def run_try(
    prompt: str,
    *,
    customer: str | None,
    workflow: str | None,
    attribute_args: list[str],
    model: str,
) -> int:
    try:
        attributes = parse_cli_attributes(
            customer=customer, workflow=workflow, attribute_args=attribute_args
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if not os.environ.get(QUICKSTART_API_KEY_ENV):
        print(_MISSING_KEY_MESSAGE, file=sys.stderr)
        return 1

    # httpx logs one INFO-level "HTTP Request: ..." line per call by
    # default (main() enables INFO-level logging globally). That's useful
    # noise for a long-running `inferrail serve`, but it would clutter the
    # single-request summary this command prints below — the receipt is
    # the intended observability surface here, not a raw request log line.
    logging.getLogger("httpx").setLevel(logging.WARNING)

    config = build_quickstart_config(model=model, telemetry_sink="none")

    try:
        providers = build_providers(config)
        receipts_sink = build_receipt_sink(config.receipts)
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(
            f"error: could not prepare receipts file at {config.receipts.path}: {exc}\n"
            "Check permissions, or run from a directory you can write to.",
            file=sys.stderr,
        )
        return 1

    capturing_sink = _CapturingReceiptSink(receipts_sink)
    engine = InferenceEngine(
        router=Router(config.routes, default_provider=config.default_provider),
        providers=providers,
        telemetry=build_telemetry_sink(config.telemetry),
        pricing_resolver=PricingResolver(config.providers, config.pricing),
        receipts=capturing_sink,
    )
    request = ChatCompletionRequest(
        model=QUICKSTART_ROUTE, messages=[ChatMessage(role="user", content=prompt)]
    )

    try:
        response = asyncio.run(_execute(engine, request, attributes, providers))
    except InferrailError as exc:
        print(f"error: {_explain_provider_error(exc, model=model)}", file=sys.stderr)
        return 1

    assert capturing_sink.last_receipt is not None  # engine always emits one on success
    _print_success(response, capturing_sink.last_receipt, attributes, config.receipts.path)
    return 0
