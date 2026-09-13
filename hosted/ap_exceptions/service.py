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
import os
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from auth import RateLimiter, authenticate, rate_limiter_from_env
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from tenant_store import TenantStoreRegistry

from inferrail.ap import Action, PolicyConfig
from inferrail.ap.models import ExceptionCase
from inferrail.ap.policy import recommend
from inferrail.ap.report import build_live_report
from inferrail.ap.store import RecoveryStore

REQUEST_TIMEOUT_SECONDS = float(os.environ.get("AP_REQUEST_TIMEOUT_SECONDS", "10"))
MAX_REPORT_ROWS = int(os.environ.get("AP_MAX_REPORT_ROWS", "500"))


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


class RetryAttemptRequest(BaseModel):
    attempt_id: str = Field(min_length=1, max_length=200)
    status: str
    cost_usd: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    provider: str = "unknown"
    validation_passed: bool | None = None
    validator_version: str | None = None


class HandoffRequest(BaseModel):
    handoff_ref: str = Field(min_length=1, max_length=500)


class OutcomeRequest(BaseModel):
    outcome: str
    timestamp: datetime | None = None
    source: str = "unknown"
    correction_delta_usd: str | None = None
    review_cost_usd: str | None = None


def _decision_response(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "work_id": row["work_id"],
        "decision_id": row["decision_id"],
        "checkpoint_attempt_id": row["checkpoint_attempt_id"],
        "failure_type": row["failure_type"],
        "recommended_action": row["recommended_action"],
        "reason": row["reason"],
        "policy_version": row["policy_version"],
        "status": row["status"],
    }


def create_app(data_dir: Path) -> FastAPI:
    app = FastAPI(title="Inferrail AP Exceptions", version="1")
    registry = TenantStoreRegistry(data_dir)
    limiter: RateLimiter = rate_limiter_from_env()

    @app.middleware("http")
    async def timeout_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        try:
            return await asyncio.wait_for(call_next(request), timeout=REQUEST_TIMEOUT_SECONDS)
        except TimeoutError:
            return JSONResponse(
                status_code=504,
                content={"detail": f"request exceeded {REQUEST_TIMEOUT_SECONDS:.0f}s timeout"},
            )

    def _tenant_store(tenant_id: str = Depends(authenticate)) -> tuple[str, RecoveryStore]:
        limiter.check(tenant_id)
        return tenant_id, registry.get(tenant_id)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/decisions")
    async def create_decision(
        body: DecisionRequest, ctx: tuple[str, RecoveryStore] = Depends(_tenant_store)
    ) -> dict[str, Any]:
        _tenant_id, store = ctx
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
        initial_status = (
            "retry_in_progress" if recommendation.action == Action.RETRY
            else "awaiting_human_review"
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
        )
        response = _decision_response(row)
        response["idempotent_replay"] = not created
        return response

    @app.get("/v1/decisions/{work_id}")
    async def get_decision(
        work_id: str, ctx: tuple[str, RecoveryStore] = Depends(_tenant_store)
    ) -> dict[str, Any]:
        _tenant_id, store = ctx
        row = store.get_decision(work_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"no decision for work_id={work_id!r}")
        return _decision_response(row)

    @app.post("/v1/decisions/{work_id}/retry-attempts")
    async def record_retry_attempt(
        work_id: str,
        body: RetryAttemptRequest,
        ctx: tuple[str, RecoveryStore] = Depends(_tenant_store),
    ) -> dict[str, Any]:
        _tenant_id, store = ctx
        if store.get_decision(work_id) is None:
            raise HTTPException(status_code=404, detail=f"no decision for work_id={work_id!r}")
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
        )
        return {**row, "newly_recorded": created}

    @app.post("/v1/decisions/{work_id}/handoff")
    async def record_handoff(
        work_id: str, body: HandoffRequest, ctx: tuple[str, RecoveryStore] = Depends(_tenant_store)
    ) -> dict[str, Any]:
        _tenant_id, store = ctx
        if store.get_decision(work_id) is None:
            raise HTTPException(status_code=404, detail=f"no decision for work_id={work_id!r}")
        row, created = store.record_handoff(work_id=work_id, handoff_ref=body.handoff_ref)
        return {**row, "newly_recorded": created}

    @app.post("/v1/decisions/{work_id}/outcome")
    async def record_outcome(
        work_id: str, body: OutcomeRequest, ctx: tuple[str, RecoveryStore] = Depends(_tenant_store)
    ) -> dict[str, Any]:
        _tenant_id, store = ctx
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
        return row

    @app.delete("/v1/decisions/{work_id}")
    async def delete_decision(
        work_id: str, ctx: tuple[str, RecoveryStore] = Depends(_tenant_store)
    ) -> dict[str, Any]:
        """Retention/deletion: irreversibly removes every record for one
        work_id (decision, retry attempt, handoff, outcome history)."""
        _tenant_id, store = ctx
        deleted = store.delete_work_id(work_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"no decision for work_id={work_id!r}")
        return {"work_id": work_id, "deleted": True}

    @app.get("/v1/report")
    async def report(ctx: tuple[str, RecoveryStore] = Depends(_tenant_store)) -> dict[str, Any]:
        _tenant_id, store = ctx
        full_report = build_live_report(store)
        rows = full_report.rows[:MAX_REPORT_ROWS]
        truncated = len(full_report.rows) > MAX_REPORT_ROWS
        return {
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
                    "observed_cost_usd": (
                        str(r.observed_cost_usd) if r.observed_cost_usd is not None else None
                    ),
                    "observed_cost_complete": r.observed_cost_complete,
                }
                for r in rows
            ],
            "truncated": truncated,
            "max_rows": MAX_REPORT_ROWS,
        }

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
