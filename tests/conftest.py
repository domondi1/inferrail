from __future__ import annotations

from typing import Any

import pytest

from inferrail.config.models import InferrailConfig


@pytest.fixture
def base_config_dict() -> dict[str, Any]:
    return {
        "providers": {
            "openai": {"type": "openai", "api_key_env": "TEST_OPENAI_API_KEY"},
        },
        "routes": {
            "default": {"provider": "openai", "model": "gpt-4o-mini"},
        },
        "telemetry": {"sink": "none"},
        "receipts": {"sink": "none"},
    }


@pytest.fixture
def base_config(base_config_dict: dict[str, Any]) -> InferrailConfig:
    return InferrailConfig.model_validate(base_config_dict)
