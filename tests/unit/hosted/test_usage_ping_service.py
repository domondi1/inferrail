"""Service-wiring tests for hosted/usage_ping/service.py: schema
enforcement, the installs/events upsert (including activation tracking
via `reached_first_receipt_at`), the kill switch, rate limiting, the
admin-gated /stats route, and that the connecting IP is never persisted.

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
    "event": "install",
    "version": "0.4.1",
    "os": "linux",
    "python_version": "3.12",
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
    assert rows == [("test-install-1", "install")]


def test_ping_upserts_the_installs_table(app_and_db) -> None:  # type: ignore[no-untyped-def]
    app, db_path = app_and_db
    client = TestClient(app)

    client.post("/ping", json=_VALID_PAYLOAD)
    client.post("/ping", json={**_VALID_PAYLOAD, "event": "serve_start", "version": "0.5.0"})

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT install_id, version, os, python_version, reached_first_receipt_at "
        "FROM installs"
    ).fetchone()
    assert row[0] == "test-install-1"
    assert row[1] == "0.5.0"  # last_seen version, not the first
    assert row[2] == "linux"
    assert row[3] == "3.12"
    assert row[4] is None  # no first_receipt event sent yet
    assert conn.execute("SELECT COUNT(*) FROM installs").fetchone()[0] == 1  # upsert, not insert


def test_first_receipt_sets_reached_first_receipt_at_once(
    app_and_db,  # type: ignore[no-untyped-def]
) -> None:
    app, db_path = app_and_db
    client = TestClient(app)
    client.post("/ping", json=_VALID_PAYLOAD)

    client.post("/ping", json={**_VALID_PAYLOAD, "event": "first_receipt"})
    conn = sqlite3.connect(db_path)
    first = conn.execute(
        "SELECT reached_first_receipt_at FROM installs WHERE install_id = ?",
        ("test-install-1",),
    ).fetchone()[0]
    assert first is not None

    # A second first_receipt (shouldn't normally happen -- once-ever on
    # the client side -- but the server must still never overwrite it).
    client.post("/ping", json={**_VALID_PAYLOAD, "event": "first_receipt"})
    second = conn.execute(
        "SELECT reached_first_receipt_at FROM installs WHERE install_id = ?",
        ("test-install-1",),
    ).fetchone()[0]
    assert second == first


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


def test_unknown_os_is_rejected(app_and_db) -> None:  # type: ignore[no-untyped-def]
    app, _db = app_and_db
    response = TestClient(app).post("/ping", json={**_VALID_PAYLOAD, "os": "haiku"})
    assert response.status_code == 422


def test_every_known_event_is_accepted(app_and_db) -> None:  # type: ignore[no-untyped-def]
    app, _db = app_and_db
    client = TestClient(app)
    for event in ("install", "serve_start", "first_receipt", "heartbeat"):
        response = client.post("/ping", json={**_VALID_PAYLOAD, "event": event})
        assert response.status_code == 202, event


def test_connecting_ip_is_never_persisted(app_and_db) -> None:  # type: ignore[no-untyped-def]
    app, db_path = app_and_db
    TestClient(app).post("/ping", json=_VALID_PAYLOAD)

    conn = sqlite3.connect(db_path)
    # Exact expected column sets, not a substring heuristic -- a naive
    # "ip"/"addr"/"host" substring check false-positives on legitimate
    # columns like `reached_first_receipt_at` (contains "receipt", which
    # contains "ip").
    installs_columns = {row[1] for row in conn.execute("PRAGMA table_info(installs)")}
    events_columns = {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    assert installs_columns == {
        "install_id", "first_seen_at", "last_seen_at", "version", "os", "python_version",
        "reached_first_receipt_at",
    }
    assert events_columns == {"id", "install_id", "event", "version", "seen_at"}


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
    assert conn.execute("SELECT COUNT(*) FROM installs").fetchone()[0] == 0


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
    client.post("/ping", json={**_VALID_PAYLOAD, "event": "first_receipt"})

    unauthed = client.get("/stats")
    assert unauthed.status_code == 401

    authed = client.get("/stats", headers={"Authorization": "Bearer secret-token"})
    assert authed.status_code == 200
    body = authed.json()
    assert body["events_received"]["install"] == 1
    assert body["events_received"]["first_receipt"] == 1
    assert body["total_installs"] == 1
    assert body["activated_installs"] == 1
