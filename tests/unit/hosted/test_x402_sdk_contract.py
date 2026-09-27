"""Contract tests pinning the installed x402 SDK's payment-lifecycle
behavior that Inferrail's hosted payment code relies on (or must work
around).

These exercise the REAL `x402ResourceServer`, its FastAPI
`payment_middleware`, payment-flow resolution and hook dispatch, with
real offline-signed EIP-3009 payloads. Only the facilitator's network
calls are replaced (see `_x402_fake_facilitator.py`), so nothing here
needs CDP credentials or network access.

If an x402 upgrade changes any of these behaviors, the matching test
should fail loudly: the hosted services' payment-correctness reasoning
depends on them, so an upgrade must be re-reviewed rather than silently
accepted.
"""

from __future__ import annotations

import contextvars
import importlib.metadata
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("x402")
pytest.importorskip("eth_account")
pytest.importorskip("fastapi")

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from _x402_fake_facilitator import (  # noqa: E402
    BASE_SEPOLIA,
    FakeFacilitator,
    decode_payment_required,
    settle_failure,
    sign_payment,
)
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from x402.http.middleware.fastapi import payment_middleware  # noqa: E402
from x402.http.types import PaymentOption, RouteConfig  # noqa: E402
from x402.mechanisms.evm.exact import register_exact_evm_server  # noqa: E402
from x402.schemas import AbortResult, SettleResponse  # noqa: E402
from x402.server import x402ResourceServer  # noqa: E402

PAY_TO = "0x000000000000000000000000000000000000dEaD"
PROBE: contextvars.ContextVar[str | None] = contextvars.ContextVar("probe", default=None)


class Harness:
    """A minimal paid route wired exactly the way the hosted services wire
    theirs: an outer `@app.middleware("http")` wrapper that awaits the x402
    middleware directly."""

    def __init__(
        self,
        *,
        flow: str | None,
        facilitator: FakeFacilitator | None = None,
        handler_status: int = 200,
    ) -> None:
        self.events: list[str] = []
        self.hook_contexts: dict[str, Any] = {}
        self.handler_state: dict[str, Any] = {}
        self.facilitator = facilitator or FakeFacilitator()
        self.facilitator.on_settle = lambda _payload: self.events.append("settle")
        self.server = x402ResourceServer(self.facilitator)
        register_exact_evm_server(self.server, networks=BASE_SEPOLIA)

        extra = {"paymentFlow": flow} if flow else None
        routes = {
            "POST /paid": RouteConfig(
                accepts=PaymentOption(
                    scheme="exact",
                    pay_to=PAY_TO,
                    price="$0.05",
                    network=BASE_SEPOLIA,
                    extra=extra,
                ),
                resource="http://testserver/paid",
                description="contract-test route",
            )
        }
        x402_mw = payment_middleware(routes, self.server)
        app = FastAPI()
        handler_status_code = handler_status
        events = self.events
        handler_state = self.handler_state

        @app.middleware("http")
        async def wrapper(request: Request, call_next):  # type: ignore[no-untyped-def]
            token = PROBE.set(request.headers.get("x-probe"))
            try:
                return await x402_mw(request, call_next)
            finally:
                PROBE.reset(token)

        @app.post("/paid")
        async def paid(request: Request) -> JSONResponse:
            events.append("handler")
            handler_state["state_keys"] = set(vars(request.state).get("_state", {}))
            return JSONResponse({"ok": True}, status_code=handler_status_code)

        self.client = TestClient(app)

    def unpaid(self) -> Any:
        resp = self.client.post("/paid", json={"x": 1})
        assert resp.status_code == 402
        return decode_payment_required(resp)

    def pay(self, headers: dict[str, str] | None = None) -> Any:
        signed = sign_payment(self.unpaid())
        all_headers = {"PAYMENT-SIGNATURE": signed.header, **(headers or {})}
        return self.client.post("/paid", json={"x": 1}, headers=all_headers), signed


def test_installed_x402_version_is_the_pinned_one():
    assert importlib.metadata.version("x402") == "2.22.0"


# -- flow phase ordering ------------------------------------------------------


def test_upfront_flow_skips_verify_and_settles_before_the_handler():
    h = Harness(flow="upfront")
    resp, _ = h.pay()
    assert resp.status_code == 200
    assert h.facilitator.calls_to("verify") == [], "upfront must not call facilitator /verify"
    assert h.events == ["settle", "handler"]


def test_upfront_flow_is_advertised_on_the_402():
    h = Harness(flow="upfront")
    required = h.unpaid()
    assert required.accepts[0].extra["paymentFlow"] == "upfront"


def test_default_flow_verifies_before_and_settles_after_the_handler():
    h = Harness(flow=None)
    resp, _ = h.pay()
    assert resp.status_code == 200
    assert len(h.facilitator.calls_to("verify")) == 1
    assert h.events == ["handler", "settle"]


@pytest.mark.parametrize("status", [400, 422, 500])
def test_default_flow_never_settles_an_error_response(status: int):
    h = Harness(flow=None, handler_status=status)
    resp, _ = h.pay()
    assert resp.status_code == status
    assert h.facilitator.calls_to("settle") == []
    assert h.events == ["handler"]


def test_upfront_flow_has_already_settled_when_the_handler_errors():
    h = Harness(flow="upfront", handler_status=400)
    resp, _ = h.pay()
    assert resp.status_code == 400
    assert h.events == ["settle", "handler"], "money has moved; the exact scheme cannot undo it"


# -- hook semantics -------------------------------------------------------------


def test_before_settle_hook_exception_prevents_settlement_and_returns_402():
    h = Harness(flow="upfront")

    def boom(_ctx: Any) -> None:
        raise RuntimeError("durable write failed")

    h.server.on_before_settle(boom)
    resp, _ = h.pay()
    assert resp.status_code == 402
    assert h.facilitator.calls_to("settle") == []
    assert "handler" not in h.events


def test_before_settle_hook_abort_prevents_settlement_and_returns_402():
    h = Harness(flow="upfront")
    h.server.on_before_settle(lambda _ctx: AbortResult(reason="conflict"))
    resp, _ = h.pay()
    assert resp.status_code == 402
    assert h.facilitator.calls_to("settle") == []
    assert "handler" not in h.events


def test_before_settle_hook_sees_phase_payload_dict_and_headers_but_not_the_body():
    h = Harness(flow="upfront")
    seen: dict[str, Any] = {}

    def capture(ctx: Any) -> None:
        seen["phase"] = ctx.phase
        seen["payload_type"] = type(ctx.payment_payload.payload)
        seen["authorization_keys"] = set(ctx.payment_payload.payload["authorization"])
        adapter = ctx.transport_context.request.adapter
        seen["purchase_header"] = adapter.get_header("x-purchase-id")
        seen["body"] = adapter.get_body()
        seen["pay_to"] = ctx.requirements.pay_to
        seen["amount"] = ctx.requirements.amount

    h.server.on_before_settle(capture)
    resp, signed = h.pay({"X-Purchase-Id": "p-1"})
    assert resp.status_code == 200
    assert seen["phase"] == "before-handler"
    assert seen["payload_type"] is dict
    assert seen["authorization_keys"] == {
        "from", "to", "value", "validAfter", "validBefore", "nonce"
    }
    assert seen["purchase_header"] == "p-1"
    assert seen["body"] is None, "hooks cannot read the request body in 2.22.0"
    assert seen["pay_to"] == PAY_TO
    assert seen["amount"] == "50000"


def test_contextvar_set_by_the_outer_wrapper_is_visible_inside_the_settle_hook():
    h = Harness(flow="upfront")
    seen: dict[str, Any] = {}
    h.server.on_before_settle(lambda _ctx: seen.setdefault("probe", PROBE.get()))
    resp, _ = h.pay({"X-Probe": "binding-123"})
    assert resp.status_code == 200
    assert seen["probe"] == "binding-123"


def test_after_settle_hook_receives_the_settle_response():
    h = Harness(flow="upfront")
    seen: list[SettleResponse] = []
    h.server.on_after_settle(lambda ctx: seen.append(ctx.result))
    resp, signed = h.pay()
    assert resp.status_code == 200
    assert len(seen) == 1
    assert seen[0].success is True
    assert seen[0].transaction.startswith("0x")
    assert seen[0].payer == signed.payer


def test_after_settle_hook_exception_turns_a_successful_settlement_into_a_402():
    h = Harness(flow="upfront")

    def boom(_ctx: Any) -> None:
        raise RuntimeError("durable write failed after money moved")

    h.server.on_after_settle(boom)
    resp, _ = h.pay()
    assert len(h.facilitator.calls_to("settle")) == 1
    assert h.facilitator.settled_nonces, "the fake recorded the settlement as successful"
    assert resp.status_code == 402, "buyer is told payment failed although it settled"
    assert "handler" not in h.events


def test_settle_failure_hook_runs_for_a_returned_failure():
    h = Harness(flow="upfront", facilitator=FakeFacilitator(settle_outcomes=[settle_failure()]))
    seen: list[Any] = []
    h.server.on_settle_failure(lambda ctx: seen.append(ctx.error))
    resp, _ = h.pay()
    assert resp.status_code == 402
    assert len(seen) == 1
    assert "insufficient_balance" in str(seen[0])


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("Facilitator settle failed (500): upstream error"),
        TimeoutError("read timed out"),
    ],
)
def test_settle_failure_hook_does_not_run_when_the_facilitator_raises(exc: BaseException):
    h = Harness(flow="upfront", facilitator=FakeFacilitator(settle_outcomes=[exc]))
    seen: list[Any] = []
    h.server.on_settle_failure(lambda ctx: seen.append(ctx.error))
    resp, _ = h.pay()
    assert resp.status_code == 402
    assert seen == [], "facilitator exceptions bypass on_settle_failure in the async server"
    assert "handler" not in h.events


def test_settlement_pending_is_retried_exactly_once():
    pending = SettleResponse(
        success=False,
        error_reason="settlement_pending",
        transaction="0x" + "ab" * 32,
        network=BASE_SEPOLIA,
    )
    h = Harness(flow="upfront", facilitator=FakeFacilitator(settle_outcomes=[pending]))
    resp, _ = h.pay()
    assert len(h.facilitator.calls_to("settle")) == 2
    assert resp.status_code == 200


# -- what the handler and the schemas expose ------------------------------------------


def test_handler_state_carries_the_payload_but_not_the_settlement_result():
    h = Harness(flow="upfront")
    resp, _ = h.pay()
    assert resp.status_code == 200
    keys = h.handler_state["state_keys"]
    assert {"payment_payload", "payment_requirements"} <= keys
    assert not any("settle" in k for k in keys)


def test_settle_response_has_no_block_or_confirmation_fields():
    fields = set(SettleResponse.model_fields)
    assert {"success", "error_reason", "error_message", "payer", "transaction", "network",
            "amount"} <= fields
    assert not any(("block" in f) or ("confirm" in f) for f in fields)


def test_payment_payload_is_a_dict_without_attribute_access():
    h = Harness(flow=None)
    signed = sign_payment(h.unpaid())
    assert isinstance(signed.payload.payload, dict)
    with pytest.raises(AttributeError):
        _ = signed.payload.payload.authorization  # type: ignore[attr-defined]
