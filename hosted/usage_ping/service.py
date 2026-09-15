"""Inferrail usage-ping collector -- the reference receiver for the
opt-in, anonymous usage ping (docs/adr/0019-opt-in-usage-ping.md,
docs/privacy/usage-ping.md).

Lives outside `src/inferrail`, own process, own storage, no shared code
path with `hosted/ap_exceptions` or `hosted/a2a_economic_authority` --
same isolation rule every hosted surface in this repository follows.
Deliberately has **zero dependency on the `inferrail` package itself**:
the payload this service accepts is small, fixed, and self-contained
(see `EventPayload` below), so there's nothing here that needs the OSS
SDK the way `hosted/ap_exceptions` genuinely does.

**What this service never does:** log or persist the connecting IP
address, require authentication to submit a ping (the payload is
harmless and anonymous, so an API key would only add friction for no
privacy benefit), or accept any field beyond the fixed schema
(`extra="forbid"` rejects anything else outright, a server-side
enforcement of the same boundary the client already promises).
"""

from __future__ import annotations

import os
import sqlite3
import time
import uuid
from collections import defaultdict, deque
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

KNOWN_EVENTS = ("first_run", "tool_connected", "first_receipt", "budget_created")

DATA_PATH = Path(os.environ.get("USAGE_PING_DB", "./usage-ping.sqlite3"))
ENABLED = os.environ.get("USAGE_PING_ENABLED", "true").lower() != "false"
ADMIN_TOKEN = os.environ.get("USAGE_PING_ADMIN_TOKEN")  # unset => /stats is disabled entirely
MAX_REQUEST_BODY_BYTES = int(os.environ.get("USAGE_PING_MAX_REQUEST_BODY_BYTES", str(4 * 1024)))
RATE_LIMIT_MAX_REQUESTS = int(os.environ.get("USAGE_PING_RATE_LIMIT_MAX_REQUESTS", "30"))
RATE_LIMIT_WINDOW_SECONDS = float(os.environ.get("USAGE_PING_RATE_LIMIT_WINDOW_SECONDS", "60"))


class EventPayload(BaseModel):
    """The one and only shape this service ever accepts -- must match
    `inferrail.usage_ping.payload.build_payload` exactly. `extra="forbid"`
    means any additional field (a bug, or an attempt to send more than
    this contract allows) is rejected with a 422, not silently stored."""

    model_config = {"extra": "forbid"}

    install_id: str = Field(min_length=1, max_length=128)
    event: Literal["first_run", "tool_connected", "first_receipt", "budget_created"]
    os: str = Field(min_length=1, max_length=32)
    inferrail_version: str = Field(min_length=1, max_length=32)
    ts: str = Field(min_length=1, max_length=64)


class RateLimiter:
    """Per-IP fixed-window limiter. In-memory, resets on process
    restart -- adequate for a single-instance deployment, matching
    `hosted/ap_exceptions/auth.py`'s own `RateLimiter` (not imported
    from there: these are independent deployables by design)."""

    def __init__(self, *, max_requests: int, window_seconds: float) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._requests: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str) -> None:
        now = time.monotonic()
        window = self._requests[key]
        while window and now - window[0] > self._window_seconds:
            window.popleft()
        if len(window) >= self._max_requests:
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        window.append(now)


def _init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                install_id TEXT NOT NULL,
                event TEXT NOT NULL,
                os TEXT NOT NULL,
                inferrail_version TEXT NOT NULL,
                client_ts TEXT NOT NULL,
                received_at TEXT NOT NULL
            )
            """
        )
        # No column for the connecting IP address, anywhere, deliberately.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_event ON events(event)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_install ON events(install_id)")
        conn.commit()
    finally:
        conn.close()


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path)
    try:
        yield conn
    finally:
        conn.close()


def create_app(*, db_path: Path | None = None) -> FastAPI:
    path = db_path or DATA_PATH
    _init_db(path)
    limiter = RateLimiter(
        max_requests=RATE_LIMIT_MAX_REQUESTS, window_seconds=RATE_LIMIT_WINDOW_SECONDS
    )

    app = FastAPI(title="Inferrail Usage Ping Collector")

    @app.middleware("http")
    async def request_size_limit_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        # Same pattern as hosted/ap_exceptions/service.py's own middleware
        # of the same name -- an unauthenticated POST is exactly the
        # route an attacker would target with an oversized body.
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = 0
            if declared_size > MAX_REQUEST_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={"detail": f"body exceeds {MAX_REQUEST_BODY_BYTES}-byte limit"},
                )
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/ping", status_code=202)
    async def ping(request: Request, payload: EventPayload) -> dict[str, bool]:
        if not ENABLED:
            # Fail open toward the client (never let a caller observe
            # that collection is killed server-side) -- just don't store.
            return {"ok": True}

        client_host = request.client.host if request.client else "unknown"
        limiter.check(client_host)  # keyed in memory only, never persisted

        with _connect(path) as conn:
            conn.execute(
                "INSERT INTO events "
                "(id, install_id, event, os, inferrail_version, client_ts, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    uuid.uuid4().hex,
                    payload.install_id,
                    payload.event,
                    payload.os,
                    payload.inferrail_version,
                    payload.ts,
                    datetime.now(UTC).isoformat(),
                ),
            )
            conn.commit()
        return {"ok": True}

    @app.get("/stats")
    async def stats(authorization: str | None = Header(default=None)) -> dict[str, object]:
        """Aggregate counts only -- never a row-level dump of individual
        install ids over HTTP, so this stays a "how many, not who" view
        even for the operator. Disabled outright (404) unless
        `USAGE_PING_ADMIN_TOKEN` is set."""
        if not ADMIN_TOKEN:
            raise HTTPException(status_code=404)
        if authorization != f"Bearer {ADMIN_TOKEN}":
            raise HTTPException(status_code=401)

        with _connect(path) as conn:
            by_event = dict(
                conn.execute("SELECT event, COUNT(*) FROM events GROUP BY event").fetchall()
            )
            distinct_installs = dict(
                conn.execute(
                    "SELECT event, COUNT(DISTINCT install_id) FROM events GROUP BY event"
                ).fetchall()
            )
            total_distinct_installs = conn.execute(
                "SELECT COUNT(DISTINCT install_id) FROM events"
            ).fetchone()[0]
        return {
            "events_received": {event: by_event.get(event, 0) for event in KNOWN_EVENTS},
            "distinct_installs_per_event": {
                event: distinct_installs.get(event, 0) for event in KNOWN_EVENTS
            },
            "total_distinct_installs": total_distinct_installs,
        }

    return app


if __name__ == "__main__":
    import sys

    import uvicorn

    # No module-level `app = create_app()` (unlike a typical
    # `uvicorn module:app` deployment shape) -- matching
    # hosted/ap_exceptions/service.py's own pattern exactly, so merely
    # *importing* this module (as the test suite's importlib-based
    # loader does) never touches disk by constructing a database at the
    # default path as a side effect.
    #
    # Same shape as hosted/ap_exceptions/service.py: a bare `python3
    # service.py` (no argv) is the production/hosted case -- bind
    # 0.0.0.0 and read the platform-injected $PORT (Render sets this).
    # Explicit argv[1] is the local/loopback test shape.
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8600))
    app = create_app()
    print(f"Inferrail usage-ping collector listening on http://0.0.0.0:{port} (db={DATA_PATH})")
    uvicorn.run(
        app, host="0.0.0.0", port=port, log_level="warning", proxy_headers=True,
        forwarded_allow_ips="*",
    )
