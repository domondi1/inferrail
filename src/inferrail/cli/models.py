"""`inferrail models` -- list the models each configured provider says it
offers, next to whether Inferrail has a price for them.

Inferrail never chooses a model: requests name their own model id, which
is passed to the provider unchanged. This command only helps the user see
what their account can call and which of those models can run under a
dollar budget. "The provider lists it" and "Inferrail has a trusted price"
are reported separately: a model without a price can still be called,
but a block budget refuses it (`INFERRAIL_E012`) until a price is added.

Discovery is read-only and uses the provider's own list endpoint with the
key configured for that provider:

- OpenAI: ``GET {base_url}/models``
- Anthropic: ``GET {base_url}/models`` (paginated, ``anthropic-version`` header)
- OpenAI-/Anthropic-compatible upstreams: the same path, best effort; many
  gateways don't implement it, and that is reported, not guessed around.

Nothing here is cached, persisted or sent anywhere else.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import httpx

from inferrail.config.models import InferrailConfig, ProviderConfig
from inferrail.pricing.resolver import PricingResolver

_ANTHROPIC_API_VERSION = "2023-06-01"
_TIMEOUT_S = 20.0


class DiscoveryUnavailableError(Exception):
    """The upstream has no usable model-list endpoint (or refused the call)."""


def _list_openai_style(client: httpx.Client, provider: ProviderConfig, key: str) -> list[str]:
    response = client.get(
        f"{provider.resolved_base_url().rstrip('/')}/models",
        headers={"Authorization": f"Bearer {key}"},
    )
    if response.status_code != 200:
        raise DiscoveryUnavailableError(f"HTTP {response.status_code} from the model list endpoint")
    data = response.json().get("data")
    if not isinstance(data, list):
        raise DiscoveryUnavailableError("the model list response has no 'data' array")
    return sorted({str(item["id"]) for item in data if isinstance(item, dict) and "id" in item})


def _list_anthropic_style(client: httpx.Client, provider: ProviderConfig, key: str) -> list[str]:
    url = f"{provider.resolved_base_url().rstrip('/')}/models"
    headers = {"x-api-key": key, "anthropic-version": _ANTHROPIC_API_VERSION}
    ids: list[str] = []
    params: dict[str, str] = {"limit": "1000"}
    while True:
        response = client.get(url, headers=headers, params=params)
        if response.status_code != 200:
            raise DiscoveryUnavailableError(
                f"HTTP {response.status_code} from the model list endpoint"
            )
        body = response.json()
        ids += [str(item["id"]) for item in body.get("data", []) if "id" in item]
        if not body.get("has_more") or not body.get("last_id"):
            return sorted(set(ids))
        params = {"limit": "1000", "after_id": str(body["last_id"])}


def list_models(client: httpx.Client, provider: ProviderConfig, key: str) -> list[str]:
    if provider.type in ("anthropic", "anthropic_compatible"):
        return _list_anthropic_style(client, provider, key)
    return _list_openai_style(client, provider, key)


def pricing_status(
    resolver: PricingResolver, config: InferrailConfig, provider: str, model: str
) -> str:
    entry = resolver.resolve(provider, model)
    if entry is None:
        return "no price (refused under a dollar budget until you add one)"
    if model in config.pricing.get(provider, {}):
        return f"operator-supplied ({entry.source})"
    if "operator-asserted" in entry.source:
        return f"operator-asserted ({entry.source})"
    return f"built-in, verified {entry.verified_date}"


def run_models(
    config: InferrailConfig,
    *,
    provider_name: str | None = None,
    as_json: bool = False,
    client: httpx.Client | None = None,
) -> int:
    if provider_name is not None and provider_name not in config.providers:
        print(
            f"error: unknown provider '{provider_name}'; known: {sorted(config.providers)}",
            file=sys.stderr,
        )
        return 1
    resolver = PricingResolver(config.providers, config.pricing)
    names = [provider_name] if provider_name else sorted(config.providers)
    rows: list[dict[str, str]] = []
    notes: list[str] = []
    http = client or httpx.Client(timeout=_TIMEOUT_S)
    try:
        for name in names:
            provider = config.providers[name]
            key = os.environ.get(provider.api_key_env, "")
            if not key:
                notes.append(f"{name}: skipped, {provider.api_key_env} is not set")
                continue
            try:
                models = list_models(http, provider, key)
            except (DiscoveryUnavailableError, httpx.HTTPError, ValueError) as exc:
                notes.append(
                    f"{name}: can't list models ({exc}). Any model id this upstream "
                    "accepts still passes through."
                )
                continue
            rows += [
                {"provider": name, "model": m, "pricing": pricing_status(resolver, config, name, m)}
                for m in models
            ]
    finally:
        if client is None:
            http.close()

    if as_json:
        print(json.dumps({"models": rows, "notes": notes}, indent=2))
    else:
        width = max((len(r["model"]) for r in rows), default=5)
        for row in rows:
            print(f"{row['provider']:<10} {row['model']:<{width}}  {row['pricing']}")
        for note in notes:
            print(note, file=sys.stderr)
        if rows:
            print(
                "\nUse any of these by sending its id as the request's `model`. "
                "Inferrail doesn't pick one for you.",
                file=sys.stderr,
            )
    return 0 if rows or not notes else 1


def load_models_config(config_path: str | None) -> InferrailConfig:
    """`--config`, else ./inferrail.yaml, else the zero-config providers."""
    from inferrail.config.loader import load_config
    from inferrail.config.quickstart import build_quickstart_config

    if config_path is not None:
        return load_config(config_path)
    if Path("inferrail.yaml").exists():
        return load_config("inferrail.yaml")
    return build_quickstart_config(telemetry_sink="none")
