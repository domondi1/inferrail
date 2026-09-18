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
QUICKSTART_ANTHROPIC_PROVIDER = "anthropic"
QUICKSTART_ROUTE = "default"
QUICKSTART_MODEL = "gpt-4o-mini"
QUICKSTART_ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"
QUICKSTART_API_KEY_ENV = "OPENAI_API_KEY"
QUICKSTART_ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
QUICKSTART_RECEIPTS_PATH = "./inferrail-receipts.jsonl"


def build_quickstart_config(
    *,
    model: str = QUICKSTART_MODEL,
    telemetry_sink: Literal["console", "none"] = "console",
) -> InferrailConfig:
    """Build the quickstart config: one OpenAI provider and one Anthropic
    provider, each passthrough-default for its own wire format, so a caller
    can point *either* the OpenAI SDK (at ``/v1/chat/completions``) or the
    Anthropic SDK (at ``/v1/messages``) at this same server with zero
    per-model configuration. Only the provider whose API key is actually
    set will succeed a real request — both are registered unconditionally
    (config parsing never touches the environment; see
    ``providers.registry.build_providers``/``build_anthropic_providers``,
    which resolve keys lazily and only raise when a request actually
    reaches an unconfigured one) so `inferrail serve --quickstart` never
    has to ask which provider you meant before it can start.

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
            QUICKSTART_ANTHROPIC_PROVIDER: ProviderConfig(
                type="anthropic", api_key_env=QUICKSTART_ANTHROPIC_API_KEY_ENV
            ),
        },
        routes={
            QUICKSTART_ROUTE: RouteConfig(provider=QUICKSTART_PROVIDER, model=model),
        },
        # Any OpenAI model id passes through to the openai provider; any
        # Anthropic model id passes through to the anthropic provider — two
        # independent passthrough defaults, one per wire format, since the
        # two pipelines can't share one (see
        # docs/adr/0020-quickstart-both-sdks-and-payload-free-verification.md).
        default_provider=QUICKSTART_PROVIDER,
        default_anthropic_provider=QUICKSTART_ANTHROPIC_PROVIDER,
        telemetry=TelemetryConfig(sink=telemetry_sink),
        receipts=ReceiptsConfig(sink="jsonl", path=QUICKSTART_RECEIPTS_PATH),
    )
