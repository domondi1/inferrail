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
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import ValidationError

from inferrail.budgets.schema import Budget, new_budget_id
from inferrail.budgets.store import BudgetStore
from inferrail.errors import LocalApiAuthenticationError
from inferrail.localapi.schemas import BudgetCreate, ReceiptsPage
from inferrail.receipts.sqlite_store import ReceiptsStore
from inferrail.work.builder import aggregate_work_summaries, build_work_summary, load_outcomes
from inferrail.work.schema import WorkSummary

router = APIRouter(prefix="/v1/local")

#: Module-level so tests can shrink it — real usage never needs a
#: sub-second tail latency, but a test polling a single pre-seeded
#: receipt shouldn't have to wait a full second for it to appear.
STREAM_POLL_INTERVAL_SECONDS = 1.0


async def _require_local_api_token(
    request: Request, authorization: str | None = Header(default=None)
) -> None:
    expected: str = request.app.state.local_api_token
    provided = (authorization or "").removeprefix("Bearer ")
    if not secrets.compare_digest(provided, expected):
        raise LocalApiAuthenticationError(
            "missing or invalid local API credentials: set the 'Authorization: "
            "Bearer <token>' header to match the per-install token printed by "
            "'inferrail serve --app-mode' (also readable from the token file "
            "under the app-data directory)"
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
    limit: int = Query(default=50, gt=0, le=1000),
    offset: int = Query(default=0, ge=0),
) -> ReceiptsPage:
    store = _receipts_store(request)
    receipts = store.query(
        work_id=work_id, project=project, model=model, limit=limit, offset=offset
    )
    total = store.count(work_id=work_id, project=project, model=model)
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
