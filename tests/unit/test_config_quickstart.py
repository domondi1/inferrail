from __future__ import annotations

from inferrail.config.models import InferrailConfig
from inferrail.config.quickstart import (
    QUICKSTART_API_KEY_ENV,
    QUICKSTART_MODEL,
    QUICKSTART_PROVIDER,
    QUICKSTART_RECEIPTS_PATH,
    QUICKSTART_ROUTE,
    build_quickstart_config,
)


def test_quickstart_config_is_a_real_inferrail_config() -> None:
    config = build_quickstart_config()

    # Went through InferrailConfig's own construction/validation, including
    # its cross-reference check (routes must reference a known provider) —
    # not a parallel, unvalidated config shape.
    assert isinstance(config, InferrailConfig)


def test_quickstart_config_defaults() -> None:
    config = build_quickstart_config()

    provider = config.providers[QUICKSTART_PROVIDER]
    assert provider.type == "openai"
    assert provider.api_key_env == QUICKSTART_API_KEY_ENV
    assert provider.base_url is None  # -> resolves to the real OpenAI API

    route = config.routes[QUICKSTART_ROUTE]
    assert route.provider == QUICKSTART_PROVIDER
    assert route.model == QUICKSTART_MODEL

    assert config.receipts.sink == "jsonl"
    assert config.receipts.path == QUICKSTART_RECEIPTS_PATH
    assert config.telemetry.sink == "console"

    # The whole point of the zero-config path: any model name works, not
    # just the "default" route above — see
    # docs/adr/0007-model-passthrough-routing.md.
    assert config.default_provider == QUICKSTART_PROVIDER


def test_quickstart_config_model_override() -> None:
    config = build_quickstart_config(model="gpt-4o")

    assert config.routes[QUICKSTART_ROUTE].model == "gpt-4o"


def test_quickstart_config_telemetry_sink_override() -> None:
    config = build_quickstart_config(telemetry_sink="none")

    assert config.telemetry.sink == "none"


def test_quickstart_config_no_secret_embedded() -> None:
    config = build_quickstart_config()

    # Only the *name* of the env var, never a value read from it.
    dumped = config.model_dump_json()
    assert "sk-" not in dumped
