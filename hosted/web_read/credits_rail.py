"""Web Read paid from prepaid Inferrail credits with an API key (no wallet, no per-request signing).

Mounted at `<mount_prefix>/credits` when the host passes a `credits` adapter (Inferrail Hosted's
workspace ledger, see `budget_gateway/cohost.py`). A caller sends its workspace key as
`Authorization: Bearer irw_...` or `x-api-key: irw_...`. Each delivered page costs
`units_per_result` credits ($0.001 each, so 2 credits = $0.002). Credits are bought with
`POST /hosted/v1/credits/x402` (USDC) or, where the deployment enables card payments,
`POST /hosted/v1/credits/checkout`.

Charged only on delivery: the credits are held while the page is read, billed when it is
delivered and released when it is not. The same key, URL(s) and `request_id` within the
recovery window return the stored result again without a second charge.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class Credits(Protocol):
    """What the host provides: Hosted's workspace ledger, behind a narrow interface."""

    units_per_result: int
    top_up: dict[str, str]  # how to buy credits: {"x402": url, "card": url?, "create_key": url}

    def authenticate(self, api_key: str) -> str | None: ...
    def hold(self, workspace_id: str, ref: str, units: int) -> tuple[str, int]: ...
    def finish(self, ref: str, delivered: bool) -> None: ...


class ResultStore:
    """Delivered results by spend ref, so a retry is answered without a second charge."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS results (ref TEXT PRIMARY KEY, body TEXT, at REAL)"
        )
        self._lock = threading.Lock()

    def put(self, ref: str, row: dict[str, Any]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO results VALUES (?,?,?)", (ref, json.dumps(row), time.time())
            )

    def get(self, ref: str, max_age_s: float) -> dict[str, Any] | None:
        with self._lock:
            self._db.execute("DELETE FROM results WHERE at < ?", (time.time() - max_age_s,))
            hit = self._db.execute("SELECT body FROM results WHERE ref=?", (ref,)).fetchone()
        return json.loads(hit[0]) if hit else None


# Integrations that label their requests (`x-inferrail-source`), counted per channel in the
# funnel. A fixed set, so no caller-chosen text ever reaches the stats.
SOURCES = frozenset({"dify", "mcp-local", "ai-sdk"})


def _source(request: Request) -> str | None:
    value = request.headers.get("x-inferrail-source", "").strip().lower()
    return value if value in SOURCES else None


def _api_key(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def build(
    *,
    task: Any,
    credits: Credits,
    price_usd: str,
    data_dir: Path,
    recovery_s: float,
    allow_get: bool,
    event: Callable[[str, str], None],
) -> FastAPI:
    """The credits sub-app (mount it at "/credits"). `event` is Web Read's funnel counter."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    noun = getattr(task, "result_noun", "page")
    again = "read the page again" if noun == "page" else f"get a new {noun}"
    results = ResultStore(data_dir / "credit_results.sqlite3")
    recovery = max(recovery_s, 600.0)

    def need_key(detail: str) -> JSONResponse:
        return JSONResponse(
            {"error": "workspace_key_required", "charged": False, "detail": detail,
             "price_usd": price_usd, "credits_per_result": credits.units_per_result,
             "get_started": credits.top_up},
            status_code=401,
        )  # fmt: skip

    async def deliver(body: Any, request: Request) -> JSONResponse:
        key = _api_key(request)
        if not key:
            event("credits", "challenge")
            return need_key("Send your Inferrail workspace key as 'Authorization: Bearer irw_...'.")
        workspace = credits.authenticate(key)
        if workspace is None:
            event("credits", "bad_key")
            return need_key("This workspace key is not valid.")
        units = (task.units(body) if task.units else 1) * credits.units_per_result
        idem = task.idem(body)
        prefix = getattr(task, "credits_ref", "web-read")
        ref = f"{prefix}:" + hashlib.sha256(f"{workspace}\n{idem}".encode()).hexdigest()[:40]
        state, balance = credits.hold(workspace, ref, units)
        if state == "billed":
            stored = results.get(ref, recovery)
            if stored is not None:
                event("credits", "recovered")
                stored["receipt"] = {**stored.get("receipt", {}), "recovered": True}
                return JSONResponse(stored, headers={"x-inferrail-recovered": "1"})
            return JSONResponse(
                {"error": "already_paid", "charged": False,
                 "detail": "this request_id was already delivered and paid; use a new "
                           f"request_id to {again}"},
                status_code=409,
            )  # fmt: skip
        if state == "busy":
            return JSONResponse(
                {"error": "in_progress", "charged": False,
                 "detail": f"this {noun} is still in progress; retry shortly"},
                status_code=409,
            )  # fmt: skip
        if state == "insufficient":
            event("credits", "insufficient")
            return JSONResponse(
                {"error": "insufficient_credits", "charged": False, "credits_needed": units,
                 "credits_remaining": balance, "top_up": credits.top_up},
                status_code=402,
            )  # fmt: skip
        source = _source(request)
        event("credits", "attempt")
        delivered = False
        try:
            status, row = await task.deliver(body, event)
            delivered = status == 200
        finally:
            credits.finish(ref, delivered)
        if not delivered:
            event("credits", "refused_free")
            return JSONResponse({**row, "charged": False}, status_code=status)
        row = {**row, "receipt": {"rail": "credits", "product": noun, "credits_charged": units,
                                  "credits_remaining": balance, "price_usd": price_usd,
                                  "request_id": getattr(body, "request_id", None)}}  # fmt: skip
        row.pop("bytes", None)
        results.put(ref, row)
        event("credits", "delivered")
        if source:
            event("channel", f"{source}:credits_delivered")
        return JSONResponse(row)

    async def page_post(body: Any, request: Request) -> JSONResponse:
        return await deliver(body, request)

    page_post.__annotations__["body"] = task.request_model
    app.post(task.path)(page_post)
    if allow_get:

        @app.get(task.path, include_in_schema=False)
        async def page_get(request: Request) -> JSONResponse:
            try:
                body = task.request_model.model_validate(dict(request.query_params))
            except Exception:
                return JSONResponse({"error": "invalid_request", "charged": False}, status_code=422)
            return await deliver(body, request)

    return app
