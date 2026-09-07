"""Inferrail Work Economics — hosted x402 seller.

This is Inferrail's first hosted, paid capability. It lives outside
`src/inferrail` deliberately: the OSS gateway (the data plane) has zero
dependency on this service or any other Inferrail-operated service — see
`docs/adr/0004-data-plane-control-plane-boundary.md` and
`docs/adr/0010-hosted-work-economics-capability.md`. Running `inferrail
serve` never requires this file to exist or be reachable.

Payment is the real x402 protocol against Coinbase's CDP-hosted
facilitator. **Base Sepolia testnet only** (`eip155:84532`) — not mainnet,
not real money. An unpaid `POST /invoke` returns HTTP 402 with
`PaymentRequirements`; a retry with a signed `X-PAYMENT` header is
verified and settled by the facilitator. See `docs/capabilities/work-economics.md`
for the full public contract.

Reads `CDP_API_KEY_ID` / `CDP_API_KEY_SECRET` from the environment (never
logged or echoed) to authenticate to the facilitator's `/verify` and
`/settle` endpoints, which perform on-chain verification and settlement on
this seller's behalf.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal
from pathlib import Path

from capability import (
    VALID_PRICE_BASIS,
    VALID_STATUS,
    EconomicEvent,
    InvalidEconomicEvent,
    compute_work_economics,
)
from cdp.x402 import create_facilitator_config
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from store import DurablePurchaseStore
from x402.extensions.bazaar import OutputConfig, declare_discovery_extension
from x402.http import HTTPFacilitatorClient
from x402.http.middleware.fastapi import payment_middleware
from x402.http.types import PaymentOption, RouteConfig
from x402.mechanisms.evm.exact import register_exact_evm_server
from x402.server import x402ResourceServer

CAPABILITY_NAME = "inferrail-work-economics"
CAPABILITY_VERSION = "work-economics-v1"
PRICE_USD = Decimal("0.05")
PRICE_CURRENCY = "USD"
RAIL = "X402_BASE_SEPOLIA_TESTNET"
NETWORK = "eip155:84532"  # Base Sepolia, CAIP-2
NETWORK_CLASS = "TESTNET"

SELLER_PAY_TO_ADDRESS = os.environ["X402_SELLER_PAY_TO_ADDRESS"]

DISCOVERY_DESCRIPTION = (
    "Inferrail turns payload-free, already-incurred economic events — "
    "inference/token cost, tool calls, search — into a normalized cost "
    "receipt for one unit of agent work: known total cost, a breakdown by "
    "resource class and supplier, unknown-cost count, and price provenance, "
    "without requiring prompt or response content. Used for AI job cost "
    "analysis, LLM spend tracking, and unit economics."
)

INPUT_EXAMPLE: dict = {
    "work_id": "example-work-001",
    "events": [
        {
            "resource_class": "inference",
            "supplier": "example-supplier",
            "known_cost_usd": "0.02",
            "price_basis": "LIST_PRICE_PUBLISHED",
            "currency": "USD",
            "status": "success",
        }
    ],
    "outcome_status": "success",
}

INPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "work_id": {"type": "string"},
        "events": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "resource_class": {"type": "string"},
                    "supplier": {"type": "string"},
                    "known_cost_usd": {"type": ["string", "null"]},
                    "price_basis": {"type": "string", "enum": sorted(VALID_PRICE_BASIS)},
                    "currency": {"type": "string", "const": "USD"},
                    "status": {"type": "string", "enum": sorted(VALID_STATUS)},
                },
                "required": ["resource_class", "supplier", "price_basis", "status"],
            },
        },
        "outcome_status": {"type": ["string", "null"]},
    },
    "required": ["work_id", "events"],
}

OUTPUT_EXAMPLE: dict = {
    "result": {
        "work_id": "example-work-001",
        "known_total_cost_usd": "0.02",
        "exact_total_known": True,
        "event_count": 1,
        "unknown_event_count": 0,
        "breakdown_by_resource_class": {"inference": "0.02"},
        "breakdown_by_supplier": {"example-supplier": "0.02"},
        "price_provenance": {"LIST_PRICE_PUBLISHED": 1},
        "outcome_status": "success",
        "capability_version": CAPABILITY_VERSION,
    },
    "commercial_receipt": {
        "purchase_id": "example-purchase-id",
        "invocation_id": "00000000-0000-0000-0000-000000000000",
        "capability": CAPABILITY_NAME,
        "capability_version": CAPABILITY_VERSION,
        "rail": RAIL,
        "network": NETWORK,
        "network_class": NETWORK_CLASS,
        "quoted_amount_usd": str(PRICE_USD),
        "currency": PRICE_CURRENCY,
        "work_id": "example-work-001",
        "status": "DELIVERED",
    },
    "newly_charged": True,
    "newly_executed": True,
}

OUTPUT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "result": {
            "type": "object",
            "properties": {
                "work_id": {"type": "string"},
                "known_total_cost_usd": {"type": ["string", "null"]},
                "exact_total_known": {"type": "boolean"},
                "event_count": {"type": "integer"},
                "unknown_event_count": {"type": "integer"},
                "breakdown_by_resource_class": {
                    "type": "object", "additionalProperties": {"type": "string"}
                },
                "breakdown_by_supplier": {
                    "type": "object", "additionalProperties": {"type": "string"}
                },
                "price_provenance": {
                    "type": "object", "additionalProperties": {"type": "integer"}
                },
                "outcome_status": {"type": ["string", "null"]},
                "capability_version": {"type": "string"},
            },
            "required": [
                "work_id", "known_total_cost_usd", "exact_total_known", "event_count",
                "unknown_event_count", "breakdown_by_resource_class", "breakdown_by_supplier",
                "price_provenance", "outcome_status", "capability_version",
            ],
        },
        "commercial_receipt": {
            "type": "object",
            "properties": {
                "purchase_id": {"type": "string"},
                "invocation_id": {"type": "string"},
                "capability": {"type": "string"},
                "capability_version": {"type": "string"},
                "rail": {"type": "string"},
                "network": {"type": "string"},
                "network_class": {"type": "string"},
                "quoted_amount_usd": {"type": "string"},
                "currency": {"type": "string"},
                "work_id": {"type": "string"},
                "status": {"type": "string"},
            },
            "required": [
                "purchase_id", "invocation_id", "capability", "rail", "network",
                "quoted_amount_usd", "currency", "work_id", "status",
            ],
        },
        "newly_charged": {"type": "boolean"},
        "newly_executed": {"type": "boolean"},
    },
    "required": ["result", "commercial_receipt", "newly_charged", "newly_executed"],
}

DISCOVERY_EXTENSION = declare_discovery_extension(
    input=INPUT_EXAMPLE,
    input_schema=INPUT_SCHEMA,
    body_type="json",
    output=OutputConfig(example=OUTPUT_EXAMPLE, schema=OUTPUT_SCHEMA),
)


def _build_receipt(purchase_id: str, work_id: str, status: str) -> dict:
    return {
        "purchase_id": purchase_id,
        "invocation_id": str(uuid.uuid5(uuid.NAMESPACE_URL, purchase_id)),
        "capability": CAPABILITY_NAME,
        "capability_version": CAPABILITY_VERSION,
        "rail": RAIL,
        "network": NETWORK,
        "network_class": NETWORK_CLASS,
        "quoted_amount_usd": str(PRICE_USD),
        "currency": PRICE_CURRENCY,
        "work_id": work_id,
        "status": status,
    }


def build_manifest(base_url: str) -> dict:
    return {
        "manifest_type": "INFERRAIL_CAPABILITY_MANIFEST_V1",
        "capability": CAPABILITY_NAME,
        "capability_version": CAPABILITY_VERSION,
        "description": "Given payload-free, resource-class-tagged economic events already "
        "incurred for one unit of AI/agent work — inference/token cost, tool calls, search — "
        "returns a normalized cost receipt: known total cost, a breakdown by resource class and "
        "supplier, an unknown-cost event count, and price provenance. Never fabricates a total "
        "across unknown-cost events. Useful for AI job cost analysis, LLM spend tracking, and "
        "unit economics — without needing the prompt or response itself.",
        "invoke_endpoint": f"{base_url}/invoke",
        "price": {
            "amount": str(PRICE_USD),
            "currency": PRICE_CURRENCY,
            "rail": RAIL,
            "network": NETWORK,
            "network_class": NETWORK_CLASS,
            "pay_to": SELLER_PAY_TO_ADDRESS,
        },
        "payment_protocol": "x402: an unpaid POST /invoke returns HTTP 402 with "
        "PaymentRequirements; retry with a signed X-PAYMENT header. Verification and "
        "settlement are performed by the CDP-hosted facilitator on Base Sepolia testnet.",
        "input_schema": INPUT_SCHEMA,
        "output_schema": OUTPUT_SCHEMA,
        "docs": "https://github.com/domondi1/inferrail/blob/main/docs/capabilities/work-economics.md",
    }


def create_app(db_path: Path) -> FastAPI:
    store = DurablePurchaseStore(db_path)
    app = FastAPI()

    facilitator_config = create_facilitator_config(
        api_key_id=os.environ["CDP_API_KEY_ID"],
        api_key_secret=os.environ["CDP_API_KEY_SECRET"],
    )
    facilitator_client = HTTPFacilitatorClient(facilitator_config)
    server = x402ResourceServer(facilitator_client)
    register_exact_evm_server(server, networks=NETWORK)

    resource_url = os.environ.get(
        "X402_RESOURCE_URL", "https://inferrail-x402-seller-testnet.onrender.com/invoke"
    )
    routes: dict[str, RouteConfig] = {
        "POST /invoke": RouteConfig(
            accepts=PaymentOption(
                scheme="exact",
                pay_to=SELLER_PAY_TO_ADDRESS,
                price=f"${PRICE_USD}",
                network=NETWORK,
            ),
            resource=resource_url,
            description=DISCOVERY_DESCRIPTION,
            service_name="Inferrail Work Economics",
            tags=[
                "work-economics", "ai-cost", "cost-summary", "economic-summary", "resource-cost",
                "unit-economics", "cost-receipt", "ai-job-cost", "llm-spend", "agent-payments",
            ],
            extensions=DISCOVERY_EXTENSION,
        )
    }

    middleware = payment_middleware(routes, server)

    @app.middleware("http")
    async def x402_middleware(request: Request, call_next):
        return await middleware(request, call_next)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/manifest")
    async def manifest(request: Request):
        base_url = f"{request.url.scheme}://{request.headers.get('host', 'localhost')}"
        return build_manifest(base_url)

    @app.post("/invoke")
    async def invoke(request: Request):
        purchase_id = request.headers.get("x-purchase-id")
        if not purchase_id:
            return JSONResponse(
                status_code=400, content={"error": "X-Purchase-Id header is required"}
            )

        body = await request.json()
        work_id = body.get("work_id", "unknown")

        # By the time this handler runs, the x402 middleware has already
        # cryptographically verified the payment (request.state.payment_payload
        # is set). On-chain settlement happens after this handler returns 200.
        payment_payload = getattr(request.state, "payment_payload", None)
        payment_ref = None
        if payment_payload is not None:
            try:
                payment_ref = payment_payload.payload.authorization.nonce
            except AttributeError:
                payment_ref = str(payment_payload)

        store.create_quote(purchase_id, work_id, str(PRICE_USD), PRICE_CURRENCY, RAIL)
        _paid_row, newly_charged = store.record_payment(purchase_id, payment_ref or "unknown")

        def compute():
            import json as _json

            try:
                events = [EconomicEvent.from_dict(e) for e in body.get("events", [])]
                result = compute_work_economics(work_id, events, body.get("outcome_status"))
            except InvalidEconomicEvent as exc:
                error_result = {"error": str(exc)}
                receipt = _build_receipt(purchase_id, work_id, status="INPUT_REJECTED")
                return _json.dumps(error_result), _json.dumps(receipt)
            receipt = _build_receipt(purchase_id, work_id, status="DELIVERED")
            return _json.dumps(result.to_json_dict()), _json.dumps(receipt)

        executed_row, newly_executed = store.execute_once(purchase_id, compute)
        import json as _json

        result = _json.loads(executed_row["result_json"])
        receipt = _json.loads(executed_row["receipt_json"])
        status_code = 422 if "error" in result else 200
        return JSONResponse(
            status_code=status_code,
            content={
                "result": result,
                "commercial_receipt": receipt,
                "newly_charged": newly_charged,
                "newly_executed": newly_executed,
            },
        )

    return app


if __name__ == "__main__":
    import sys

    import uvicorn

    # A bare `python3 service.py` (no CLI args) is the production/hosted
    # shape: bind 0.0.0.0 and read the platform-injected $PORT. Explicit
    # argv[1] (db_path) / argv[2] (port) is the local/loopback test shape.
    explicit_args = len(sys.argv) > 1
    db_path = (
        Path(sys.argv[1])
        if explicit_args
        else Path(os.environ.get("WORK_ECONOMICS_DB_PATH", "/tmp/inferrail_work_economics.sqlite3"))
    )
    port = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get("PORT", 8421))
    host = "127.0.0.1" if explicit_args else "0.0.0.0"
    app = create_app(db_path)
    print(
        f"Inferrail Work Economics (Base Sepolia testnet) listening on "
        f"http://{host}:{port} (db={db_path})"
    )
    uvicorn.run(
        app, host=host, port=port, log_level="warning", proxy_headers=True, forwarded_allow_ips="*"
    )
