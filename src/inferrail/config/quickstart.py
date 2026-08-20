"""A minimal, in-memory :class:`InferrailConfig` for the zero-config path.

``inferrail try`` and ``inferrail serve --quickstart`` both need a runnable
config without requiring the caller to first write an ``inferrail.yaml``.
This is deliberately not a second configuration system: it builds the exact
same :class:`InferrailConfig` pydantic model that :func:`inferrail.config.loader.load_config`
produces from YAML, so it goes through the same field validation and the
same cross-reference check (routes must reference a known provider) either
way. There is one config *shape*; this is just an alternate in-memory
*source* for it.
"""

from __future__ import annotations

from typing import Literal

from inferrail.config.models import (
    InferrailConfig,
    ProviderConfig,
    ReceiptsConfig,
    RouteConfig,
    TelemetryConfig,
)

QUICKSTART_PROVIDER = "openai"
QUICKSTART_ROUTE = "default"
QUICKSTART_MODEL = "gpt-4o-mini"
QUICKSTART_API_KEY_ENV = "OPENAI_API_KEY"
QUICKSTART_RECEIPTS_PATH = "./inferrail-receipts.jsonl"


def build_quickstart_config(
    *,
    model: str = QUICKSTART_MODEL,
    telemetry_sink: Literal["console", "none"] = "console",
) -> InferrailConfig:
    """Build the quickstart config: one OpenAI provider, one ``default`` route.

    ``telemetry_sink`` defaults to ``"console"`` to match
    :class:`~inferrail.config.models.TelemetryConfig`'s own default (what
    ``inferrail serve`` would use for an explicit config that doesn't set
    ``telemetry:``). ``inferrail try`` passes ``"none"`` since it already
    prints a focused receipt summary and a console telemetry line would
    just be noise for a single one-off request.
    """
    return InferrailConfig(
        providers={
            QUICKSTART_PROVIDER: ProviderConfig(
                type="openai", api_key_env=QUICKSTART_API_KEY_ENV
            ),
        },
        routes={
            QUICKSTART_ROUTE: RouteConfig(provider=QUICKSTART_PROVIDER, model=model),
        },
        # There's exactly one provider in the quickstart path, so any model
        # name the caller sends — not just the "default" route above — can
        # go straight to it. See docs/adr/0007-model-passthrough-routing.md.
        default_provider=QUICKSTART_PROVIDER,
        telemetry=TelemetryConfig(sink=telemetry_sink),
        receipts=ReceiptsConfig(sink="jsonl", path=QUICKSTART_RECEIPTS_PATH),
    )
