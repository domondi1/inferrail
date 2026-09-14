"""Service-wiring tests for hosted/ap_exceptions/service.py: auth,
per-tenant isolation, idempotency, retention/deletion, and rate limiting.

Uses only dependencies already required by `inferrail` core (fastapi,
pydantic) -- no optional extra needed, unlike the CDP/x402/a2a-gated
hosted-service tests.

Loads hosted/ap_exceptions's flat-file modules by explicit path via
`importlib.util` rather than `sys.path.insert` + a bare `import service`.
`hosted/work_economics/service.py` is a same-named sibling module under
the same flat-file convention -- pytest collects every test file before
running any of them, so by test-run time *every* hosted test's
`sys.path.insert` has already executed, and a bare `import service`
would silently resolve to whichever hosted directory landed first on
`sys.path`, not necessarily this one. Loading by explicit file path (and
pre-registering `auth`/`tenant_store` under their own plain names --
unique in this repo, so this is safe) avoids that entirely, regardless
of collection order or what other hosted directories exist now or later.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

HOSTED_DIR = Path(__file__).resolve().parents[3] / "hosted" / "ap_exceptions"

_POLICY_CONFIG = {
    "eligible_failure_types": ["low_confidence", "validation_check_failed"],
    "retry_floor": 0.5,
    "human_review_threshold": 0.75,
    "max_retry_cost_usd": "1.00",
    "decision_deadline_seconds": 86400,
}


def _load(name: str, filename: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, HOSTED_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def service_module(monkeypatch, tmp_path):
    monkeypatch.setenv("AP_API_KEYS", "key-a,key-b")
    monkeypatch.setenv("AP_RATE_LIMIT_MAX_REQUESTS", "1000")
    monkeypatch.setenv("AP_RATE_LIMIT_WINDOW_SECONDS", "60")
    # "auth"/"tenant_store"/"sandbox" are unique names in this repo --
    # registering them under their own plain names is safe and lets
    # service.py's internal `from auth import ...` /
    # `from tenant_store import ...` / `from sandbox import ...` resolve
    # correctly without hosted/ap_exceptions ever touching sys.path.
    # "service" is deliberately NOT reused as the registration name --
    # that's the exact name the collision above is about.
    _load("tenant_store", "tenant_store.py")
    _load("auth", "auth.py")
    _load("sandbox", "sandbox.py")
    return _load("ap_exceptions_service_under_test", "service.py")


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
    del service_module  # re-loaded fresh below with the tighter rate limit in effect
    monkeypatch.setenv("AP_RATE_LIMIT_MAX_REQUESTS", "3")
    monkeypatch.setenv("AP_RATE_LIMIT_WINDOW_SECONDS", "60")
    tight_service_module = _load("ap_exceptions_service_under_test_tight", "service.py")
    from fastapi.testclient import TestClient

    limited_client = TestClient(tight_service_module.create_app(tmp_path / "data"))

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


def test_report_marks_incomplete_when_retry_succeeded_but_validation_failed(client):
    """Hosted regression for the reproduced cost-completeness defect: a
    retry attempt recorded as status="success" but validation_passed=false
    must not be reported as cost-complete."""
    body = {
        "work_id": "WORK-6",
        "checkpoint_attempt_id": "att-6",
        "failure_type": "low_confidence",
        "confidence": 0.6,
        "policy_config": _POLICY_CONFIG,
    }
    client.post("/v1/decisions", json=body, headers=_auth())
    client.post(
        "/v1/decisions/WORK-6/retry-attempts",
        json={
            "attempt_id": "ret-6", "status": "success", "cost_usd": "0.06",
            "validation_passed": False, "validator_version": "ap.validator/v1",
        },
        headers=_auth(),
    )
    report = client.get("/v1/report", headers=_auth())
    row = next(r for r in report.json()["rows"] if r["work_id"] == "WORK-6")
    assert row["retry_status"] == "success"
    assert row["validation_passed"] is False
    assert row["observed_cost_complete"] is False


def test_report_marks_incomplete_when_outcome_recorded_without_review_cost(client):
    body = {
        "work_id": "WORK-7",
        "checkpoint_attempt_id": "att-7",
        "failure_type": "low_confidence",
        "confidence": 0.95,  # outside retry band -> human_review
        "policy_config": _POLICY_CONFIG,
    }
    client.post("/v1/decisions", json=body, headers=_auth())
    client.post(
        "/v1/decisions/WORK-7/outcome",
        json={"outcome": "corrected"},  # no review_cost_usd
        headers=_auth(),
    )
    report = client.get("/v1/report", headers=_auth())
    row = next(r for r in report.json()["rows"] if r["work_id"] == "WORK-7")
    assert row["established_outcome"] == "corrected"
    assert row["observed_cost_complete"] is False


def test_reap_endpoint_transitions_a_stale_decision(client):
    body = {
        "work_id": "WORK-8",
        "checkpoint_attempt_id": "att-8",
        "failure_type": "low_confidence",
        "confidence": 0.6,  # in retry band -> retry_in_progress
        "policy_config": _POLICY_CONFIG,
        "lease_seconds": 0.01,
    }
    created = client.post("/v1/decisions", json=body, headers=_auth())
    assert created.json()["status"] == "retry_in_progress"

    import time

    time.sleep(0.05)
    reap = client.post("/v1/decisions/WORK-8/reap", headers=_auth())
    assert reap.status_code == 200
    assert reap.json()["work_id"] == "WORK-8"
    assert reap.json()["reaped"] is True
    assert reap.json()["kind"] == "reaped"

    after = client.get("/v1/decisions/WORK-8", headers=_auth())
    assert after.json()["status"] == "awaiting_human_review"

    # Idempotent: a repeat reap for the same work_id is a no-op.
    second_reap = client.post("/v1/decisions/WORK-8/reap", headers=_auth())
    assert second_reap.json()["work_id"] == "WORK-8"
    assert second_reap.json()["reaped"] is False
    assert second_reap.json()["kind"] is None


def test_reap_endpoint_reconciles_when_a_real_attempt_already_exists(app, tmp_path):
    """The crash boundary this pass's review named: a real attempt was
    already durably recorded before the process died, only the
    decision's own status transition ever ran. /reap must reconcile
    using that attempt's own recorded validation result, not insert a
    second synthetic one, and not leave the decision stuck forever.

    Simulates the crash precisely by writing directly to the same
    tenant SQLite file the hosted service uses -- bypassing the
    `/retry-attempts` endpoint's own (atomic, single-request)
    status-transition step -- rather than trying to interrupt a real
    HTTP request mid-flight."""
    from fastapi.testclient import TestClient
    from tenant_store import tenant_id_for_api_key

    from inferrail.ap.store import RecoveryStore

    tenant_id = tenant_id_for_api_key("key-a")
    db_path = tmp_path / "data" / f"{tenant_id}.sqlite3"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    raw_store = RecoveryStore(db_path)
    raw_store.create_decision(
        work_id="WORK-RECONCILE", decision_id="dec-reconcile", checkpoint_attempt_id="att-r1",
        failure_type="low_confidence", confidence="0.6", cost_so_far_usd="0.10",
        policy_name="candidate_policy", policy_version="ap.policy/v1",
        recommended_action="retry", reason="test", status="retry_in_progress",
        worker_id="crashed-worker", lease_expires_at=1.0,  # already expired
    )
    raw_store.record_retry_attempt(
        work_id="WORK-RECONCILE", attempt_id="ret-real", status="success", cost_usd="0.06",
        confidence="0.9", provider="fixture", validation_passed="True",
        validator_version="ap.validator/v1",
    )
    # The decision is now stuck exactly as if the process had crashed
    # between record_retry_attempt succeeding and the status transition
    # that would normally follow it in the same request.
    assert raw_store.get_decision("WORK-RECONCILE")["status"] == "retry_in_progress"

    client = TestClient(app)
    reap = client.post("/v1/decisions/WORK-RECONCILE/reap", headers=_auth())
    assert reap.json()["kind"] == "reconciled"
    after = client.get("/v1/decisions/WORK-RECONCILE", headers=_auth())
    assert after.json()["status"] == "retry_resolved"


def test_reap_endpoint_404s_for_unknown_work_id(client):
    resp = client.post("/v1/decisions/NEVER-EXISTED/reap", headers=_auth())
    assert resp.status_code == 404


def test_reap_stale_sweep_endpoint_returns_count_and_is_tenant_isolated(client):
    import time

    for i, key in enumerate(["key-a", "key-a", "key-b"]):
        client.post(
            "/v1/decisions",
            json={
                "work_id": f"SWEEP-{i}", "checkpoint_attempt_id": f"att-sweep-{i}",
                "failure_type": "low_confidence", "confidence": 0.6,
                "policy_config": _POLICY_CONFIG, "lease_seconds": 0.01,
            },
            headers=_auth(key),
        )
    time.sleep(0.05)

    swept_a = client.post("/v1/reap-stale", headers=_auth("key-a"))
    assert swept_a.json()["reaped_count"] == 2
    assert set(swept_a.json()["reaped_work_ids"]) == {"SWEEP-0", "SWEEP-1"}

    # Tenant key-b's own stale lease is untouched by key-a's sweep.
    still_in_progress = client.get("/v1/decisions/SWEEP-2", headers=_auth("key-b"))
    assert still_in_progress.json()["status"] == "retry_in_progress"

    swept_b = client.post("/v1/reap-stale", headers=_auth("key-b"))
    assert swept_b.json()["reaped_count"] == 1
    assert swept_b.json()["reaped_work_ids"] == ["SWEEP-2"]


def test_retry_attempt_transitions_decision_status_off_retry_in_progress(client):
    """Regression: recording a retry attempt used to leave the decision
    stuck at retry_in_progress forever, so /v1/report's
    observed_cost_complete could never become true on the hosted path.
    Now it mirrors the local SDK engine's own transition."""
    body = {
        "work_id": "WORK-9",
        "checkpoint_attempt_id": "att-9",
        "failure_type": "low_confidence",
        "confidence": 0.6,
        "policy_config": _POLICY_CONFIG,
    }
    created = client.post("/v1/decisions", json=body, headers=_auth())
    assert created.json()["status"] == "retry_in_progress"

    client.post(
        "/v1/decisions/WORK-9/retry-attempts",
        json={
            "attempt_id": "ret-9", "status": "success", "cost_usd": "0.06",
            "validation_passed": True, "validator_version": "ap.validator/v1",
        },
        headers=_auth(),
    )
    after = client.get("/v1/decisions/WORK-9", headers=_auth())
    assert after.json()["status"] == "retry_resolved"

    report = client.get("/v1/report", headers=_auth())
    row = next(r for r in report.json()["rows"] if r["work_id"] == "WORK-9")
    assert row["observed_cost_complete"] is True


def test_retry_attempt_failed_validation_transitions_to_awaiting_human_review(client):
    body = {
        "work_id": "WORK-10",
        "checkpoint_attempt_id": "att-10",
        "failure_type": "low_confidence",
        "confidence": 0.6,
        "policy_config": _POLICY_CONFIG,
    }
    client.post("/v1/decisions", json=body, headers=_auth())
    client.post(
        "/v1/decisions/WORK-10/retry-attempts",
        json={
            "attempt_id": "ret-10", "status": "success", "cost_usd": "0.06",
            "validation_passed": False, "validator_version": "ap.validator/v1",
        },
        headers=_auth(),
    )
    after = client.get("/v1/decisions/WORK-10", headers=_auth())
    assert after.json()["status"] == "awaiting_human_review"


def test_sandbox_issuance_returns_usable_key_and_is_stamped(client):
    resp = client.post("/v1/sandbox")
    assert resp.status_code == 200
    body = resp.json()
    assert body["api_key"].startswith("sbx_")
    assert body["tenant_id"].startswith("sbx_")
    assert body["sandbox"] is True
    assert body["sandbox_notice"]  # non-empty synthetic-data-only notice
    assert body["max_rows_per_tenant"] > 0
    assert body["expires_at"]


def test_sandbox_key_runs_the_full_walkthrough_and_every_response_is_stamped(client):
    """The v0.2.1 walkthrough: get key -> create decision -> record
    retry attempt -> read report, entirely with a self-serve key and no
    operator-provisioned AP_API_KEYS entry involved."""
    key = client.post("/v1/sandbox").json()["api_key"]
    headers = _auth(key)

    created = client.post(
        "/v1/decisions",
        json={
            "work_id": "SANDBOX-DEMO-1", "checkpoint_attempt_id": "att-1",
            "failure_type": "low_confidence", "confidence": 0.6,
            "policy_config": _POLICY_CONFIG,
        },
        headers=headers,
    )
    assert created.status_code == 200
    assert created.json()["sandbox"] is True
    assert created.json()["status"] == "retry_in_progress"

    retry = client.post(
        "/v1/decisions/SANDBOX-DEMO-1/retry-attempts",
        json={
            "attempt_id": "ret-1", "status": "success", "cost_usd": "0.05",
            "validation_passed": True, "validator_version": "ap.validator/v1",
        },
        headers=headers,
    )
    assert retry.status_code == 200
    assert retry.json()["sandbox"] is True

    report = client.get("/v1/report", headers=headers)
    assert report.status_code == 200
    assert report.json()["sandbox"] is True
    row = next(r for r in report.json()["rows"] if r["work_id"] == "SANDBOX-DEMO-1")
    assert row["status"] == "retry_resolved"


def test_operator_tenant_response_is_stamped_sandbox_false(client):
    ok = client.post(
        "/v1/decisions",
        json={
            "work_id": "OPERATOR-STAMP-1", "checkpoint_attempt_id": "att-1",
            "failure_type": "low_confidence", "confidence": 0.6,
            "policy_config": _POLICY_CONFIG,
        },
        headers=_auth("key-a"),
    )
    assert ok.json()["sandbox"] is False
    assert ok.json()["sandbox_notice"] is None


def test_two_sandbox_keys_are_isolated_from_each_other_and_from_operator_tenants(client):
    key_1 = client.post("/v1/sandbox").json()["api_key"]
    key_2 = client.post("/v1/sandbox").json()["api_key"]

    client.post(
        "/v1/decisions",
        json={
            "work_id": "SHARED-SANDBOX-ID", "checkpoint_attempt_id": "att-1",
            "failure_type": "low_confidence", "confidence": 0.6,
            "policy_config": _POLICY_CONFIG,
        },
        headers=_auth(key_1),
    )
    assert client.get("/v1/decisions/SHARED-SANDBOX-ID", headers=_auth(key_2)).status_code == 404
    assert client.get("/v1/decisions/SHARED-SANDBOX-ID", headers=_auth("key-a")).status_code == 404
    assert client.get("/v1/decisions/SHARED-SANDBOX-ID", headers=_auth(key_1)).status_code == 200


def test_sandbox_key_expires_and_fails_cleanly_with_a_helpful_error(
    service_module, tmp_path, monkeypatch
):
    del service_module  # re-loaded fresh below with a near-zero TTL in effect
    monkeypatch.setenv("AP_SANDBOX_TTL_SECONDS", "0.01")
    short_ttl_module = _load("ap_exceptions_service_under_test_short_ttl", "service.py")
    from fastapi.testclient import TestClient

    short_ttl_client = TestClient(short_ttl_module.create_app(tmp_path / "data"))

    key = short_ttl_client.post("/v1/sandbox").json()["api_key"]

    import time

    time.sleep(0.05)
    resp = short_ttl_client.get("/v1/decisions/ANYTHING", headers=_auth(key))
    assert resp.status_code == 401
    assert "expired" in resp.json()["detail"].lower()
    assert "/v1/sandbox" in resp.json()["detail"]


def test_sandbox_issuance_throttled_per_ip(service_module, tmp_path, monkeypatch):
    del service_module
    monkeypatch.setenv("AP_SANDBOX_ISSUE_MAX_PER_IP", "2")
    monkeypatch.setenv("AP_SANDBOX_ISSUE_WINDOW_SECONDS", "60")
    throttled_module = _load("ap_exceptions_service_under_test_throttled", "service.py")
    from fastapi.testclient import TestClient

    throttled_client = TestClient(throttled_module.create_app(tmp_path / "data"))

    for _ in range(2):
        assert throttled_client.post("/v1/sandbox").status_code == 200
    limited = throttled_client.post("/v1/sandbox")
    assert limited.status_code == 429
    assert "sandbox key issuance limit" in limited.json()["detail"]


def test_sandbox_global_tenant_ceiling_returns_503(service_module, tmp_path, monkeypatch):
    del service_module
    monkeypatch.setenv("AP_SANDBOX_MAX_LIVE_TENANTS", "1")
    monkeypatch.setenv("AP_SANDBOX_ISSUE_MAX_PER_IP", "1000")
    capped_module = _load("ap_exceptions_service_under_test_capped", "service.py")
    from fastapi.testclient import TestClient

    capped_client = TestClient(capped_module.create_app(tmp_path / "data"))

    assert capped_client.post("/v1/sandbox").status_code == 200
    full = capped_client.post("/v1/sandbox")
    assert full.status_code == 503
    assert "capacity" in full.json()["detail"]


def test_sandbox_kill_switch_disables_issuance(service_module, tmp_path, monkeypatch):
    del service_module
    monkeypatch.setenv("AP_SANDBOX_ENABLED", "false")
    disabled_module = _load("ap_exceptions_service_under_test_disabled", "service.py")
    from fastapi.testclient import TestClient

    disabled_client = TestClient(disabled_module.create_app(tmp_path / "data"))

    resp = disabled_client.post("/v1/sandbox")
    assert resp.status_code == 503
    assert "disabled" in resp.json()["detail"]


def test_sandbox_row_cap_blocks_further_decisions(service_module, tmp_path, monkeypatch):
    del service_module
    monkeypatch.setenv("AP_SANDBOX_MAX_ROWS_PER_TENANT", "2")
    capped_module = _load("ap_exceptions_service_under_test_row_capped", "service.py")
    from fastapi.testclient import TestClient

    capped_client = TestClient(capped_module.create_app(tmp_path / "data"))
    key = capped_client.post("/v1/sandbox").json()["api_key"]

    for i in range(2):
        resp = capped_client.post(
            "/v1/decisions",
            json={
                "work_id": f"ROW-CAP-{i}", "checkpoint_attempt_id": f"att-{i}",
                "failure_type": "low_confidence", "confidence": 0.6,
                "policy_config": _POLICY_CONFIG,
            },
            headers=_auth(key),
        )
        assert resp.status_code == 200

    over_cap = capped_client.post(
        "/v1/decisions",
        json={
            "work_id": "ROW-CAP-OVER", "checkpoint_attempt_id": "att-over",
            "failure_type": "low_confidence", "confidence": 0.6,
            "policy_config": _POLICY_CONFIG,
        },
        headers=_auth(key),
    )
    assert over_cap.status_code == 429
    assert "row cap" in over_cap.json()["detail"]

    # A repeat of an already-created work_id is still an idempotent
    # replay, never blocked by the row cap -- it isn't a new row.
    replay = capped_client.post(
        "/v1/decisions",
        json={
            "work_id": "ROW-CAP-0", "checkpoint_attempt_id": "att-0",
            "failure_type": "low_confidence", "confidence": 0.6,
            "policy_config": _POLICY_CONFIG,
        },
        headers=_auth(key),
    )
    assert replay.status_code == 200
    assert replay.json()["idempotent_replay"] is True


def test_sandbox_key_never_authenticates_as_an_operator_key_and_vice_versa(client):
    sandbox_key = client.post("/v1/sandbox").json()["api_key"]
    # An operator key is never treated as a sandbox key just because it
    # doesn't match AP_API_KEYS -- it still gets the plain "invalid API
    # key" 401, not the sandbox "expired" branch.
    resp = client.get("/v1/decisions/X", headers=_auth("not-configured-and-not-sandbox"))
    assert resp.status_code == 401
    assert "invalid api key" in resp.json()["detail"].lower()
    # A real sandbox key works.
    assert client.get("/v1/decisions/X", headers=_auth(sandbox_key)).status_code == 404


def test_oversized_request_body_is_rejected_with_413(service_module, tmp_path, monkeypatch):
    del service_module
    monkeypatch.setenv("AP_MAX_REQUEST_BODY_BYTES", "100")
    small_limit_module = _load("ap_exceptions_service_under_test_small_body", "service.py")
    from fastapi.testclient import TestClient

    small_limit_client = TestClient(small_limit_module.create_app(tmp_path / "data"))
    oversized_body = {
        "work_id": "OVERSIZED", "checkpoint_attempt_id": "att-1",
        "failure_type": "low_confidence" * 20, "confidence": 0.6,
        "policy_config": _POLICY_CONFIG,
    }
    resp = small_limit_client.post("/v1/decisions", json=oversized_body, headers=_auth("key-a"))
    assert resp.status_code == 413


def _load_hosted_client_example():
    example_path = (
        Path(__file__).resolve().parents[3]
        / "examples" / "ap_invoice_exception_recovery" / "hosted_client_example.py"
    )
    spec = importlib.util.spec_from_file_location("hosted_client_example", example_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # needed for dataclass field resolution under PEP 563
    spec.loader.exec_module(module)
    return module


def test_hosted_client_example_end_to_end_retry_path(client, tmp_path):
    """Runs the example's real walkthrough function against the FastAPI
    TestClient fixture -- one coherent flow, not separately-exercised
    endpoints -- and asserts real field-level values in the final
    report, not just HTTP 200s."""
    example = _load_hosted_client_example()
    receiver = example.ExampleReviewReceiver(path=tmp_path / "reviews.jsonl")

    report = example.run_hosted_client_walkthrough(
        client, api_key="key-a", work_id="HOSTED-EXAMPLE-1", review_receiver=receiver,
    )
    row = next(r for r in report["rows"] if r["work_id"] == "HOSTED-EXAMPLE-1")
    assert row["status"] == "retry_resolved"
    assert row["retry_status"] == "success"
    assert row["observed_cost_usd"] == "0.07"
    assert row["observed_cost_complete"] is True
    assert row["handoff_ref"] is None  # resolved by retry -- no review needed


def test_hosted_client_example_review_path_records_handoff_and_outcome(client, tmp_path):
    example = _load_hosted_client_example()
    receiver = example.ExampleReviewReceiver(path=tmp_path / "reviews.jsonl")

    # Force the review path directly via the decisions endpoint first,
    # then let the walkthrough's own idempotent replay pick it up.
    client.post(
        "/v1/decisions",
        json={
            "work_id": "HOSTED-EXAMPLE-2", "checkpoint_attempt_id": "att-1",
            "failure_type": "low_confidence", "confidence": 0.95,  # outside retry band
            "policy_config": _POLICY_CONFIG,
        },
        headers=_auth("key-a"),
    )
    report = example.run_hosted_client_walkthrough(
        client, api_key="key-a", work_id="HOSTED-EXAMPLE-2", review_receiver=receiver,
    )
    row = next(r for r in report["rows"] if r["work_id"] == "HOSTED-EXAMPLE-2")
    assert row["established_outcome"] == "corrected"
    assert row["handoff_ref"] is not None
    assert row["observed_cost_complete"] is True
    assert receiver.path.exists()


def test_retry_attempt_rejects_contradictory_validation_passed_true_with_failed_status(client):
    """A failed (or ambiguous) attempt cannot become retry_resolved
    merely because the caller also claims validation_passed=true --
    this combination is contradictory and must be rejected explicitly,
    not silently accepted as resolved."""
    body = {
        "work_id": "WORK-CONTRADICTORY",
        "checkpoint_attempt_id": "att-c1",
        "failure_type": "low_confidence",
        "confidence": 0.6,
        "policy_config": _POLICY_CONFIG,
    }
    client.post("/v1/decisions", json=body, headers=_auth())
    resp = client.post(
        "/v1/decisions/WORK-CONTRADICTORY/retry-attempts",
        json={
            "attempt_id": "ret-c1", "status": "failed", "cost_usd": "0.06",
            "validation_passed": True, "validator_version": "ap.validator/v1",
        },
        headers=_auth(),
    )
    assert resp.status_code == 422

    # The contradictory input must never have been recorded at all.
    after = client.get("/v1/decisions/WORK-CONTRADICTORY", headers=_auth())
    assert after.json()["status"] == "retry_in_progress"


def test_retry_attempt_rejects_contradictory_validation_passed_true_with_ambiguous_status(
    client,
):
    body = {
        "work_id": "WORK-CONTRADICTORY-2",
        "checkpoint_attempt_id": "att-c2",
        "failure_type": "low_confidence",
        "confidence": 0.6,
        "policy_config": _POLICY_CONFIG,
    }
    client.post("/v1/decisions", json=body, headers=_auth())
    resp = client.post(
        "/v1/decisions/WORK-CONTRADICTORY-2/retry-attempts",
        json={"attempt_id": "ret-c2", "status": "ambiguous", "validation_passed": True},
        headers=_auth(),
    )
    assert resp.status_code == 422


def test_late_retry_attempt_after_reap_returns_409_not_500_and_is_recorded(client):
    body = {
        "work_id": "WORK-LATE-HOSTED",
        "checkpoint_attempt_id": "att-late",
        "failure_type": "low_confidence",
        "confidence": 0.6,
        "policy_config": _POLICY_CONFIG,
        "lease_seconds": 0.01,
    }
    client.post("/v1/decisions", json=body, headers=_auth())

    import time

    time.sleep(0.05)
    reap = client.post("/v1/decisions/WORK-LATE-HOSTED/reap", headers=_auth())
    assert reap.json()["kind"] == "reaped"

    # The "dead" caller's real result now arrives -- a distinct
    # attempt_id for a work_id that already has one recorded (the
    # synthetic reaped one).
    late = client.post(
        "/v1/decisions/WORK-LATE-HOSTED/retry-attempts",
        json={
            "attempt_id": "ret-real-late", "status": "success", "cost_usd": "0.06",
            "validation_passed": True, "validator_version": "ap.validator/v1",
        },
        headers=_auth(),
    )
    assert late.status_code == 409  # defined response, never an unhandled 500

    report = client.get("/v1/report", headers=_auth())
    row = next(r for r in report.json()["rows"] if r["work_id"] == "WORK-LATE-HOSTED")
    assert row["late_result_status"] == "success"
    assert row["late_result_cost_usd"] == "0.06"
    # Never silently promoted -- the official record is unaffected.
    assert row["status"] == "awaiting_human_review"
    assert row["observed_cost_complete"] is False


def test_hosted_client_example_second_run_never_reinvokes_adapter_or_bills_twice(
    client, tmp_path
):
    """Run the hosted integration example twice with the same database
    and work_id while counting adapter calls -- the second run must
    not execute another retry or create another billable provider
    call; it must inspect the stored decision and return the existing
    result."""
    example = _load_hosted_client_example()
    receiver = example.ExampleReviewReceiver(path=tmp_path / "reviews.jsonl")

    calls: list[str] = []

    class CountingAdapter(example.LocalStandInAdapter):
        def retry(self, case):  # type: ignore[no-untyped-def]
            calls.append(case.work_id)
            return super().retry(case)

    adapter = CountingAdapter()
    work_id = "HOSTED-REPEAT-1"

    first_report = example.run_hosted_client_walkthrough(
        client, api_key="key-a", work_id=work_id, review_receiver=receiver, adapter=adapter,
    )
    assert len(calls) == 1
    first_row = next(r for r in first_report["rows"] if r["work_id"] == work_id)
    assert first_row["status"] == "retry_resolved"

    second_report = example.run_hosted_client_walkthrough(
        client, api_key="key-a", work_id=work_id, review_receiver=receiver, adapter=adapter,
    )
    assert len(calls) == 1  # never invoked a second time -- no second billable call
    second_row = next(r for r in second_report["rows"] if r["work_id"] == work_id)
    assert second_row == first_row  # same, unchanged, safely re-inspected record


def test_hosted_client_example_unknown_cost_never_invokes_adapter_routes_to_review(
    client, tmp_path
):
    """An adapter that cannot bound its own next-attempt cost must
    never be invoked -- the workflow routes to human review instead,
    exactly like the local SDK engine's own authorize_retry_cost gate."""
    example = _load_hosted_client_example()
    receiver = example.ExampleReviewReceiver(path=tmp_path / "reviews.jsonl")

    class NoEstimateAdapter:
        name = "no_estimate_adapter"

        def __init__(self) -> None:
            self.calls = 0

        def retry(self, case):  # type: ignore[no-untyped-def]
            self.calls += 1
            raise AssertionError("must never be invoked when cost cannot be authorized")

    adapter = NoEstimateAdapter()
    work_id = "HOSTED-UNKNOWN-COST-1"
    report = example.run_hosted_client_walkthrough(
        client, api_key="key-a", work_id=work_id, review_receiver=receiver, adapter=adapter,
    )
    assert adapter.calls == 0
    row = next(r for r in report["rows"] if r["work_id"] == work_id)
    assert row["status"] == "resolved"
    assert row["handoff_ref"] is not None
    assert row["retry_status"] is None  # no retry_attempts row at all


def test_hosted_client_example_recovers_interrupted_prior_run_without_reinvoking(
    client, tmp_path
):
    """Simulates a prior run of this exact workflow crashing right
    after the decision was created but before it ever executed the
    retry -- the next run of the same workflow, for the same work_id,
    must recover via /reap and complete the review path, never
    invoking the adapter for a work_id it doesn't know the true state
    of."""
    example = _load_hosted_client_example()
    receiver = example.ExampleReviewReceiver(path=tmp_path / "reviews.jsonl")
    work_id = "HOSTED-RESTART-1"

    # The "interrupted prior run": only the decision gets created, with
    # a short lease, then nothing else ever happens (as if the process
    # died right there).
    client.post(
        "/v1/decisions",
        json={
            "work_id": work_id, "checkpoint_attempt_id": f"{work_id}-checkpoint",
            "failure_type": "low_confidence", "confidence": 0.6, "cost_so_far_usd": "0.10",
            "policy_config": _POLICY_CONFIG, "lease_seconds": 0.01,
        },
        headers=_auth("key-a"),
    )
    import time

    time.sleep(0.05)  # let the lease expire

    calls: list[str] = []

    class CountingAdapter(example.LocalStandInAdapter):
        def retry(self, case):  # type: ignore[no-untyped-def]
            calls.append(case.work_id)
            return super().retry(case)

    report = example.run_hosted_client_walkthrough(
        client, api_key="key-a", work_id=work_id, review_receiver=receiver,
        adapter=CountingAdapter(),
    )
    assert calls == []  # never invoked -- the prior run's true state was unknown
    row = next(r for r in report["rows"] if r["work_id"] == work_id)
    assert row["status"] == "resolved"
    assert row["retry_status"] == "ambiguous"
    assert row["established_outcome"] == "corrected"
    assert row["handoff_ref"] is not None
