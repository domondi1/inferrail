"""Inferrail Economic Authority — FastAPI/A2A server assembly (Phase B).

Wires the real, installed `a2a-sdk` (verified against its actual API, not
an invented interface -- see the module docstrings in `executor.py` and
`capabilities.py`) onto a plain FastAPI app: the A2A Agent Card + JSON-RPC
routes (locked down to `SendMessage` only -- see `access_control.py`),
plus exactly one additional plain HTTP route, `POST /capabilities/claim`,
that exists solely to hand a newly-minted child capability token to its
rightful caller outside A2A message/task content.

## Documented SDK limitation and the smallest safe alternative

The installed `a2a-sdk` (1.1.2) gives an `AgentExecutor` no channel to
influence the outbound HTTP response of a JSON-RPC `SendMessage` call other
than A2A `Task`/`Message` content: `JsonRpcDispatcher._create_response`
(`a2a/server/routes/jsonrpc_dispatcher.py`) builds a plain
`JSONResponse(handler_result)` and never reads back anything an executor
might have stashed on `ServerCallContext.state` after the handler runs.
Concretely: `DefaultServerCallContextBuilder` builds a one-way channel (HTTP
request -> `ServerCallContext.state['headers']` -> `RequestContext`), but
there is no return channel (`RequestContext` -> HTTP response). Persisted
`Task`/`Message` content -- including artifacts -- is retrievable later via
`GetTask` (though `GetTask` is itself disabled in this Phase B server; see
`access_control.py`), so it is exactly the "task history" a credential
must never appear in.

`reserve` is the only operation that mints a brand-new credential (the
child delegation's own capability), so it is the only place this matters.
The smallest safe alternative implemented here: `reserve` returns only a
non-secret, single-use `credential_claim_id` in its A2A artifact --
never the credential itself. The actual plaintext token is retrieved by a
**separate, authenticated HTTP call** to `POST /capabilities/claim` on
this same server -- a plain route that exists entirely outside the A2A
message/task pipeline, so its request and response never touch
`TaskStore`.

Presented credentials always travel in the standard `Authorization:
Bearer <token>` HTTP header -- both for every A2A `SendMessage` call and
for this claim route -- never inside a message body, extension metadata,
task history, or an economic receipt. At claim time, the presented bearer
token is fully revalidated against the live `CapabilityStore` (existence,
expiry, revocation, delegation binding, and scope) -- not merely matched
by hash against whichever token happened to trigger the reservation; see
`capabilities.InMemoryCredentialHandoff.redeem`. This is also what
separates `grant` authority from `reserve` authority (repair item 6): a
claim is bound to "currently holds `reserve` scope on this parent_id", so
a grant-only credential can unblock a parked reservation but can never
itself redeem the resulting child credential.

The claim response is marked `Cache-Control: no-store` (plus the other
headers below) so it is never cached by an intermediary -- and, when this
service is deployed rather than run locally over plain HTTP as in tests,
it **must** be served over HTTPS, exactly like the `Authorization` header
itself; nothing about the claim mechanism is safe to run over an
unencrypted connection. This deployment requirement is not yet enforced
by any code in this repository -- Phase B has no deployment configuration
at all (see `README.md`'s "Known limitations").

This claim route is a deliberate, narrow deviation from "A2A operations
only" -- it does not touch the A2A protocol itself (no new JSON-RPC
method, no new Agent Card capability), and it is not present at all for
grant/consume/settle/status/revoke, none of which mint a new credential.

## Durability and single-process requirement

`core.EconomicAuthorityStore` and `capabilities.CapabilityStore` are both
SQLite-backed with `BEGIN IMMEDIATE` transactions: economic state,
capability-token issuance/revocation, and the revocation-in-progress
marker `core.py`'s race-safety design depends on all durably survive a
process restart, and are safe under multiple concurrent processes sharing
the same database files (SQLite's file-level locking serializes writers
regardless of process boundary).

`a2a.server.tasks.InMemoryTaskStore` (A2A task/message state, including a
`reserve` parked at `TASK_STATE_AUTH_REQUIRED` awaiting a `grant`) and
`capabilities.InMemoryCredentialHandoff` (unclaimed reservation credentials)
are **not** durable: both live only in this process's memory. A process
restart loses any parked task (the caller must re-issue the `reserve` from
scratch -- since `core.reserve()` was never called for a parked task, this
is safe, not a partial-state bug) and any unclaimed claim (the reservation
itself is unaffected; only the as-yet-unclaimed child credential is lost,
same as an unclaimed one-time code from any other system would be).

Because of this, **this server must run as a single process** -- `main()`
below never exposes a `--workers` option and calls `uvicorn.run()` without
one, which keeps it single-process by default. Running multiple worker
processes (or multiple independent server processes) against the same
task/claim state would silently break both in-memory stores; only the
SQLite-backed economic and capability state would remain correct. Phase B
has no deployment configuration that could accidentally introduce a
multi-worker setup, but this constraint is called out here explicitly so
a future phase does not add one without addressing it first.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_HOSTED_DIR = Path(__file__).resolve().parent
if str(_HOSTED_DIR) not in sys.path:
    sys.path.insert(0, str(_HOSTED_DIR))

from a2a.server.request_handlers import DefaultRequestHandler  # noqa: E402
from a2a.server.routes import (  # noqa: E402
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
)
from a2a.server.tasks import InMemoryTaskStore  # noqa: E402
from access_control import SendMessageOnlyRequestHandler  # noqa: E402
from agent_card import build_agent_card  # noqa: E402
from capabilities import CapabilityError, CapabilityStore, InMemoryCredentialHandoff  # noqa: E402
from core import EconomicAuthorityStore  # noqa: E402
from executor import EconomicAuthorityExecutor  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

_NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
}


def build_app(*, base_url: str, db_path: str | Path, capability_db_path: str | Path) -> FastAPI:
    """Assembles one server instance. Callers own the SQLite files at
    `db_path` (economic state, `core.EconomicAuthorityStore`'s schema) and
    `capability_db_path` (capability tokens, `capabilities.CapabilityStore`'s
    schema) -- both must already contain a bootstrapped root delegation and
    root capability before this app is asked to do anything useful; see
    `bootstrap.py`, which is test-only and never wired to an HTTP route.
    """
    core_store = EconomicAuthorityStore(db_path)
    capability_store = CapabilityStore(capability_db_path)
    handoff = InMemoryCredentialHandoff()
    executor = EconomicAuthorityExecutor(core_store, capability_store, handoff)

    agent_card = build_agent_card(url=base_url)
    task_store = InMemoryTaskStore()
    real_request_handler = DefaultRequestHandler(
        agent_executor=executor,
        task_store=task_store,
        agent_card=agent_card,
    )
    # Locks the A2A surface down to SendMessage only -- see
    # access_control.py's module docstring (repair item 1).
    request_handler = SendMessageOnlyRequestHandler(real_request_handler)

    app = FastAPI(title="Inferrail Economic Authority")
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(agent_card),
        jsonrpc_routes=create_jsonrpc_routes(request_handler, rpc_url="/"),
    )

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await real_request_handler.aclose()

    @app.post("/capabilities/claim")
    async def claim_capability(request: Request) -> JSONResponse:
        """Plain HTTP route, deliberately outside the A2A pipeline.

        See the module docstring: this exists only because the A2A JSON-RPC
        transport has no other channel to deliver a newly-minted credential
        without persisting it in task history. The presented bearer
        credential is fully revalidated (not just hash-matched) at claim
        time by `InMemoryCredentialHandoff.redeem`, and the response is
        marked non-cacheable.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": "malformed JSON body"}, status_code=400, headers=_NO_STORE_HEADERS
            )
        claim_id = body.get("claim_id") if isinstance(body, dict) else None
        auth_header = request.headers.get("authorization", "")
        scheme, _, presented_token = auth_header.partition(" ")
        if scheme.lower() != "bearer" or not presented_token.strip():
            return JSONResponse(
                {"error": "MissingCredential"}, status_code=401, headers=_NO_STORE_HEADERS
            )
        if not claim_id or not isinstance(claim_id, str):
            return JSONResponse(
                {"error": "claim_id is required"}, status_code=400, headers=_NO_STORE_HEADERS
            )
        try:
            plaintext = handoff.redeem(capability_store, claim_id, presented_token.strip())
        except CapabilityError as exc:
            return JSONResponse(
                {"error": type(exc).__name__}, status_code=403, headers=_NO_STORE_HEADERS
            )
        return JSONResponse({"token": plaintext}, headers=_NO_STORE_HEADERS)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run an Inferrail Economic Authority Phase B server."
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--capability-db-path", required=True)
    args = parser.parse_args()

    import uvicorn

    base_url = f"http://{args.host}:{args.port}/"
    app = build_app(
        base_url=base_url, db_path=args.db_path, capability_db_path=args.capability_db_path
    )
    # No `workers=` argument, deliberately -- see this module's docstring's
    # "Durability and single-process requirement" section. Do not add one
    # without first making InMemoryTaskStore and InMemoryCredentialHandoff
    # durable/shared across processes.
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
