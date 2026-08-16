"""Resolves a (provider, model) pair to a verified :class:`PriceEntry`.

Pure and deterministic — a function of `inferrail.yaml` alone, no runtime
state — mirroring `routing.router.Router`. Kept independent of the HTTP
layer and of provider execution so it's testable standalone.
"""

from __future__ import annotations

from inferrail.config.models import PriceEntry, ProviderConfig
from inferrail.pricing.builtin import BUILTIN_OPENAI_PRICING


class PricingResolver:
    """Looks up the price to use for one executed (provider, model) pair.

    Lookup order:

    1. An operator override in `inferrail.yaml`'s `pricing:` section for
       this exact (provider name, model) — always wins, regardless of
       provider type, since the operator has explicitly declared it.
    2. The built-in catalog, but *only* if the provider is configured as
       `type: openai` with no custom `base_url` — i.e. it's verifiably
       OpenAI's own API, not an `openai_compatible` endpoint (vLLM, a
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
        if (
            provider_config is not None
            and provider_config.type == "openai"
            and provider_config.base_url is None
        ):
            return BUILTIN_OPENAI_PRICING.get(model)

        return None
