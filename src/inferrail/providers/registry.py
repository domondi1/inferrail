from __future__ import annotations

import os

from inferrail.config.models import InferrailConfig, ProviderConfig
from inferrail.errors import ConfigurationError
from inferrail.providers.base import Provider
from inferrail.providers.openai import OpenAIProvider


def build_providers(config: InferrailConfig) -> dict[str, Provider]:
    """Construct a runnable :class:`Provider` for every configured provider.

    This is where secrets are resolved from the environment, which is why
    it's separate from config *parsing*: `inferrail config check` can
    validate the shape of ``inferrail.yaml`` and then call this to confirm
    every referenced secret is actually present, with one clear error per
    missing variable.
    """
    providers: dict[str, Provider] = {}
    for provider_name, provider_config in config.providers.items():
        providers[provider_name] = _build_one(provider_name, provider_config)
    return providers


def _build_one(name: str, provider_config: ProviderConfig) -> Provider:
    api_key = os.environ.get(provider_config.api_key_env)
    if not api_key:
        raise ConfigurationError(
            f"provider '{name}' requires environment variable "
            f"'{provider_config.api_key_env}' to be set, but it is missing or empty"
        )

    if provider_config.type in ("openai", "openai_compatible"):
        return OpenAIProvider(
            name=name,
            api_key=api_key,
            base_url=provider_config.resolved_base_url(),
        )

    raise ConfigurationError(
        f"provider '{name}' has unsupported type '{provider_config.type}'"
    )
