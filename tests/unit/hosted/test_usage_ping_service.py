"""Service-wiring tests for hosted/usage_ping/service.py: schema
enforcement, the kill switch, rate limiting, the admin-gated /stats
route, and that the connecting IP is never persisted.

Loaded by explicit file path under a unique module name, not a bare
`import service` -- see test_ap_exceptions_service.py's own docstring
for why (multiple hosted/*/service.py files share the plain name
"service", and pytest collects every test file before running any of
them).
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path
from types import ModuleType

import pytest
from fastapi.testclient import TestClient

HOSTED_DIR = Path(__file__).resolve().parents[3] / "hosted" / "usage_ping"

_VALID_PAYLOAD = {
    "install_id": "test-install-1",
    "event": "first_run",
    "os": "linux",
    "inferrail_version": "0.4.1",
    "ts": "2026-09-15T00:00:00Z",
}


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "usage_ping_service_under_test", HOSTED_DIR / "service.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["usage_ping_service_under_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def service_module(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("USAGE_PING_RATE_LIMIT_MAX_REQUESTS", "1000")
    monkeypatch.setenv("USAGE_PING_RATE_LIMIT_WINDOW_SECONDS", "60")
    return _load_module()


@pytest.fixture
def app_and_db(service_module: ModuleType, tmp_path: Path):  # type: ignore[no-untyped-def]
    db_path = tmp_path / "usage-ping.sqlite3"
    app = service_module.create_app(db_path=db_path)
    return app, db_path


def test_health(app_and_db) -> None:  # type: ignore[no-untyped-def]
    app, _db = app_and_db
    response = TestClient(app).get("/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_valid_ping_is_accepted_and_stored(app_and_db) -> None:  # type: ignore[no-untyped-def]
    app, db_path = app_and_db
    response = TestClient(app).post("/ping", json=_VALID_PAYLOAD)
    assert response.status_code == 202
    assert response.json() == {"ok": True}

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT install_id, event FROM events").fetchall()
    assert rows == [("test-install-1", "first_run")]


def test_extra_field_is_rejected(app_and_db) -> None:  # type: ignore[no-untyped-def]
    app, _db = app_and_db
    response = TestClient(app).post("/ping", json={**_VALID_PAYLOAD, "prompt": "leaked!"})
    assert response.status_code == 422


def test_unknown_event_is_rejected(app_and_db) -> None:  # type: ignore[no-untyped-def]
    app, _db = app_and_db
    response = TestClient(app).post(
        "/ping", json={**_VALID_PAYLOAD, "event": "not_a_real_event"}
    )
    assert response.status_code == 422


def test_every_known_event_is_accepted(app_and_db) -> None:  # type: ignore[no-untyped-def]
    app, _db = app_and_db
    client = TestClient(app)
    for event in ("first_run", "tool_connected", "first_receipt", "budget_created"):
        response = client.post("/ping", json={**_VALID_PAYLOAD, "event": event})
        assert response.status_code == 202, event


def test_connecting_ip_is_never_persisted(app_and_db) -> None:  # type: ignore[no-untyped-def]
    app, db_path = app_and_db
    TestClient(app).post("/ping", json=_VALID_PAYLOAD)

    conn = sqlite3.connect(db_path)
    columns = [row[1].lower() for row in conn.execute("PRAGMA table_info(events)")]
    assert not any("ip" in c or "addr" in c or "host" in c for c in columns)


def test_kill_switch_returns_ok_without_storing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("USAGE_PING_ENABLED", "false")
    monkeypatch.setenv("USAGE_PING_RATE_LIMIT_MAX_REQUESTS", "1000")
    monkeypatch.setenv("USAGE_PING_RATE_LIMIT_WINDOW_SECONDS", "60")
    module = _load_module()
    db_path = tmp_path / "usage-ping.sqlite3"
    app = module.create_app(db_path=db_path)

    response = TestClient(app).post("/ping", json=_VALID_PAYLOAD)

    assert response.status_code == 202
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_rate_limit_returns_429_once_exceeded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("USAGE_PING_RATE_LIMIT_MAX_REQUESTS", "2")
    monkeypatch.setenv("USAGE_PING_RATE_LIMIT_WINDOW_SECONDS", "60")
    module = _load_module()
    app = module.create_app(db_path=tmp_path / "usage-ping.sqlite3")
    client = TestClient(app)

    assert client.post("/ping", json=_VALID_PAYLOAD).status_code == 202
    assert client.post("/ping", json=_VALID_PAYLOAD).status_code == 202
    assert client.post("/ping", json=_VALID_PAYLOAD).status_code == 429


def test_request_size_limit(app_and_db) -> None:  # type: ignore[no-untyped-def]
    app, _db = app_and_db
    oversized = {**_VALID_PAYLOAD, "install_id": "x" * 20_000}
    response = TestClient(app).post("/ping", json=oversized)
    assert response.status_code == 413


def test_stats_disabled_without_admin_token(app_and_db) -> None:  # type: ignore[no-untyped-def]
    app, _db = app_and_db
    assert TestClient(app).get("/stats").status_code == 404


def test_stats_enabled_with_admin_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("USAGE_PING_ADMIN_TOKEN", "secret-token")
    monkeypatch.setenv("USAGE_PING_RATE_LIMIT_MAX_REQUESTS", "1000")
    monkeypatch.setenv("USAGE_PING_RATE_LIMIT_WINDOW_SECONDS", "60")
    module = _load_module()
    app = module.create_app(db_path=tmp_path / "usage-ping.sqlite3")
    client = TestClient(app)
    client.post("/ping", json=_VALID_PAYLOAD)

    unauthed = client.get("/stats")
    assert unauthed.status_code == 401

    authed = client.get("/stats", headers={"Authorization": "Bearer secret-token"})
    assert authed.status_code == 200
    body = authed.json()
    assert body["events_received"]["first_run"] == 1
    assert body["total_distinct_installs"] == 1
