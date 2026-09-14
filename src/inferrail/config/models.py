"""Typed schema for ``inferrail.yaml``.

Config is data, not code: parsing this file should never require network
access or provider SDKs. Building runnable provider instances from a parsed
:class:`InferrailConfig` (which does require reading secrets out of the
environment) happens separately in ``inferrail.providers.registry``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

ProviderType = Literal["openai", "openai_compatible", "anthropic", "anthropic_compatible"]

_DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"
_DEFAULT_ANTHROPIC_BASE_URL = "https://api.anthropic.com/v1"


class ProviderConfig(BaseModel):
    """A named upstream inference provider.

    ``type: openai`` and ``type: openai_compatible`` currently resolve to
    the same adapter (:class:`inferrail.providers.openai.OpenAIProvider`);
    likewise ``type: anthropic``/``anthropic_compatible`` both resolve to
    :class:`inferrail.providers.anthropic.AnthropicProvider` (see
    docs/adr/0014-anthropic-messages-passthrough.md). The ``_compatible``
    variants exist so config reads clearly and so a self-hosted or
    third-party endpoint that merely shares a wire format is never
    conflated with the real, verifiably-operated vendor API for pricing
    purposes (see ``pricing.resolver.PricingResolver``).
    """

    model_config = {"extra": "forbid"}

    type: ProviderType
    api_key_env: str
    base_url: str | None = None

    def resolved_base_url(self) -> str:
        if self.base_url:
            return self.base_url
        if self.type == "openai":
            return _DEFAULT_OPENAI_BASE_URL
        if self.type == "anthropic":
            return _DEFAULT_ANTHROPIC_BASE_URL
        raise ValueError(
            f"provider type '{self.type}' requires an explicit base_url"
        )


class RouteConfig(BaseModel):
    """A named route: what a client selects via the request's ``model`` field."""

    model_config = {"extra": "forbid"}

    provider: str
    model: str
    max_retries: int = Field(default=0, ge=0, le=5)
    timeout_seconds: float = Field(default=30.0, gt=0, le=600)


class TelemetryConfig(BaseModel):
    model_config = {"extra": "forbid"}

    sink: Literal["console", "jsonl", "none"] = "console"
    path: str | None = None

    @model_validator(mode="after")
    def _require_path_for_jsonl(self) -> TelemetryConfig:
        if self.sink == "jsonl" and not self.path:
            raise ValueError("telemetry.path is required when telemetry.sink is 'jsonl'")
        return self


class ServerConfig(BaseModel):
    model_config = {"extra": "forbid"}

    host: str = "127.0.0.1"
    port: int = Field(default=8000, gt=0, le=65535)


class ReceiptsConfig(BaseModel):
    """Where :class:`inferrail.receipts.schema.InferenceReceipt` records go.

    Unlike telemetry (default: console), receipts default to a local JSONL
    file: the whole point of a receipt is to be aggregated later by
    ``inferrail report``, and console-only receipts can't be read back. See
    docs/adr/0005-privacy-preserving-economic-receipts.md.

    ``sqlite`` (see docs/adr/0013-sqlite-receipts-store.md) is a first-class
    alternative to ``jsonl``, not a replacement for it — indexed queries and
    a bounded file per receipt (vs. an ever-growing text file) at the cost
    of needing ``inferrail receipts export`` to get a plain-text copy back.
    ``inferrail report``/``transaction``/``work`` work unchanged against
    either: they detect which one a given path actually is.
    """

    model_config = {"extra": "forbid"}

    sink: Literal["jsonl", "sqlite", "none"] = "jsonl"
    path: str = "./inferrail-receipts.jsonl"


class BudgetsConfig(BaseModel):
    """Where the `Budget` store (`inferrail.budgets.store.BudgetStore`)
    lives, and whether the gateway actually enforces it.

    `enabled` defaults to `False` so an operator who hasn't configured
    any budgets never pays for the enforcement path (and, more
    importantly, so `inferrail serve`/`create_app` never touches
    `path` on disk at all unless budgets are explicitly turned on — see
    docs/adr/0015-budget-enforcement.md). `inferrail budget set|list|rm`
    manage the store's contents independently of this flag (via their
    own `--db`), so budgets can be authored before enforcement is
    switched on.
    """

    model_config = {"extra": "forbid"}

    enabled: bool = False
    path: str = "./inferrail-budgets.db"


class PriceEntry(BaseModel):
    """A verified per-model price, with the provenance to audit it later.

    Used both for the built-in catalog (`inferrail.pricing.builtin`) and
    for operator overrides (`InferrailConfig.pricing`). `source` and
    `verified_date` are mandatory in both cases — Inferrail never persists
    a price it can't explain the origin of. Values are `Decimal`, not
    `float`: money is never computed with binary floating point here.
    """

    model_config = {"extra": "forbid"}

    input_usd_per_million: Decimal = Field(gt=0)
    output_usd_per_million: Decimal = Field(gt=0)
    source: str
    verified_date: date


class InferrailConfig(BaseModel):
    model_config = {"extra": "forbid"}

    providers: dict[str, ProviderConfig]
    routes: dict[str, RouteConfig]
    # If set, a request whose `model` doesn't match any named route above is
    # not rejected — it's forwarded to this provider with `model` passed
    # through unchanged, instead of requiring every upstream model id to be
    # pre-registered as a route. See docs/adr/0007-model-passthrough-routing.md.
    default_provider: str | None = None
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    receipts: ReceiptsConfig = Field(default_factory=ReceiptsConfig)
    budgets: BudgetsConfig = Field(default_factory=BudgetsConfig)
    # provider name -> model -> price override. Always wins over the
    # built-in catalog; see inferrail.pricing.resolver.PricingResolver.
    pricing: dict[str, dict[str, PriceEntry]] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _budgets_require_sqlite_receipts(self) -> InferrailConfig:
        # Budget enforcement computes spend-so-far via
        # `ReceiptsStore.query()` (inferrail.budgets.enforcement) — there
        # is no other efficient way to answer "how much has this
        # project/work_id spent so far" without re-deriving the same
        # indexed SQLite access receipts.sink: sqlite already provides.
        # Refusing to load rather than silently enforcing against an
        # empty/wrong view of spend is the same fail-fast honesty
        # TelemetryConfig's jsonl-path check already applies.
        if self.budgets.enabled and self.receipts.sink != "sqlite":
            raise ValueError(
                "budgets.enabled requires receipts.sink: sqlite — budget enforcement "
                "computes spend-so-far from an indexed receipts store; see "
                "docs/adr/0015-budget-enforcement.md"
            )
        return self

    @model_validator(mode="after")
    def _routes_reference_known_providers(self) -> InferrailConfig:
        if not self.providers:
            raise ValueError("at least one provider must be configured under 'providers'")
        if not self.routes:
            raise ValueError("at least one route must be configured under 'routes'")
        for route_name, route in self.routes.items():
            if route.provider not in self.providers:
                raise ValueError(
                    f"route '{route_name}' references unknown provider '{route.provider}'; "
                    f"known providers: {sorted(self.providers)}"
                )
        if self.default_provider is not None and self.default_provider not in self.providers:
            raise ValueError(
                f"default_provider '{self.default_provider}' is not a configured provider; "
                f"known providers: {sorted(self.providers)}"
            )
        return self
