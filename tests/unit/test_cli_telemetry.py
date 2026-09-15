"""`inferrail telemetry preview|status|enable|disable` (cli/telemetry.py,
docs/adr/0019-opt-in-usage-ping.md). Never touches the network -- these
commands only ever read/write local state and print, regardless of
whether an endpoint is configured."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from inferrail.cli.main import main
from inferrail.cli.telemetry import (
    run_telemetry_disable,
    run_telemetry_enable,
    run_telemetry_preview,
    run_telemetry_status,
)
from inferrail.usage_ping.payload import KNOWN_EVENTS


def _write_config(path: Path, **overrides: Any) -> Path:
    config: dict[str, Any] = {
        "providers": {"openai": {"type": "openai", "api_key_env": "TEST_OPENAI_API_KEY"}},
        "routes": {"default": {"provider": "openai", "model": "gpt-4o-mini"}},
        "telemetry": {"sink": "none"},
        "receipts": {"sink": "none"},
    }
    config.update(overrides)
    config_path = path / "inferrail.yaml"
    config_path.write_text(yaml.safe_dump(config))
    return config_path


@pytest.fixture(autouse=True)
def _isolated_app_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Never let these tests touch the real developer machine's app-data dir.
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


def test_status_with_no_config_file_reports_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)  # no inferrail.yaml here

    result = run_telemetry_status(None)

    assert result == 0
    out = capsys.readouterr().out
    assert "enabled:    False" in out
    assert "not configured" in out


def test_status_reads_endpoint_from_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _write_config(
        tmp_path, usage_ping={"enabled": False, "endpoint": "https://ping.example/ping"}
    )

    result = run_telemetry_status(str(config_path))

    assert result == 0
    assert "https://ping.example/ping" in capsys.readouterr().out


def test_preview_prints_every_known_event_without_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import httpx

    sent: list[object] = []
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: sent.append(1))
    monkeypatch.chdir(tmp_path)

    result = run_telemetry_preview(None)

    assert result == 0
    out = capsys.readouterr().out
    for event in KNOWN_EVENTS:
        assert event in out
    # Every printed payload line is valid JSON with exactly the fixed shape.
    payload_lines = [
        line.strip() for line in out.splitlines() if line.strip().startswith("{")
    ]
    assert len(payload_lines) == len(KNOWN_EVENTS)
    for line in payload_lines:
        payload = json.loads(line)
        assert set(payload) == {"install_id", "event", "os", "inferrail_version", "ts"}
    assert sent == []  # preview must never actually send anything


def test_enable_then_disable_round_trips(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _write_config(tmp_path)

    assert run_telemetry_enable(str(config_path)) == 0
    assert "enabled" in capsys.readouterr().out.lower()
    assert run_telemetry_status(str(config_path)) == 0
    assert "enabled:    True" in capsys.readouterr().out

    assert run_telemetry_disable(str(config_path)) == 0
    assert run_telemetry_status(str(config_path)) == 0
    assert "enabled:    False" in capsys.readouterr().out


def test_enable_warns_when_no_endpoint_configured(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _write_config(tmp_path)  # no usage_ping.endpoint

    run_telemetry_enable(str(config_path))

    assert "still inert" in capsys.readouterr().out


def test_cli_telemetry_preview_via_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)

    result = main(["telemetry", "preview"])

    assert result == 0
    assert "first_run" in capsys.readouterr().out
