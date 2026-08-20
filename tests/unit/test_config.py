from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from inferrail.config.loader import load_config
from inferrail.config.models import InferrailConfig, TelemetryConfig
from inferrail.errors import ConfigurationError
from inferrail.providers.registry import build_providers


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


def test_jsonl_telemetry_requires_path() -> None:
    with pytest.raises(ValidationError, match="telemetry.path is required"):
        TelemetryConfig(sink="jsonl")


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
