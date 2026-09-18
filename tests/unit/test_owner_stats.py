"""`scripts/owner_stats.py` -- both layers of owner-side install/usage
counting: PyPI download stats (mocked network) and the usage-ping
collector's own installs/events database (a real SQLite fixture built via
the actual collector service, not hand-crafted SQL, so a schema drift
between the two would break this test too).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

_SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
_HOSTED_DIR = Path(__file__).resolve().parents[2] / "hosted" / "usage_ping"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def owner_stats() -> ModuleType:
    return _load(_SCRIPTS_DIR / "owner_stats.py", "owner_stats_under_test")


@pytest.fixture
def collector_service() -> ModuleType:
    return _load(_HOSTED_DIR / "service.py", "owner_stats_collector_service_under_test")


def _seed_db(collector_service: ModuleType, db_path: Path) -> None:
    app = collector_service.create_app(db_path=db_path)
    client = TestClient(app)
    # Install "a": installed and reached a real receipt (activated).
    client.post(
        "/ping",
        json={
            "install_id": "a", "event": "install", "version": "0.4.1", "os": "linux",
            "python_version": "3.12",
        },
    )
    client.post(
        "/ping",
        json={
            "install_id": "a", "event": "first_receipt", "version": "0.4.1", "os": "linux",
            "python_version": "3.12",
        },
    )
    # Install "b": installed only, never activated.
    client.post(
        "/ping",
        json={
            "install_id": "b", "event": "install", "version": "0.4.1", "os": "darwin",
            "python_version": "3.11",
        },
    )


# --- PyPI stats (layer 1) --------------------------------------------


def test_fetch_pypi_download_counts_parses_a_successful_response(
    owner_stats: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    body = json.dumps(
        {"data": {"last_day": 12, "last_week": 84, "last_month": 360}, "package": "inferrail"}
    ).encode()

    class _FakeResponse:
        def __enter__(self) -> _FakeResponse:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def read(self) -> bytes:
            return body

    monkeypatch.setattr(owner_stats, "urlopen", lambda *a, **kw: _FakeResponse())

    counts = owner_stats.fetch_pypi_download_counts()

    assert counts == {"last_day": 12, "last_week": 84, "last_month": 360}


def test_fetch_pypi_download_counts_never_raises_on_network_failure(
    owner_stats: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    from urllib.error import URLError

    def _raise(*a: object, **kw: object) -> None:
        raise URLError("offline")

    monkeypatch.setattr(owner_stats, "urlopen", _raise)

    assert owner_stats.fetch_pypi_download_counts() is None


def test_print_pypi_stats_handles_unavailable_gracefully(
    owner_stats: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(owner_stats, "fetch_pypi_download_counts", lambda: None)

    owner_stats.print_pypi_stats()

    out = capsys.readouterr().out
    assert "unavailable this run" in out


# --- collector stats (layer 2) ----------------------------------------


def test_print_collector_stats_with_no_database(
    owner_stats: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    owner_stats.print_collector_stats(tmp_path / "does-not-exist.sqlite3")

    out = capsys.readouterr().out
    assert "no database found" in out


def test_print_collector_stats_reports_real_numbers(
    owner_stats: ModuleType,
    collector_service: ModuleType,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "usage-ping.sqlite3"
    _seed_db(collector_service, db_path)

    owner_stats.print_collector_stats(db_path)

    out = capsys.readouterr().out
    assert "Total installs:              2" in out
    assert "Activated (reached a real receipt): 1" in out
    assert "Activation rate:             50.0%" in out
    assert "Active in the last 7 days:   2" in out
    assert "Active in the last 30 days:  2" in out


def test_weekly_counts_groups_by_iso_week(owner_stats: ModuleType) -> None:
    counts = owner_stats._weekly_counts(
        [
            "2026-01-05T00:00:00+00:00",  # 2026-W02
            "2026-01-06T00:00:00+00:00",  # 2026-W02
            "2026-01-12T00:00:00+00:00",  # 2026-W03
            "not-a-timestamp",  # ignored, never crashes
        ]
    )
    assert counts["2026-W02"] == 2
    assert counts["2026-W03"] == 1
    assert len(counts) == 2


def test_main_runs_end_to_end_with_a_real_db(
    owner_stats: ModuleType,
    collector_service: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "usage-ping.sqlite3"
    _seed_db(collector_service, db_path)
    monkeypatch.setattr(owner_stats, "fetch_pypi_download_counts", lambda: None)

    result = owner_stats.main(["--db", str(db_path)])

    assert result == 0
    out = capsys.readouterr().out
    assert "Total installs:              2" in out


def test_main_without_db_flag_skips_collector_layer_cleanly(
    owner_stats: ModuleType, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(owner_stats, "fetch_pypi_download_counts", lambda: None)

    result = owner_stats.main([])

    assert result == 0
    out = capsys.readouterr().out
    assert "--db not given" in out
