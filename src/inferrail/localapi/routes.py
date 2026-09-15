"""The local control API — `/v1/local/*`, mounted only when `inferrail
serve --app-mode` is used. See docs/adr/0016-local-control-api.md for
why this exists and how it differs from the *hosted* control plane
docs/adr/0004 anticipates (it doesn't — this is single-process,
localhost-only, data-plane-local, same as everything else in this
repository).

Every route here requires the per-install bearer token
(`localapi.token.ensure_local_api_token`) — unlike
`INFERRAIL_GATEWAY_TOKEN` (optional, guards inference cost), this is
mandatory, because these routes read back a caller's own local economic
history (receipts, work, budgets), not just proxy inference.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import AsyncIterator, Iterator
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from inferrail.ap.models import DecisionStatus
from inferrail.ap.report import build_live_report
from inferrail.ap.store import RecoveryStore
from inferrail.budgets.enforcement import spent_so_far_usd
from inferrail.budgets.schema import Budget, new_budget_id
from inferrail.budgets.store import BudgetStore
from inferrail.cli.pricing import catalog_freshness
from inferrail.errors import LocalApiAuthenticationError
from inferrail.localapi.schemas import BudgetCreate, BudgetSpend, OutcomeRequest, ReceiptsPage
from inferrail.receipts.sqlite_store import ReceiptsStore
from inferrail.work.builder import aggregate_work_summaries, build_work_summary, load_outcomes
from inferrail.work.schema import WorkSummary

router = APIRouter(prefix="/v1/local")

#: Module-level so tests can shrink it — real usage never needs a
#: sub-second tail latency, but a test polling a single pre-seeded
#: receipt shouldn't have to wait a full second for it to appear.
STREAM_POLL_INTERVAL_SECONDS = 1.0


async def _require_local_api_token(
    request: Request,
    authorization: str | None = Header(default=None),
    token: str | None = Query(
        default=None,
        description=(
            "Alternative to the Authorization header, accepted only because browser "
            "EventSource cannot set custom headers -- see "
            "docs/adr/0017-dashboard-in-app-directory.md. The dashboard is the only "
            "intended user of this; prefer the header for anything else (curl, scripts)."
        ),
    ),
) -> None:
    expected: str = request.app.state.local_api_token
    provided = (authorization or "").removeprefix("Bearer ") or (token or "")
    if not secrets.compare_digest(provided, expected):
        raise LocalApiAuthenticationError(
            "missing or invalid local API credentials: set the 'Authorization: "
            "Bearer <token>' header (or a '?token=' query parameter) to match the "
            "per-install token printed by 'inferrail serve --app-mode' (also readable "
            "from the token file under the app-data directory)"
        )


def _receipts_store(request: Request) -> ReceiptsStore:
    store: ReceiptsStore = request.app.state.local_receipts_store
    return store


def _budget_store(request: Request) -> BudgetStore:
    store: BudgetStore = request.app.state.local_budget_store
    return store


@router.get(
    "/receipts",
    dependencies=[Depends(_require_local_api_token)],
    response_model=ReceiptsPage,
    summary="Paginated local receipts query",
)
async def list_receipts(
    request: Request,
    work_id: str | None = None,
    project: str | None = None,
    model: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, gt=0, le=1000),
    offset: int = Query(default=0, ge=0),
) -> ReceiptsPage:
    store = _receipts_store(request)
    receipts = store.query(
        work_id=work_id, project=project, model=model, status=status, limit=limit, offset=offset
    )
    total = store.count(work_id=work_id, project=project, model=model, status=status)
    return ReceiptsPage(receipts=receipts, total=total, limit=limit, offset=offset)


@router.get(
    "/work",
    dependencies=[Depends(_require_local_api_token)],
    response_model=list[WorkSummary],
    summary="Every work rollup with any receipt or outcome evidence",
)
async def list_work(request: Request) -> list[WorkSummary]:
    receipts, _skipped = _receipts_store(request).read_all()
    outcomes, _skipped2 = load_outcomes(request.app.state.local_outcomes_path)
    return aggregate_work_summaries(receipts, outcomes)


@router.get(
    "/work/{work_id}",
    dependencies=[Depends(_require_local_api_token)],
    response_model=WorkSummary,
    summary="One work_id's derived rollup",
)
async def get_work(request: Request, work_id: str) -> WorkSummary:
    receipts, _skipped = _receipts_store(request).read_all()
    outcomes, _skipped2 = load_outcomes(request.app.state.local_outcomes_path)
    matching_receipts = [r for r in receipts if r.attributes.get("work_id") == work_id]
    matching_outcomes = [o for o in outcomes if o.work_id == work_id]
    summary = build_work_summary(work_id, matching_receipts, matching_outcomes)
    if summary is None:
        raise HTTPException(404, f"no receipt or outcome evidence found for work_id '{work_id}'")
    return summary


@router.get(
    "/budgets",
    dependencies=[Depends(_require_local_api_token)],
    response_model=list[Budget],
    summary="List every configured budget",
)
async def list_budgets(request: Request) -> list[Budget]:
    return _budget_store(request).list()


@router.get(
    "/budgets/spend",
    dependencies=[Depends(_require_local_api_token)],
    response_model=list[BudgetSpend],
    summary="Current spend for every configured budget, for the dashboard's burn bar",
)
async def list_budget_spend(request: Request) -> list[BudgetSpend]:
    receipts = _receipts_store(request)
    results = []
    for budget in _budget_store(request).list():
        spent = spent_so_far_usd(receipts, budget)
        results.append(
            BudgetSpend(
                budget_id=budget.budget_id,
                limit_usd=budget.limit_usd,
                spent_usd=spent.spent_usd,
                has_unpriced_usage=spent.has_unpriced_usage,
            )
        )
    return results


@router.post(
    "/budgets",
    dependencies=[Depends(_require_local_api_token)],
    response_model=Budget,
    status_code=201,
    summary="Create or update a budget (upsert, keyed on scope/scope_value/window)",
)
async def create_budget(request: Request, payload: BudgetCreate) -> Budget:
    budget_id = new_budget_id(payload.scope, payload.scope_value, payload.window)
    try:
        budget = Budget(
            budget_id=budget_id,
            scope=payload.scope,
            scope_value=payload.scope_value,
            window=payload.window,
            mode=payload.mode,
            limit_usd=payload.limit_usd,
        )
    except ValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    _budget_store(request).set(budget)
    return budget


@router.delete(
    "/budgets/{budget_id}",
    dependencies=[Depends(_require_local_api_token)],
    status_code=204,
    summary="Remove a budget by id",
)
async def delete_budget(request: Request, budget_id: str) -> None:
    if not _budget_store(request).remove(budget_id):
        raise HTTPException(404, f"no budget '{budget_id}'")


@router.get(
    "/stream",
    dependencies=[Depends(_require_local_api_token)],
    summary="SSE tail of newly-emitted receipts",
)
async def stream_receipts(request: Request) -> StreamingResponse:
    return StreamingResponse(
        _tail_new_receipts(_receipts_store(request), request), media_type="text/event-stream"
    )


async def _tail_new_receipts(store: ReceiptsStore, request: Request) -> AsyncIterator[bytes]:
    """Polls `ReceiptsStore.query(since=...)` rather than anything
    push-based — SQLite has no native pub-sub, and a short poll interval
    over an indexed `ts` column is simple, correct, and cheap enough for
    a single local install. Stops as soon as the client disconnects
    (checked before every poll, never mid-write), same discipline
    `gateway/execution.py`'s streaming already follows."""
    since = time.time()
    while not await request.is_disconnected():
        for receipt in store.query(since=since):
            since = max(since, receipt.timestamp.timestamp())
            yield f"data: {receipt.model_dump_json()}\n\n".encode()
        await asyncio.sleep(STREAM_POLL_INTERVAL_SECONDS)


def _ap_store(request: Request) -> RecoveryStore:
    store: RecoveryStore = request.app.state.ap_recovery_store
    return store


@router.get(
    "/ap/pending",
    dependencies=[Depends(_require_local_api_token)],
    summary="AP work_ids currently awaiting human review",
)
async def list_ap_pending(request: Request) -> dict[str, Any]:
    """The dashboard's Recover screen (docs/PRODUCT.md's "Dashboard"
    section) — every work_id whose decision is currently
    `awaiting_human_review`, built from `ap.report.build_live_report`
    (the same auditable report `inferrail ap report`/the hosted API's
    `GET /v1/report` produce), filtered to one status. Returns the same
    dict shape as those, not a new response model — keeps exactly one
    place that decides what a report row looks like."""
    report = build_live_report(_ap_store(request))
    rows = [
        row
        for row in report.to_dict()["rows"]
        if row["status"] == DecisionStatus.AWAITING_HUMAN_REVIEW.value
    ]
    return {"rows": rows}


@router.post(
    "/ap/{work_id}/outcome",
    dependencies=[Depends(_require_local_api_token)],
    summary="Record a human-review outcome for one work_id",
)
async def record_ap_outcome(
    request: Request, work_id: str, body: OutcomeRequest
) -> dict[str, Any]:
    """Same store-level call `inferrail ap outcome`/the hosted API's
    `POST /v1/decisions/{work_id}/outcome` make — closing the loop the
    CLI can't with a click instead of composing a command
    (`MISSION.md`'s v0.4.0 "Recover" screen)."""
    try:
        return _ap_store(request).record_outcome(
            work_id=work_id,
            outcome=body.outcome,
            timestamp=time.time(),
            source=body.source,
            correction_delta_usd=body.correction_delta_usd,
            review_cost_usd=body.review_cost_usd,
        )
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc


@router.get(
    "/pricing/freshness",
    dependencies=[Depends(_require_local_api_token)],
    summary="Built-in pricing catalog age, for the dashboard's Settings screen",
)
async def pricing_freshness(_request: Request) -> dict[str, Any]:
    """Same computation `inferrail pricing update`/`inferrail doctor`
    already share (`cli.pricing.catalog_freshness`) — never a network
    fetch (see that module's own docstring for why: there is no built-in
    price this codebase could refresh live and still keep verified)."""
    return {
        "catalogs": [
            {
                "name": name,
                "model_count": model_count,
                "oldest_verified_date": oldest.isoformat() if oldest is not None else None,
                "age_days": age_days,
                "is_stale": is_stale,
            }
            for name, model_count, oldest, age_days, is_stale in catalog_freshness()
        ]
    }


@router.get(
    "/receipts/export",
    dependencies=[Depends(_require_local_api_token)],
    summary="Download every stored receipt as JSONL",
)
async def export_receipts(request: Request) -> StreamingResponse:
    """Streams the exact same JSONL shape `inferrail receipts export`
    writes to a file, generated directly from `ReceiptsStore.read_all()`
    rather than routing through that CLI command's file-to-file
    `export_jsonl` -- avoids writing a server-side temp file just to
    immediately re-read it for an HTTP response body."""
    store = _receipts_store(request)

    def _lines() -> Iterator[bytes]:
        receipts, _skipped = store.read_all()
        for receipt in receipts:
            yield (receipt.model_dump_json() + "\n").encode()

    return StreamingResponse(
        _lines(),
        media_type="application/x-ndjson",
        headers={"Content-Disposition": 'attachment; filename="inferrail-receipts.jsonl"'},
    )
