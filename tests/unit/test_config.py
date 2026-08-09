from __future__ import annotations

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
