from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from inferrail.config.loader import load_config
from inferrail.config.models import InferrailConfig, ProviderConfig, TelemetryConfig
from inferrail.errors import ConfigurationError
from inferrail.providers.anthropic import AnthropicProvider
from inferrail.providers.openai import OpenAIProvider
from inferrail.providers.registry import build_anthropic_providers, build_providers


def _write_yaml(path: Path, data: dict[str, Any]) -> Path:
    config_path = path / "inferrail.yaml"
    config_path.write_text(yaml.safe_dump(data))
    return config_path


def test_load_config_valid(tmp_path: Path, base_config_dict: dict[str, Any]) -> None:
    config_path = _write_yaml(tmp_path, base_config_dict)

    config = load_config(config_path)

    assert config.providers["openai"].type == "openai"
    assert config.routes["default"].provider == "openai"
    assert config.routes["default"].model == "gpt-4o-mini"
    assert config.server.port == 8000


def test_example_yaml_loads() -> None:
    """inferrail.example.yaml is the file README, docs/PRODUCT.md, and
    llms.txt all tell every new user (and agent) to copy. CI regenerates
    config.schema.json from InferrailConfig on every change, but nothing
    else re-checks this hand-maintained file against that model — it could
    silently drift out of sync with a real config change with no test
    catching it until someone actually runs `inferrail config check`.
    """
    repo_root = Path(__file__).resolve().parents[2]
    config = load_config(repo_root / "inferrail.example.yaml")

    assert config.routes["default"].provider == "openai"
    assert config.providers["openai"].type == "openai"


def test_load_config_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="not found"):
        load_config(tmp_path / "does-not-exist.yaml")


def test_load_config_invalid_yaml(tmp_path: Path) -> None:
    config_path = tmp_path / "inferrail.yaml"
    config_path.write_text("providers: [this is: not: valid")

    with pytest.raises(ConfigurationError, match="not valid YAML"):
        load_config(config_path)


def test_load_config_empty_file(tmp_path: Path) -> None:
    config_path = tmp_path / "inferrail.yaml"
    config_path.write_text("")

    with pytest.raises(ConfigurationError, match="empty"):
        load_config(config_path)


def test_load_config_wraps_validation_error(
    tmp_path: Path, base_config_dict: dict[str, Any]
) -> None:
    base_config_dict["routes"]["default"]["provider"] = "does-not-exist"
    config_path = _write_yaml(tmp_path, base_config_dict)

    with pytest.raises(ConfigurationError, match="unknown provider"):
        load_config(config_path)


def test_route_referencing_unknown_provider_fails_validation(
    base_config_dict: dict[str, Any],
) -> None:
    base_config_dict["routes"]["default"]["provider"] = "does-not-exist"

    with pytest.raises(ValidationError, match="unknown provider"):
        InferrailConfig.model_validate(base_config_dict)


def test_config_requires_at_least_one_provider_and_route() -> None:
    with pytest.raises(ValidationError):
        InferrailConfig.model_validate({"providers": {}, "routes": {}})


def test_default_provider_referencing_unknown_provider_fails_validation(
    base_config_dict: dict[str, Any],
) -> None:
    base_config_dict["default_provider"] = "does-not-exist"

    with pytest.raises(ValidationError, match="default_provider 'does-not-exist'"):
        InferrailConfig.model_validate(base_config_dict)


def test_default_provider_referencing_known_provider_is_valid(
    base_config_dict: dict[str, Any],
) -> None:
    base_config_dict["default_provider"] = "openai"

    config = InferrailConfig.model_validate(base_config_dict)

    assert config.default_provider == "openai"


def test_default_provider_is_none_by_default(base_config: InferrailConfig) -> None:
    assert base_config.default_provider is None


def test_default_anthropic_provider_referencing_unknown_provider_fails_validation(
    base_config_dict: dict[str, Any],
) -> None:
    base_config_dict["default_anthropic_provider"] = "does-not-exist"

    with pytest.raises(ValidationError, match="default_anthropic_provider 'does-not-exist'"):
        InferrailConfig.model_validate(base_config_dict)


def test_default_anthropic_provider_referencing_known_provider_is_valid(
    base_config_dict: dict[str, Any],
) -> None:
    base_config_dict["default_anthropic_provider"] = "openai"

    config = InferrailConfig.model_validate(base_config_dict)

    assert config.default_anthropic_provider == "openai"


def test_default_anthropic_provider_is_none_by_default(base_config: InferrailConfig) -> None:
    assert base_config.default_anthropic_provider is None


def test_jsonl_telemetry_requires_path() -> None:
    with pytest.raises(ValidationError, match="telemetry.path is required"):
        TelemetryConfig(sink="jsonl")


def test_budgets_disabled_by_default(base_config: InferrailConfig) -> None:
    assert base_config.budgets.enabled is False


def test_budgets_enabled_requires_sqlite_receipts(base_config_dict: dict[str, Any]) -> None:
    base_config_dict["receipts"] = {"sink": "jsonl", "path": "./r.jsonl"}
    base_config_dict["budgets"] = {"enabled": True}
    with pytest.raises(ValidationError, match="budgets.enabled requires receipts.sink"):
        InferrailConfig.model_validate(base_config_dict)


def test_budgets_enabled_with_sqlite_receipts_is_valid(
    base_config_dict: dict[str, Any],
) -> None:
    base_config_dict["receipts"] = {"sink": "sqlite", "path": "./r.db"}
    base_config_dict["budgets"] = {"enabled": True}
    config = InferrailConfig.model_validate(base_config_dict)
    assert config.budgets.enabled is True


def test_budgets_disabled_is_valid_with_any_receipts_sink(
    base_config_dict: dict[str, Any],
) -> None:
    base_config_dict["receipts"] = {"sink": "jsonl", "path": "./r.jsonl"}
    config = InferrailConfig.model_validate(base_config_dict)
    assert config.budgets.enabled is False


def test_usage_ping_opt_out_by_default_but_unconfigured(base_config: InferrailConfig) -> None:
    # ADR-0020: opt-out (enabled=True) by default, but still fully inert
    # with no endpoint configured -- see UsagePingConfig's own docstring.
    assert base_config.usage_ping.enabled is True
    assert base_config.usage_ping.endpoint is None


def test_usage_ping_accepts_an_explicit_endpoint(base_config_dict: dict[str, Any]) -> None:
    base_config_dict["usage_ping"] = {"enabled": True, "endpoint": "https://ping.example/ping"}
    config = InferrailConfig.model_validate(base_config_dict)
    assert config.usage_ping.enabled is True
    assert config.usage_ping.endpoint == "https://ping.example/ping"


def test_usage_ping_rejects_unknown_fields(base_config_dict: dict[str, Any]) -> None:
    base_config_dict["usage_ping"] = {"enabled": True, "extra_field": "nope"}
    with pytest.raises(ValidationError):
        InferrailConfig.model_validate(base_config_dict)


def test_build_providers_fails_loudly_when_api_key_missing(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    monkeypatch.delenv("TEST_OPENAI_API_KEY", raising=False)

    with pytest.raises(ConfigurationError, match="TEST_OPENAI_API_KEY"):
        build_providers(base_config)


def test_build_providers_succeeds_when_api_key_present(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    monkeypatch.setenv("TEST_OPENAI_API_KEY", "test-key")

    providers = build_providers(base_config)

    assert set(providers) == {"openai"}


def test_build_providers_require_keys_false_tolerates_missing_key(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    # require_keys=False is what gateway/app.py:create_app uses, so the
    # server can start (and serve /health) before a secret is configured —
    # see providers/openai.py's OpenAIProvider.complete for where a
    # still-missing key is caught instead, at actual request time.
    monkeypatch.delenv("TEST_OPENAI_API_KEY", raising=False)

    providers = build_providers(base_config, require_keys=False)

    assert set(providers) == {"openai"}


# ---------------------------------------------------------------------------
# Anthropic providers (docs/adr/0014-anthropic-messages-passthrough.md)
# ---------------------------------------------------------------------------


def _anthropic_config() -> InferrailConfig:
    return InferrailConfig.model_validate(
        {
            "providers": {
                "anthropic": {"type": "anthropic", "api_key_env": "TEST_ANTHROPIC_API_KEY"},
            },
            "routes": {"claude": {"provider": "anthropic", "model": "claude-sonnet-5"}},
        }
    )


def test_build_anthropic_providers_fails_loudly_when_api_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(ConfigurationError, match="TEST_ANTHROPIC_API_KEY"):
        build_anthropic_providers(_anthropic_config())


def test_build_anthropic_providers_succeeds_when_api_key_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_ANTHROPIC_API_KEY", "test-key")

    providers = build_anthropic_providers(_anthropic_config())

    assert set(providers) == {"anthropic"}
    assert isinstance(providers["anthropic"], AnthropicProvider)


def test_build_anthropic_providers_require_keys_false_tolerates_missing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TEST_ANTHROPIC_API_KEY", raising=False)

    providers = build_anthropic_providers(_anthropic_config(), require_keys=False)

    assert set(providers) == {"anthropic"}


def test_build_providers_never_builds_an_anthropic_typed_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An anthropic-typed provider is simply invisible to build_providers
    # (the OpenAI-shaped /v1/chat/completions pipeline) -- never an error
    # for "having the wrong kind" of provider configured. See ADR-0014.
    monkeypatch.setenv("TEST_ANTHROPIC_API_KEY", "test-key")

    providers = build_providers(_anthropic_config())

    assert providers == {}


def test_build_anthropic_providers_never_builds_an_openai_typed_entry(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    monkeypatch.setenv("TEST_OPENAI_API_KEY", "test-key")

    providers = build_anthropic_providers(base_config)

    assert providers == {}


def test_anthropic_compatible_requires_explicit_base_url() -> None:
    config = ProviderConfig(type="anthropic_compatible", api_key_env="KEY")

    with pytest.raises(ValueError, match="requires an explicit base_url"):
        config.resolved_base_url()


def test_anthropic_defaults_to_the_real_api_base_url() -> None:
    config = ProviderConfig(type="anthropic", api_key_env="KEY")

    assert config.resolved_base_url() == "https://api.anthropic.com/v1"


def test_openai_provider_class_still_returned_for_openai_types(
    monkeypatch: pytest.MonkeyPatch, base_config: InferrailConfig
) -> None:
    monkeypatch.setenv("TEST_OPENAI_API_KEY", "test-key")

    providers = build_providers(base_config)

    assert isinstance(providers["openai"], OpenAIProvider)


def test_receipts_defaults_to_jsonl_when_omitted(base_config_dict: dict[str, Any]) -> None:
    del base_config_dict["receipts"]

    config = InferrailConfig.model_validate(base_config_dict)

    assert config.receipts.sink == "jsonl"
    assert config.receipts.path == "./inferrail-receipts.jsonl"


def test_pricing_defaults_to_empty_when_omitted(base_config_dict: dict[str, Any]) -> None:
    config = InferrailConfig.model_validate(base_config_dict)

    assert config.pricing == {}


def test_pricing_override_parses_from_yaml_shaped_dict(
    base_config_dict: dict[str, Any],
) -> None:
    base_config_dict["pricing"] = {
        "openai": {
            "gpt-4o-mini": {
                "input_usd_per_million": "0.15",
                "output_usd_per_million": "0.60",
                "source": "https://developers.openai.com/api/docs/pricing",
                "verified_date": "2026-08-16",
            }
        }
    }

    config = InferrailConfig.model_validate(base_config_dict)

    price = config.pricing["openai"]["gpt-4o-mini"]
    assert price.input_usd_per_million == Decimal("0.15")
    assert price.output_usd_per_million == Decimal("0.60")


def test_pricing_override_requires_source_and_verified_date(
    base_config_dict: dict[str, Any],
) -> None:
    base_config_dict["pricing"] = {
        "openai": {
            "gpt-4o-mini": {
                "input_usd_per_million": "0.15",
                "output_usd_per_million": "0.60",
            }
        }
    }

    with pytest.raises(ValidationError):
        InferrailConfig.model_validate(base_config_dict)
