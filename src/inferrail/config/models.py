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
    # Coexistence (running in front of another gateway such as LiteLLM or
    # OpenRouter): opt-in, operator-asserted. `price_as` applies a vendor's
    # built-in list-price catalog to this compatible upstream (the receipt's
    # pricing source records the assertion); `request_stream_usage` asks the
    # upstream for the final usage chunk on streams, as Inferrail already
    # does for verified OpenAI. See docs/adr/0022.
    price_as: Literal["openai", "anthropic"] | None = None
    request_stream_usage: bool = False

    @model_validator(mode="after")
    def _price_as_only_for_compatible(self) -> ProviderConfig:
        if self.price_as is not None and not self.type.endswith("_compatible"):
            raise ValueError(
                "price_as only applies to an *_compatible provider; the vendor's own "
                "provider type already uses its catalog"
            )
        return self

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
    # Per-run budgets without pre-registration — see
    # docs/adr/0022-per-run-budget-declaration.md. A request carrying a
    # work_id may declare its run's ceiling (X-Inferrail-Budget-Usd), or
    # inherit `per_work_default_usd`; `per_work_max_usd` caps declarations.
    allow_declared_budgets: bool = True
    per_work_default_usd: Decimal | None = Field(default=None, gt=0)
    per_work_max_usd: Decimal | None = Field(default=None, gt=0)


class UsagePingConfig(BaseModel):
    """The anonymous usage/presence beacon
    (docs/adr/0020-quickstart-both-sdks-and-payload-free-verification.md,
    superseding docs/adr/0019-opt-in-usage-ping.md's "default off").

    **Opt-out by default** (`enabled: bool = True`) — a deliberate,
    explicit reversal of ADR-0019's original "default off" decision, at
    the founder's direction, recorded in ADR-0020 rather than silently
    changed. Still fully inert with no `endpoint` configured regardless
    of `enabled` — there is no built-in default endpoint baked into this
    package; an operator (or, for the desktop dashboard's toggle, the
    person running `inferrail serve` on their own machine) must
    explicitly point this at a real collector before anything can ever be
    sent. The payload is fixed and minimal (see
    `usage_ping.payload.build_payload`): a locally-generated random
    install id, event name, Inferrail version, OS, Python major.minor —
    never a prompt, response, model name, cost, work_id, project name,
    IP address, or anything about the traffic this install actually
    handles. See `docs/privacy/usage-ping.md` for the full disclosure,
    and `INFERRAIL_TELEMETRY=0` / `--no-telemetry` / `DO_NOT_TRACK=1` /
    running under CI or the test suite for how to turn it off.

    `enabled` here is only the *config-file* default. Once
    `inferrail serve` has run once, the dashboard's Settings toggle (and
    `inferrail telemetry enable|disable`) control a separate, mutable
    on/off state under the OS app-data directory that takes over from
    this default — the same "config seeds it, a store owns it after
    that" pattern `budgets`/`receipts` already use under `--app-mode`.
    """

    model_config = {"extra": "forbid"}

    enabled: bool = True
    endpoint: str | None = None


class LongContextPrice(BaseModel):
    """Rates for a request whose input tokens exceed `above_input_tokens`.
    They replace the base rates for the whole request, not only for the
    tokens past the threshold."""

    model_config = {"extra": "forbid"}

    above_input_tokens: int = Field(gt=0)
    input_usd_per_million: Decimal = Field(gt=0)
    output_usd_per_million: Decimal = Field(gt=0)
    cache_read_usd_per_million: Decimal | None = Field(default=None, gt=0)


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
    # Prompt-cache rates (Anthropic: 5-minute and 1-hour cache writes,
    # cache reads). Optional: a request that reports cache tokens against
    # an entry without the matching rate gets cost `None`, never a cost
    # that silently prices cache tokens at the base input rate or at zero.
    cache_write_5m_usd_per_million: Decimal | None = Field(default=None, gt=0)
    cache_write_1h_usd_per_million: Decimal | None = Field(default=None, gt=0)
    cache_read_usd_per_million: Decimal | None = Field(default=None, gt=0)
    # Context-tiered pricing (OpenAI gpt-5.6-*: a request whose input is
    # above the threshold is billed at the higher rates for the *full*
    # request). `None` means one flat rate.
    long_context: LongContextPrice | None = None

    @model_validator(mode="after")
    def _long_context_rates_are_complete(self) -> PriceEntry:
        tier = self.long_context
        if tier is None:
            return self
        if self.cache_write_5m_usd_per_million or self.cache_write_1h_usd_per_million:
            raise ValueError(
                "long_context can't be combined with cache write rates: the long "
                "tier would have no matching write rate"
            )
        if (self.cache_read_usd_per_million is None) != (tier.cache_read_usd_per_million is None):
            raise ValueError(
                "cache_read_usd_per_million must be set on both the base price and "
                "long_context, or on neither"
            )
        return self

    def for_input_tokens(self, input_tokens: int) -> PriceEntry:
        """The rates that apply to a request with this many input tokens:
        the long-context tier when the input is above its threshold, else
        this entry. The result is flat (no `long_context`), so it can be
        embedded in a receipt as the price actually used."""
        tier = self.long_context
        if tier is None:
            return self
        if input_tokens <= tier.above_input_tokens:
            return self.model_copy(update={"long_context": None})
        return self.model_copy(
            update={
                "input_usd_per_million": tier.input_usd_per_million,
                "output_usd_per_million": tier.output_usd_per_million,
                "cache_read_usd_per_million": tier.cache_read_usd_per_million,
                "long_context": None,
            }
        )


class InferrailConfig(BaseModel):
    model_config = {"extra": "forbid"}

    providers: dict[str, ProviderConfig]
    # Named aliases (optional when a default provider is set: then every
    # request's `model` is passed through to that provider as-is).
    routes: dict[str, RouteConfig] = Field(default_factory=dict)
    # If set, a request whose `model` doesn't match any named route above is
    # not rejected — it's forwarded to this provider with `model` passed
    # through unchanged, instead of requiring every upstream model id to be
    # pre-registered as a route. See docs/adr/0007-model-passthrough-routing.md.
    # Applies to the `/v1/chat/completions` (OpenAI-shaped) pipeline only.
    default_provider: str | None = None
    # Same passthrough behavior as `default_provider`, but for the separate
    # `/v1/messages` (Anthropic-shaped) pipeline — see
    # docs/adr/0014-anthropic-messages-passthrough.md. Kept as its own field
    # rather than reusing `default_provider` because the two pipelines each
    # build their own `Router` against disjoint provider sets (an
    # `openai`-type provider is invisible to the Anthropic pipeline and vice
    # versa — see `providers.registry.build_providers`/
    # `build_anthropic_providers`), so one shared default could never
    # correctly passthrough for both wire formats at once. See
    # docs/adr/0020-quickstart-both-sdks-and-payload-free-verification.md.
    default_anthropic_provider: str | None = None
    # Request headers to read a work id from when the caller sends no
    # `X-Inferrail-Attribute-Work-Id`, checked in order (first non-empty
    # wins). For agents that already identify their session on every
    # request, e.g. Claude Code's `X-Claude-Code-Session-Id` or OpenCode's
    # `x-opencode-session-id`, so each session gets its own work id (and
    # budget) without a wrapper. Empty by default: nothing is inferred.
    work_id_headers: list[str] = Field(default_factory=list)
    telemetry: TelemetryConfig = Field(default_factory=TelemetryConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    receipts: ReceiptsConfig = Field(default_factory=ReceiptsConfig)
    budgets: BudgetsConfig = Field(default_factory=BudgetsConfig)
    usage_ping: UsagePingConfig = Field(default_factory=UsagePingConfig)
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
        has_default = (
            self.default_provider is not None or self.default_anthropic_provider is not None
        )
        if not self.routes and not has_default:
            raise ValueError(
                "configure at least one route under 'routes', or a default_provider "
                "so requests pass their model through"
            )
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
        if (
            self.default_anthropic_provider is not None
            and self.default_anthropic_provider not in self.providers
        ):
            raise ValueError(
                f"default_anthropic_provider '{self.default_anthropic_provider}' is not a "
                f"configured provider; known providers: {sorted(self.providers)}"
            )
        return self
