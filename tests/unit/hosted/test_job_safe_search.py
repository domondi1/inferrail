from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest
from eth_account import Account
from x402.mechanisms.evm.exact.client import ExactEvmScheme
from x402.schemas import PaymentPayload, ResourceInfo

from hosted.job_safe_search.contract import SearchRequest, atomic
from hosted.job_safe_search.economics import financial_state
from hosted.job_safe_search.metrics import report
from hosted.job_safe_search.payments import encode
from hosted.job_safe_search.service import Config, SearchService, create_app
from hosted.job_safe_search.store import Refused, Store
from hosted.job_safe_search.supplier import SupplierResult

ROOT = Path(__file__).resolve().parents[3]
spec = importlib.util.spec_from_file_location(
    "search_local_chain", ROOT / "examples/agent_economy/local_chain.py"
)
assert spec and spec.loader
local = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = local
spec.loader.exec_module(local)


class Supplier:
    name = "test"
    max_cost = 7000

    def __init__(self, failure: str | None = None):
        self.calls = 0
        self.failure = failure

    async def search(self, request: SearchRequest) -> SupplierResult:
        self.calls += 1
        await asyncio.sleep(0)
        if self.failure == "crash":
            raise KeyboardInterrupt()
        if self.failure in (
            "timeout_before_charge",
            "timeout_after_charge",
            "http_error",
            "malformed",
            "settlement_uncertain",
        ):
            raise RuntimeError(self.failure)
        hits = (
            []
            if self.failure == "empty"
            else [{"title": "Result", "url": "https://example.com/", "snippet": request.query}]
        )
        cost = (
            None
            if self.failure == "cost_unknown"
            else 8000
            if self.failure == "cost_overrun"
            else 7000
        )
        if self.failure == "bad_url":
            hits[0]["url"] = "javascript:alert(1)"
        return SupplierResult(hits, cost)


class Chain:
    def __init__(self, chain: Any):
        self.chain = chain
        self.final = True
        self.unavailable = False

    async def confirmed(self, payload: PaymentPayload, tx: str) -> bool:
        if self.unavailable:
            raise RuntimeError("rpc unavailable")
        event = self.chain.used_event(
            payload.payload["authorization"]["from"], payload.payload["authorization"]["nonce"]
        )
        return self.final and event is not None and event.tx == tx

    async def find_transaction(self, payload: PaymentPayload, from_block: str) -> str | None:
        event = self.chain.used_event(
            payload.payload["authorization"]["from"], payload.payload["authorization"]["nonce"]
        )
        return event.tx if event else None


class Facilitator:
    def __init__(self, chain: Any):
        self.inner = local.LocalFacilitator(chain)
        self.chain = chain
        self.verifies = self.settles = 0
        self.failure: str | None = None

    async def verify(self, payload: Any, requirements: Any) -> Any:
        self.verifies += 1
        if self.failure == "verify":
            raise RuntimeError("facilitator unavailable")
        return await self.inner.verify(payload, requirements)

    async def settle(self, payload: Any, requirements: Any) -> Any:
        self.settles += 1
        if self.failure == "settle_before":
            raise RuntimeError("settle timeout")
        result = await self.inner.settle(payload, requirements)
        if self.failure == "settle_after":
            raise RuntimeError("lost settled response")
        if self.failure == "crash_after_settle":
            raise KeyboardInterrupt()
        return result


@pytest.fixture
def system(tmp_path: Path) -> tuple[SearchService, Any, Any]:
    account = Account.create()
    pay_to = Account.create().address
    config = Config(
        pay_to=pay_to, resource_url="https://search.example.com/search", token_secret=b"s" * 32
    )
    chain = local.LocalUsdcChain()
    chain.mint(account.address, 1_000_000)
    service = SearchService(
        config, Store(tmp_path / "search.sqlite"), Facilitator(chain), Chain(chain), Supplier()
    )
    return service, account, chain


def payment(service: SearchService, account: Any) -> str:
    inner = ExactEvmScheme(account).create_payment_payload(service.requirements)
    payload = PaymentPayload(
        x402_version=2,
        payload=inner,
        accepted=service.requirements,
        resource=ResourceInfo(url=service.config.resource_url),
    )
    return encode(payload.model_dump(by_alias=True))


def body(request_id: str = "one", **changes: Any) -> SearchRequest:
    return SearchRequest(
        query="machine budgets", request_id=request_id, job_budget_usd="0.045", **changes
    )


def result(response: Any) -> dict[str, Any]:
    return json.loads(response.body)


def row(service: SearchService) -> dict[str, Any]:
    return service.store.get(1)


@pytest.mark.asyncio
async def test_success_and_financial_state(system: Any) -> None:
    service, account, chain = system
    response = await service.handle(body(), payment(service, account))
    assert response.status_code == 200
    assert service.supplier.calls == service.facilitator.settles == 1
    assert chain.balance_of(service.config.pay_to) == 15000
    f = financial_state(row(service))
    assert f == {
        "settled_revenue": "0.015",
        "recognized_revenue": "0.015",
        "supplier_cogs": "0.007",
        "refunds": "0",
        "credits": "0",
        "variable_fees": "0",
        "unresolved_liability": "0",
        "realized_margin": "0.008",
        "resolved": True,
    }


@pytest.mark.asyncio
async def test_replay_lost_response_fresh_payment_restart(system: Any) -> None:
    service, account, chain = system
    signed = payment(service, account)
    first = result(await service.handle(body(), signed))
    for signature in (signed, signed, payment(service, account)):
        response = result(await service.handle(body(), signature))
        assert response["receipt"]["charged_usd"] == "0"
    restarted = SearchService(
        service.config,
        Store(service.store.path),
        service.facilitator,
        service.chain,
        service.supplier,
    )
    assert result(await restarted.handle(body(), signed))["results"] == first["results"]
    assert chain.balance_of(service.config.pay_to) == 15000
    assert service.facilitator.settles == service.supplier.calls == 1


@pytest.mark.asyncio
async def test_cache_alias_budget_authentication(system: Any) -> None:
    service, account, _ = system
    first = result(await service.handle(body(), payment(service, account)))
    cache = SearchRequest(
        query="  machine   budgets ", request_id="two", job_token=first["job_token"]
    )
    response = result(await service.handle(cache, None))
    assert response["receipt"]["cache_hit"] and response["receipt"]["charged_usd"] == "0"
    # Alias preserves idempotency after cache expiry too.
    assert (
        result(await service.handle(cache, payment(service, account)))["receipt"]["charged_usd"]
        == "0"
    )
    assert service.supplier.calls == service.facilitator.settles == 1
    with pytest.raises(Refused, match="conflict"):
        await service.handle(cache.model_copy(update={"query": "different"}), None)
    with pytest.raises(Refused, match="immutable"):
        await service.handle(cache.model_copy(update={"job_budget_usd": "1"}), None)
    with pytest.raises(Refused, match="invalid"):
        await service.handle(cache.model_copy(update={"job_token": first["job_token"] + "a"}), None)
    with pytest.raises(Refused, match="mismatch"):
        await service.handle(cache.model_copy(update={"job_id": "wrong"}), None)


@pytest.mark.asyncio
async def test_concurrent_same_request_and_query(system: Any) -> None:
    service, account, chain = system
    signed = payment(service, account)
    results = await asyncio.gather(*(service.handle(body(), signed) for _ in range(12)))
    assert any(r.status_code == 200 for r in results)
    assert service.supplier.calls == service.facilitator.settles == 1
    assert chain.balance_of(service.config.pay_to) == 15000


@pytest.mark.asyncio
async def test_atomic_last_job_budget(system: Any) -> None:
    service, account, chain = system
    initial = body().model_copy(update={"job_budget_usd": "0.03"})
    first = result(await service.handle(initial, payment(service, account)))
    requests = [
        SearchRequest(query=f"query {i}", request_id=f"next{i}", job_token=first["job_token"])
        for i in range(8)
    ]
    responses = await asyncio.gather(
        *(service.handle(r, payment(service, account)) for r in requests), return_exceptions=True
    )
    assert sum(isinstance(r, Refused) for r in responses) == 7
    assert chain.balance_of(service.config.pay_to) == 30000
    assert service.supplier.calls == 2
    with pytest.raises(Refused, match="exhausted"):
        await service.handle(
            SearchRequest(query="new", request_id="no", job_token=first["job_token"]), None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        "timeout_before_charge",
        "timeout_after_charge",
        "http_error",
        "malformed",
        "empty",
        "settlement_uncertain",
        "cost_overrun",
        "bad_url",
    ],
)
async def test_supplier_failure_never_retries_or_recognizes_margin(
    system: Any, failure: str
) -> None:
    service, account, chain = system
    service.supplier.failure = failure
    signed = payment(service, account)
    await service.handle(body(), signed)
    await service.handle(body(), signed)
    await service.recover("0x0")
    f = financial_state(row(service))
    assert row(service)["state"] == "SUPPLIER_UNKNOWN"
    assert service.supplier.calls == 1
    assert chain.balance_of(service.config.pay_to) == 15000
    assert f["settled_revenue"] == "0.015" and f["realized_margin"] is None
    assert f["supplier_cogs"] is None and f["unresolved_liability"] == "0.015"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["settle_before", "settle_after"])
async def test_settlement_uncertainty_blocks_cogs_and_recovers(system: Any, failure: str) -> None:
    service, account, chain = system
    service.facilitator.failure = failure
    signed = payment(service, account)
    response = await service.handle(body(), signed)
    assert response.status_code == 202 and service.supplier.calls == 0
    assert financial_state(row(service))["realized_margin"] is None
    await service.handle(body(), signed)
    assert service.facilitator.settles == 1
    service.facilitator.failure = None
    await service.recover("0x0")
    assert service.supplier.calls == (1 if failure == "settle_after" else 0)
    assert chain.balance_of(service.config.pay_to) == (15000 if failure == "settle_after" else 0)


@pytest.mark.asyncio
async def test_finality_rpc_failure_and_crash_before_supplier(system: Any) -> None:
    service, account, _ = system
    service.chain.final = False
    signed = payment(service, account)
    assert (await service.handle(body(), signed)).status_code == 202
    assert service.supplier.calls == 0
    service.chain.unavailable = True
    await service.handle(body(), signed)
    assert service.supplier.calls == 0
    service.chain.unavailable = False
    service.chain.final = True
    await service.recover("0x0")
    assert row(service)["state"] == "DELIVERED" and service.supplier.calls == 1


@pytest.mark.asyncio
async def test_crash_after_supplier_dispatch_freezes(system: Any) -> None:
    service, account, _ = system
    service.supplier.failure = "crash"
    with pytest.raises(KeyboardInterrupt):
        await service.handle(body(), payment(service, account))
    assert row(service)["state"] == "SUPPLIER_INFLIGHT"
    service.supplier.failure = None
    await service.recover("0x0")
    assert row(service)["state"] == "SUPPLIER_UNKNOWN" and service.supplier.calls == 1
    assert financial_state(row(service))["realized_margin"] is None


@pytest.mark.asyncio
async def test_crash_after_settlement_reconciles_without_second_charge(system: Any) -> None:
    service, account, chain = system
    service.facilitator.failure = "crash_after_settle"
    with pytest.raises(KeyboardInterrupt):
        await service.handle(body(), payment(service, account))
    assert row(service)["state"] == "SETTLING" and service.supplier.calls == 0
    service.facilitator.failure = None
    await service.recover("0x0")
    assert row(service)["state"] == "DELIVERED"
    assert chain.balance_of(service.config.pay_to) == 15000 and service.facilitator.settles == 1


@pytest.mark.asyncio
async def test_invalid_auth_funds_expiry_and_nonce_binding(system: Any) -> None:
    service, account, chain = system
    with pytest.raises(Refused):
        await service.handle(body(), "invalid")
    chain.balances[account.address.lower()] = 0
    signed = payment(service, account)
    await service.handle(body(), signed)
    assert row(service)["state"] == "PAYMENT_REJECTED" and service.supplier.calls == 0
    assert financial_state(row(service))["realized_margin"] == "0"
    assert service.store.job(row(service)["job"])["committed"] == 0
    chain.mint(account.address, 1_000_000)
    with pytest.raises(Refused, match="already_bound"):
        await service.handle(body("other"), signed)


@pytest.mark.asyncio
async def test_unknown_cost_never_zero_and_risk_cap(system: Any) -> None:
    service, account, _ = system
    service.supplier.failure = "cost_unknown"
    await service.handle(body(), payment(service, account))
    assert financial_state(row(service))["realized_margin"] is None
    assert financial_state(row(service))["supplier_cogs"] is None
    service.config = replace(service.config, risk_ceiling=23000)
    with pytest.raises(Refused, match="risk_ceiling"):
        await service.handle(
            SearchRequest(query="other", request_id="other"), payment(service, account)
        )


@pytest.mark.asyncio
async def test_http_discovery_schema_validation(system: Any) -> None:
    service, account, _ = system
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)), base_url="https://test"
    ) as client:
        response = await client.post("/search", json={})
        assert response.status_code == 402 and "payment-required" in response.headers
        assert response.json()["extensions"]["bazaar"]["info"]["input"]["method"] == "POST"
        assert response.json()["resource"]["serviceName"] == "Inferrail Job-Safe Web Search"
        assert response.json()["resource"]["tags"] == [
            "search",
            "web",
            "job-budget",
            "idempotency",
            "agent-payments",
        ]
        invalid = await client.post(
            "/search", json={"query": "q", "request_id": "r", "num_results": 100}
        )
        assert invalid.status_code == 422 and service.facilitator.settles == 0
        valid = await client.post(
            "/search",
            json=body().model_dump(),
            headers={"PAYMENT-SIGNATURE": payment(service, account)},
        )
        assert valid.status_code == 200 and "payment-response" in valid.headers


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "0.0000001"])
def test_money_validation(value: str) -> None:
    with pytest.raises(ValueError):
        atomic(value)


def test_mainnet_and_loss_gates(system: Any) -> None:
    service, _, _ = system
    with pytest.raises(ValueError, match="approval"):
        replace(service.config, network="eip155:8453").validate(service.supplier)
    with pytest.raises(ValueError, match="envelope"):
        replace(service.config, price=10000).validate(service.supplier)
    with pytest.raises(ValueError, match="range"):
        replace(service.config, risk_ceiling=20_000_001).validate(service.supplier)
    with pytest.raises(ValueError, match="payment_fee"):
        replace(
            service.config,
            network="eip155:8453",
            mainnet_approved=True,
            supplier_rights_confirmed=True,
            realized_payment_fee=None,
        ).validate(Supplier())


def test_external_metrics_exclude_controlled_wallets(tmp_path: Path) -> None:
    store = Store(tmp_path / "metrics.sqlite")
    payers = ["0x" + "1" * 40] * 3 + ["0x" + "2" * 40]
    for index, payer in enumerate(payers):
        fingerprint = f"fingerprint-{index}"
        row, _ = store.reserve(
            payer=payer,
            job=f"job-{index}",
            request=f"request-{index}",
            fingerprint=fingerprint,
            budget=None,
            authenticated=False,
            price=15_000,
            supplier_bound=7_000,
            fee_bound=1_000,
            nonce=f"nonce-{index}",
            payload="{}",
            body="{}",
            risk_ceiling=20_000_000,
            expires=9999999999,
        )
        purchase = row["id"]
        store.transition(purchase, "RESERVED", "VERIFYING")
        store.transition(purchase, "VERIFYING", "SETTLING")
        store.transition(purchase, "SETTLING", "FINALITY_PENDING", tx=f"tx-{index}")
        store.transition(purchase, "FINALITY_PENDING", "SUPPLIER_INFLIGHT")
        store.transition(
            purchase,
            "SUPPLIER_INFLIGHT",
            "DELIVERED",
            supplier_cogs=7_000,
            variable_fees=500,
            liability=0,
            result="[]",
            delivered=100.0 + (90_000.0 if index == 2 else index * 10.0),
        )
    with store.connect() as conn:
        conn.execute("UPDATE purchases SET created=100.0 WHERE payer=?", (payers[0],))
        conn.execute("UPDATE purchases SET created=90100.0 WHERE request_id='request-2'")
    metrics = report(store.path, {payers[-1]})
    assert metrics["external_paid_calls"] == 3
    assert metrics["wallets_with_3_plus_paid_calls"] == 1
    assert metrics["wallets_with_3_plus_distinct_queries"] == 1
    assert metrics["wallets_returning_after_24h"] == 1
    assert metrics["gross_external_settled_revenue_usd"] == "0.045"
    assert metrics["realized_external_contribution_margin_usd"] == "0.0225"
