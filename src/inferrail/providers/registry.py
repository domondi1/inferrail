from __future__ import annotations

import os

from inferrail.config.models import InferrailConfig, ProviderConfig
from inferrail.errors import ConfigurationError
from inferrail.providers.anthropic import AnthropicProvider
from inferrail.providers.anthropic_base import AnthropicMessagesProvider
from inferrail.providers.base import Provider
from inferrail.providers.openai import OpenAIProvider


def _resolve_api_key(name: str, provider_config: ProviderConfig, *, require_key: bool) -> str:
    api_key = os.environ.get(provider_config.api_key_env) or ""
    if require_key and not api_key:
        raise ConfigurationError(
            f"provider '{name}' requires environment variable "
            f"'{provider_config.api_key_env}' to be set, but it is missing or empty"
        )
    return api_key


def build_providers(config: InferrailConfig, *, require_keys: bool = True) -> dict[str, Provider]:
    """Construct a runnable OpenAI-shaped :class:`Provider` for every
    ``openai``/``openai_compatible`` provider configured — used by
    `gateway.execution.InferenceEngine` (the `/v1/chat/completions`
    pipeline) only. An ``anthropic``/``anthropic_compatible`` entry in
    ``config.providers`` is silently not included here (it's built by
    `build_anthropic_providers` instead, for the parallel `/v1/messages`
    pipeline — see docs/adr/0014-anthropic-messages-passthrough.md); this
    is not an error, since a provider is simply invisible to the engine
    that can't use it, exactly like an unconfigured route is.

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
        if provider_config.type not in ("openai", "openai_compatible"):
            continue
        api_key = _resolve_api_key(provider_name, provider_config, require_key=require_keys)
        providers[provider_name] = OpenAIProvider(
            name=provider_name,
            api_key=api_key,
            base_url=provider_config.resolved_base_url(),
            # Same "verifiably OpenAI's own API" test as
            # pricing.resolver.PricingResolver.resolve — see its docstring
            # for why an openai_compatible endpoint never gets this.
            is_verified_openai=(
                provider_config.type == "openai" and provider_config.base_url is None
            ),
        )
    return providers


def build_anthropic_providers(
    config: InferrailConfig, *, require_keys: bool = True
) -> dict[str, AnthropicMessagesProvider]:
    """Construct a runnable :class:`AnthropicMessagesProvider` for every
    ``anthropic``/``anthropic_compatible`` provider configured — used by
    `gateway.anthropic_execution.AnthropicInferenceEngine` (the
    `/v1/messages` pipeline) only. Mirrors `build_providers` exactly
    (including the ``require_keys`` split between `inferrail config
    check` and the gateway); see that function's docstring."""
    providers: dict[str, AnthropicMessagesProvider] = {}
    for provider_name, provider_config in config.providers.items():
        if provider_config.type not in ("anthropic", "anthropic_compatible"):
            continue
        api_key = _resolve_api_key(provider_name, provider_config, require_key=require_keys)
        providers[provider_name] = AnthropicProvider(
            name=provider_name,
            api_key=api_key,
            base_url=provider_config.resolved_base_url(),
        )
    return providers
