"""Resolves a (provider, model) pair to a verified :class:`PriceEntry`.

Pure and deterministic — a function of `inferrail.yaml` alone, no runtime
state — mirroring `routing.router.Router`. Kept independent of the HTTP
layer and of provider execution so it's testable standalone.
"""

from __future__ import annotations

from inferrail.config.models import PriceEntry, ProviderConfig
from inferrail.pricing.builtin import BUILTIN_OPENAI_PRICING
from inferrail.pricing.builtin_anthropic import BUILTIN_ANTHROPIC_PRICING

# Keyed by a provider's own verified `type` — deliberately *not* including
# "openai_compatible"/"anthropic_compatible" as keys, since those types
# may be a completely different backend that merely speaks the same wire
# format (see `resolve`'s docstring).
_BUILTIN_CATALOGS: dict[str, dict[str, PriceEntry]] = {
    "openai": BUILTIN_OPENAI_PRICING,
    "anthropic": BUILTIN_ANTHROPIC_PRICING,
}


class PricingResolver:
    """Looks up the price to use for one executed (provider, model) pair.

    Lookup order:

    1. An operator override in `inferrail.yaml`'s `pricing:` section for
       this exact (provider name, model) — always wins, regardless of
       provider type, since the operator has explicitly declared it.
    2. The built-in catalog matching the provider's own verified `type`
       (`openai` or `anthropic`), but *only* with no custom `base_url` —
       i.e. it's verifiably that vendor's own API, not an
       `openai_compatible`/`anthropic_compatible` endpoint (vLLM, a
       proxy, a local server) that merely speaks the same wire format and
       could be serving a completely different, differently-priced model
       under a name that happens to collide (e.g. a self-hosted model
       someone names "gpt-4o-mini"). Guessing that one model/endpoint's
       price applies to another is exactly the fabrication this project
       refuses to do.
    3. Otherwise `None` — unknown, never `0`.
    """

    def __init__(
        self,
        providers: dict[str, ProviderConfig],
        overrides: dict[str, dict[str, PriceEntry]],
    ) -> None:
        self._providers = providers
        self._overrides = overrides

    def resolve(self, provider_name: str, model: str) -> PriceEntry | None:
        override = self._overrides.get(provider_name, {}).get(model)
        if override is not None:
            return override

        provider_config = self._providers.get(provider_name)
        if provider_config is not None and provider_config.base_url is None:
            catalog = _BUILTIN_CATALOGS.get(provider_config.type)
            if catalog is not None:
                return catalog.get(model)

        return None
