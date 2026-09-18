"""Inferrail usage-ping collector -- the reference receiver for the
anonymous, opt-out usage/presence beacon
(docs/adr/0020-quickstart-both-sdks-and-payload-free-verification.md,
superseding docs/adr/0019's "opt-in, default off").

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

**Schema.** Two tables, matching the shape the founder specified
verbatim (SQLite here; the same shape, unchanged, works as Postgres --
swap `TEXT`/`INTEGER` timestamp columns for `TIMESTAMPTZ` and
`id INTEGER PRIMARY KEY AUTOINCREMENT` for `BIGSERIAL PRIMARY KEY`, and
add the `REFERENCES installs(install_id)` foreign key SQLite's own
`PRAGMA foreign_keys` already enforces at runtime):

    installs(install_id PK, first_seen_at, last_seen_at, version, os,
             python_version, reached_first_receipt_at)
    events(id PK, install_id FK, event, version, seen_at)

On every beacon: upsert `installs` (set `last_seen_at`/`version`; set
`reached_first_receipt_at` on the `first_receipt` event if it was still
null), then insert one `events` row. `owner_stats.py`'s queries assume
exactly this shape.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections import defaultdict, deque
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

# Exactly the four lifecycle events docs/adr/0020 specifies. Matches
# `inferrail.usage_ping.state.KNOWN_EVENTS` and the client's own
# `Literal` below verbatim -- deliberately duplicated, not imported: this
# service has zero dependency on the `inferrail` package (see module
# docstring).
KNOWN_EVENTS = ("install", "serve_start", "first_receipt", "heartbeat")

DATA_PATH = Path(os.environ.get("USAGE_PING_DB", "./usage-ping.sqlite3"))
ENABLED = os.environ.get("USAGE_PING_ENABLED", "true").lower() != "false"
ADMIN_TOKEN = os.environ.get("USAGE_PING_ADMIN_TOKEN")  # unset => /stats is disabled entirely
MAX_REQUEST_BODY_BYTES = int(os.environ.get("USAGE_PING_MAX_REQUEST_BODY_BYTES", str(4 * 1024)))
RATE_LIMIT_MAX_REQUESTS = int(os.environ.get("USAGE_PING_RATE_LIMIT_MAX_REQUESTS", "30"))
RATE_LIMIT_WINDOW_SECONDS = float(os.environ.get("USAGE_PING_RATE_LIMIT_WINDOW_SECONDS", "60"))

_ALLOWED_OS = {"linux", "darwin", "windows"}


class EventPayload(BaseModel):
    """The one and only shape this service ever accepts -- must match
    `inferrail.usage_ping.payload.build_payload` exactly. `extra="forbid"`
    means any additional field (a bug, or an attempt to send more than
    this contract allows) is rejected with a 422, not silently stored.
    No `ts`/timestamp field -- the server stamps `seen_at`/`first_seen_at`/
    `last_seen_at` itself, never trusts a client clock."""

    model_config = {"extra": "forbid"}

    install_id: str = Field(min_length=1, max_length=128)
    event: Literal["install", "serve_start", "first_receipt", "heartbeat"]
    version: str = Field(min_length=1, max_length=32)
    os: Literal["linux", "darwin", "windows"]
    python_version: str = Field(min_length=1, max_length=16)


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
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS installs (
                install_id TEXT PRIMARY KEY,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                version TEXT NOT NULL,
                os TEXT,
                python_version TEXT,
                reached_first_receipt_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                install_id TEXT NOT NULL REFERENCES installs(install_id),
                event TEXT NOT NULL CHECK (
                    event IN ('install', 'serve_start', 'first_receipt', 'heartbeat')
                ),
                version TEXT NOT NULL,
                seen_at TEXT NOT NULL
            )
            """
        )
        # No column for the connecting IP address, anywhere, deliberately.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS events_install_seen ON events (install_id, seen_at)"
        )
        conn.commit()
    finally:
        conn.close()


@contextmanager
def _connect(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON")
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

        now = datetime.now(UTC).isoformat()
        # Only actually a value when *this* event is the first_receipt
        # milestone -- None otherwise. The upsert's COALESCE below then
        # only ever sets `reached_first_receipt_at` from this value when
        # the column was still null, so an existing install's real first
        # timestamp is never overwritten and a non-first_receipt event
        # never fabricates one.
        first_receipt_at = now if payload.event == "first_receipt" else None
        with _connect(path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                INSERT INTO installs
                    (install_id, first_seen_at, last_seen_at, version, os, python_version,
                     reached_first_receipt_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(install_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    version = excluded.version,
                    os = excluded.os,
                    python_version = excluded.python_version,
                    reached_first_receipt_at = COALESCE(
                        installs.reached_first_receipt_at, excluded.reached_first_receipt_at
                    )
                """,
                (
                    payload.install_id,
                    now,
                    now,
                    payload.version,
                    payload.os,
                    payload.python_version,
                    first_receipt_at,
                ),
            )
            conn.execute(
                "INSERT INTO events (install_id, event, version, seen_at) VALUES (?, ?, ?, ?)",
                (payload.install_id, payload.event, payload.version, now),
            )
            conn.commit()
        return {"ok": True}

    @app.get("/stats")
    async def stats(authorization: str | None = Header(default=None)) -> dict[str, object]:
        """Aggregate counts only -- never a row-level dump of individual
        install ids over HTTP, so this stays a "how many, not who" view
        even for the operator. Disabled outright (404) unless
        `USAGE_PING_ADMIN_TOKEN` is set. `scripts/owner_stats.py` queries
        the same underlying tables directly for the fuller breakdown
        (activation rate, weekly cohorts, ...) -- this endpoint is a
        lightweight remote check, not the primary reporting surface."""
        if not ADMIN_TOKEN:
            raise HTTPException(status_code=404)
        if authorization != f"Bearer {ADMIN_TOKEN}":
            raise HTTPException(status_code=401)

        with _connect(path) as conn:
            by_event = dict(
                conn.execute("SELECT event, COUNT(*) FROM events GROUP BY event").fetchall()
            )
            total_installs = conn.execute("SELECT COUNT(*) FROM installs").fetchone()[0]
            activated = conn.execute(
                "SELECT COUNT(*) FROM installs WHERE reached_first_receipt_at IS NOT NULL"
            ).fetchone()[0]
        return {
            "events_received": {event: by_event.get(event, 0) for event in KNOWN_EVENTS},
            "total_installs": total_installs,
            "activated_installs": activated,
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
