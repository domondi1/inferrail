"""`inferrail models`: lists what each provider says it offers, reports
pricing separately, and never selects a model."""

from __future__ import annotations

import json

import httpx
import pytest

from inferrail.cli.models import run_models
from inferrail.config.models import InferrailConfig


def _config(**extra: object) -> InferrailConfig:
    return InferrailConfig.model_validate(
        {
            "providers": {
                "openai": {"type": "openai", "api_key_env": "T_OPENAI"},
                "anthropic": {"type": "anthropic", "api_key_env": "T_ANTHROPIC"},
                "gw": {
                    "type": "openai_compatible",
                    "api_key_env": "T_GW",
                    "base_url": "http://gw.test/v1",
                },
            },
            "default_provider": "openai",
            "telemetry": {"sink": "none"},
            "receipts": {"sink": "none"},
            **extra,
        }
    )


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "api.openai.com" and request.url.path == "/v1/models":
        assert request.headers["authorization"] == "Bearer k-openai"
        return httpx.Response(200, json={"data": [{"id": "gpt-4o"}, {"id": "brand-new-model"}]})
    if request.url.host == "api.anthropic.com" and request.url.path == "/v1/models":
        assert request.headers["x-api-key"] == "k-anthropic"
        assert request.headers["anthropic-version"]
        if request.url.params.get("after_id") is None:
            return httpx.Response(
                200,
                json={
                    "data": [{"id": "claude-haiku-4-5"}],
                    "has_more": True,
                    "last_id": "claude-haiku-4-5",
                },
            )
        return httpx.Response(200, json={"data": [{"id": "claude-next"}], "has_more": False})
    if request.url.host == "gw.test":
        return httpx.Response(404, json={"error": "not found"})
    raise AssertionError(f"unexpected request {request.url}")


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("T_OPENAI", "k-openai")
    monkeypatch.setenv("T_ANTHROPIC", "k-anthropic")
    monkeypatch.setenv("T_GW", "k-gw")


def _run(
    config: InferrailConfig, capsys: pytest.CaptureFixture[str], **kw: object
) -> dict[str, object]:
    client = httpx.Client(transport=httpx.MockTransport(_handler))
    code = run_models(config, as_json=True, client=client, **kw)  # type: ignore[arg-type]
    out = json.loads(capsys.readouterr().out)
    out["code"] = code
    return out


def test_lists_models_per_provider_with_pricing_reported_separately(
    keys: None, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _run(_config(), capsys)
    rows = {(r["provider"], r["model"]): r["pricing"] for r in out["models"]}  # type: ignore[union-attr]
    assert rows[("openai", "gpt-4o")].startswith("built-in, verified ")
    assert rows[("openai", "brand-new-model")].startswith("no price")
    assert rows[("anthropic", "claude-haiku-4-5")].startswith("built-in, verified ")
    assert rows[("anthropic", "claude-next")].startswith("no price")  # second page fetched
    # An upstream without a model list is reported, not guessed around.
    assert any(n.startswith("gw: can't list models") for n in out["notes"])  # type: ignore[union-attr]
    assert out["code"] == 0


def test_operator_pricing_is_labelled_as_operator_supplied(
    keys: None, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(
        pricing={
            "openai": {
                "brand-new-model": {
                    "input_usd_per_million": "1",
                    "output_usd_per_million": "2",
                    "source": "vendor pricing page",
                    "verified_date": "2026-10-01",
                }
            }
        }
    )
    out = _run(config, capsys, provider_name="openai")
    rows = {r["model"]: r["pricing"] for r in out["models"]}  # type: ignore[union-attr]
    assert rows["brand-new-model"] == "operator-supplied (vendor pricing page)"


def test_missing_key_is_skipped_and_unknown_provider_is_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("T_OPENAI", raising=False)
    out = _run(_config(), capsys, provider_name="openai")
    assert out["models"] == [] and out["notes"] == ["openai: skipped, T_OPENAI is not set"]
    assert out["code"] == 1
    assert run_models(_config(), provider_name="nope") == 1


def test_config_without_routes_needs_a_default_provider() -> None:
    assert _config().routes == {}
    with pytest.raises(ValueError, match="default_provider"):
        InferrailConfig.model_validate(
            {
                "providers": {"openai": {"type": "openai", "api_key_env": "X"}},
                "telemetry": {"sink": "none"},
                "receipts": {"sink": "none"},
            }
        )
