"""`inferrail doctor` (see cli/doctor.py). All socket use is mocked —
this must never depend on real network reachability to pass in CI."""

from __future__ import annotations

import socket
from pathlib import Path
from typing import Any

import pytest
import yaml

from inferrail.cli.doctor import _check_port, _check_provider_reachability, run_doctor
from inferrail.cli.main import main


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


def test_check_port_free(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeSocket:
        def __enter__(self) -> _FakeSocket:
            return self

        def __exit__(self, *exc: object) -> None:
            pass

        def settimeout(self, *_a: object) -> None:
            pass

        def connect_ex(self, *_a: object) -> int:
            return 111  # ECONNREFUSED-ish: nothing listening

    monkeypatch.setattr(socket, "socket", lambda *a, **kw: _FakeSocket())

    ok, message = _check_port("127.0.0.1", 8000)

    assert ok is True
    assert "free" in message


def test_check_port_in_use(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeSocket:
        def __enter__(self) -> _FakeSocket:
            return self

        def __exit__(self, *exc: object) -> None:
            pass

        def settimeout(self, *_a: object) -> None:
            pass

        def connect_ex(self, *_a: object) -> int:
            return 0  # something is listening

    monkeypatch.setattr(socket, "socket", lambda *a, **kw: _FakeSocket())

    ok, message = _check_port("127.0.0.1", 8000)

    assert ok is False
    assert "already in use" in message


def test_check_provider_reachability_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeConn:
        def __enter__(self) -> _FakeConn:
            return self

        def __exit__(self, *exc: object) -> None:
            pass

    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: _FakeConn())

    ok, message = _check_provider_reachability("openai", "https://api.openai.com/v1")

    assert ok is True
    assert "reachable" in message


def test_check_provider_reachability_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_a: object, **_kw: object) -> None:
        raise OSError("simulated: no route to host")

    monkeypatch.setattr(socket, "create_connection", _raise)

    ok, message = _check_provider_reachability("openai", "https://api.openai.com/v1")

    assert ok is False
    assert "cannot reach" in message


def test_check_provider_reachability_unparseable_base_url() -> None:
    ok, message = _check_provider_reachability("weird", "not-a-url")

    assert ok is False
    assert "could not parse a host" in message


def test_run_doctor_missing_config_fails_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = run_doctor(str(tmp_path / "does-not-exist.yaml"))

    assert result == 1
    assert "FAIL" in capsys.readouterr().out


def test_run_doctor_all_checks_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _write_config(tmp_path)
    monkeypatch.setattr("inferrail.cli.doctor._check_port", lambda *a, **kw: (True, "port free"))
    monkeypatch.setattr(
        "inferrail.cli.doctor._check_provider_reachability",
        lambda *a, **kw: (True, "reachable"),
    )

    result = run_doctor(str(config_path))

    out = capsys.readouterr().out
    assert result == 0
    assert "No problems found." in out


def test_run_doctor_reports_port_and_provider_problems(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _write_config(tmp_path)
    monkeypatch.setattr(
        "inferrail.cli.doctor._check_port", lambda *a, **kw: (False, "port in use")
    )
    monkeypatch.setattr(
        "inferrail.cli.doctor._check_provider_reachability",
        lambda *a, **kw: (False, "unreachable"),
    )

    result = run_doctor(str(config_path))

    out = capsys.readouterr().out
    assert result == 1
    assert "2 problem(s) found" in out


def test_cli_doctor_via_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = _write_config(tmp_path)
    monkeypatch.setattr("inferrail.cli.doctor._check_port", lambda *a, **kw: (True, "port free"))
    monkeypatch.setattr(
        "inferrail.cli.doctor._check_provider_reachability",
        lambda *a, **kw: (True, "reachable"),
    )

    result = main(["doctor", "--config", str(config_path)])

    assert result == 0
    assert "config:" in capsys.readouterr().out
