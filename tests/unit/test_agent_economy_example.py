"""Attacks on examples/agent_economy: one work budget across model calls,
x402 purchases and a delegated sub-agent.

Everything is real except the chain (examples/agent_economy/local_chain.py):
the x402 seller middleware, buyer-side EIP-3009 signing, x402's own
signature verification, and the economic-authority ledger
(hosted/a2a_economic_authority/core.py).
"""

from __future__ import annotations

import json
import sys
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("x402")
pytest.importorskip("eth_account")
pytest.importorskip("fastapi")

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "agent_economy"
if str(EXAMPLE_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLE_DIR))

from authority import AuthorityRuntime, ModelProvider, Refused, estimate_prompt_tokens  # noqa: E402
from eth_account import Account  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from local_chain import LocalFacilitator, LocalUsdcChain  # noqa: E402
from seller import create_seller_app  # noqa: E402

SELLER = "http://seller.test"
SEARCH = f"{SELLER}/search"
CHAT = f"{SELLER}/v1/chat/completions"


class Clock:
    def __init__(self) -> None:
        import time

        self._time = time.time
        self.offset = 0.0

    def __call__(self) -> float:
        return self._time() + self.offset


class Crash(BaseException):
    """A process death: escapes every `except Exception`."""


class CountingHttp:
    """The runtime's HTTP client. Counts paid requests; can crash on demand."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.paid_requests = 0
        self.crash_before_send = False
        self.crash_after_send = False

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        paid = "PAYMENT-SIGNATURE" in (kwargs.get("headers") or {})
        if paid:
            self.paid_requests += 1
            if self.crash_before_send:
                raise Crash()
        response = self.client.request(method, url, **kwargs)
        if paid and self.crash_after_send:
            raise Crash()
        return response


def _provider() -> ModelProvider:
    def complete(messages: list[dict[str, str]], max_tokens: int) -> tuple[str, int, int]:
        return "ok", estimate_prompt_tokens(messages), min(max_tokens, 50)

    return ModelProvider(complete, Decimal("0.000001"), Decimal("0.000004"), name="provider:stub")


@pytest.fixture
def world(tmp_path: Path) -> dict[str, Any]:
    clock = Clock()
    chain = LocalUsdcChain(clock=clock)
    payer = Account.create()
    seller_address = Account.create().address
    chain.mint(payer.address, 1_000_000)  # $1.00 test-USDC; budgets below are smaller
    http = CountingHttp(
        TestClient(
            create_seller_app(LocalFacilitator(chain), seller_address, SELLER), base_url=SELLER
        )
    )

    def runtime() -> AuthorityRuntime:
        return AuthorityRuntime(
            tmp_path / "authority",
            payer=payer,
            chain=chain,
            http=http,
            provider=_provider(),
            clock=clock,
        )

    return {
        "chain": chain,
        "payer": payer,
        "seller": seller_address,
        "http": http,
        "clock": clock,
        "runtime": runtime,
    }


def _outflow(world: dict[str, Any]) -> int:
    return 1_000_000 - world["chain"].balance_of(world["payer"].address)


def test_one_budget_across_model_x402_and_a_delegated_child(world: dict[str, Any]) -> None:
    rt = world["runtime"]()
    parent = rt.open_work("research-run-42", Decimal("0.020"))
    model = rt.model_call(
        parent, [{"role": "user", "content": "plan the research"}], max_tokens=200
    )
    search = rt.pay(parent, "GET", SEARCH, params={"q": "budgets delegation"})
    child = rt.delegate(parent, "summarizer", Decimal("0.006"))
    chat = rt.pay(
        child, "POST", CHAT, json={"messages": [{"role": "user", "content": "x402 budgets"}]}
    )

    assert search["paid"] and chat["paid"]
    assert search["body"]["results"][0]["id"] in {"budgets", "delegation"}
    record = rt.record("research-run-42")
    root = record["authority"]
    assert Decimal(root["delegations"][0]["consumed_usd"]) == Decimal("0.003")
    assert Decimal(root["consumed_usd"]) == Decimal(model["cost_usd"]) + Decimal("0.002")
    assert Decimal(root["reserved_for_children_usd"]) == Decimal("0.006")
    assert _outflow(world) == 2_000 + 3_000  # only x402 purchases move USDC
    assert all(a["chain_authorization_used"] for a in record["actions"] if a["kind"] == "x402")
    assert root["invariant"] == "SATISFIED"

    rt.finish(child)  # unspent $0.003 returns to the parent
    root = rt.record("research-run-42")["authority"]
    assert Decimal(root["remaining_usd"]) == Decimal("0.020") - Decimal(
        model["cost_usd"]
    ) - Decimal("0.005")


def test_overspend_is_refused_before_anything_is_signed_or_sent(world: dict[str, Any]) -> None:
    rt = world["runtime"]()
    token = rt.open_work("w", Decimal("0.003"))
    assert rt.pay(token, "GET", SEARCH, params={"q": "x402"})["paid"]
    sent_before, nonces_before = world["http"].paid_requests, len(world["chain"].events)

    with pytest.raises(Refused) as refused:
        rt.pay(token, "GET", SEARCH, params={"q": "x402"})  # $0.002 > $0.001 remaining

    assert refused.value.reason == "insufficient_authority"
    assert Decimal(refused.value.detail["remaining_usd"]) == Decimal("0.001")
    assert world["http"].paid_requests == sent_before  # no signed payment left the runtime
    assert len(world["chain"].events) == nonces_before
    assert _outflow(world) == 2_000
    assert len(rt.record("w")["refused_before_authorization"]) == 1


def test_a_child_can_never_spend_more_than_it_was_delegated(world: dict[str, Any]) -> None:
    rt = world["runtime"]()
    parent = rt.open_work("w", Decimal("1.00"))
    child = rt.delegate(parent, "child", Decimal("0.004"))
    assert rt.pay(child, "POST", CHAT, json={"messages": []})["paid"]  # $0.003
    with pytest.raises(Refused):
        rt.pay(child, "GET", SEARCH, params={"q": "x402"})  # $0.002 > $0.001
    assert Decimal(rt.status(parent)["remaining_usd"]) == Decimal("0.996")


def test_concurrent_purchases_cannot_double_spend_the_remaining_budget(
    world: dict[str, Any],
) -> None:
    rt = world["runtime"]()
    token = rt.open_work("w", Decimal("0.010"))

    def attempt(_: int) -> str:
        try:
            return (
                "paid" if rt.pay(token, "GET", SEARCH, params={"q": "x402"})["paid"] else "unpaid"
            )
        except Refused:
            return "refused"

    with ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(attempt, range(20)))

    assert outcomes.count("paid") == 5
    assert outcomes.count("refused") == 15
    assert _outflow(world) == 10_000  # exactly the budget, never more
    assert rt.record("w")["authority"]["invariant"] == "SATISFIED"


def test_concurrent_delegations_cannot_double_allocate_the_parent(world: dict[str, Any]) -> None:
    rt = world["runtime"]()
    parent = rt.open_work("w", Decimal("0.010"))

    def attempt(i: int) -> bool:
        try:
            rt.delegate(parent, f"agent-{i}", Decimal("0.002"))
            return True
        except Refused:
            return False

    with ThreadPoolExecutor(max_workers=10) as pool:
        granted = sum(pool.map(attempt, range(10)))

    assert granted == 5
    assert Decimal(rt.status(parent)["remaining_usd"]) == Decimal("0.000")


def test_a_hostile_child_looping_concurrently_stays_inside_its_delegation(
    world: dict[str, Any],
) -> None:
    rt = world["runtime"]()
    parent = rt.open_work("w", Decimal("1.00"))
    child = rt.delegate(parent, "hostile", Decimal("0.007"))

    def attempt(_: int) -> int:
        try:
            return 1 if rt.pay(child, "GET", SEARCH, params={"q": "x402"})["paid"] else 0
        except Refused:
            return 0

    with ThreadPoolExecutor(max_workers=16) as pool:
        paid = sum(pool.map(attempt, range(40)))

    assert paid == 3  # 3 x $0.002 = $0.006 <= $0.007
    assert _outflow(world) == 6_000


def test_payer_balance_is_the_hard_backstop_if_the_ledger_is_wrong(
    world: dict[str, Any], tmp_path: Path
) -> None:
    """A misconfigured or compromised ledger can't move more than the payer holds."""
    chain, clock = world["chain"], world["clock"]
    poor = Account.create()
    chain.mint(poor.address, 4_000)  # $0.004 funded, but the ledger says $1.00
    rt = AuthorityRuntime(
        tmp_path / "poor",
        payer=poor,
        chain=chain,
        http=world["http"],
        provider=_provider(),
        clock=clock,
    )
    token = rt.open_work("w", Decimal("1.00"))
    results = [rt.pay(token, "GET", SEARCH, params={"q": "x402"}) for _ in range(3)]
    assert [r["paid"] for r in results] == [True, True, False]
    assert chain.balance_of(poor.address) == 0
    assert results[2]["state"] == "AUTHORIZED"  # held until it can provably never settle
    clock.offset = 3600
    assert rt.reconcile() == {results[2]["action_id"]: "EXPIRED"}
    assert Decimal(rt.status(token)["remaining_usd"]) == Decimal("0.996")


def test_crash_after_reserving_before_signing_releases_on_restart(world: dict[str, Any]) -> None:
    rt = world["runtime"]()
    token = rt.open_work("w", Decimal("0.010"))
    caller = rt._caller(token)
    rt._admit(caller, "x402", SEARCH, Decimal("0.002"), world["seller"])  # then the process dies
    assert Decimal(rt.status(token)["remaining_usd"]) == Decimal("0.008")

    restarted = world["runtime"]()
    assert list(restarted.reconcile().values()) == ["ABANDONED"]
    assert Decimal(restarted.status(token)["remaining_usd"]) == Decimal("0.010")
    assert _outflow(world) == 0


def test_crash_after_signing_before_sending_holds_until_expiry(world: dict[str, Any]) -> None:
    rt = world["runtime"]()
    token = rt.open_work("w", Decimal("0.010"))
    world["http"].crash_before_send = True
    with pytest.raises(Crash):
        rt.pay(token, "GET", SEARCH, params={"q": "x402"})
    world["http"].crash_before_send = False

    restarted = world["runtime"]()
    assert list(restarted.reconcile().values()) == [
        "AUTHORIZED"
    ]  # could still settle: keep it held
    assert Decimal(restarted.status(token)["remaining_usd"]) == Decimal("0.008")
    world["clock"].offset = 3600
    assert list(restarted.reconcile().values()) == ["EXPIRED"]
    assert Decimal(restarted.status(token)["remaining_usd"]) == Decimal("0.010")
    assert _outflow(world) == 0


def test_crash_after_settlement_before_recording_is_recovered_from_the_chain(
    world: dict[str, Any],
) -> None:
    rt = world["runtime"]()
    token = rt.open_work("w", Decimal("0.010"))
    world["http"].crash_after_send = True
    with pytest.raises(Crash):
        rt.pay(token, "GET", SEARCH, params={"q": "x402"})
    world["http"].crash_after_send = False
    assert _outflow(world) == 2_000  # money moved; the runtime died before recording it

    restarted = world["runtime"]()
    assert list(restarted.reconcile().values()) == ["SETTLED"]
    record = restarted.record("w")
    assert Decimal(record["settled_spend_usd"]) == Decimal("0.002")
    assert record["actions"][0]["tx"] == world["chain"].events[0].tx
    assert Decimal(restarted.status(token)["remaining_usd"]) == Decimal("0.008")
    assert restarted.reconcile() == {}  # idempotent: nothing left to resolve


def test_the_local_chain_rejects_a_tampered_authorization(world: dict[str, Any]) -> None:
    """The stand-in chain checks signatures with x402's own verifier."""
    import base64

    from x402.client import x402ClientSync
    from x402.http.utils import encode_payment_signature_header
    from x402.mechanisms.evm.exact import ExactEvmScheme
    from x402.schemas import PaymentRequired

    client = world["http"].client
    probe = client.get("/search", params={"q": "x"})
    required = PaymentRequired.model_validate(
        json.loads(base64.b64decode(probe.headers["payment-required"]))
    )
    buyer = x402ClientSync()
    buyer.register("eip155:84532", ExactEvmScheme(signer=world["payer"]))
    payload = buyer.create_payment_payload(required)
    payload.payload["authorization"]["value"] = "1"  # pay less than signed for
    tampered = client.get(
        "/search",
        params={"q": "x"},
        headers={"PAYMENT-SIGNATURE": encode_payment_signature_header(payload)},
    )
    assert tampered.status_code == 402
    assert _outflow(world) == 0


def test_the_payer_key_never_appears_in_any_output(world: dict[str, Any]) -> None:
    rt = world["runtime"]()
    token = rt.open_work("w", Decimal("0.010"))
    rt.pay(token, "GET", SEARCH, params={"q": "x402"})
    key_hex = world["payer"].key.hex().removeprefix("0x")
    dumped = json.dumps([rt.record("w"), rt.status(token)])
    assert key_hex not in dumped


def test_a_concurrent_reconcile_cannot_release_a_purchase_that_is_being_signed(
    world: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import authority

    rt = world["runtime"]()
    other = world["runtime"]()  # a second process on the same state, e.g. a reconcile job
    token = rt.open_work("w", Decimal("0.002"))  # room for exactly one $0.002 search

    real_client = authority.x402ClientSync

    class ReconcileWhileSigning(real_client):  # type: ignore[misc, valid-type]
        def create_payment_payload(self, *args: Any, **kwargs: Any) -> Any:
            other.reconcile()  # runs between admission and AUTHORIZED
            return super().create_payment_payload(*args, **kwargs)

    monkeypatch.setattr(authority, "x402ClientSync", ReconcileWhileSigning)
    with pytest.raises(Refused) as refused:
        rt.pay(token, "GET", SEARCH, params={"q": "x402"})
    monkeypatch.setattr(authority, "x402ClientSync", real_client)

    # The reconcile won: the signature never left, nothing moved, the budget is whole
    assert refused.value.reason == "reservation_released"
    assert world["http"].paid_requests == 0
    assert _outflow(world) == 0
    assert Decimal(rt.status(token)["remaining_usd"]) == Decimal("0.002")

    # ...and the budget still allows exactly one purchase
    assert rt.pay(token, "GET", SEARCH, params={"q": "again"})["paid"] is True
    with pytest.raises(Refused):
        rt.pay(token, "GET", SEARCH, params={"q": "third"})
    assert _outflow(world) == 2_000


def test_a_model_call_found_in_flight_on_restart_counts_its_ceiling(
    world: dict[str, Any],
) -> None:
    rt = world["runtime"]()
    token = rt.open_work("w", Decimal("0.010"))
    caller = rt._caller(token)
    action_id = rt._admit(caller, "model", "provider:stub", Decimal("0.003"), None)
    assert rt._transition(action_id, "RESERVING", state="CALLING")  # then the process dies

    restarted = world["runtime"]()
    assert restarted.reconcile() == {action_id: "SETTLED"}
    assert Decimal(restarted.status(token)["remaining_usd"]) == Decimal("0.007")
