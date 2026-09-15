"""Unit tests for `hatch_build.py`'s own skip-gracefully logic — the
happy path (a real `npm run build` bundled into a real wheel) is
verified end-to-end by the `dashboard` CI job instead (a mocked
subprocess wouldn't prove the real integration works); these tests
cover the paths that must never fail the build, since most build
environments that produce this wheel from an sdist won't have Node.

See `hatch_build.py` and `docs/adr/0017-dashboard-in-app-directory.md`.
"""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from typing import Any

import pytest

_HOOK_PATH = Path(__file__).resolve().parents[2] / "hatch_build.py"
_spec = importlib.util.spec_from_file_location("hatch_build", _HOOK_PATH)
assert _spec is not None and _spec.loader is not None
hatch_build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hatch_build)


def _hook(root: Path) -> Any:
    # BuildHookInterface's other constructor args (build_config, metadata,
    # app) are never touched by DashboardBuildHook.initialize() -- only
    # `root` and `target_name` are read (see hatch_build.py itself).
    return hatch_build.DashboardBuildHook(
        root=str(root), config={}, build_config=None, metadata=None,
        directory="", target_name="wheel",
    )


def _make_app_dir(root: Path, *, with_package_json: bool = True) -> Path:
    app_dir = root / "app"
    app_dir.mkdir()
    if with_package_json:
        (app_dir / "package.json").write_text("{}")
    return app_dir


def test_skips_for_a_non_wheel_target(tmp_path: Path) -> None:
    hook = hatch_build.DashboardBuildHook(
        root=str(tmp_path), config={}, build_config=None, metadata=None,
        directory="", target_name="sdist",
    )
    build_data: dict[str, Any] = {}

    hook.initialize("0.0.0", build_data)

    assert "force_include" not in build_data
    assert not (tmp_path / "src" / "inferrail" / "dashboard_static").exists()


def test_skips_when_app_directory_has_no_package_json(tmp_path: Path) -> None:
    _make_app_dir(tmp_path, with_package_json=False)
    build_data: dict[str, Any] = {}

    _hook(tmp_path).initialize("0.0.0", build_data)

    assert "force_include" not in build_data


def test_skips_when_npm_is_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_app_dir(tmp_path)
    monkeypatch.setattr(hatch_build.shutil, "which", lambda _cmd: None)
    build_data: dict[str, Any] = {}

    _hook(tmp_path).initialize("0.0.0", build_data)

    assert "force_include" not in build_data
    assert "npm not found" in capsys.readouterr().err


def test_skips_when_the_npm_build_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_app_dir(tmp_path)
    monkeypatch.setattr(hatch_build.shutil, "which", lambda _cmd: "/usr/bin/npm")

    def _fail(*_args: Any, **_kwargs: Any) -> None:
        raise subprocess.CalledProcessError(1, ["npm", "ci"])

    monkeypatch.setattr(hatch_build.subprocess, "run", _fail)
    build_data: dict[str, Any] = {}

    _hook(tmp_path).initialize("0.0.0", build_data)

    assert "force_include" not in build_data
    assert "dashboard build failed" in capsys.readouterr().err


def test_skips_when_build_succeeds_but_produces_no_index_html(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _make_app_dir(tmp_path)
    monkeypatch.setattr(hatch_build.shutil, "which", lambda _cmd: "/usr/bin/npm")
    monkeypatch.setattr(hatch_build.subprocess, "run", lambda *a, **k: None)
    build_data: dict[str, Any] = {}

    _hook(tmp_path).initialize("0.0.0", build_data)

    assert "force_include" not in build_data
    assert "did not produce" in capsys.readouterr().err


def test_bundles_and_sets_force_include_on_a_successful_build(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app_dir = _make_app_dir(tmp_path)
    dist_dir = app_dir / "dist"

    def _fake_run(cmd: list[str], **_kwargs: Any) -> None:
        if cmd[1:] == ["run", "build"]:
            dist_dir.mkdir()
            (dist_dir / "index.html").write_text("<html></html>")

    monkeypatch.setattr(hatch_build.shutil, "which", lambda _cmd: "/usr/bin/npm")
    monkeypatch.setattr(hatch_build.subprocess, "run", _fake_run)
    build_data: dict[str, Any] = {}

    _hook(tmp_path).initialize("0.0.0", build_data)

    target_dir = tmp_path / "src" / "inferrail" / "dashboard_static"
    assert (target_dir / "index.html").is_file()
    assert build_data["force_include"][str(target_dir)] == "inferrail/dashboard_static"
