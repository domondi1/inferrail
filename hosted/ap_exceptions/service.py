"""Inferrail AP Exceptions -- hosted decision, persistence, and reporting
service.

Lives outside `src/inferrail`, exactly like `hosted/work_economics` and
`hosted/a2a_economic_authority` (see `docs/adr/0004`,
`docs/adr/0010`) -- the OSS gateway and the `inferrail.ap` SDK have zero
dependency on this service; both work standalone with local storage.

**What this service does, and does not, do.** It runs the same
`inferrail.ap.policy.recommend` policy evaluation and
`inferrail.ap.store.RecoveryStore` persistence the local SDK uses, over
the network, per authenticated tenant. **It never executes a retry
itself** -- retry execution always happens in the caller's own process
(via a `RetryAdapter`, see `inferrail.ap.adapters`), because that is
where invoice content and provider credentials belong (see
docs/capabilities/ap-invoice-exception-recovery.md, "Data boundary").
This service only ever receives identifiers, policy-config numbers,
confidence/validation results, cost figures, and status enums -- never
invoice field values or provider credentials.

Every request is authenticated (`Authorization: Bearer <api-key>`,
`auth.py`), isolated per tenant/API key (`tenant_store.py` -- one SQLite
file per tenant, not a shared table with a row filter), rate-limited
(`auth.RateLimiter`), and subject to a per-request timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import time
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from auth import RateLimiter, authenticate, rate_limiter_from_env
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sandbox import SANDBOX_NOTICE, SandboxRegistry, iso, sandbox_registry_from_env
from tenant_store import TenantStoreRegistry

from inferrail.ap import Action, PolicyConfig
from inferrail.ap.models import AttemptStatus, DecisionStatus, ExceptionCase
from inferrail.ap.policy import recommend
from inferrail.ap.report import build_live_report
from inferrail.ap.store import AmbiguousRetryError, RecoveryStore

DEFAULT_LEASE_SECONDS = 300.0

REQUEST_TIMEOUT_SECONDS = float(os.environ.get("AP_REQUEST_TIMEOUT_SECONDS", "10"))
MAX_REPORT_ROWS = int(os.environ.get("AP_MAX_REPORT_ROWS", "500"))
SANDBOX_MAX_ROWS_PER_TENANT = int(os.environ.get("AP_SANDBOX_MAX_ROWS_PER_TENANT", "20"))
SANDBOX_PURGE_INTERVAL_SECONDS = float(os.environ.get("AP_SANDBOX_PURGE_INTERVAL_SECONDS", "60"))
MAX_REQUEST_BODY_BYTES = int(os.environ.get("AP_MAX_REQUEST_BODY_BYTES", str(64 * 1024)))


class PolicyConfigPayload(BaseModel):
    eligible_failure_types: list[str]
    retry_floor: float
    human_review_threshold: float
    max_retry_cost_usd: str
    decision_deadline_seconds: float
    name: str = "candidate_policy"

    def to_policy_config(self) -> PolicyConfig:
        try:
            max_cost = Decimal(self.max_retry_cost_usd)
        except InvalidOperation as exc:
            raise HTTPException(
                status_code=422, detail=f"max_retry_cost_usd is not a valid decimal: {exc}"
            ) from exc
        try:
            return PolicyConfig(
                eligible_failure_types=frozenset(self.eligible_failure_types),
                retry_floor=self.retry_floor,
                human_review_threshold=self.human_review_threshold,
                max_retry_cost_usd=max_cost,
                decision_deadline_seconds=self.decision_deadline_seconds,
                name=self.name,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


class DecisionRequest(BaseModel):
    work_id: str = Field(min_length=1, max_length=200)
    checkpoint_attempt_id: str = Field(min_length=1, max_length=200)
    failure_type: str = Field(min_length=1, max_length=100)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    validation_check_passed: bool | None = None
    cost_so_far_usd: str | None = None
    opened_at: datetime | None = None
    source: str = "unknown"
    policy_config: PolicyConfigPayload
    lease_seconds: float = DEFAULT_LEASE_SECONDS
    """How long a `retry_in_progress` decision's lease lasts before it
    becomes eligible for `/v1/reap-stale` -- the durable crash-recovery
    path for a caller whose own process dies after receiving this
    decision but before calling back `/retry-attempts` (see
    `inferrail.ap.store.RecoveryStore.find_stale_retry_leases`)."""


class RetryAttemptRequest(BaseModel):
    attempt_id: str = Field(min_length=1, max_length=200)
    status: str
    cost_usd: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    provider: str = "unknown"
    validation_passed: bool | None = None
    validator_version: str | None = None
    pre_flight_estimate_usd: str | None = None
    """The caller's own `inferrail.ap.policy.authorize_retry_cost`
    pre-flight bound for this attempt, if it made one -- recorded so the
    hosted report can show an honest overrun (see
    `inferrail.ap.report.LiveReportRow.retry_cost_overrun_usd`) the same
    way the local SDK engine does. The hosted service never computes
    this itself: it never invokes the caller's retry adapter (see this
    module's own docstring), so authorization must happen in the
    caller's own process before this endpoint is called."""


class HandoffRequest(BaseModel):
    handoff_ref: str = Field(min_length=1, max_length=500)


class OutcomeRequest(BaseModel):
    outcome: str
    timestamp: datetime | None = None
    source: str = "unknown"
    correction_delta_usd: str | None = None
    review_cost_usd: str | None = None


def _decision_response(row: dict[str, Any], *, is_sandbox: bool) -> dict[str, Any]:
    return _stamp(
        {
            "work_id": row["work_id"],
            "decision_id": row["decision_id"],
            "checkpoint_attempt_id": row["checkpoint_attempt_id"],
            "failure_type": row["failure_type"],
            "recommended_action": row["recommended_action"],
            "reason": row["reason"],
            "policy_version": row["policy_version"],
            "status": row["status"],
        },
        is_sandbox=is_sandbox,
    )


def _stamp(payload: dict[str, Any], *, is_sandbox: bool) -> dict[str, Any]:
    """Every response is explicitly labeled `sandbox: true/false` --
    never left ambiguous -- and a sandbox response additionally carries
    `sandbox_notice` (see `sandbox.SANDBOX_NOTICE`), satisfying the
    product promise that a sandbox tenant is "clearly labeled as such in
    every response" (`MISSION.md`, v0.2.1)."""
    payload["sandbox"] = is_sandbox
    payload["sandbox_notice"] = SANDBOX_NOTICE if is_sandbox else None
    return payload


def _client_ip(request: Request) -> str:
    """`request.client.host` reflects the real client address here, not
    the proxy's: `uvicorn.run(..., proxy_headers=True,
    forwarded_allow_ips="*")` (see this module's `__main__` block)
    rewrites it from `X-Forwarded-For`/`Forwarded` at the ASGI-server
    level for every deployment behind a reverse proxy (e.g. Render)."""
    return request.client.host if request.client is not None else "unknown"


def create_app(data_dir: Path) -> FastAPI:
    app = FastAPI(title="Inferrail AP Exceptions", version="1")
    registry = TenantStoreRegistry(data_dir)
    limiter: RateLimiter = rate_limiter_from_env()
    sandbox_registry: SandboxRegistry = sandbox_registry_from_env(on_purge=registry.purge_tenant)

    @app.middleware("http")
    async def timeout_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        try:
            return await asyncio.wait_for(call_next(request), timeout=REQUEST_TIMEOUT_SECONDS)
        except TimeoutError:
            return JSONResponse(
                status_code=504,
                content={"detail": f"request exceeded {REQUEST_TIMEOUT_SECONDS:.0f}s timeout"},
            )

    @app.middleware("http")
    async def request_size_limit_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        """Abuse guard: rejects an oversized body before it is ever
        parsed, by trusting a declared `Content-Length` (a request that
        lies about a small `Content-Length` and then streams more bytes
        is cut off downstream by FastAPI/Starlette's own body-size
        handling, not by this check) -- applies to every route, not just
        the sandbox ones, since an unauthenticated `POST /v1/sandbox` is
        exactly the route an attacker would target with an oversized
        body."""
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = 0
            if declared_size > MAX_REQUEST_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={
                        "detail": (
                            f"request body of {declared_size} bytes exceeds the "
                            f"{MAX_REQUEST_BODY_BYTES}-byte limit"
                        )
                    },
                )
        return await call_next(request)

    @app.on_event("startup")
    async def _start_sandbox_purge_loop() -> None:
        async def _loop() -> None:
            while True:
                await asyncio.sleep(SANDBOX_PURGE_INTERVAL_SECONDS)
                sandbox_registry.purge_expired()

        app.state.sandbox_purge_task = asyncio.create_task(_loop())

    @app.on_event("shutdown")
    async def _stop_sandbox_purge_loop() -> None:
        task = getattr(app.state, "sandbox_purge_task", None)
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    def _authenticate(request: Request) -> tuple[str, bool]:
        """Resolves the caller's tenant id from `Authorization: Bearer
        <api-key>`, checking self-serve sandbox keys before falling back
        to operator-provisioned `AP_API_KEYS` -- a sandbox key's prefix
        (`sbx_`) never collides with an operator key an operator chose
        to provision (`sandbox.lookup` returns `None` immediately for
        any key not carrying that prefix, so this never accidentally
        treats an operator key as a sandbox one or vice versa). Returns
        `(tenant_id, is_sandbox)`; raises 401 exactly like plain
        `auth.authenticate` for anything invalid."""
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            api_key = header[len("bearer ") :].strip()
            sandbox_tenant = sandbox_registry.lookup(api_key)
            if sandbox_tenant is not None:
                if sandbox_tenant.is_expired():
                    raise HTTPException(
                        status_code=401,
                        detail=(
                            f"sandbox key expired at {iso(sandbox_tenant.expires_at)} -- "
                            "sandbox keys are short-lived; issue a new one with "
                            "POST /v1/sandbox"
                        ),
                    )
                return sandbox_tenant.tenant_id, True
        return authenticate(request), False

    def _tenant_store(
        ctx: tuple[str, bool] = Depends(_authenticate),
    ) -> tuple[str, bool, RecoveryStore]:
        tenant_id, is_sandbox = ctx
        limiter.check(tenant_id)
        return tenant_id, is_sandbox, registry.get(tenant_id)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/sandbox")
    async def issue_sandbox(request: Request) -> dict[str, Any]:
        """No authentication required -- this is the self-serve entry
        point (`MISSION.md`, v0.2.1, End-state 2). Returns a short-lived
        `api_key`/`tenant_id` pair a visitor can use immediately against
        every other route exactly like an operator-provisioned key,
        scoped to its own isolated tenant store and capped at
        `SANDBOX_MAX_ROWS_PER_TENANT` decisions."""
        api_key, tenant = sandbox_registry.issue(client_ip=_client_ip(request))
        return _stamp(
            {
                "api_key": api_key,
                "tenant_id": tenant.tenant_id,
                "expires_at": iso(tenant.expires_at),
                "ttl_seconds": sandbox_registry.ttl_seconds,
                "max_rows_per_tenant": SANDBOX_MAX_ROWS_PER_TENANT,
                "rate_limit": {
                    "max_requests": limiter.max_requests,
                    "window_seconds": limiter.window_seconds,
                },
            },
            is_sandbox=True,
        )

    @app.post("/v1/decisions")
    async def create_decision(
        body: DecisionRequest, ctx: tuple[str, bool, RecoveryStore] = Depends(_tenant_store)
    ) -> dict[str, Any]:
        tenant_id, is_sandbox, store = ctx
        if is_sandbox:
            existing_work_ids = store.all_work_ids()
            is_new_row = body.work_id not in existing_work_ids
            if is_new_row and len(existing_work_ids) >= SANDBOX_MAX_ROWS_PER_TENANT:
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"sandbox tenant row cap reached ({SANDBOX_MAX_ROWS_PER_TENANT} "
                        "decisions per sandbox key) -- issue a new sandbox key with "
                        "POST /v1/sandbox to continue"
                    ),
                )
        config = body.policy_config.to_policy_config()
        try:
            cost_so_far = Decimal(body.cost_so_far_usd) if body.cost_so_far_usd else None
        except InvalidOperation as exc:
            raise HTTPException(status_code=422, detail=f"cost_so_far_usd invalid: {exc}") from exc

        case = ExceptionCase(
            work_id=body.work_id,
            checkpoint_attempt_id=body.checkpoint_attempt_id,
            failure_type=body.failure_type,
            confidence=body.confidence,
            validation_check_passed=body.validation_check_passed,
            cost_so_far_usd=cost_so_far,
            opened_at=body.opened_at,
            source=body.source,
        )
        recommendation = recommend(case, config)
        is_retry = recommendation.action == Action.RETRY
        initial_status = (
            DecisionStatus.RETRY_IN_PROGRESS.value if is_retry
            else DecisionStatus.AWAITING_HUMAN_REVIEW.value
        )
        row, created = store.create_decision(
            work_id=case.work_id,
            decision_id=f"dec_{case.work_id}",
            checkpoint_attempt_id=case.checkpoint_attempt_id,
            failure_type=case.failure_type,
            confidence=str(case.confidence) if case.confidence is not None else None,
            cost_so_far_usd=str(cost_so_far) if cost_so_far is not None else None,
            policy_name=recommendation.policy_name,
            policy_version=recommendation.policy_version,
            recommended_action=recommendation.action.value,
            reason=recommendation.reason,
            status=initial_status,
            worker_id=f"tenant:{tenant_id}" if is_retry else None,
            lease_expires_at=time.time() + body.lease_seconds if is_retry else None,
        )
        response = _decision_response(row, is_sandbox=is_sandbox)
        response["idempotent_replay"] = not created
        return response

    @app.get("/v1/decisions/{work_id}")
    async def get_decision(
        work_id: str, ctx: tuple[str, bool, RecoveryStore] = Depends(_tenant_store)
    ) -> dict[str, Any]:
        _tenant_id, is_sandbox, store = ctx
        row = store.get_decision(work_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"no decision for work_id={work_id!r}")
        return _decision_response(row, is_sandbox=is_sandbox)

    @app.post("/v1/decisions/{work_id}/retry-attempts")
    async def record_retry_attempt(
        work_id: str,
        body: RetryAttemptRequest,
        ctx: tuple[str, bool, RecoveryStore] = Depends(_tenant_store),
    ) -> dict[str, Any]:
        """Records the result of a retry the caller already executed
        locally, and -- exactly like the local SDK's `RecoveryEngine.
        _execute_retry` -- transitions the decision's own status off
        `retry_in_progress` accordingly: `retry_resolved` only when the
        attempt's own `status` is exactly `success` *and*
        `validation_passed` is `true`; `awaiting_human_review` otherwise
        (a failed/partial/ambiguous attempt, or one whose validation
        result isn't `true`). Applying this consistently here (not just
        in the local engine) is what lets `/v1/report`'s
        `observed_cost_complete` ever become `true` on the hosted path
        -- a decision left at `retry_in_progress` forever is never
        treated as cost-complete (see `report._observed_cost`).

        **Rejects a contradictory input explicitly (422)**: a caller
        claiming `validation_passed=true` for an attempt whose own
        `status` is not `success` (e.g. `failed` or `ambiguous`) is
        nonsensical -- an attempt that failed or was interrupted cannot
        also have "passed validation." The local SDK's own bundled
        validator structurally cannot produce this combination (it
        checks the attempt's status itself), but this endpoint accepts
        raw, independently-supplied fields from an arbitrary caller and
        must not let a contradictory pair silently become
        `retry_resolved`.

        **A late result -- one that arrives after this work_id's lease
        was already reaped (a *different*, real attempt_id already
        recorded) -- is durably recorded through a defined `409`
        response, never an unhandled `500`**: see
        `RecoveryStore.record_late_retry_result` and
        `report.LiveReportRow.late_result_status`.
        """
        _tenant_id, is_sandbox, store = ctx
        if store.get_decision(work_id) is None:
            raise HTTPException(status_code=404, detail=f"no decision for work_id={work_id!r}")
        if body.validation_passed is True and body.status != AttemptStatus.SUCCESS.value:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"contradictory input: validation_passed=true is not valid for "
                    f"status={body.status!r} -- only a status={AttemptStatus.SUCCESS.value!r} "
                    "attempt can have passed validation"
                ),
            )
        try:
            row, created = store.record_retry_attempt(
                work_id=work_id,
                attempt_id=body.attempt_id,
                status=body.status,
                cost_usd=body.cost_usd,
                confidence=str(body.confidence) if body.confidence is not None else None,
                provider=body.provider,
                validation_passed=(
                    str(body.validation_passed) if body.validation_passed is not None else None
                ),
                validator_version=body.validator_version,
                pre_flight_estimate_usd=body.pre_flight_estimate_usd,
            )
        except AmbiguousRetryError:
            # This work_id's lease was already reaped (or a real
            # attempt already exists for another reason) -- the real
            # result is never lost, but the decision's authoritative
            # status is never silently flipped back, exactly like the
            # local engine's own AmbiguousRetryError handling in
            # `RecoveryEngine._execute_retry`.
            store.record_late_retry_result(
                work_id=work_id,
                attempt_id=body.attempt_id,
                status=body.status,
                cost_usd=body.cost_usd,
                provider=body.provider,
                detail="arrived after this work_id's lease was already reaped",
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    f"work_id={work_id!r} already has a recorded retry attempt -- this late "
                    "result was durably recorded for audit (see GET /v1/report's "
                    "late_result_status/late_result_cost_usd) but does not change the "
                    "decision's own status"
                ),
            ) from None
        if created:
            resolved = (
                body.status == AttemptStatus.SUCCESS.value and body.validation_passed is True
            )
            store.set_decision_status(
                work_id,
                DecisionStatus.RETRY_RESOLVED.value
                if resolved
                else DecisionStatus.AWAITING_HUMAN_REVIEW.value,
            )
            store.clear_retry_lease(work_id)
        return _stamp({**row, "newly_recorded": created}, is_sandbox=is_sandbox)

    @app.post("/v1/decisions/{work_id}/handoff")
    async def record_handoff(
        work_id: str,
        body: HandoffRequest,
        ctx: tuple[str, bool, RecoveryStore] = Depends(_tenant_store),
    ) -> dict[str, Any]:
        _tenant_id, is_sandbox, store = ctx
        if store.get_decision(work_id) is None:
            raise HTTPException(status_code=404, detail=f"no decision for work_id={work_id!r}")
        row, created = store.record_handoff(work_id=work_id, handoff_ref=body.handoff_ref)
        return _stamp({**row, "newly_recorded": created}, is_sandbox=is_sandbox)

    @app.post("/v1/decisions/{work_id}/outcome")
    async def record_outcome(
        work_id: str,
        body: OutcomeRequest,
        ctx: tuple[str, bool, RecoveryStore] = Depends(_tenant_store),
    ) -> dict[str, Any]:
        _tenant_id, is_sandbox, store = ctx
        timestamp = (body.timestamp or datetime.now()).timestamp()
        try:
            row = store.record_outcome(
                work_id=work_id,
                outcome=body.outcome,
                timestamp=timestamp,
                source=body.source,
                correction_delta_usd=body.correction_delta_usd,
                review_cost_usd=body.review_cost_usd,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return _stamp(row, is_sandbox=is_sandbox)

    @app.post("/v1/decisions/{work_id}/reap")
    async def reap_one(
        work_id: str, ctx: tuple[str, bool, RecoveryStore] = Depends(_tenant_store)
    ) -> dict[str, Any]:
        """Operator recovery for one work_id whose retry lease has
        expired -- the hosted-service analog of `inferrail ap reap` /
        `RecoveryEngine.reap_stale_retries` for a caller whose own
        process died after receiving a `retry_in_progress` decision but
        before calling back `/retry-attempts`. Tenant-scoped like every
        other route -- a tenant can only reap its own leases. Idempotent:
        returns `reaped: false` on a repeat call once the lease is no
        longer stale. `kind` distinguishes the two recovery cases (see
        `RecoveryStore.reap_stale_retry_lease`): `"reaped"` (no real
        attempt was ever recorded -- a synthetic ambiguous one is
        inserted) or `"reconciled"` (a real attempt WAS already
        recorded before the crash -- its own validation result decides
        the terminal status, never re-guessed)."""
        _tenant_id, is_sandbox, store = ctx
        if store.get_decision(work_id) is None:
            raise HTTPException(status_code=404, detail=f"no decision for work_id={work_id!r}")
        result = store.reap_stale_retry_lease(work_id)
        return _stamp(
            {
                "work_id": work_id,
                "reaped": result is not None,
                "kind": result["kind"] if result is not None else None,
            },
            is_sandbox=is_sandbox,
        )

    @app.post("/v1/reap-stale")
    async def reap_stale(
        ctx: tuple[str, bool, RecoveryStore] = Depends(_tenant_store),
    ) -> dict[str, Any]:
        """Sweeps every stale retry lease for the calling tenant only.
        Safe to call on a schedule (an operator's own cron/health job) --
        a repeat call reaps nothing new for a work_id already reaped or
        resolved by its real worker."""
        _tenant_id, is_sandbox, store = ctx
        reaped_work_ids = []
        for row in store.find_stale_retry_leases():
            if store.reap_stale_retry_lease(row["work_id"]) is not None:
                reaped_work_ids.append(row["work_id"])
        return _stamp(
            {"reaped_count": len(reaped_work_ids), "reaped_work_ids": reaped_work_ids},
            is_sandbox=is_sandbox,
        )

    @app.delete("/v1/decisions/{work_id}")
    async def delete_decision(
        work_id: str, ctx: tuple[str, bool, RecoveryStore] = Depends(_tenant_store)
    ) -> dict[str, Any]:
        """Retention/deletion: irreversibly removes every record for one
        work_id (decision, retry attempt, handoff, outcome history)."""
        _tenant_id, is_sandbox, store = ctx
        deleted = store.delete_work_id(work_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"no decision for work_id={work_id!r}")
        return _stamp({"work_id": work_id, "deleted": True}, is_sandbox=is_sandbox)

    @app.get("/v1/report")
    async def report(
        ctx: tuple[str, bool, RecoveryStore] = Depends(_tenant_store),
    ) -> dict[str, Any]:
        _tenant_id, is_sandbox, store = ctx
        full_report = build_live_report(store)
        rows = full_report.rows[:MAX_REPORT_ROWS]
        truncated = len(full_report.rows) > MAX_REPORT_ROWS
        payload = {
            "rows": [
                {
                    "work_id": r.work_id,
                    "decision_id": r.decision_id,
                    "recommended_action": r.recommended_action,
                    "status": r.status,
                    "retry_status": r.retry_status,
                    "validation_passed": r.validation_passed,
                    "handoff_ref": r.handoff_ref,
                    "established_outcome": r.established_outcome,
                    "review_cost_usd": (
                        str(r.review_cost_usd) if r.review_cost_usd is not None else None
                    ),
                    "retry_cost_overrun_usd": (
                        str(r.retry_cost_overrun_usd)
                        if r.retry_cost_overrun_usd is not None
                        else None
                    ),
                    "observed_cost_usd": (
                        str(r.observed_cost_usd) if r.observed_cost_usd is not None else None
                    ),
                    "observed_cost_complete": r.observed_cost_complete,
                    "late_result_status": r.late_result_status,
                    "late_result_cost_usd": (
                        str(r.late_result_cost_usd)
                        if r.late_result_cost_usd is not None
                        else None
                    ),
                }
                for r in rows
            ],
            "truncated": truncated,
            "max_rows": MAX_REPORT_ROWS,
        }
        return _stamp(payload, is_sandbox=is_sandbox)

    return app


if __name__ == "__main__":
    import sys

    import uvicorn

    # A bare `python3 service.py` (no CLI args) is the production/hosted
    # shape: bind 0.0.0.0 and read the platform-injected $PORT. Explicit
    # argv[1] (data_dir) / argv[2] (port) is the local/loopback test shape.
    explicit_args = len(sys.argv) > 1
    data_dir = (
        Path(sys.argv[1])
        if explicit_args
        else Path(os.environ.get("AP_DATA_DIR", "/tmp/inferrail_ap_exceptions"))
    )
    port = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get("PORT", 8422))
    host = "127.0.0.1" if explicit_args else "0.0.0.0"
    app = create_app(data_dir)
    print(f"Inferrail AP Exceptions listening on http://{host}:{port} (data_dir={data_dir})")
    uvicorn.run(
        app, host=host, port=port, log_level="warning", proxy_headers=True, forwarded_allow_ips="*"
    )
