"""Inferrail Economic Authority — FastAPI/A2A server assembly (Phase B/C).

Wires the real, installed `a2a-sdk` (verified against its actual API, not
an invented interface -- see the module docstrings in `executor.py` and
`capabilities.py`) onto a plain FastAPI app: the A2A Agent Card + JSON-RPC
routes (locked down to `SendMessage` only -- see `access_control.py`),
plus exactly one additional plain HTTP route, `POST /capabilities/claim`,
that exists solely to hand a newly-minted child capability token to its
rightful caller outside A2A message/task content.

## Phase C: paid session creation

`POST /sessions`, added in Phase C, is the ONLY x402-gated route in this
service -- see `sessions.py`'s module docstring for the full design.
Every other operation (reserve/grant/consume/settle/status/revoke, and
the claim route above) remains protected exclusively by its own
capability credential, exactly as in Phase B; purchasing a session is not
required to use them, and using them never requires a second payment.
`/sessions` (and its unpaid sibling `/sessions/recover`, see below) is
registered ONLY when `ECONOMIC_AUTHORITY_SESSION_PAY_TO_ADDRESS` is set in
the environment, so every existing Phase B deployment, test, or import of
this module that does not set it behaves exactly as before -- no new
required environment variable, no new required dependency import at call
time for anyone not using Phase C.

`/sessions` settles payment BEFORE calling its route handler (x402's
`"upfront"` payment flow, not the scheme's default) -- see `sessions.py`'s
"Settlement-before-handler" docstring section for the payment-security
defect this closes and why. `/sessions/recover` is a plain, unpaid HTTP
route for a buyer who was genuinely charged but never received (or has
since lost) their session's root credential -- identified by
`payment_nonce`, never the server-generated `session_id` -- see
`sessions.recover_session`'s docstring for exactly why, and `sessions.
handle_session_recovery_request`/`capabilities.CapabilityStore.
get_session_purchase_for_recovery` for the full design.

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
`capabilities.InMemoryCredentialHandoff.redeem`. Redemption is bound to
the exact credential that originally authorized the reservation, by its
non-secret `token_id` -- not to "any credential that currently holds
`reserve` scope on this parent_id". A different, otherwise-valid
`reserve`-scoped credential is rejected (`WrongAuthorizer`) even if it is
live and correctly scoped. This is also what separates `grant` authority
from `reserve` authority: a grant-only credential can unblock a parked
reservation but can never itself redeem the resulting child credential.
An ordinary retry of the same reservation never mints or rotates a
credential, for anyone -- only an explicit `recover_credential: true`
retry from the exact same authorizer does, used specifically when the
process or the caller's response was genuinely lost between committing
the reservation and the caller ever obtaining this claim -- see
`README.md`'s "Crash-safe recovery vs. ordinary retries" and
`capabilities.rotate_reservation_credential`.

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
SQLite-backed economic and capability state would remain correct. This
constraint is enforced by omission (no `--workers` flag exists to misuse)
and must be addressed explicitly, not merely re-checked, before any
future phase makes this service multi-process. Not deployed anywhere
yet -- see `hosted/a2a_economic_authority/README.md`'s "Deploying it"
for what `main()`'s two invocation shapes (explicit local/test args vs.
`PORT`/`ECONOMIC_AUTHORITY_*`-env-var-driven production startup) verify
in advance of one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from decimal import Decimal
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
from capabilities import (  # noqa: E402
    CapabilityError,
    CapabilityStore,
    InMemoryCredentialHandoff,
)
from cdp.x402 import create_facilitator_config  # noqa: E402
from core import EconomicAuthorityStore  # noqa: E402
from executor import EconomicAuthorityExecutor  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from sessions import (  # noqa: E402
    _RECOVERY_HASH_PATTERN,
    handle_session_recovery_request,
    handle_session_request,
)
from x402.extensions.payment_identifier import (  # noqa: E402
    PAYMENT_IDENTIFIER,
    declare_payment_identifier_extension,
    extract_payment_identifier,
)
from x402.http import HTTPFacilitatorClient  # noqa: E402
from x402.http.middleware.fastapi import payment_middleware  # noqa: E402
from x402.http.types import PaymentOption, RouteConfig  # noqa: E402
from x402.mechanisms.evm.exact import register_exact_evm_server  # noqa: E402
from x402.server import x402ResourceServer  # noqa: E402

_NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
}

# Phase C: session-purchase x402 configuration. Deliberately read with
# `.get` (not `os.environ[...]`), so importing or running this module for
# plain Phase B purposes -- every existing Phase B test does exactly that
# -- never requires these to be set. `/sessions` is registered in
# `build_app` only when `_SESSION_PAY_TO_ADDRESS` is present. A distinct
# pay-to address from `hosted/work_economics`'s own
# `X402_SELLER_PAY_TO_ADDRESS` keeps the two capabilities' commercial
# identities separate even though both may share the same underlying CDP
# facilitator account (`CDP_API_KEY_ID`/`CDP_API_KEY_SECRET`, also reused
# from Work Economics' own env var names -- those identify the CDP
# account talking to the facilitator, not a capability-specific secret).
_SESSION_PAY_TO_ADDRESS = os.environ.get("ECONOMIC_AUTHORITY_SESSION_PAY_TO_ADDRESS")
_SESSION_PRICE_USD = Decimal(os.environ.get("ECONOMIC_AUTHORITY_SESSION_PRICE_USD", "0.05"))
_SESSION_NETWORK = "eip155:84532"  # Base Sepolia, CAIP-2 -- testnet only, see sessions.py


def build_app(*, base_url: str, db_path: str | Path, capability_db_path: str | Path) -> FastAPI:
    """Assembles one server instance. Callers own the SQLite files at
    `db_path` (economic state, `core.EconomicAuthorityStore`'s schema) and
    `capability_db_path` (capability tokens, `capabilities.CapabilityStore`'s
    schema). A root delegation and its root capability can come from
    either of two places: `bootstrap.py` (test-only, never wired to an
    HTTP route, writes directly into these same files before the server
    starts) or, since Phase C, a real buyer completing a payment against
    `POST /sessions` once this app is already running -- see this
    module's docstring.
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

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Plain, unauthenticated liveness check for a deployment platform's
        health-check probe -- deliberately outside the A2A/capability
        pipeline, same as `hosted/work_economics/service.py`'s `/health`.
        Returns process liveness only, not economic-state correctness.
        """
        return {"status": "ok"}

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
            plaintext = handoff.redeem(
                capability_store,
                claim_id,
                presented_token.strip(),
                is_target_revoked=core_store.is_revocation_in_progress,
            )
        except CapabilityError as exc:
            return JSONResponse(
                {"error": type(exc).__name__}, status_code=403, headers=_NO_STORE_HEADERS
            )
        return JSONResponse({"token": plaintext}, headers=_NO_STORE_HEADERS)

    if _SESSION_PAY_TO_ADDRESS:
        _wire_session_purchase_route(app, core_store, capability_store, base_url)

    return app


def _wire_session_purchase_route(
    app: FastAPI,
    core_store: EconomicAuthorityStore,
    capability_store: CapabilityStore,
    base_url: str,
) -> None:
    """Registers the ONLY x402-gated route in this service, `POST
    /sessions` -- see this module's docstring and `sessions.py`'s for the
    full design. Only called when `_SESSION_PAY_TO_ADDRESS` is set, so a
    plain Phase B deployment/test never pays this section's import-time
    or wiring cost.
    """
    facilitator_config = create_facilitator_config(
        api_key_id=os.environ["CDP_API_KEY_ID"],
        api_key_secret=os.environ["CDP_API_KEY_SECRET"],
    )
    facilitator_client = HTTPFacilitatorClient(facilitator_config)
    x402_server = x402ResourceServer(facilitator_client)
    register_exact_evm_server(x402_server, networks=_SESSION_NETWORK)

    resource_url = os.environ.get(
        "ECONOMIC_AUTHORITY_SESSION_RESOURCE_URL", f"{base_url.rstrip('/')}/sessions"
    )
    routes: dict[str, RouteConfig] = {
        "POST /sessions": RouteConfig(
            accepts=PaymentOption(
                scheme="exact",
                pay_to=_SESSION_PAY_TO_ADDRESS,  # type: ignore[arg-type]  # guarded by the caller
                price=f"${_SESSION_PRICE_USD}",
                network=_SESSION_NETWORK,
                # Payment-security repair: settle BEFORE calling the route
                # handler, instead of the "exact"/eip3009 scheme's default
                # "authorization" flow (settle after). This is an
                # officially supported flow for this asset transfer
                # method (see `x402.mechanisms.evm.exact.server
                # .ExactEvmScheme.payment_flows`), not a bespoke
                # workaround. See `sessions.py`'s module docstring,
                # "Settlement-before-handler", for the full defect this
                # closes and why it is closed structurally rather than by
                # convention.
                extra={"paymentFlow": "upfront"},
            ),
            resource=resource_url,
            description=(
                "Purchase an Inferrail Economic Authority session: a durable "
                "coordination boundary with a buyer-declared spending ceiling "
                "(authority_ceiling_usd) that lets agents operate under a "
                "shared budget without double-allocation. This fee pays for "
                "the coordination service itself -- it is never a deposit "
                "into, or escrow of, the delegated ceiling; Inferrail holds, "
                "transfers, or escrows none of it."
            ),
            service_name="Inferrail Economic Authority",
            # Optional (not required): a buyer MAY include the official
            # x402 payment-identifier extension. See sessions.py's module
            # docstring for exactly what this is, and is not, used for
            # here (audit/correlation only -- never a substitute for the
            # verified on-chain payment_nonce as the idempotency key).
            extensions={PAYMENT_IDENTIFIER: declare_payment_identifier_extension(required=False)},
        )
    }
    x402_middleware = payment_middleware(routes, x402_server)

    @app.middleware("http")
    async def _sessions_payment_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
        return await x402_middleware(request, call_next)

    # Registered AFTER `_sessions_payment_middleware` above -- Starlette's
    # `Starlette.add_middleware` INSERTS each new middleware at the front
    # of its internal list (`user_middleware.insert(0, ...)`), which
    # `build_middleware_stack` then wraps such that the LAST-registered
    # middleware ends up OUTERMOST and therefore runs FIRST on the way in.
    # Registering this one second makes it run before x402 ever verifies
    # or settles anything -- so it can reject, and does, BEFORE any money
    # moves. This is what makes `recovery_secret_hash` genuinely "reject
    # before payment" rather than merely "reject before the route handler
    # runs": the latter is NOT good enough under this route's `"upfront"`
    # payment flow, where settlement happens before the handler runs
    # regardless of what the handler would have validated. Reads the body
    # via `request.json()` -- Starlette caches the raw bytes on first
    # read, so the x402 middleware (which already ran) and the route
    # handler below can still read the same body afterward. Malformed
    # JSON is deliberately NOT rejected here (that is `create_session`'s
    # job) -- this middleware only ever produces an early rejection for
    # the one thing it exists to check.
    @app.middleware("http")
    async def _require_recovery_secret_hash(request: Request, call_next):  # type: ignore[no-untyped-def]
        if request.method == "POST" and request.url.path == "/sessions":
            try:
                body = await request.json()
            except (json.JSONDecodeError, ValueError):
                body = None
            if isinstance(body, dict):
                recovery_secret_hash = body.get("recovery_secret_hash")
                if not isinstance(recovery_secret_hash, str) or not _RECOVERY_HASH_PATTERN.match(
                    recovery_secret_hash
                ):
                    return JSONResponse(
                        {"error": "recovery_secret_hash is required"},
                        status_code=400,
                        headers=_NO_STORE_HEADERS,
                    )
        return await call_next(request)

    @app.post("/sessions")
    async def create_session(request: Request) -> JSONResponse:
        """x402-protected. The only payment-gated route in this service.

        By the time this handler runs, the x402 middleware has already
        cryptographically verified the payment AND completed real
        on-chain settlement (`request.state.payment_payload` is set, and
        the route's `"upfront"` payment flow means settlement is a
        precondition of this handler ever being called at all -- see
        `sessions.py`'s "Settlement-before-handler" docstring section).
        If settlement fails, the middleware returns a 402 directly and
        this handler never runs -- there is no code path here that must
        itself distinguish "verified" from "settled". This handler is
        deliberately thin: it never calls the facilitator directly and
        never decides whether a payment is valid, only extracts the
        verified payment's nonce (and, if present, its optional
        payment-identifier extension value) and hands everything else to
        `sessions.handle_session_request` -- see that function and
        `sessions.create_or_recover_session` for the actual idempotency
        and crash-safety guarantee, and for how this is tested without a
        real facilitator/network dependency.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": "malformed JSON body"}, status_code=400, headers=_NO_STORE_HEADERS
            )

        payment_payload = getattr(request.state, "payment_payload", None)
        if payment_payload is None:
            # Unreachable in practice -- the middleware only calls through
            # to this handler once payment is verified AND (for this
            # route's "upfront" flow) settled -- but fail closed rather
            # than trust an unverified request.
            return JSONResponse(
                {"error": "payment not verified"}, status_code=402, headers=_NO_STORE_HEADERS
            )
        # `payment_payload.payload` is a plain `dict[str, Any]` (the exact
        # scheme's V2 wire shape is `{"authorization": {...}, "signature":
        # ...}`), never an attribute-accessible object -- there is no
        # `PaymentPayload.payload.authorization` attribute on the installed
        # SDK. Fail closed (never silently substitute some other string as
        # the idempotency key) if the shape is ever something else.
        nonce = None
        if isinstance(payment_payload.payload, dict):
            authorization = payment_payload.payload.get("authorization")
            if isinstance(authorization, dict):
                nonce = authorization.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            return JSONResponse(
                {"error": "malformed verified payment payload"},
                status_code=500,
                headers=_NO_STORE_HEADERS,
            )
        payment_nonce = nonce
        try:
            payment_identifier = extract_payment_identifier(payment_payload)
        except (AttributeError, TypeError, ValueError):
            payment_identifier = None

        status_code, response_body = handle_session_request(
            core_store,
            capability_store,
            body=body,
            payment_nonce=payment_nonce,
            service_fee_usd=_SESSION_PRICE_USD,
            payment_identifier=payment_identifier,
        )
        return JSONResponse(response_body, status_code=status_code, headers=_NO_STORE_HEADERS)

    @app.post("/sessions/recover")
    async def recover_session_route(request: Request) -> JSONResponse:
        """Plain HTTP route, deliberately outside the x402/A2A pipeline
        and NOT payment-gated -- recovering access to a session the
        buyer already paid for costs nothing further. Identifies the
        purchase by `payment_nonce` (never `session_id` -- see
        `sessions.recover_session`'s docstring for exactly why). See
        `sessions.py`'s `handle_session_recovery_request` and
        `capabilities.CapabilityStore.get_session_purchase_for_recovery`
        for the full design: authorization is proof of knowledge of the
        plaintext behind the `recovery_secret_hash` commitment required
        on the original `POST /sessions` call, never anything
        server-issued that could share the same loss-of-response risk as
        the credential it recovers.
        """
        try:
            body = await request.json()
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": "malformed JSON body"}, status_code=400, headers=_NO_STORE_HEADERS
            )
        status_code, response_body = handle_session_recovery_request(
            core_store, capability_store, body=body
        )
        return JSONResponse(response_body, status_code=status_code, headers=_NO_STORE_HEADERS)


def main() -> None:
    """Two invocation shapes, mirroring `hosted/work_economics/service.py`'s
    own explicit-args-vs-production convention:

    - **Local/test shape** (every existing Phase B/C test uses this):
      `--port`, `--db-path`, and `--capability-db-path` given explicitly on
      the command line. `--host` defaults to `127.0.0.1`. `base_url`
      defaults to the computed `http://<host>:<port>/` unless `--base-url`
      is also given. Argument parsing and validation are unchanged from
      before this function grew env-var support; the one behavioral change
      that applies to *both* shapes is `uvicorn.run()` now also passing
      `proxy_headers=True, forwarded_allow_ips="*"` (see below), matching
      `hosted/work_economics/service.py`'s own production-shape pattern --
      inert for a direct loopback connection with no reverse proxy in
      front of it, as every existing test uses.
    - **Production/deployment shape** (a bare `python3 server.py`, no CLI
      args): `--port`/`--db-path`/`--capability-db-path` are read from
      `PORT`/`ECONOMIC_AUTHORITY_DB_PATH`/`ECONOMIC_AUTHORITY_CAPABILITY_DB_PATH`
      instead, `--host` defaults to `0.0.0.0`, and the public HTTPS
      `base_url` -- used for the Agent Card's own `url` field and as the
      default x402 resource URL -- **must** come from
      `ECONOMIC_AUTHORITY_BASE_URL` (there is no way to derive a public
      HTTPS URL from an internal bind address/port). Fails closed with a
      clear error rather than silently advertising an unreachable Agent
      Card URL.
    """
    parser = argparse.ArgumentParser(
        description="Run an Inferrail Economic Authority Phase B/C server."
    )
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--db-path")
    parser.add_argument("--capability-db-path")
    parser.add_argument("--base-url")
    args = parser.parse_args()

    explicit_args = (
        args.port is not None or args.db_path is not None or args.capability_db_path is not None
    )

    port_str = str(args.port) if args.port is not None else os.environ.get("PORT")
    if not port_str:
        parser.error(
            "--port is required (explicit shape) or the PORT environment "
            "variable must be set (production shape)"
        )
    port = int(port_str)

    db_path = args.db_path or os.environ.get("ECONOMIC_AUTHORITY_DB_PATH")
    if not db_path:
        parser.error(
            "--db-path is required (explicit shape) or ECONOMIC_AUTHORITY_DB_PATH "
            "must be set (production shape)"
        )

    capability_db_path = args.capability_db_path or os.environ.get(
        "ECONOMIC_AUTHORITY_CAPABILITY_DB_PATH"
    )
    if not capability_db_path:
        parser.error(
            "--capability-db-path is required (explicit shape) or "
            "ECONOMIC_AUTHORITY_CAPABILITY_DB_PATH must be set (production shape)"
        )

    host = args.host or ("127.0.0.1" if explicit_args else "0.0.0.0")

    base_url = args.base_url or os.environ.get("ECONOMIC_AUTHORITY_BASE_URL")
    if not base_url:
        if explicit_args:
            base_url = f"http://{host}:{port}/"
        else:
            parser.error(
                "ECONOMIC_AUTHORITY_BASE_URL (or --base-url) must be set in the "
                "production shape -- the Agent Card and x402 resource URL must "
                "advertise the real public HTTPS URL, which cannot be derived "
                "from an internal bind host/port"
            )

    import uvicorn

    app = build_app(base_url=base_url, db_path=db_path, capability_db_path=capability_db_path)
    # No `workers=` argument, deliberately -- see this module's docstring's
    # "Durability and single-process requirement" section. Do not add one
    # without first making InMemoryTaskStore and InMemoryCredentialHandoff
    # durable/shared across processes.
    uvicorn.run(
        app, host=host, port=port, log_level="warning", proxy_headers=True, forwarded_allow_ips="*"
    )


if __name__ == "__main__":
    main()
