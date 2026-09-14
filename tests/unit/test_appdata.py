from __future__ import annotations

from pathlib import Path

import pytest

from inferrail.appdata import app_data_dir, ensure_app_data_dir


def test_app_data_dir_on_linux_defaults_to_xdg_data_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    path = app_data_dir()

    assert path == Path.home() / ".local" / "share" / "inferrail"


def test_app_data_dir_on_linux_honors_xdg_data_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    assert app_data_dir() == tmp_path / "inferrail"


def test_app_data_dir_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.platform", "darwin")

    path = app_data_dir()

    assert path == Path.home() / "Library" / "Application Support" / "inferrail"


def test_app_data_dir_on_windows_honors_appdata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path))

    assert app_data_dir() == tmp_path / "inferrail"


def test_app_data_dir_never_creates_the_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    app_data_dir()

    assert not (tmp_path / "inferrail").exists()


def test_ensure_app_data_dir_creates_it_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))

    first = ensure_app_data_dir()
    second = ensure_app_data_dir()

    assert first == second == tmp_path / "inferrail"
    assert first.is_dir()
