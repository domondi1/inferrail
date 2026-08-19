from __future__ import annotations

import os

from inferrail.config.models import InferrailConfig, ProviderConfig
from inferrail.errors import ConfigurationError
from inferrail.providers.base import Provider
from inferrail.providers.openai import OpenAIProvider


def build_providers(config: InferrailConfig, *, require_keys: bool = True) -> dict[str, Provider]:
    """Construct a runnable :class:`Provider` for every configured provider.

    This is where secrets are resolved from the environment, which is why
    it's separate from config *parsing*. Two callers, two needs:

    - `inferrail config check` uses the default ``require_keys=True`` to
      validate the shape of ``inferrail.yaml`` and then confirm every
      referenced secret is actually present, with one clear error per
      missing variable — that command's entire job is to fail loudly here.
    - The gateway (`gateway/app.py:create_app`) uses ``require_keys=False``
      so the server (and ``/health``) can start even before a secret is
      configured — a missing key only becomes an error when a request
      actually reaches that provider, via
      :meth:`inferrail.providers.openai.OpenAIProvider.complete`.
    """
    providers: dict[str, Provider] = {}
    for provider_name, provider_config in config.providers.items():
        providers[provider_name] = _build_one(
            provider_name, provider_config, require_key=require_keys
        )
    return providers


def _build_one(name: str, provider_config: ProviderConfig, *, require_key: bool) -> Provider:
    api_key = os.environ.get(provider_config.api_key_env) or ""
    if require_key and not api_key:
        raise ConfigurationError(
            f"provider '{name}' requires environment variable "
            f"'{provider_config.api_key_env}' to be set, but it is missing or empty"
        )

    if provider_config.type in ("openai", "openai_compatible"):
        return OpenAIProvider(
            name=name,
            api_key=api_key,
            base_url=provider_config.resolved_base_url(),
            # Same "verifiably OpenAI's own API" test as
            # pricing.resolver.PricingResolver.resolve — see its docstring
            # for why an openai_compatible endpoint never gets this.
            is_verified_openai=(
                provider_config.type == "openai" and provider_config.base_url is None
            ),
        )

    raise ConfigurationError(
        f"provider '{name}' has unsupported type '{provider_config.type}'"
    )
