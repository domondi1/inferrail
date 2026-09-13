"""Service-wiring tests for hosted/ap_exceptions/service.py: auth,
per-tenant isolation, idempotency, retention/deletion, and rate limiting.

Uses only dependencies already required by `inferrail` core (fastapi,
pydantic) -- no optional extra needed, unlike the CDP/x402/a2a-gated
hosted-service tests.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

HOSTED_DIR = Path(__file__).resolve().parents[3] / "hosted" / "ap_exceptions"
if str(HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(HOSTED_DIR))

_POLICY_CONFIG = {
    "eligible_failure_types": ["low_confidence", "validation_check_failed"],
    "retry_floor": 0.5,
    "human_review_threshold": 0.75,
    "max_retry_cost_usd": "1.00",
    "decision_deadline_seconds": 86400,
}


@pytest.fixture
def service_module(monkeypatch, tmp_path):
    monkeypatch.setenv("AP_API_KEYS", "key-a,key-b")
    monkeypatch.setenv("AP_RATE_LIMIT_MAX_REQUESTS", "1000")
    monkeypatch.setenv("AP_RATE_LIMIT_WINDOW_SECONDS", "60")
    import auth
    import service
    import tenant_store

    importlib.reload(tenant_store)
    importlib.reload(auth)
    importlib.reload(service)
    return service


@pytest.fixture
def app(service_module, tmp_path):
    return service_module.create_app(tmp_path / "data")


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    return TestClient(app)


def _auth(key: str = "key-a") -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def test_health_requires_no_auth(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_missing_auth_header_is_401(client):
    resp = client.post("/v1/decisions", json={})
    assert resp.status_code == 401


def test_invalid_api_key_is_401(client):
    resp = client.get("/v1/decisions/WORK-1", headers=_auth("not-a-real-key"))
    assert resp.status_code == 401


def test_create_decision_and_idempotent_replay(client):
    body = {
        "work_id": "WORK-1",
        "checkpoint_attempt_id": "att-1",
        "failure_type": "low_confidence",
        "confidence": 0.6,
        "cost_so_far_usd": "0.10",
        "policy_config": _POLICY_CONFIG,
    }
    first = client.post("/v1/decisions", json=body, headers=_auth())
    assert first.status_code == 200
    assert first.json()["recommended_action"] == "retry"
    assert first.json()["idempotent_replay"] is False

    second = client.post("/v1/decisions", json=body, headers=_auth())
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True
    assert second.json()["decision_id"] == first.json()["decision_id"]


def test_two_valid_tenants_are_isolated(client):
    body = {
        "work_id": "SHARED-ID",
        "checkpoint_attempt_id": "att-1",
        "failure_type": "low_confidence",
        "confidence": 0.6,
        "policy_config": _POLICY_CONFIG,
    }
    created = client.post("/v1/decisions", json=body, headers=_auth("key-a"))
    assert created.status_code == 200

    seen_by_owner = client.get("/v1/decisions/SHARED-ID", headers=_auth("key-a"))
    assert seen_by_owner.status_code == 200

    seen_by_other_tenant = client.get("/v1/decisions/SHARED-ID", headers=_auth("key-b"))
    assert seen_by_other_tenant.status_code == 404


def test_full_lifecycle_retry_attempt_handoff_outcome_report(client):
    body = {
        "work_id": "WORK-2",
        "checkpoint_attempt_id": "att-2",
        "failure_type": "low_confidence",
        "confidence": 0.95,  # outside retry band -> human_review
        "policy_config": _POLICY_CONFIG,
    }
    client.post("/v1/decisions", json=body, headers=_auth())

    handoff = client.post(
        "/v1/decisions/WORK-2/handoff", json={"handoff_ref": "queue-ticket-1"}, headers=_auth()
    )
    assert handoff.status_code == 200

    outcome = client.post(
        "/v1/decisions/WORK-2/outcome",
        json={"outcome": "corrected", "correction_delta_usd": "5.00", "review_cost_usd": "2.00"},
        headers=_auth(),
    )
    assert outcome.status_code == 200

    report = client.get("/v1/report", headers=_auth())
    assert report.status_code == 200
    row = next(r for r in report.json()["rows"] if r["work_id"] == "WORK-2")
    assert row["established_outcome"] == "corrected"
    assert row["handoff_ref"] == "queue-ticket-1"


def test_delete_is_retention_deletion(client):
    body = {
        "work_id": "WORK-3",
        "checkpoint_attempt_id": "att-3",
        "failure_type": "low_confidence",
        "confidence": 0.6,
        "policy_config": _POLICY_CONFIG,
    }
    client.post("/v1/decisions", json=body, headers=_auth())
    deleted = client.delete("/v1/decisions/WORK-3", headers=_auth())
    assert deleted.status_code == 200
    assert client.get("/v1/decisions/WORK-3", headers=_auth()).status_code == 404


def test_deleting_a_never_created_work_id_is_404(client):
    resp = client.delete("/v1/decisions/NEVER-EXISTED", headers=_auth())
    assert resp.status_code == 404


def test_rate_limit_returns_429_after_the_configured_max(
    service_module, tmp_path, monkeypatch
):
    monkeypatch.setenv("AP_RATE_LIMIT_MAX_REQUESTS", "3")
    monkeypatch.setenv("AP_RATE_LIMIT_WINDOW_SECONDS", "60")
    importlib.reload(service_module)
    from fastapi.testclient import TestClient

    limited_client = TestClient(service_module.create_app(tmp_path / "data"))

    for i in range(3):
        resp = limited_client.get(f"/v1/decisions/NOPE-{i}", headers=_auth())
        assert resp.status_code == 404  # not rate-limited yet, just not found
    limited = limited_client.get("/v1/decisions/NOPE-EXTRA", headers=_auth())
    assert limited.status_code == 429


def test_unsupported_failure_type_yields_insufficient_evidence(client):
    body = {
        "work_id": "WORK-4",
        "checkpoint_attempt_id": "att-4",
        "failure_type": "some_unknown_failure_mode",
        "confidence": 0.6,
        "policy_config": _POLICY_CONFIG,
    }
    resp = client.post("/v1/decisions", json=body, headers=_auth())
    assert resp.status_code == 200
    assert resp.json()["recommended_action"] == "insufficient_evidence"


def test_malformed_policy_config_is_422(client):
    body = {
        "work_id": "WORK-5",
        "checkpoint_attempt_id": "att-5",
        "failure_type": "low_confidence",
        "confidence": 0.6,
        "policy_config": {**_POLICY_CONFIG, "retry_floor": 2.0},
    }
    resp = client.post("/v1/decisions", json=body, headers=_auth())
    assert resp.status_code == 422
