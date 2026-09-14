"""Dashboard discovery and static-file mounting — see
docs/adr/0017-dashboard-in-app-directory.md.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from inferrail.config.models import InferrailConfig
from inferrail.dashboard import find_dashboard_dist
from inferrail.gateway import app as app_module


def _write_fake_build(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "index.html").write_text("<html><body>dashboard</body></html>")


def test_find_dashboard_dist_honors_the_env_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _write_fake_build(tmp_path / "built")
    monkeypatch.setenv("INFERRAIL_DASHBOARD_DIR", str(tmp_path / "built"))

    assert find_dashboard_dist() == tmp_path / "built"


def test_find_dashboard_dist_env_override_with_missing_index_is_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("INFERRAIL_DASHBOARD_DIR", str(empty))

    assert find_dashboard_dist() is None


def _app_mode_config(tmp_path: Path) -> InferrailConfig:
    return InferrailConfig.model_validate(
        {
            "providers": {
                "openai": {"type": "openai", "api_key_env": "TEST_OPENAI_API_KEY"},
            },
            "routes": {
                "default": {"provider": "openai", "model": "gpt-4o-mini"},
            },
            "telemetry": {"sink": "none"},
            "receipts": {"sink": "sqlite", "path": str(tmp_path / "receipts.db")},
            "budgets": {"enabled": True, "path": str(tmp_path / "budgets.db")},
        }
    )


def test_app_mode_with_no_dashboard_built_starts_normally(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("INFERRAIL_DASHBOARD_DIR", raising=False)
    monkeypatch.setattr("inferrail.gateway.app.find_dashboard_dist", lambda: None)
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {})
    monkeypatch.setattr(app_module, "build_anthropic_providers", lambda cfg, **_kw: {})

    app = app_module.create_app(
        _app_mode_config(tmp_path), app_mode=True, local_outcomes_path=tmp_path / "outcomes.jsonl"
    )
    client = TestClient(app)

    assert app.state.dashboard_dist is None
    assert client.get("/health").status_code == 200
    assert client.get("/dashboard/").status_code == 404


def test_app_mode_with_a_built_dashboard_serves_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    built = tmp_path / "dist"
    _write_fake_build(built)
    monkeypatch.setattr("inferrail.gateway.app.find_dashboard_dist", lambda: built)
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {})
    monkeypatch.setattr(app_module, "build_anthropic_providers", lambda cfg, **_kw: {})

    app = app_module.create_app(
        _app_mode_config(tmp_path), app_mode=True, local_outcomes_path=tmp_path / "outcomes.jsonl"
    )
    client = TestClient(app)

    assert app.state.dashboard_dist == built
    response = client.get("/dashboard/")
    assert response.status_code == 200
    assert "dashboard" in response.text


def test_dashboard_is_not_mounted_without_app_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    built = tmp_path / "dist"
    _write_fake_build(built)
    monkeypatch.setattr("inferrail.gateway.app.find_dashboard_dist", lambda: built)
    monkeypatch.setattr(app_module, "build_providers", lambda cfg, **_kw: {})
    monkeypatch.setattr(app_module, "build_anthropic_providers", lambda cfg, **_kw: {})

    app = app_module.create_app(_app_mode_config(tmp_path), app_mode=False)
    client = TestClient(app)

    assert client.get("/dashboard/").status_code == 404
